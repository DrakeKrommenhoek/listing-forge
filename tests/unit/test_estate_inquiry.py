"""Unit tests for the decoupled public inquiry flow.

Pure-logic tests: no database, no filesystem beyond a tmp_path fixture for
the notifier. Python 3.10 compatible, same as tests/unit/test_estate.py.
"""

from __future__ import annotations

from estate.inquiry_notifier import LocalLogNotifier
from estate.inquiry_validation import (
    ValidationResult,
    is_honeypot_tripped,
    is_known_item,
    is_rate_limited,
    is_valid_item_id_format,
    parse_offer,
    validate_inquiry,
)

MANIFEST = {
    "DK-202608-002": {"status": "Listed", "sold": False},
    "DK-202608-005": {"status": "Sold", "sold": True},
}


# ---------------------------------------------------------------------------
# Item ID format + manifest membership
# ---------------------------------------------------------------------------

def test_valid_item_id_format_accepted():
    assert is_valid_item_id_format("DK-202608-002") is True


def test_malformed_item_id_rejected():
    assert is_valid_item_id_format("not-an-id") is False
    assert is_valid_item_id_format("") is False
    assert is_valid_item_id_format("dk-202608-002") is False  # lowercase prefix


def test_known_item_membership():
    assert is_known_item("DK-202608-002", MANIFEST) is True
    assert is_known_item("DK-202608-999", MANIFEST) is False
    assert is_known_item("", MANIFEST) is False


# ---------------------------------------------------------------------------
# Honeypot
# ---------------------------------------------------------------------------

def test_honeypot_blank_is_not_tripped():
    assert is_honeypot_tripped("") is False
    assert is_honeypot_tripped(None) is False


def test_honeypot_filled_is_tripped():
    assert is_honeypot_tripped("http://spam.example") is True


# ---------------------------------------------------------------------------
# Offer parsing
# ---------------------------------------------------------------------------

def test_parse_offer_variants():
    assert parse_offer("") is None
    assert parse_offer(None) is None
    assert parse_offer("not a number") is None
    assert parse_offer("$1,250") == 1250.0
    assert parse_offer("400") == 400.0


# ---------------------------------------------------------------------------
# validate_inquiry
# ---------------------------------------------------------------------------

def test_valid_inquiry_with_known_item_passes():
    result = validate_inquiry(
        item_id="DK-202608-002", name="Jane Buyer", contact="jane@example.com",
        message="Is this still available?", manifest=MANIFEST,
    )
    assert isinstance(result, ValidationResult)
    assert result.ok is True
    assert result.errors == []
    assert result.is_bot is False


def test_general_inquiry_with_no_item_id_is_allowed():
    """The About/Contact page's form has no specific item -- blank is fine."""
    result = validate_inquiry(
        item_id="", name="Jane Buyer", contact="jane@example.com",
        message="Do you have any rugs?", manifest=MANIFEST,
    )
    assert result.ok is True


def test_unknown_item_id_rejected_against_manifest():
    result = validate_inquiry(
        item_id="DK-202608-999", name="Jane Buyer", contact="jane@example.com",
        message="hi", manifest=MANIFEST,
    )
    assert result.ok is False
    assert any("catalogue" in e for e in result.errors)


def test_malformed_item_id_rejected_even_without_manifest():
    result = validate_inquiry(
        item_id="not-an-id", name="Jane Buyer", contact="jane@example.com",
        message="hi", manifest=None,
    )
    assert result.ok is False
    assert any("format" in e for e in result.errors)


def test_missing_required_fields_rejected():
    result = validate_inquiry(item_id="", name="", contact="", message="")
    assert result.ok is False
    assert any("name" in e for e in result.errors)
    assert any("contact" in e for e in result.errors)


def test_honeypot_short_circuits_and_reports_ok_but_is_bot():
    """A bot fills every field correctly plus the hidden field. It must be
    accepted (HTTP 200, no error detail -- don't teach the bot anything) but
    flagged so the caller discards it instead of recording/forwarding it."""
    result = validate_inquiry(
        item_id="DK-202608-002", name="Bot", contact="bot@example.com",
        message="buy now", website="http://spam.example", manifest=MANIFEST,
    )
    assert result.ok is True
    assert result.is_bot is True


