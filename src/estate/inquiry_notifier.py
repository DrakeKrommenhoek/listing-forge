"""Buyer-inquiry notification providers.

Mirrors the ``VisionProvider`` / ``ResearchProvider`` pattern already used
elsewhere in ``estate/``: an ABC plus a safe, credential-free default that
requires no external account, and opt-in subclasses that are only selected
when their configuration is explicitly present.

Deliberately dependency-free (standard library only): this module is copied
verbatim into the standalone serverless inquiry function's build output (see
``estate/serverless.py``), which runs outside this package entirely.

Durability
----------
``LocalLogNotifier`` is **not** durable on a serverless host and must never
be treated as if it were — see its docstring. ``TelegramNotifier`` is the
durable path: a delivered Telegram message persists in the reviewer's chat
history independently of the function that sent it, which is exactly the
property a serverless ``/tmp`` file lacks.

Credential boundary
-------------------
``TelegramNotifier`` reads its bot token and chat ID from the environment and
nothing else. No token is ever written into a site build, committed, logged,
echoed in an HTTP response, or included in an exception message. The token
that configures it should be a **dedicated bot created for this endpoint**,
not the main D.R.A.K.E. bot token: a public serverless function is a wider
trust boundary than the private VPS, and a leak there must not hand anyone
control of the assistant bot that reads Drake's own messages. Creating a
second bot via @BotFather is free and creates no account of any kind — see
DEPLOYMENT.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path


class InquiryNotifier(ABC):
    """Delivers one buyer inquiry somewhere a human will see it."""

    @abstractmethod
    def notify(self, inquiry: dict) -> bool:
        """Deliver ``inquiry``. Return True if delivered or durably queued.

        Must never raise — a notifier failure must not lose the caller's
        ability to at least tell the buyer "thank you, we received this."
        """


class LocalLogNotifier(InquiryNotifier):
    """Default. Appends the inquiry as one JSON line to a local file.

    Requires no credentials and makes no network call, so it is safe to run
    anywhere — including a serverless function — without provisioning
    anything.

    Caveat: on most serverless hosts (e.g. Vercel) only ``/tmp`` is writable,
    and it is wiped between cold starts and never shared across concurrent
    invocations. This is therefore a best-effort local record for local/dev
    use, not durable production storage. It exists so the inquiry flow has
    *some* visible trail before a real notifier is wired up. Do not treat an
    inquiry logged only here as guaranteed to reach anyone.
    """

    def __init__(self, path: str = "/tmp/estate_inquiries.jsonl") -> None:
        self.path = Path(path)

    def notify(self, inquiry: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            record = dict(inquiry)
            record.setdefault("received_at", time.time())
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            return True
        except OSError:
            return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

TELEGRAM_API_ROOT = "https://api.telegram.org"
TELEGRAM_MAX_LEN = 4096


def _money(value) -> str:
    """Format a dollar figure for a reviewer reading it on a phone."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"${amount:,.0f}" if amount == int(amount) else f"${amount:,.2f}"


def _bundle_lines(bundle: dict) -> list:
    """Render a basket so a reply can be written without opening a laptop.

    Every item, its individual price, the indicative total, and the discount
    applied — in that order, because that is the order the answer gets
    written in. If the tier the basket size earned was reduced by an item
    sitting close to its approved floor, say so: the reviewer needs to know
    the difference between a standard discount and a constrained one before
    they offer to round it down.

    The word "indicative" is not decoration. Nothing on the website commits
    to a price, and the reviewer is the one who confirms.
    """
    rows = bundle.get("items") or []
    lines = ["", f"Bundle: {len(rows)} items"]
    for row in rows:
        price = row.get("price")
        lines.append(
            "  {} - {}".format(
                row.get("item_id", "?"),
                _money(price) if price is not None else "price on request",
            )
        )
    lines.append(f"  Subtotal: {_money(bundle.get('subtotal'))}")

    discount = bundle.get("discount_pct") or 0
    if discount:
        lines.append(
            "  Discount: {:.0f}% (-{})".format(
                discount * 100, _money(bundle.get("discount_amount"))
            )
        )
    else:
        lines.append("  Discount: none")

    lines.append(f"  Indicative total: {_money(bundle.get('total'))}")

    if bundle.get("capped_by_floor"):
        tier = bundle.get("tier_discount_pct") or 0
        lines.append(
            f"  NOTE: size alone would give {tier * 100:.0f}% - capped by an item "
            "near its floor."
        )
    if bundle.get("unpriced_items"):
        lines.append(
            "  Not priced yet: " + ", ".join(bundle["unpriced_items"])
        )
    lines.append("  Indicative only - you confirm the price on reply.")
    return lines


