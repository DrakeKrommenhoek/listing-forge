"""Pure, storage-agnostic validation for buyer inquiries.

This module has no I/O, no secrets, and no storage backend — it only decides
whether an inquiry payload is well-formed. That makes it safe to reuse from
two very different places without duplicating the rules: the private review
API on the VPS, and the decoupled public serverless function that
``estate/site.py`` emits into every static-site build. The public copy never
calls back into the private side.

Keeping the rules here, and having both surfaces call into this module (the
public one does so via a build-time copy — see ``estate/serverless.py``),
means "what counts as a valid inquiry" cannot drift between the two.

This file is copied verbatim into a publicly deployed bundle, so its
comments are deliberately free of internal route paths, hostnames, ports and
anything else that describes the private system's shape.

Architecture note: the public site validates ``item_id`` against a
*manifest* (a plain dict of currently-approved item IDs), not against the
live database. The manifest is exported at build time
(``catalog_manifest.json``) precisely so the public-facing inquiry endpoint
never needs network access to the private VPS to answer "is this a real,
approved item?".
"""

from __future__ import annotations

import hashlib
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Mirrors estate.ids.ID_RE. Duplicated deliberately: this
# module must have zero imports from the rest of the package so it can be
# copied, verbatim, into a standalone serverless function bundle.
ITEM_ID_RE = re.compile(r"^([A-Z]{2,4})-(\d{6})-(\d{3,})$")

MAX_ITEM_ID = 40
MAX_NAME = 120
MAX_CONTACT = 200
MAX_MESSAGE = 4000

#: Hard ceiling on how many items one basket may contain, independent of the
#: configured ``bundle.max_items``. A basket is untrusted input; this bound is
#: what stops a submission claiming ten thousand item IDs from turning the
#: manifest lookup into a denial-of-service.
MAX_BUNDLE_ITEMS = 25

#: Manifest statuses that still accept a buyer inquiry. Everything else that
#: can legitimately appear in the manifest (``Sold``, ``Pickup Scheduled``,
#: ``Shipping``) describes an item that is spoken for: its page stays up so a
#: shared link does not 404 and so the catalogue reads honestly, but a new
#: inquiry against it must be refused rather than silently queued behind a
#: sale that has already happened. Mirrors estate/schema.py's
#: PUBLISHABLE_STATUSES minus the committed states; duplicated for the same
#: zero-import reason as ITEM_ID_RE.
AVAILABLE_STATUSES = frozenset(
    {"Approved", "Ready to List", "Listed", "Offer Received"}
)


def is_valid_item_id_format(value: str) -> bool:
    """True if ``value`` has the shape of an item ID (e.g. DK-202608-002).

    Does not check whether the ID actually exists — see ``is_known_item``.
    """
    return bool(ITEM_ID_RE.match((value or "").strip()))


def is_known_item(item_id: str, manifest: dict | None) -> bool:
    """True if ``item_id`` is present in a catalogue manifest.

    ``manifest`` is the dict loaded from ``catalog_manifest.json`` (item_id
    -> minimal status info). Passing ``None`` skips the membership check
    (format-only validation) — used by callers that have no manifest handy.
    """
    return (item_id or "").strip() in (manifest or {})


def is_item_available(item_id: str, manifest: dict | None) -> bool:
    """True if ``item_id`` is in ``manifest`` and can still be enquired about.

    An item is unavailable once it is marked sold, or once its status moves
    into a committed state (``Pickup Scheduled``, ``Shipping``, ``Sold``).
    Unknown items are, by definition, not available.

    The manifest entry is the minimal ``{"status": ..., "sold": bool}`` shape
    written by ``site.build_site()``. A malformed or missing entry is treated
    as unavailable — failing closed here only ever costs one enquiry, whereas
    failing open would accept enquiries for items that are gone.
    """
    entry = (manifest or {}).get((item_id or "").strip())
    if not isinstance(entry, dict):
        return False
    if entry.get("sold"):
        return False
    return str(entry.get("status", "")) in AVAILABLE_STATUSES