def test_oversized_fields_rejected():
    result = validate_inquiry(
        item_id="DK-202608-002", name="x" * 200, contact="jane@example.com",
        message="hi", manifest=MANIFEST,
    )
    assert result.ok is False
    assert any("too long" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Rate limit policy (pure function, no storage)
# ---------------------------------------------------------------------------

def test_rate_limit_allows_under_threshold():
    now = 1_000_000.0
    timestamps = [now - 10, now - 20]
    assert is_rate_limited(timestamps, now=now, window_seconds=300, max_requests=5) is False


def test_rate_limit_blocks_at_threshold():
    now = 1_000_000.0
    timestamps = [now - t for t in (1, 2, 3, 4, 5)]
    assert is_rate_limited(timestamps, now=now, window_seconds=300, max_requests=5) is True


def test_rate_limit_ignores_requests_outside_window():
    now = 1_000_000.0
    timestamps = [now - 10_000] * 10  # all far outside the window
    assert is_rate_limited(timestamps, now=now, window_seconds=300, max_requests=5) is False


# ---------------------------------------------------------------------------
# LocalLogNotifier
# ---------------------------------------------------------------------------

def test_local_log_notifier_appends_json_line(tmp_path):
    path = tmp_path / "inquiries.jsonl"
    notifier = LocalLogNotifier(str(path))
    assert notifier.notify({"item_id": "DK-202608-002", "name": "Jane"}) is True
    assert notifier.notify({"item_id": "DK-202608-005", "name": "Sam"}) is True

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    import json
    first = json.loads(lines[0])
    assert first["item_id"] == "DK-202608-002"
    assert "received_at" in first


def test_local_log_notifier_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "inquiries.jsonl"
    notifier = LocalLogNotifier(str(path))
    assert notifier.notify({"item_id": "DK-202608-002"}) is True
    assert path.exists()


def test_local_log_notifier_fails_safely_never_raises(tmp_path):
    """Point the notifier at a path whose parent cannot exist (a file
    standing where a directory is needed) and confirm it returns False
    instead of raising."""
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")
    notifier = LocalLogNotifier(str(blocker / "inquiries.jsonl"))
    assert notifier.notify({"item_id": "DK-202608-002"}) is False


# ===========================================================================
# Session 4: durable Telegram delivery, audit fields, duplicates, availability
# ===========================================================================

import json  # noqa: E402
import urllib.error  # noqa: E402

from estate.inquiry_notifier import (  # noqa: E402
    ChainNotifier,
    InquiryNotifier,
    TelegramNotifier,
    format_inquiry_message,
    notifier_from_env,
)
from estate.inquiry_validation import (  # noqa: E402
    build_audit_record,
    hash_identity,
    inquiry_fingerprint,
    is_duplicate,
    is_item_available,
    normalize_contact,
    normalize_inquiry,
    record_request,
    record_seen,
)

AVAILABILITY_MANIFEST = {
    "DK-202608-002": {"status": "Listed", "sold": False},
    "DK-202608-003": {"status": "Approved", "sold": False},
    "DK-202608-004": {"status": "Offer Received", "sold": False},
    "DK-202608-005": {"status": "Sold", "sold": True},
    "DK-202608-006": {"status": "Pickup Scheduled", "sold": False},
    "DK-202608-007": {"status": "Shipping", "sold": False},
}


# ---------------------------------------------------------------------------
# Item availability
# ---------------------------------------------------------------------------

def test_available_statuses_accepted():
    for item_id in ("DK-202608-002", "DK-202608-003", "DK-202608-004"):
        assert is_item_available(item_id, AVAILABILITY_MANIFEST) is True


def test_sold_and_committed_statuses_are_unavailable():
    for item_id in ("DK-202608-005", "DK-202608-006", "DK-202608-007"):
        assert is_item_available(item_id, AVAILABILITY_MANIFEST) is False


def test_unknown_or_malformed_manifest_entry_is_unavailable():
    assert is_item_available("DK-202608-999", AVAILABILITY_MANIFEST) is False
    assert is_item_available("DK-202608-002", None) is False
    assert is_item_available("DK-202608-002", {"DK-202608-002": "not-a-dict"}) is False


def test_sold_item_inquiry_rejected_with_unavailable_flag():
    result = validate_inquiry(
        item_id="DK-202608-005", name="Jane Buyer", contact="jane@example.com",
        message="Is this still available?", manifest=AVAILABILITY_MANIFEST,
    )
    assert result.ok is False
    assert result.unavailable is True
    assert any("no longer available" in e for e in result.errors)


def test_unavailable_response_does_not_reveal_internal_status():
    """The buyer learns the item is gone, never whether it sold, shipped, or
    is awaiting pickup -- that distinction is internal."""
    result = validate_inquiry(
        item_id="DK-202608-006", name="Jane", contact="jane@example.com",
        message="hi", manifest=AVAILABILITY_MANIFEST,
    )
    joined = " ".join(result.errors).lower()
    assert "pickup" not in joined
    assert "shipping" not in joined
    assert "sold" not in joined


def test_unknown_item_is_not_flagged_unavailable():
    """An unknown ID must read as a validation error, not as 'sold' -- that
    would confirm to a prober that some IDs exist and others do not."""
    result = validate_inquiry(
        item_id="DK-202608-999", name="Jane", contact="jane@example.com",
        message="hi", manifest=AVAILABILITY_MANIFEST,
    )
    assert result.ok is False
    assert result.unavailable is False


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def test_normalize_inquiry_trims_collapses_and_caps():
    out = normalize_inquiry(
        item_id="  DK-202608-002 ", name="  Jane\t\tBuyer ",
        contact=" Jane@Example.COM ", message="line\n\n\nline", offer="$1,250",
    )
    assert out["item_id"] == "DK-202608-002"
    assert out["name"] == "Jane Buyer"
    assert out["contact"] == "Jane@Example.COM"  # original case preserved
    assert out["message"] == "line line"
    assert out["offer"] == 1250.0


def test_normalize_inquiry_strips_control_characters():
    out = normalize_inquiry(name="Jane\x00\x07Buyer", contact="x@y.z", message="ok")
    assert "\x00" not in out["name"]
    assert "\x07" not in out["name"]


def test_normalize_inquiry_truncates_oversized_message():
    out = normalize_inquiry(name="J", contact="x@y.z", message="x" * 9000)
    assert len(out["message"]) == 4000


def test_normalize_contact_is_case_and_punctuation_insensitive():
    assert normalize_contact(" Jane@Example.COM ") == "jane@example.com"
    # Brackets and spaces go; the hyphen stays. Keeping '-' is deliberate:
    # dropping it would fold jane-doe@x.com and janedoe@x.com onto the same
    # fingerprint and suppress a genuinely different buyer's message. The
    # cost is that two phone formats may not dedupe -- which only ever means
    # the reviewer sees one extra message, the safe direction to fail in.
    assert normalize_contact("(555) 010-9999") == "555010-9999"


# ---------------------------------------------------------------------------
# Audit fields
# ---------------------------------------------------------------------------

def test_audit_record_carries_required_fields():
    record = build_audit_record(
        normalize_inquiry(item_id="DK-202608-002", name="Jane",
                          contact="jane@example.com", message="hi"),
        client_ip="203.0.113.9", user_agent="Mozilla/5.0", salt="pepper",
        now=1_700_000_000.0,
    )
    for key in ("inquiry_id", "received_at", "received_at_epoch", "source",
                "fingerprint", "client_hash", "user_agent"):
        assert key in record, key
    assert record["source"] == "public_site"
    assert record["received_at"].endswith("Z")
    assert record["received_at"].startswith("2023-11-14T")


def test_audit_record_never_stores_the_raw_ip():
    record = build_audit_record(
        normalize_inquiry(name="Jane", contact="jane@example.com", message="hi"),
        client_ip="203.0.113.9", salt="pepper",
    )
    assert "203.0.113.9" not in json.dumps(record)
    assert len(record["client_hash"]) == 16


def test_client_hash_is_salted():
    assert hash_identity("203.0.113.9", "salt-a") != hash_identity("203.0.113.9", "salt-b")
    assert hash_identity("", "salt") == ""


def test_inquiry_ids_are_unique_per_submission():
    args = dict(item_id="DK-202608-002", name="Jane",
                contact="jane@example.com", message="hi")
    a = build_audit_record(normalize_inquiry(**args))
    b = build_audit_record(normalize_inquiry(**args))
    assert a["inquiry_id"] != b["inquiry_id"]
    assert a["fingerprint"] == b["fingerprint"]  # same content, same fingerprint


# ---------------------------------------------------------------------------
# Duplicate submissions
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_case_and_whitespace_noise():
    a = inquiry_fingerprint("DK-202608-002", "Jane@Example.com", "Is this available?")
    b = inquiry_fingerprint("DK-202608-002", " jane@example.com ", "is this available?")
    assert a == b


def test_fingerprint_differs_for_a_different_message():
    a = inquiry_fingerprint("DK-202608-002", "jane@example.com", "Is this available?")
    b = inquiry_fingerprint("DK-202608-002", "jane@example.com", "What are the dimensions?")
    assert a != b


def test_duplicate_detected_inside_window_and_not_outside():
    seen = {}
    now = 1_000_000.0
    fp = "abc123"
    assert is_duplicate(fp, seen, now=now) is False
    record_seen(fp, seen, now=now)
    assert is_duplicate(fp, seen, now=now + 30, window_seconds=900) is True
    assert is_duplicate(fp, seen, now=now + 5000, window_seconds=900) is False


def test_record_seen_evicts_expired_and_bounds_growth():
    seen = {}
    now = 1_000_000.0
    record_seen("old", seen, now=now - 10_000, window_seconds=900)
    record_seen("new", seen, now=now, window_seconds=900)
    assert "old" not in seen
    for i in range(600):
        record_seen(f"fp{i}", seen, now=now + i, window_seconds=100_000, max_entries=100)
    assert len(seen) <= 100


def test_record_request_prunes_outside_window():
    now = 1_000_000.0
    stamps = record_request([now - 10_000, now - 5], now=now, window_seconds=300)
    assert stamps == [now - 5, now]


# ---------------------------------------------------------------------------
# TelegramNotifier
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status = status
        self._payload = json.dumps(payload if payload is not None else {"ok": True})

    def read(self):
        return self._payload.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_notifier_is_unconfigured_without_credentials():
    assert TelegramNotifier("", "").configured is False
    assert TelegramNotifier("token", "").configured is False
    assert TelegramNotifier("", "123").configured is False
    # And an unconfigured notifier reports failure rather than pretending.
    assert TelegramNotifier("", "").notify({"name": "Jane"}) is False


def test_telegram_notifier_posts_to_the_configured_chat(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("utf-8")
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    notifier = TelegramNotifier("SECRET-TOKEN", "999")
    record = build_audit_record(
        normalize_inquiry(item_id="DK-202608-002", name="Jane",
                          contact="jane@example.com", message="Is this available?")
    )
    assert notifier.notify(record) is True
    assert captured["url"].endswith("/botSECRET-TOKEN/sendMessage")
    assert "chat_id=999" in captured["body"]
    assert "DK-202608-002" in captured["body"]


def test_telegram_notifier_reports_failure_on_network_error(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert TelegramNotifier("t", "1").notify({"name": "Jane"}) is False


def test_telegram_notifier_reports_failure_on_api_not_ok(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: _FakeResponse(200, {"ok": False, "description": "x"}),
    )
    assert TelegramNotifier("t", "1").notify({"name": "Jane"}) is False


def test_telegram_notifier_never_raises_on_malformed_response(monkeypatch):
    class _Garbage(_FakeResponse):
        def read(self):
            return b"not json"

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _Garbage()
    )
    assert TelegramNotifier("t", "1").notify({"name": "Jane"}) is False


def test_telegram_notifier_error_never_leaks_the_token(monkeypatch):
    """A URLError from urllib carries the request URL, which carries the bot
    token. notify() must swallow it, not propagate or re-wrap it."""
    def boom(request, timeout=None):
        raise urllib.error.URLError("failed opening " + request.full_url)

    monkeypatch.setattr("urllib.request.urlopen", boom)
    notifier = TelegramNotifier("SUPER-SECRET", "1")
    assert notifier.notify({"name": "Jane"}) is False  # no exception escapes


def test_message_is_plain_text_and_carries_the_audit_ref():
    record = build_audit_record(
        normalize_inquiry(item_id="DK-202608-002", name="Jane",
                          contact="jane@example.com", message="Hello *there* _friend_")
    )
    text = format_inquiry_message(record)
    assert "DK-202608-002" in text
    assert "Hello *there* _friend_" in text  # preserved literally, not escaped
    assert record["inquiry_id"] in text


def test_message_is_truncated_below_the_telegram_limit():
    record = build_audit_record(
        normalize_inquiry(name="Jane", contact="j@x.z", message="y" * 4000)
    )
    assert len(format_inquiry_message(record)) <= 4096


def test_message_never_contains_private_fields():
    """Only buyer-supplied and audit fields are rendered. Even if a private
    key somehow reached the record, it must not appear in the message."""
    record = build_audit_record(
        normalize_inquiry(item_id="DK-202608-002", name="Jane",
                          contact="j@x.z", message="hi")
    )
    record.update({
        "floor_price": 275, "internal_notes": "will take 200",
        "pickup_address": "1 Private Road", "review_token": "tok_abc",
    })
    text = format_inquiry_message(record)
    for secret in ("275", "will take 200", "1 Private Road", "tok_abc"):
        assert secret not in text


# ---------------------------------------------------------------------------
# ChainNotifier and env selection
# ---------------------------------------------------------------------------

class _Recorder(InquiryNotifier):
    def __init__(self, result=True, raises=False):
        self.result = result
        self.raises = raises
        self.calls = []

    def notify(self, inquiry):
        self.calls.append(inquiry)
        if self.raises:
            raise RuntimeError("boom")
        return self.result


def test_chain_reports_success_only_when_a_durable_notifier_succeeds():
    durable = _Recorder(True)
    trail = _Recorder(True)
    assert ChainNotifier([durable], [trail]).notify({"name": "Jane"}) is True
    assert len(durable.calls) == 1
    assert len(trail.calls) == 1


def test_chain_reports_failure_when_only_the_trail_succeeds():
    """The whole point: a /tmp write is not delivery and must not be
    reported as one."""
    trail = _Recorder(True)
    assert ChainNotifier([], [trail]).notify({"name": "Jane"}) is False
    assert len(trail.calls) == 1  # still written, as a debugging breadcrumb


def test_chain_still_writes_the_trail_when_delivery_fails():
    durable = _Recorder(False)
    trail = _Recorder(True)
    assert ChainNotifier([durable], [trail]).notify({"name": "Jane"}) is False
    assert len(trail.calls) == 1


def test_chain_survives_a_notifier_that_raises():
    bad = _Recorder(raises=True)
    good = _Recorder(True)
    assert ChainNotifier([bad, good], []).notify({"name": "Jane"}) is True


def test_notifier_from_env_without_telegram_is_not_durable(tmp_path):
    notifier = notifier_from_env({"ESTATE_INQUIRY_LOG_PATH": str(tmp_path / "i.jsonl")})
    assert isinstance(notifier, ChainNotifier)
    assert notifier.durable == []
    assert notifier.notify({"name": "Jane"}) is False


def test_notifier_from_env_selects_telegram_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda request, timeout=None: _FakeResponse()
    )
    notifier = notifier_from_env({
        "ESTATE_INQUIRY_LOG_PATH": str(tmp_path / "i.jsonl"),
        "ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN": "t",
        "ESTATE_INQUIRY_TELEGRAM_CHAT_ID": "42",
    })
    assert len(notifier.durable) == 1
    assert isinstance(notifier.durable[0], TelegramNotifier)
    assert notifier.notify({"name": "Jane", "inquiry_id": "x"}) is True
    assert (tmp_path / "i.jsonl").exists()
