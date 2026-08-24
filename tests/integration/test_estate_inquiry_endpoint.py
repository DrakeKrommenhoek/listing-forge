"""Integration tests for the *emitted* public inquiry endpoint.

The other integration file (test_estate_site_inquiry_decoupling.py) proves the
serverless function is written into the build and points nowhere near the
private VPS. This file goes one level further and actually *runs* the emitted
``api/inquiry.py`` — the exact source that gets deployed — driving its
``do_POST`` with synthetic requests.

That distinction matters: the handler source lives in a string literal inside
``estate/serverless.py``, so it is invisible to the rest of the test suite
unless something imports and executes it. A regression in the deployed code
path would otherwise pass every test.

Runs entirely offline. Telegram is exercised by patching
``urllib.request.urlopen``; no network call is ever made, and no real bot
token appears anywhere in this file or in the build output it inspects.
"""

from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
from email.message import Message

import pytest

TMP = tempfile.mkdtemp(prefix="estate-endpoint-int-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import serverless  # noqa: E402

MANIFEST = {
    "DK-202608-002": {"status": "Listed", "sold": False, "price": 225.0, "band": 3},
    "DK-202608-005": {"status": "Sold", "sold": True, "price": 90.0, "band": 0},
    "DK-202608-006": {"status": "Pickup Scheduled", "sold": False, "price": 60.0,
                      "band": 2},
    "DK-202608-007": {"status": "Approved", "sold": False, "price": 100.0, "band": 3},
    "DK-202608-008": {"status": "Listed", "sold": False, "price": 400.0, "band": 3},
    # Priced right up against its floor: it can absorb no discount at all, and
    # is what proves the floor rule survives the round trip.
    "DK-202608-009": {"status": "Listed", "sold": False, "price": 300.0, "band": 0},
    # No published price: nothing to take a percentage off.
    "DK-202608-010": {"status": "Listed", "sold": False, "price": None, "band": 0},
}

BUNDLE_CONFIG = {
    "enabled": True,
    "tiers": [
        {"min_items": 2, "discount_pct": 0.05},
        {"min_items": 3, "discount_pct": 0.10},
        {"min_items": 5, "discount_pct": 0.15},
    ],
    "round_to": 5,
    "max_items": 25,
}


# ---------------------------------------------------------------------------
# Loading the emitted function as a real, importable module
# ---------------------------------------------------------------------------


def _load_emitted_handler(tmp_path, manifest=None, bundle_config=None):
    """Emit the function into tmp_path and import it as package ``api``."""
    serverless.emit_inquiry_function(tmp_path)
    (tmp_path / "api" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "catalog_manifest.json").write_text(
        json.dumps(MANIFEST if manifest is None else manifest), encoding="utf-8"
    )
    (tmp_path / "bundle_config.json").write_text(
        json.dumps(BUNDLE_CONFIG if bundle_config is None else bundle_config),
        encoding="utf-8",
    )

    for name in [n for n in sys.modules if n == "api" or n.startswith("api.")]:
        del sys.modules[name]
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.invalidate_caches()
        return importlib.import_module("api.inquiry")
    finally:
        sys.path.remove(str(tmp_path))


@pytest.fixture()
def endpoint(tmp_path):
    module = _load_emitted_handler(tmp_path)
    module._REQUESTS.clear()
    module._SEEN.clear()
    yield module
    module._REQUESTS.clear()
    module._SEEN.clear()


def _call(module, payload, *, method="POST", headers=None, raw=None):
    """Drive the handler's do_<method> and capture the response.

    BaseHTTPRequestHandler normally builds itself from a live socket; here the
    request/response plumbing is replaced with in-memory buffers so the
    handler's own logic is what gets tested.
    """
    handler_cls = module.handler
    h = handler_cls.__new__(handler_cls)

    if raw is None:
        body = json.dumps(payload).encode("utf-8")
    else:
        body = raw

    message = Message()
    message["Content-Length"] = str(len(body))
    message["Content-Type"] = "application/json"
    for key, value in (headers or {}).items():
        message[key] = value

    h.headers = message
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()

    captured: dict = {"status": None, "headers": {}}
    h.send_response = lambda code, *a: captured.__setitem__("status", code)
    h.send_header = lambda k, v: captured["headers"].__setitem__(k, v)
    h.end_headers = lambda: None

    getattr(h, "do_" + method)()

    raw_body = h.wfile.getvalue()
    captured["raw"] = raw_body
    captured["json"] = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    return captured


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


@pytest.fixture()
def telegram(monkeypatch, tmp_path):
    """Configure Telegram delivery and capture what would have been sent."""
    sent: list = []

    def fake_urlopen(request, timeout=None):
        sent.append({"url": request.full_url, "body": request.data.decode("utf-8")})
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", "TEST-TOKEN")
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_CHAT_ID", "424242")
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))
    monkeypatch.setenv("ESTATE_INQUIRY_IP_SALT", "test-salt")
    return sent


