#!/usr/bin/env python3
"""Is this thing actually ready for someone's dad to use? Check before he tries.

    python scripts/estate_preflight.py            # check everything
    python scripts/estate_preflight.py --submitter 123456789
    python scripts/estate_preflight.py --live-vision   # makes ONE paid call

Every check prints PASS, WARN, or FAIL with a specific instruction. Exit code
is 0 when nothing FAILed, 1 otherwise, so this can gate a deploy.

Read-only by default. `--live-vision` is the single exception and it is
opt-in precisely because it costs money: it sends one small generated test
image to the configured provider to prove the credential works, which is the
one thing no amount of offline checking can establish.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_ICON = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Report:
    def __init__(self) -> None:
        self.rows: list = []

    def add(self, level: str, check: str, detail: str, fix: str = "") -> None:
        self.rows.append((level, check, detail, fix))

    def render(self) -> int:
        print()
        for level, check, detail, fix in self.rows:
            print("[%s] %-28s %s" % (_ICON[level], check, detail))
            if fix and level != PASS:
                print(" " * 39 + "-> " + fix)
        failed = sum(1 for r in self.rows if r[0] == FAIL)
        warned = sum(1 for r in self.rows if r[0] == WARN)
        print()
        if failed:
            print("NOT READY: %d check(s) failed, %d warning(s)." % (failed, warned))
        elif warned:
            print("Ready, with %d warning(s) worth reading." % warned)
        else:
            print("Ready. Hand him the phone.")
        return 1 if failed else 0


def check_submitters(report: Report, settings, expected: str) -> None:
    submitters = settings.estate_submitters()
    general = settings.allowed_user_ids()

    if not submitters:
        report.add(FAIL, "submitter allowlist", "nobody may submit items",
                   "set ESTATE_ALLOWED_SUBMITTER_IDS to a comma-separated list "
                   "of Telegram numeric user IDs")
        return

    report.add(PASS, "submitter allowlist", "%d submitter(s) configured" % len(submitters))

    if expected:
        if expected in submitters:
            report.add(PASS, "his Telegram ID", "%s may submit items" % expected)
        else:
            report.add(FAIL, "his Telegram ID", "%s is NOT on the submitter list" % expected,
                       "add it to ESTATE_ALLOWED_SUBMITTER_IDS and restart")
        if expected in general:
            report.add(WARN, "his access level", "%s is also a full D.R.A.K.E. user" % expected,
                       "he will get the assistant greeting, not the selling guide, and "
                       "can drive the whole assistant. Remove him from "
                       "TELEGRAM_ALLOWED_USER_IDS unless that is intended.")
        else:
            report.add(PASS, "his access level", "selling only, not the whole assistant")

    if not settings.estate_reviewer_ids.strip():
        report.add(WARN, "reviewer allowlist", "no separate reviewer configured",
                   "ESTATE_REVIEWER_IDS is empty, so every submitter can also "
                   "approve prices. Set it to your own ID if that matters.")
    else:
        report.add(PASS, "reviewer allowlist", "approval restricted to named reviewers")


def check_vision(report: Report, settings, live: bool) -> None:
    from estate import vision

    name = (settings.estate_vision_provider or "mock").strip().lower()
    if name == "mock":
        report.add(FAIL, "vision provider", "mock — every item will be unidentifiable",
                   "set ESTATE_VISION_PROVIDER=anthropic (or openai) and the matching "
                   "API key, then restart. Mock output is blocked from publication, so "
                   "he can submit but nothing usable comes back.")
        return

    key = {"anthropic": settings.anthropic_api_key,
           "openai": settings.openai_api_key}.get(name, "")
    if not key:
        report.add(FAIL, "vision provider",
                   "%s selected but no API key is set" % name,
                   "set %s_API_KEY. Without it the system silently falls back to "
                   "mock." % name.upper())
        return

    provider = vision.get_vision_provider()
    if isinstance(provider, vision.MockVisionProvider):
        report.add(FAIL, "vision provider",
                   "%s could not start: %s" % (name, getattr(provider, "fallback_reason", "")),
                   "the SDK may be missing from the venv — pip install -e '.[dev]'")
        return

    report.add(PASS, "vision provider", "%s (%s) ready" % (name, provider.model))

    if not live:
        report.add(WARN, "vision credential", "not verified — no call was made",
                   "re-run with --live-vision to prove the key works. Costs about "
                   "3 cents. Worth it before he starts.")
        return

    try:
        image = _test_image()
        ident = provider.identify([image], hint="A plain grey test square.")
        report.add(PASS, "vision credential",
                   "live call succeeded in %.1fs" % ident.processing_seconds)
    except Exception as exc:
        report.add(FAIL, "vision credential", "live call failed: %s" % type(exc).__name__,
                   "check the API key and that the account has credit")


def _test_image() -> Path:
    """A tiny generated PNG. Never a real photo of anything private."""
    import base64
    import tempfile

    # 1x1 grey PNG.
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    path = Path(tempfile.gettempdir()) / "estate_preflight_test.png"
    path.write_bytes(data)
    return path


def check_research(report: Report, settings) -> None:
    name = (settings.estate_research_provider or "manual_queue").strip().lower()
    if name == "manual_queue":
        report.add(WARN, "research provider", "manual queue — comparables are filled in by hand",
                   "expected. He can still submit everything; you fill in sold prices "
                   "from the worksheet in each item's research/ folder before approving.")
    else:
        report.add(PASS, "research provider", name)


def check_deadline(report: Report, settings) -> None:
    from datetime import date, datetime

    from estate.settings import move_out_date

    raw = move_out_date()
    if not raw:
        report.add(WARN, "move-out date", "not set — urgency and markdowns are switched off",
                   "set ESTATE_MOVE_OUT_DATE=YYYY-MM-DD")
        return
    try:
        deadline = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        report.add(FAIL, "move-out date", "%r is not YYYY-MM-DD" % raw,
                   "fix ESTATE_MOVE_OUT_DATE")
        return
    days = (deadline - date.today()).days
    level = PASS if days > 14 else WARN
    report.add(level, "move-out date", "%s — %d day(s) away" % (raw, days))


def check_storage(report: Report) -> None:
    from estate import paths

    try:
        root = paths.inventory_root()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".preflight"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.add(PASS, "photo storage", "writable at %s" % root)
    except Exception as exc:
        report.add(FAIL, "photo storage", "not writable: %s" % type(exc).__name__,
                   "check ESTATE_INVENTORY_DIR and its permissions. Photos are the "
                   "one thing that must never be lost.")


def check_database(report: Report) -> None:
    from estate.migrations import ensure_estate_schema
    from estate.repository import ItemRepository
    from estate._compat import get_engine, get_session, init_db

    try:
        init_db()
        added = ensure_estate_schema(get_engine())
    except Exception as exc:
        report.add(FAIL, "database", "could not open or migrate: %s" % type(exc).__name__,
                   "check DATABASE_URL and disk space")
        return

    if added:
        report.add(PASS, "database migration", "%d column(s) added just now" % len(added))
    else:
        report.add(PASS, "database migration", "schema already current")

    session = get_session()
    try:
        items = ItemRepository(session).all()
        unscored = [i for i in items if not (i.priority_score or 0)
                    and i.status not in ("Sold", "Donated", "Removed")]
        if unscored:
            report.add(WARN, "priority scores",
                       "%d open item(s) have no score yet" % len(unscored),
                       "run: python scripts/estate_inventory.py --reprioritise")
        else:
            report.add(PASS, "priority scores", "%d item(s) scored" % len(items))
    finally:
        session.close()


def check_telegram(report: Report, settings) -> None:
    if not settings.telegram_bot_token.strip():
        report.add(FAIL, "telegram bot", "TELEGRAM_BOT_TOKEN is not set",
                   "without it the bot cannot receive anything")
        return
    report.add(PASS, "telegram bot", "token configured")
    if not getattr(settings, "estate_enabled", True):
        report.add(FAIL, "estate feature flag", "ESTATE_ENABLED is false",
                   "set ESTATE_ENABLED=true and restart, or the handlers never register")
    else:
        report.add(PASS, "estate feature flag", "enabled")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--submitter", default="",
                        help="the Telegram numeric user ID you are setting up")
    parser.add_argument("--live-vision", action="store_true",
                        help="make ONE real vision call to prove the credential works")
    args = parser.parse_args()

    from estate._compat import get_settings

    settings = get_settings()
    report = Report()

    check_telegram(report, settings)
    check_submitters(report, settings, args.submitter.strip())
    check_vision(report, settings, args.live_vision)
    check_research(report, settings)
    check_storage(report)
    check_database(report)
    check_deadline(report, settings)

    return report.render()


if __name__ == "__main__":
    raise SystemExit(main())