def is_honeypot_tripped(website_field: str) -> bool:
    """True if the hidden ``website`` field was filled in (bot behaviour)."""
    return bool((website_field or "").strip())


def parse_offer(raw) -> float | None:
    """Parse a free-text offer field into a float, or None if absent/invalid."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text.replace("$", "").replace(",", ""))
    except ValueError:
        return None


@dataclass
class ValidationResult:
    ok: bool
    errors: list = field(default_factory=list)
    is_bot: bool = False
    #: Set when the only thing wrong is that the item is spoken for. Callers
    #: use this to answer with an honest "no longer available" instead of a
    #: generic validation error, without having to string-match ``errors``.
    unavailable: bool = False


def validate_inquiry(
    *,
    item_id: str = "",
    name: str = "",
    contact: str = "",
    message: str = "",
    offer: str = "",
    website: str = "",
    manifest: dict | None = None,
) -> ValidationResult:
    """Validate one inquiry submission.

    A honeypot trip short-circuits everything else and reports ``ok=True,
    is_bot=True`` — callers should accept it silently (HTTP 200, no error
    detail) so the bot learns nothing, but must not actually record or
    forward it.

    ``item_id`` may be blank (the About/Contact page's general enquiry form
    has no specific item). If present, it must both match the ID format and,
    when a manifest is supplied, be a currently-known item — this is what
    stops someone probing for unpublished or internal item IDs through the
    public form.
    """
    if is_honeypot_tripped(website):
        return ValidationResult(ok=True, errors=[], is_bot=True)

    errors: list[str] = []
    unavailable = False

    item_id = (item_id or "").strip()
    name = (name or "").strip()
    contact = (contact or "").strip()
    message = (message or "").strip()

    if item_id:
        if len(item_id) > MAX_ITEM_ID:
            errors.append("item_id is too long")
        elif not is_valid_item_id_format(item_id):
            errors.append("item_id is not a recognised format")
        elif manifest is not None and not is_known_item(item_id, manifest):
            errors.append("item_id is not in the current catalogue")
        elif manifest is not None and not is_item_available(item_id, manifest):
            # Deliberately worded the same way the public item page words it,
            # and deliberately says nothing about *why* (sold vs. pickup
            # scheduled vs. shipping) — the buyer does not need the internal
            # status and probing must not reveal it.
            errors.append("this item is no longer available")
            unavailable = True

    if not name:
        errors.append("name is required")
    elif len(name) > MAX_NAME:
        errors.append("name is too long")

    if not contact:
        errors.append("contact is required")
    elif len(contact) > MAX_CONTACT:
        errors.append("contact is too long")

    if len(message) > MAX_MESSAGE:
        errors.append("message is too long")

    return ValidationResult(
        ok=not errors, errors=errors, is_bot=False, unavailable=unavailable
    )


# ---------------------------------------------------------------------------
# Bundles
#
# One implementation of the bundle arithmetic, living here because this file
# is copied verbatim into the public serverless bundle and must therefore stay
# free of package imports. The private side reaches it through
# ``estate/bundling.py``; the browser mirrors it in a few lines of inline
# JavaScript. Keeping the maths in one place is what stops the price a buyer
# saw, the price recomputed when their inquiry arrives, and the price that
# reaches Telegram from ever disagreeing.
#
# Nothing here has any concept of a floor price. Floors are never published,
# never sent to a browser, and never present in this bundle. What *is*
# published is a per-item "band": the index of the deepest configured tier
# that item can absorb without breaching its floor, computed on the private
# side by ``bundling.discount_band``. See that module for why a band rather
# than a maximum-discount percentage, and for the proof that the floor holds.
# ---------------------------------------------------------------------------


def normalise_tiers(tiers) -> list:
    """Clean an arbitrary tier table into a sorted, validated list.

    Accepts the shape stored in ``estate/config/pricing.json``. Rows that are
    malformed, negative, or claim a discount of 100% or more are dropped
    rather than clamped: a nonsensical tier is a configuration mistake, and
    quietly reinterpreting it as some other discount would hide the mistake
    behind a number that looks deliberate. The result is sorted by
    ``min_items`` ascending.
    """
    cleaned = []
    for row in tiers or []:
        if not isinstance(row, dict):
            continue
        try:
            min_items = int(row.get("min_items", 0))
            pct = float(row.get("discount_pct", 0))
        except (TypeError, ValueError):
            continue
        if min_items < 2 or not (0 < pct < 1):
            continue
        cleaned.append({"min_items": min_items, "discount_pct": pct})
    cleaned.sort(key=lambda r: (r["min_items"], r["discount_pct"]))
    return cleaned


def tier_discount_for_count(count: int, tiers) -> float:
    """The discount the tier table alone would grant for ``count`` items.

    Ignores floors entirely — ``bundle_quote`` is what applies the binding
    per-item constraint on top of this.
    """
    try:
        count = int(count)
    except (TypeError, ValueError):
        return 0.0
    applicable = 0.0
    for tier in normalise_tiers(tiers):
        if count >= tier["min_items"]:
            applicable = max(applicable, tier["discount_pct"])
    return applicable


def _round_up_to(value: float, step: int) -> float:
    """Round ``value`` UP to the next multiple of ``step``.

    Upward, deliberately. Rounding a bundle total downward could shave a few
    dollars off a basket that was already sitting exactly on a floor
    constraint, which is the one thing the floor rule forbids. Rounding up
    costs a buyer at most ``step - 1`` on an explicitly indicative figure and
    can never breach a floor.
    """
    if step <= 0:
        return round(value, 2)
    return float(math.ceil(value / step) * step)


def bundle_quote(entries, tiers, *, round_to: int = 5) -> dict:
    """Price a basket. Pure arithmetic over already-public numbers.

    ``entries`` is a list of ``{"item_id", "price", "band"}`` dicts drawn from
    the catalogue manifest — never from the request body, so a buyer cannot
    talk their own basket into a discount by claiming a generous band.

    Returns the full breakdown a human needs to answer the enquiry: every
    item and its individual price, the undiscounted subtotal, the discount
    fraction actually applied, and the indicative total. ``capped_by_floor``
    reports whether the tier the basket's size would have earned was reduced
    by an item that could not absorb it — the reviewer wants to know that,
    because it is the difference between "I gave them the standard 15%" and
    "one piece in here is already near its floor".
    """
    rows = []
    subtotal = 0.0
    priced = 0
    band_limit = None

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        item_id = _collapse(entry.get("item_id", ""), MAX_ITEM_ID)
        try:
            price = float(entry.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            band = int(entry.get("band") or 0)
        except (TypeError, ValueError):
            band = 0
        rows.append({"item_id": item_id, "price": price if price > 0 else None})
        if price > 0:
            subtotal += price
            priced += 1
        # An unpriced item ("price on request") contributes no discount
        # headroom, so it pins the whole basket at band 0. That is the honest
        # outcome: we cannot promise a percentage off a number we have not
        # published.
        limit = band if price > 0 else 0
        band_limit = limit if band_limit is None else min(band_limit, limit)

    tiers = normalise_tiers(tiers)
    count_discount = tier_discount_for_count(len(rows), tiers)
    if band_limit is None or band_limit <= 0 or not tiers:
        floor_capped_discount = 0.0
    else:
        floor_capped_discount = tiers[min(band_limit, len(tiers)) - 1]["discount_pct"]

    discount = min(count_discount, floor_capped_discount)
    if priced == 0:
        discount = 0.0

    total = _round_up_to(subtotal * (1.0 - discount), round_to) if subtotal else 0.0
    # Rounding up must never hand back more than the undiscounted subtotal.
    if subtotal and total > subtotal:
        total = round(subtotal, 2)

    return {
        "items": rows,
        "count": len(rows),
        "priced_count": priced,
        "subtotal": round(subtotal, 2),
        "discount_pct": discount,
        "discount_amount": round(max(0.0, subtotal - total), 2),
        "total": total,
        "tier_discount_pct": count_discount,
        "capped_by_floor": bool(discount < count_discount),
        "unpriced_items": [r["item_id"] for r in rows if r["price"] is None],
    }


def parse_basket(raw, *, limit: int = MAX_BUNDLE_ITEMS) -> list:
    """Extract a clean, deduplicated list of item IDs from untrusted input.

    Accepts either a list or a comma-separated string, because a basket can
    arrive from a JSON body or from a shared ``?b=`` URL. Everything that is
    not a well-formed item ID is dropped silently — this is a filter, not a
    validator; ``validate_bundle`` is what decides whether the surviving IDs
    are acceptable. Order is preserved so the reviewer sees the basket in the
    order the buyer built it.
    """
    if isinstance(raw, str):
        candidates = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        candidates = list(raw)[: limit * 4]
    else:
        return []

    out = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, (str, int)):
            continue
        item_id = _collapse(candidate, MAX_ITEM_ID)
        if not item_id or item_id in seen:
            continue
        if not is_valid_item_id_format(item_id):
            continue
        seen.add(item_id)
        out.append(item_id)
        if len(out) >= limit:
            break
    return out


def validate_bundle(item_ids, manifest, *, limit: int = MAX_BUNDLE_ITEMS) -> ValidationResult:
    """Check every ID in a basket against the manifest, server-side.

    A basket is untrusted input and is validated exactly as strictly as a
    single ``item_id`` would be: every entry must be a known, currently
    available catalogue item. One unavailable item fails the whole basket
    rather than being quietly dropped, because silently selling someone three
    of the four things they asked for — without saying which one went — is
    the kind of small dishonesty that loses a buyer's trust at the worst
    possible moment.

    The errors deliberately name the offending IDs. Unlike the single-item
    path there is nothing to protect here: the buyer already holds these IDs,
    they are all public catalogue references, and a basket that fails without
    saying which item failed is unactionable.
    """
    errors = []
    if not item_ids:
        return ValidationResult(ok=False, errors=["no items selected"])
    if len(item_ids) > limit:
        return ValidationResult(ok=False, errors=["too many items selected"])

    unknown = [i for i in item_ids if not is_known_item(i, manifest)]
    if unknown:
        errors.append("not in the current catalogue: " + ", ".join(sorted(unknown)))

    gone = [
        i
        for i in item_ids
        if i not in unknown and not is_item_available(i, manifest)
    ]
    if gone:
        errors.append("no longer available: " + ", ".join(sorted(gone)))

    return ValidationResult(
        ok=not errors, errors=errors, is_bot=False, unavailable=bool(gone) and not unknown
    )


def manifest_entries(item_ids, manifest) -> list:
    """Build ``bundle_quote`` input from the manifest, never from the request.

    This is the seam that makes the quoted total trustworthy: prices and
    bands are read out of the build-time manifest by item ID, so the only
    thing the buyer controls is *which* items are in the basket.
    """
    entries = []
    for item_id in item_ids or []:
        entry = (manifest or {}).get(item_id)
        if not isinstance(entry, dict):
            continue
        entries.append(
            {
                "item_id": item_id,
                "price": entry.get("price"),
                "band": entry.get("band", 0),
            }
        )
    return entries


# ---------------------------------------------------------------------------
# Normalisation, audit fields, and duplicate detection
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def _collapse(value, limit: int) -> str:
    """Trim, collapse runs of whitespace, strip control characters, truncate.

    Control-character stripping matters because these values end up in a
    Telegram message and a JSON audit line; a raw newline run or an embedded
    NUL from a scripted submission should not be able to reshape either.
    """
    text = str(value or "")
    # Control characters become spaces rather than vanishing, so "a\tb" reads
    # as two words rather than "ab"; the collapse below then removes the runs.
    text = "".join(ch if (ch >= " " or ch in "\n\r\t") else " " for ch in text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def normalize_contact(contact) -> str:
    """Normalise a contact value for comparison purposes.

    Lower-cases it (email addresses are case-insensitive in practice, and
    phone numbers are unaffected) and strips every character that is not
    alphanumeric or one of ``@ . + -``. Used only to build the duplicate
    fingerprint — the value shown to the reviewer is always the buyer's
    original text, never this reduction.
    """
    text = _collapse(contact, MAX_CONTACT).lower()
    return "".join(ch for ch in text if ch.isalnum() or ch in "@.+-")


def normalize_inquiry(
    *,
    item_id: str = "",
    name: str = "",
    contact: str = "",
    message: str = "",
    offer: str = "",
    quote: dict | None = None,
) -> dict:
    """Return the buyer-supplied fields, cleaned and length-capped.

    Only ever contains data the buyer typed, the item ID they were already
    looking at, and — for a basket — the quote the *server* computed from the
    manifest. Nothing from the private side of the system (floor price,
    internal notes, pickup address, review token) is in scope here or
    anywhere downstream of here.

    ``quote`` is the output of ``bundle_quote``. It is attached verbatim so
    the reviewer's notification can show every item, its individual price,
    the indicative total and the discount applied without a second lookup.
    """
    record = {
        "item_id": _collapse(item_id, MAX_ITEM_ID),
        "name": _collapse(name, MAX_NAME),
        "contact": _collapse(contact, MAX_CONTACT),
        "message": _collapse(message, MAX_MESSAGE),
        "offer": parse_offer(offer),
    }
    if quote:
        record["bundle"] = quote
    return record


def inquiry_fingerprint(item_id: str, contact: str, message: str, items=None) -> str:
    """A stable hash identifying "the same person asking the same thing".

    Deliberately excludes ``name`` and ``offer``: a buyer who resubmits with
    a corrected name, or who comes back with a different number, is saying
    something new and should reach a human. A byte-identical resend of the
    same message about the same item to the same contact is a double-click or
    a retry, not a second inquiry.

    ``items`` folds a basket into the identity, so the same buyer sending the
    same words about a *different* selection reaches a human, while a
    double-clicked basket does not. The basket is sorted first: the same four
    items chosen in a different order is the same enquiry.
    """
    basis = "|".join(
        [
            _collapse(item_id, MAX_ITEM_ID),
            normalize_contact(contact),
            _collapse(message, MAX_MESSAGE).lower(),
            ",".join(sorted(items or [])),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def hash_identity(value, salt: str = "") -> str:
    """Salted, truncated hash of a client identifier (typically an IP).

    The raw IP is never stored or forwarded: it is only useful here as a
    rate-limit and abuse-correlation key, and keeping it in hashed form means
    an inquiry log that leaks does not also leak the visitors' addresses.
    Truncated to 16 hex characters — enough to correlate, short enough to be
    obviously not a reversible record.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    return hashlib.sha256((salt + "|" + text).encode("utf-8")).hexdigest()[:16]