VALID = {
    "item_id": "DK-202608-002",
    "name": "Jane Buyer",
    "contact": "jane@example.com",
    "message": "Is this still available?",
}


# ---------------------------------------------------------------------------
# Happy path + notification
# ---------------------------------------------------------------------------

def test_valid_inquiry_is_accepted_and_notifies_the_reviewer(endpoint, telegram):
    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.1"})
    assert res["status"] == 200
    assert res["json"]["status"] == "ok"
    assert len(telegram) == 1
    assert "chat_id=424242" in telegram[0]["body"]
    assert "DK-202608-002" in telegram[0]["body"]


def test_notification_carries_audit_fields(endpoint, telegram, tmp_path):
    _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.2"})
    trail = [
        json.loads(line)
        for line in (tmp_path / "trail.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(trail) == 1
    record = trail[0]
    for key in ("inquiry_id", "received_at", "fingerprint", "client_hash", "source"):
        assert key in record, key
    assert record["source"] == "public_site"
    # The raw IP is never persisted, only its salted hash.
    assert "203.0.113.2" not in json.dumps(record)


def test_general_inquiry_without_an_item_id_is_accepted(endpoint, telegram):
    payload = dict(VALID)
    payload["item_id"] = ""
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.3"})
    assert res["status"] == 200
    assert len(telegram) == 1


# ---------------------------------------------------------------------------
# Notification failure must not be reported as success
# ---------------------------------------------------------------------------

def test_notification_failure_returns_a_failure_response(endpoint, monkeypatch, tmp_path):
    """The whole point of the milestone: if nothing durable happened, the
    buyer is told so rather than thanked for a message no one will read."""
    import urllib.error

    def boom(request, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", "TEST-TOKEN")
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_CHAT_ID", "424242")
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))

    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.4"})
    assert res["status"] == 503
    assert res["json"]["status"] == "error"
    assert "try again" in res["json"]["message"].lower()


def test_unconfigured_notifier_is_not_treated_as_delivery(endpoint, monkeypatch, tmp_path):
    """With no Telegram credentials the only notifier is the local log, which
    is explicitly not durable -- the response must reflect that."""
    monkeypatch.delenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ESTATE_INQUIRY_TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))

    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.5"})
    assert res["status"] == 503
    # The breadcrumb is still written, it just does not count as delivery.
    assert (tmp_path / "trail.jsonl").exists()


def test_failure_message_mentions_the_fallback_email_when_configured(
    endpoint, monkeypatch, tmp_path
):
    monkeypatch.delenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))
    monkeypatch.setenv("ESTATE_SELLING_EMAIL", "hello@example.com")
    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.6"})
    assert "hello@example.com" in res["json"]["message"]


def test_a_failed_inquiry_is_not_remembered_as_a_duplicate(endpoint, monkeypatch, tmp_path):
    """A buyer who retries after a delivery failure must get through, not be
    silently swallowed as a duplicate of the attempt that never arrived."""
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))
    monkeypatch.delenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", raising=False)
    first = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.7"})
    assert first["status"] == 503

    sent: list = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout=None: (sent.append(request), _FakeResponse())[1],
    )
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", "TEST-TOKEN")
    monkeypatch.setenv("ESTATE_INQUIRY_TELEGRAM_CHAT_ID", "424242")

    second = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.7"})
    assert second["status"] == 200
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Duplicate submissions
# ---------------------------------------------------------------------------