def format_inquiry_message(inquiry: dict, *, brand: str = "Future Only") -> str:
    """Render one inquiry as plain text for a Telegram message.

    Plain text, no ``parse_mode`` — the same policy as
    ``channels/telegram.py``. Buyer-supplied text is therefore rendered
    literally and cannot inject formatting or a link preview through stray
    Markdown characters.

    Only fields present in the audit record are rendered, and that record
    only ever contains buyer-supplied values, locally generated audit fields,
    and a bundle quote the server computed from the public manifest. There is
    no code path from a private field (floor price, internal notes, pickup
    address) into this string.
    """
    item_id = str(inquiry.get("item_id") or "").strip()
    bundle = inquiry.get("bundle") or {}
    lines = [
        f"{brand} - new enquiry",
        "Item: {}".format(
            item_id
            or ("{} items (bundle)".format(bundle.get("count"))
                if bundle else "(general enquiry)")
        ),
        f"From: {inquiry.get('name') or '(no name)'}",
        f"Contact: {inquiry.get('contact') or '(none given)'}",
    ]
    offer = inquiry.get("offer")
    if offer is not None:
        lines.append(f"Offer: {offer}")
    if bundle:
        lines.extend(_bundle_lines(bundle))
    message = str(inquiry.get("message") or "").strip()
    lines.append("")
    lines.append(message or "(no message)")
    lines.append("")
    lines.append(f"Ref: {inquiry.get('inquiry_id', '')}")
    received = inquiry.get("received_at")
    if received:
        lines.append(f"Received: {received}")
    text = "\n".join(lines)
    if len(text) > TELEGRAM_MAX_LEN:
        # Truncate rather than split: the reviewer needs the header and the
        # ref, and a message long enough to overflow is already suspect.
        text = text[: TELEGRAM_MAX_LEN - 3] + "..."
    return text


class TelegramNotifier(InquiryNotifier):
    """Durable delivery: sends the inquiry to one approved Telegram chat.

    Durable in the sense that matters here — once ``sendMessage`` returns
    ``ok``, the message exists in Telegram's history on the reviewer's device
    and no longer depends on this process, this instance, or this host
    surviving. That is the property ``LocalLogNotifier`` cannot provide on a
    serverless runtime.

    Configuration is environment-only and mandatory: no token or chat ID is
    ever defaulted, embedded, or inferred. ``configured`` is False when
    either is missing, which is how ``notifier_from_env`` decides not to
    select it — an unconfigured deployment falls back to the local log rather
    than silently pretending to notify.

    ``chat_id`` is the approved reviewer's chat. It is supplied by
    configuration rather than by the request, so a caller cannot redirect an
    inquiry to an arbitrary chat.
    """

    def __init__(
        self,
        token: str = "",
        chat_id: str = "",
        *,
        brand: str = "Future Only",
        timeout: float = 8.0,
        api_root: str = TELEGRAM_API_ROOT,
    ) -> None:
        self.token = (token or "").strip()
        self.chat_id = str(chat_id or "").strip()
        self.brand = brand or "Future Only"
        self.timeout = timeout
        self.api_root = (api_root or TELEGRAM_API_ROOT).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _post(self, text: str) -> bool:
        """POST one sendMessage call. Returns True only on a confirmed ok."""
        url = f"{self.api_root}/bot{self.token}/sendMessage"
        body = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - fixed https api root
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
        return bool(payload.get("ok"))

    def notify(self, inquiry: dict) -> bool:
        if not self.configured:
            return False
        try:
            return self._post(format_inquiry_message(inquiry, brand=self.brand))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Swallowed by contract: notify() must never raise, and the
            # exception text can contain the request URL, which contains the
            # bot token. Never re-raise it and never log it.
            return False