def build_audit_record(
    normalized: dict,
    *,
    client_ip: str = "",
    user_agent: str = "",
    salt: str = "",
    source: str = "public_site",
    now: float | None = None,
    inquiry_id: str | None = None,
) -> dict:
    """Wrap normalised buyer fields with the audit fields a reviewer needs.

    Every field here is either buyer-supplied, derived from the request
    envelope, or generated locally. Nothing is read from the private estate
    database, so this record is safe to hand to a notifier that runs outside
    the VPS trust boundary.
    """
    now = time.time() if now is None else now
    received_at = datetime.fromtimestamp(now, tz=timezone.utc)
    record = dict(normalized)
    record.update(
        {
            "inquiry_id": inquiry_id or uuid.uuid4().hex,
            "received_at": received_at.isoformat().replace("+00:00", "Z"),
            "received_at_epoch": now,
            "source": source,
            "fingerprint": inquiry_fingerprint(
                normalized.get("item_id", ""),
                normalized.get("contact", ""),
                normalized.get("message", ""),
                [
                    row.get("item_id", "")
                    for row in ((normalized.get("bundle") or {}).get("items") or [])
                ],
            ),
            "client_hash": hash_identity(client_ip, salt),
            "user_agent": _collapse(user_agent, 200),
        }
    )
    return record