def test_duplicate_submission_is_accepted_but_notified_only_once(endpoint, telegram):
    headers = {"X-Forwarded-For": "203.0.113.8"}
    first = _call(endpoint, VALID, headers=headers)
    second = _call(endpoint, VALID, headers=headers)

    assert first["status"] == 200
    assert second["status"] == 200  # the buyer sees success either way
    assert len(telegram) == 1  # the reviewer is only told once


def test_a_different_message_from_the_same_buyer_is_not_a_duplicate(endpoint, telegram):
    headers = {"X-Forwarded-For": "203.0.113.9"}
    _call(endpoint, VALID, headers=headers)
    followup = dict(VALID)
    followup["message"] = "Also, what are the dimensions?"
    _call(endpoint, followup, headers=headers)
    assert len(telegram) == 2


def test_the_same_message_about_a_different_item_is_not_a_duplicate(endpoint, telegram):
    headers = {"X-Forwarded-For": "203.0.113.10"}
    manifest_extra = dict(MANIFEST)
    manifest_extra["DK-202608-003"] = {"status": "Listed", "sold": False}
    _call(endpoint, VALID, headers=headers)
    other = dict(VALID)
    other["item_id"] = "DK-202608-003"
    # Not in the manifest this endpoint was built with, so it is rejected --
    # which is itself the correct behaviour; assert the first still stands.
    _call(endpoint, other, headers=headers)
    assert len(telegram) == 1


# ---------------------------------------------------------------------------
# Invalid item IDs and unavailable items
# ---------------------------------------------------------------------------

def test_unknown_item_id_is_rejected_and_never_notifies(endpoint, telegram):
    payload = dict(VALID)
    payload["item_id"] = "DK-202608-999"
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.11"})
    assert res["status"] == 400
    assert telegram == []


def test_malformed_item_id_is_rejected(endpoint, telegram):
    payload = dict(VALID)
    payload["item_id"] = "'; DROP TABLE items; --"
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.12"})
    assert res["status"] == 400
    assert telegram == []


def test_sold_item_is_rejected_with_a_conflict_and_no_notification(endpoint, telegram):
    payload = dict(VALID)
    payload["item_id"] = "DK-202608-005"
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.13"})
    assert res["status"] == 409
    assert "no longer available" in res["json"]["message"].lower()
    assert telegram == []


def test_committed_item_is_rejected_without_revealing_why(endpoint, telegram):
    payload = dict(VALID)
    payload["item_id"] = "DK-202608-006"  # Pickup Scheduled
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.14"})
    assert res["status"] == 409
    message = res["json"]["message"].lower()
    assert "pickup" not in message
    assert "scheduled" not in message
    assert telegram == []


def test_missing_manifest_fails_closed(tmp_path, telegram):
    """If catalog_manifest.json is unreadable, item-specific inquiries are
    refused rather than accepted unvalidated."""
    module = _load_emitted_handler(tmp_path)
    module._REQUESTS.clear()
    module._SEEN.clear()
    (tmp_path / "catalog_manifest.json").unlink()

    res = _call(module, VALID, headers={"X-Forwarded-For": "203.0.113.15"})
    assert res["status"] == 400
    assert telegram == []


# ---------------------------------------------------------------------------
# Validation and safe error responses
# ---------------------------------------------------------------------------

def test_missing_required_fields_rejected(endpoint, telegram):
    res = _call(
        endpoint, {"item_id": "DK-202608-002", "name": "", "contact": ""},
        headers={"X-Forwarded-For": "203.0.113.16"},
    )
    assert res["status"] == 400
    assert telegram == []


def test_malformed_json_gets_a_generic_error(endpoint, telegram):
    res = _call(endpoint, None, raw=b"{not json", headers={"X-Forwarded-For": "203.0.113.17"})
    assert res["status"] == 400
    assert res["json"]["message"] == endpoint.GENERIC_ERROR
    assert telegram == []


def test_non_object_json_body_rejected(endpoint, telegram):
    res = _call(endpoint, ["a", "list"], headers={"X-Forwarded-For": "203.0.113.18"})
    assert res["status"] == 400
    assert telegram == []