# ---------------------------------------------------------------------------
# Composition and selection
# ---------------------------------------------------------------------------


class ChainNotifier(InquiryNotifier):
    """Tries several notifiers; reports success if any durable one succeeded.

    Two distinct roles are kept separate on purpose:

    ``durable``  notifiers whose success actually means a human will see this
                 (currently only Telegram). At least one must succeed for
                 ``notify`` to return True.
    ``trail``    best-effort local records that always run, whether or not a
                 durable notifier worked, so there is a local breadcrumb for
                 debugging. Their result never affects the return value —
                 treating a ``/tmp`` write as delivery is exactly the false
                 confidence this class exists to prevent.

    With no durable notifiers configured, ``notify`` returns False even
    though the trail write succeeded. That is deliberate: the caller must be
    able to tell the buyer honestly that the message did not get through.
    """

    def __init__(self, durable: list, trail: list | None = None) -> None:
        self.durable = list(durable or [])
        self.trail = list(trail or [])

    def notify(self, inquiry: dict) -> bool:
        delivered = False
        for notifier in self.durable:
            try:
                if notifier.notify(inquiry):
                    delivered = True
            except Exception:  # noqa: BLE001 - contract: never raise
                continue
        for notifier in self.trail:
            try:
                notifier.notify(inquiry)
            except Exception:  # noqa: BLE001 - contract: never raise
                continue
        return delivered


def notifier_from_env(env: dict | None = None) -> InquiryNotifier:
    """Build the notifier this deployment is configured for.

    Selection is explicit and fails safe:

    * ``ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN`` and
      ``ESTATE_INQUIRY_TELEGRAM_CHAT_ID`` both set -> Telegram (durable),
      with the local log kept as a non-authoritative trail.
    * either missing -> local log only, and ``notify`` will report failure,
      so the endpoint answers with an honest "could not deliver" rather than
      a thank-you that nobody will ever read.

    There is no default token and no fallback chat. An unconfigured
    deployment is loud, not silently broken.
    """
    env = os.environ if env is None else env
    log_path = env.get("ESTATE_INQUIRY_LOG_PATH", "/tmp/estate_inquiries.jsonl")  # noqa: S108
    trail = [LocalLogNotifier(log_path)]

    telegram = TelegramNotifier(
        token=env.get("ESTATE_INQUIRY_TELEGRAM_BOT_TOKEN", ""),
        chat_id=env.get("ESTATE_INQUIRY_TELEGRAM_CHAT_ID", ""),
        brand=env.get("ESTATE_BRAND_NAME", "Future Only"),
    )
    durable = [telegram] if telegram.configured else []
    return ChainNotifier(durable=durable, trail=trail)


# ---------------------------------------------------------------------------
# Future: an EmailNotifier for the dedicated buyer inbox.
#
# Once ESTATE_SELLING_EMAIL names an actual, monitored mailbox, add an
# EmailNotifier here using a transactional-email API (Resend, Postmark,
# SES...) and append it to `durable` above. It must:
#   - read its API key from an environment variable, never hard-code one
#   - fail safely (return False, never raise) so an outage never loses the
#     inquiry -- it joins Telegram in `durable`, it does not replace it
#   - never be selected by default; require an explicit env var to opt in
# Do not implement this until a provider is chosen and an account exists --
# see the standing restriction against activating paid APIs. Telegram exists
# specifically so that restriction does not leave inquiries undelivered in
# the meantime.
# ---------------------------------------------------------------------------