def is_duplicate(
    fingerprint: str,
    seen: dict,
    *,
    now: float | None = None,
    window_seconds: int = 900,
) -> bool:
    """True if ``fingerprint`` was already seen inside the trailing window.

    ``seen`` maps fingerprint -> last-seen epoch seconds and is owned by the
    caller, exactly like ``is_rate_limited``'s ``timestamps``: this function
    reads it but never writes it, so it stays pure and testable without a
    store. See ``record_seen`` for the write half.

    The default 15-minute window targets the real failure mode — a
    double-click, an impatient resubmit, a browser retry after a slow
    response — rather than trying to deduplicate across days, which would
    wrongly swallow a buyer legitimately following up.
    """
    now = time.time() if now is None else now
    last = (seen or {}).get(fingerprint)
    if last is None:
        return False
    return (now - last) <= window_seconds


def record_seen(
    fingerprint: str,
    seen: dict,
    *,
    now: float | None = None,
    window_seconds: int = 900,
    max_entries: int = 512,
) -> None:
    """Record ``fingerprint`` in ``seen`` and evict anything past the window.

    Bounded so a long-lived process (or a warm serverless instance) cannot
    grow this dict without limit from hostile traffic: once ``max_entries``
    is exceeded, the oldest entries go first.
    """
    now = time.time() if now is None else now
    seen[fingerprint] = now
    for key, ts in list(seen.items()):
        if now - ts > window_seconds:
            del seen[key]
    if len(seen) > max_entries:
        for key, _ in sorted(seen.items(), key=lambda kv: kv[1])[
            : len(seen) - max_entries
        ]:
            del seen[key]