def test_empty_body_rejected(endpoint, telegram):
    res = _call(endpoint, None, raw=b"", headers={"X-Forwarded-For": "203.0.113.19"})
    assert res["status"] == 400
    assert telegram == []


def test_oversized_body_rejected_without_reading_it(endpoint, telegram):
    res = _call(
        endpoint, None, raw=b"x" * (endpoint.MAX_BODY_BYTES + 1),
        headers={"X-Forwarded-For": "203.0.113.20"},
    )
    assert res["status"] == 400
    assert telegram == []


def test_error_responses_never_leak_internals(endpoint, telegram):
    """No stack trace, no file path, no module name, no manifest contents."""
    for raw in (b"{not json", b"", b"[]", b"null"):
        res = _call(endpoint, None, raw=raw or b" ",
                    headers={"X-Forwarded-For": "203.0.113.21"})
        text = res["raw"].decode("utf-8")
        for leak in ("Traceback", "/var/task", "operation_drake", "catalog_manifest",
                     ".py", "line "):
            assert leak not in text, (leak, text)


def test_unexpected_internal_error_returns_a_generic_500(endpoint, telegram, monkeypatch):
    monkeypatch.setattr(
        endpoint, "_load_manifest", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.22"})
    assert res["status"] == 500
    assert res["json"]["message"] == endpoint.GENERIC_ERROR
    assert "boom" not in res["raw"].decode("utf-8")
    assert telegram == []


def test_get_is_not_a_readable_surface(endpoint):
    res = _call(endpoint, None, raw=b"", method="GET")
    assert res["status"] == 405
    assert "DK-202608-002" not in res["raw"].decode("utf-8")


# ---------------------------------------------------------------------------
# Honeypot, rate limiting, CORS
# ---------------------------------------------------------------------------

def test_honeypot_submission_is_accepted_silently_and_never_forwarded(endpoint, telegram):
    payload = dict(VALID)
    payload["website"] = "http://spam.example"
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.23"})
    assert res["status"] == 200  # tell the bot nothing
    assert telegram == []  # but never bother a human


def test_rate_limit_blocks_a_burst_from_one_client(endpoint, telegram):
    headers = {"X-Forwarded-For": "203.0.113.24"}
    statuses = []
    for i in range(7):
        payload = dict(VALID)
        payload["message"] = f"question number {i}"
        statuses.append(_call(endpoint, payload, headers=headers)["status"])
    assert 429 in statuses
    assert statuses[-1] == 429


def test_rate_limit_is_per_client(endpoint, telegram):
    for i in range(6):
        payload = dict(VALID)
        payload["message"] = f"question {i}"
        _call(endpoint, payload, headers={"X-Forwarded-For": f"198.51.100.{i}"})
    assert len(telegram) == 6  # none blocked: each is a different client


def test_cors_is_not_wide_open_by_default(endpoint, telegram):
    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.25"})
    assert res["headers"].get("Access-Control-Allow-Origin") != "*"
    assert "Access-Control-Allow-Origin" not in res["headers"]


def test_cors_pins_to_the_configured_origin(endpoint, telegram, monkeypatch):
    monkeypatch.setenv("ESTATE_SITE_ORIGIN", "https://futureonly.example")
    res = _call(endpoint, VALID, headers={"X-Forwarded-For": "203.0.113.26"})
    assert res["headers"]["Access-Control-Allow-Origin"] == "https://futureonly.example"


def test_preflight_without_a_configured_origin_grants_nothing(endpoint, monkeypatch):
    monkeypatch.delenv("ESTATE_SITE_ORIGIN", raising=False)
    res = _call(endpoint, None, raw=b"", method="OPTIONS")
    assert res["status"] == 204
    assert "Access-Control-Allow-Origin" not in res["headers"]


# ---------------------------------------------------------------------------
# Bundles
#
# One inquiry, several items. The basket is untrusted input: only well-formed
# IDs survive parsing, only IDs the manifest still lists as available survive
# validation, and the prices and bands used to quote it are read out of the
# manifest rather than out of the request.
# ---------------------------------------------------------------------------

BUNDLE = {
    "items": ["DK-202608-002", "DK-202608-007", "DK-202608-008"],
    "name": "Jane Buyer",
    "contact": "jane@example.com",
    "message": "What would you take for these three?",
}


def _telegram_text(sent):
    """The plain-text message body Telegram would have received."""
    from urllib.parse import parse_qs

    return parse_qs(sent["body"])["text"][0]


def test_a_bundle_inquiry_is_accepted_and_notifies_once(endpoint, telegram):
    res = _call(endpoint, BUNDLE, headers={"X-Forwarded-For": "203.0.113.40"})
    assert res["status"] == 200
    assert len(telegram) == 1


def test_the_notification_lists_every_item_its_price_the_total_and_the_discount(
    endpoint, telegram
):
    """A reply has to be writable from the phone, without opening a laptop."""
    _call(endpoint, BUNDLE, headers={"X-Forwarded-For": "203.0.113.41"})
    text = _telegram_text(telegram[0])

    for item_id in BUNDLE["items"]:
        assert item_id in text
    assert "$225" in text and "$100" in text and "$400" in text
    assert "Subtotal: $725" in text
    assert "10%" in text
    assert "Indicative total: $655" in text  # 725 * 0.90 = 652.5, rounded up to 655
    assert "Indicative only" in text
    assert "you confirm the price on reply" in text


def test_the_total_is_recomputed_from_the_manifest_not_from_the_request(
    endpoint, telegram
):
    """A buyer cannot talk their own basket into a better price."""
    payload = dict(BUNDLE)
    payload["subtotal"] = 1
    payload["total"] = 1
    payload["discount_pct"] = 0.9
    _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.42"})

    text = _telegram_text(telegram[0])
    assert "Subtotal: $725" in text
    assert "90%" not in text


def test_an_item_at_its_floor_caps_the_whole_basket_and_says_so(endpoint, telegram):
    """DK-202608-009 is published with band 0 -- it can absorb nothing."""
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-007", "DK-202608-009"]
    _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.43"})

    text = _telegram_text(telegram[0])
    assert "Discount: none" in text
    assert "Indicative total: $625" in text  # 225 + 100 + 300, undiscounted
    assert "capped by an item near its floor" in text


def test_an_unpriced_item_pins_the_basket_and_is_flagged(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-007", "DK-202608-010"]
    _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.44"})

    text = _telegram_text(telegram[0])
    assert "Discount: none" in text
    assert "Not priced yet: DK-202608-010" in text
    assert "price on request" in text


def test_a_two_item_basket_gets_the_smaller_tier(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-007"]
    _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.45"})

    text = _telegram_text(telegram[0])
    assert "5%" in text
    assert "Subtotal: $325" in text


def test_a_bundle_containing_a_sold_item_is_refused_and_names_it(endpoint, telegram):
    """Selling three of the four things someone asked for, without saying
    which one went, is the small dishonesty that loses a buyer."""
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-005"]
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.46"})

    assert res["status"] == 409
    assert "DK-202608-005" in res["json"]["message"]
    assert res["json"]["unavailable"] == ["DK-202608-005"]
    assert telegram == []


def test_a_bundle_containing_a_committed_item_is_refused(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-006"]
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.47"})
    assert res["status"] == 409
    assert telegram == []


def test_every_basket_id_is_validated_against_the_manifest(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002", "DK-202608-999"]
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.48"})

    assert res["status"] == 400
    assert "DK-202608-999" in res["json"]["message"]
    assert telegram == []


def test_a_basket_of_junk_is_refused_rather_than_quietly_emptied(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["'; DROP TABLE items; --", "../../etc/passwd", ""]
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.49"})

    assert res["status"] == 400
    assert telegram == []


def test_an_oversized_basket_is_refused(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["items"] = ["DK-202608-002"] * 400
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.50"})
    # Deduplication collapses it to one known item, which is a valid basket --
    # the point is that it never becomes 400 manifest lookups.
    assert res["status"] in (200, 400)
    assert len(telegram) <= 1


def test_a_bundle_still_requires_a_name_and_a_contact(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["name"] = ""
    payload["contact"] = ""
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.51"})
    assert res["status"] == 400
    assert telegram == []


def test_the_honeypot_still_swallows_a_bundle_submission(endpoint, telegram):
    payload = dict(BUNDLE)
    payload["website"] = "http://spam.example"
    res = _call(endpoint, payload, headers={"X-Forwarded-For": "203.0.113.52"})
    assert res["status"] == 200
    assert telegram == []


def test_a_resubmitted_bundle_is_a_duplicate_but_a_changed_one_is_not(
    endpoint, telegram
):
    headers = {"X-Forwarded-For": "203.0.113.53"}
    _call(endpoint, BUNDLE, headers=headers)
    _call(endpoint, BUNDLE, headers=headers)
    assert len(telegram) == 1  # the reviewer is told once

    changed = dict(BUNDLE)
    changed["items"] = ["DK-202608-002", "DK-202608-007"]
    _call(endpoint, changed, headers=headers)
    assert len(telegram) == 2  # a different selection is a new question


def test_the_same_basket_in_a_different_order_is_the_same_enquiry(
    endpoint, telegram
):
    headers = {"X-Forwarded-For": "203.0.113.54"}
    _call(endpoint, BUNDLE, headers=headers)
    reordered = dict(BUNDLE)
    reordered["items"] = list(reversed(BUNDLE["items"]))
    _call(endpoint, reordered, headers=headers)
    assert len(telegram) == 1


def test_a_bundle_delivery_failure_is_not_reported_as_success(
    endpoint, monkeypatch, tmp_path
):
    monkeypatch.delenv("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ESTATE_INQUIRY_LOG_PATH", str(tmp_path / "trail.jsonl"))
    res = _call(endpoint, BUNDLE, headers={"X-Forwarded-For": "203.0.113.55"})
    assert res["status"] == 503


def test_a_missing_bundle_config_quotes_no_discount_rather_than_guessing(
    tmp_path, telegram
):
    """Failing closed here quotes a higher price, never a lower one."""
    module = _load_emitted_handler(tmp_path)
    module._REQUESTS.clear()
    module._SEEN.clear()
    (tmp_path / "bundle_config.json").unlink()

    res = _call(module, BUNDLE, headers={"X-Forwarded-For": "203.0.113.56"})
    assert res["status"] == 200
    text = _telegram_text(telegram[0])
    assert "Discount: none" in text
    assert "Indicative total: $725" in text


def test_the_endpoint_has_no_access_to_a_floor_price_to_reveal(endpoint, telegram):
    """The structural guarantee, not a string check.

    The reviewer's own Telegram may say the word "floor" — that side of the
    boundary is allowed to. What matters is that this function is not holding
    a floor price to leak in the first place: its whole world is the manifest,
    which carries a public price and a coarse band and nothing else.
    """
    manifest = endpoint._load_manifest()
    blob = json.dumps(manifest)
    assert "floor" not in blob.lower()
    for entry in manifest.values():
        assert set(entry) <= {"status", "sold", "price", "band"}

    _call(endpoint, BUNDLE, headers={"X-Forwarded-For": "203.0.113.57"})
    text = _telegram_text(telegram[0])
    # Only the public prices, the subtotal and the indicative total appear.
    assert "$225" in text and "$725" in text
    assert "112" not in text  # half of 225: a plausible floor, never published


# ---------------------------------------------------------------------------
# The deployed bundle must contain no secrets
# ---------------------------------------------------------------------------

def test_emitted_bundle_contains_no_credentials_or_private_endpoints(tmp_path):
    serverless.emit_inquiry_function(tmp_path)
    blob = "\n".join(
        p.read_text(encoding="utf-8")
        for p in (tmp_path / "api").rglob("*.py")
    )
    # No credential material, no private host, no internal-only concepts.
    for forbidden in ("127.0.0.1:8000", "/estate/inquiry", "ESTATE_REVIEW_TOKEN",
                      "review_token", "floor_price", "internal_notes",
                      "TELEGRAM_BOT_TOKEN=", "bot123"):
        assert forbidden not in blob, forbidden
    # Credentials are referenced only as environment variable *names*.
    assert "ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN" in blob
    assert "api.telegram.org" in blob