# ---------------------------------------------------------------------------
# Rate limiting: policy only, no storage.
#
# Deliberately pure so it is testable without a fake Redis/KV store, and so
# it can run identically inside a stateless serverless function invocation
# (which owns wherever `timestamps` actually persists between calls, if
# anywhere) or inside the long-running VPS process.
# ---------------------------------------------------------------------------


def is_rate_limited(
    timestamps: list,
    *,
    now: float | None = None,
    window_seconds: int = 300,
    max_requests: int = 5,
) -> bool:
    """Decide whether a new request from one identity should be blocked.

    ``timestamps`` is that identity's prior request times (epoch seconds) —
    e.g. keyed by a hashed IP or contact value by the caller. Policy: no more
    than ``max_requests`` within the trailing ``window_seconds``.

    This function does not read or write any store. The serverless function
    keeps ``timestamps`` in module-level state, which survives for as long as
    one warm instance lives and is therefore *best effort*: it stops the
    common case (one client hammering one instance) and does not stop a
    distributed flood, because a stateless host gives it nowhere shared to
    look. That is the ceiling the deployment environment safely permits
    without provisioning an external store; see ``estate/serverless.py`` and
    ``NEXT_SESSION_HANDOFF.md`` for the durable options (Vercel KV/Edge
    Config, or a platform-level WAF rule) and why none is wired up yet.
    """
    now = time.time() if now is None else now
    recent = [t for t in timestamps if now - t <= window_seconds]
    return len(recent) >= max_requests


def record_request(
    timestamps: list,
    *,
    now: float | None = None,
    window_seconds: int = 300,
) -> list:
    """Append ``now`` to ``timestamps`` and drop anything outside the window.

    Returns the pruned list. Kept separate from ``is_rate_limited`` so the
    policy stays a pure predicate and the bookkeeping is explicit at the call
    site.
    """
    now = time.time() if now is None else now
    kept = [t for t in timestamps if now - t <= window_seconds]
    kept.append(now)
    return kept
