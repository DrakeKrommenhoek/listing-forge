"""Tests for the /done command-precedence fix.

Background: the estate ``/done`` handler and the original, general-purpose
D.R.A.K.E. ``/done`` handler (channels/telegram.py) are both registered on the
same python-telegram-bot Application. Within a single handler group, only the
first handler whose filter matches actually runs; the estate handler must
therefore (a) be registered in an earlier group than the original, and (b)
explicitly defer -- without replying, without raising
``ApplicationHandlerStop`` -- whenever this Telegram user has no open estate
submission, so the original handler still gets the update exactly as before
the estate system existed. These tests exercise EstateHandlers.cmd_done
directly with a mocked Update, since spinning up a full Application dispatch
loop would exercise python-telegram-bot's own routing rather than this
project's code.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

TMP = tempfile.mkdtemp(prefix="estate-routing-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"
os.environ["ESTATE_ALLOWED_SUBMITTER_IDS"] = "111"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from estate import pipeline  # noqa: E402
from estate.repository import SubmissionRepository  # noqa: E402
from estate.telegram_estate import EstateHandlers  # noqa: E402
from estate._compat import get_session, init_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


def _fake_update(user_id: str):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_done_defers_silently_when_no_estate_submission_is_open():
    """No open submission -> not ours. Must not reply or stop propagation,
    so the general D.R.A.K.E. /done handler still runs for this update."""
    handlers = EstateHandlers(_config.get_settings())
    update = _fake_update("111")

    # Should return normally -- ApplicationHandlerStop must NOT be raised.
    _run(handlers.cmd_done(update, MagicMock()))
    update.message.reply_text.assert_not_called()


def test_done_defers_for_a_user_with_no_submission_even_if_unauthorised():
    """An unrelated/unauthorised Telegram user sending /done (e.g. chatting
    with the general assistant) must not be swallowed by the estate handler
    just because they are not on the estate allowlist."""
    handlers = EstateHandlers(_config.get_settings())
    update = _fake_update("999999")  # not in ESTATE_ALLOWED_SUBMITTER_IDS

    _run(handlers.cmd_done(update, MagicMock()))
    update.message.reply_text.assert_not_called()


def test_done_stops_propagation_when_a_submission_is_open():
    """An open estate submission -> the estate handler takes over fully and
    must raise ApplicationHandlerStop so the general /done never also fires
    on the same update."""
    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        item = pipeline.start_item(session, owner="telegram:111", prefix="DK")
        SubmissionRepository(session).start("111", item.item_id)
    finally:
        session.close()

    update = _fake_update("111")
    with pytest.raises(ApplicationHandlerStop):
        _run(handlers.cmd_done(update, MagicMock()))
    # No photos yet -- should have told the submitter that, not gone silent.
    update.message.reply_text.assert_called_once()
    assert "photo" in update.message.reply_text.call_args[0][0].lower()


def test_done_denies_and_stops_when_submission_open_but_no_longer_allowlisted():
    session = get_session()
    try:
        item = pipeline.start_item(session, owner="telegram:222", prefix="DK")
        SubmissionRepository(session).start("222", item.item_id)
    finally:
        session.close()

    # "222" has an open submission but is not in ESTATE_ALLOWED_SUBMITTER_IDS.
    handlers2 = EstateHandlers(_config.get_settings())
    update = _fake_update("222")
    with pytest.raises(ApplicationHandlerStop):
        _run(handlers2.cmd_done(update, MagicMock()))
    update.message.reply_text.assert_called_once()


# ---------------------------------------------------------------------------
# /done runs the whole job; /myitems reports where every item actually is
# ---------------------------------------------------------------------------

def test_done_runs_the_full_job_and_reports_a_price_back(monkeypatch):
    """The whole point of the system, exercised through the Telegram entry
    point: photos plus a sentence in, a priced and channelled record out,
    with the confirmation message carrying the numbers back to the sender."""
    from estate.repository import CompRepository, ItemRepository

    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        item = pipeline.start_item(session, owner="telegram:111", prefix="DK")
        item_id = item.item_id
        SubmissionRepository(session).start("111", item_id)
        for i in range(4):
            pipeline.attach_photo(session, item_id, b"tg-photo-%d" % i, ext="jpg")
        ItemRepository(session).update(item_id, actor="test", category="Home Decor",
                                       condition="Good", item_name="Teak wall art")
        pipeline.identify_item(session, item_id, hint="Crate & Barrel wall decoration")
        fresh = ItemRepository(session).get(item_id)
        for key in list(fresh.missing_fields or []):
            pipeline.apply_answer(
                session, item_id, key,
                "yes" if key in ("ownership_approval", "shipping_feasible") else "skip",
            )
        for n in range(3):
            CompRepository(session).add(
                item_id, platform="ebay", title="comp", url=f"https://ebay.com/i/{item_id}-{n}",
                is_sold=True, price=100.0 + n * 10, condition="Good",
                observed_date="2026-07-15", relevance=0.9,
            )
    finally:
        session.close()

    summary = handlers._finalise("111", item_id)
    assert "Suggested asking price" in summary
    assert "Best place to sell it" in summary

    session = get_session()
    try:
        fresh = ItemRepository(session).get(item_id)
        assert fresh.processing_stage == "Ready for Review"
        assert fresh.initial_list_price and fresh.expected_net_proceeds is not None
        assert fresh.listing_packages and fresh.priority_score > 0
        # A price recommendation is not an approval.
        assert fresh.approval_status == "Pending"
    finally:
        session.close()


def test_myitems_reports_the_processing_stage_at_every_point(monkeypatch):
    from estate.repository import ItemRepository

    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        waiting = pipeline.start_item(session, owner="telegram:333", prefix="DK")
        pipeline.attach_photo(session, waiting.item_id, b"x", ext="jpg")
        ItemRepository(session).update(
            waiting.item_id, actor="test", processing_stage="Needs Information",
            missing_fields=["condition"], item_name="Unnamed thing", priority_score=10.0,
        )
        listed = pipeline.start_item(session, owner="telegram:333", prefix="DK")
        ItemRepository(session).update(
            listed.item_id, actor="test", processing_stage="Listed",
            item_name="Listed thing", current_price=120.0, priority_score=50.0,
        )
        listed_id, waiting_id = listed.item_id, waiting.item_id
    finally:
        session.close()

    text = handlers._my_items("333")
    assert "listed and waiting for a buyer" in text
    assert "needs a bit more info from you" in text
    # Ranked by priority: the listed item scored higher, so it comes first.
    assert text.index(listed_id) < text.index(waiting_id)
    # And the outstanding question is shown, not just the fact that one exists.
    assert "condition" in text.lower()


# ---------------------------------------------------------------------------
# The non-technical path: what someone's dad will actually do
# ---------------------------------------------------------------------------

def _photo_update(user_id: str, file_id: str = "f1"):
    update = _fake_update(user_id)
    photo = MagicMock()
    photo.file_id = file_id
    photo.file_size = 1024
    update.message.photo = [photo]
    update.message.media_group_id = None
    update.message.caption = ""
    return update


def _context_returning(data: bytes):
    ctx = MagicMock()
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    ctx.bot.get_file = AsyncMock(return_value=tg_file)
    return ctx


def test_photos_with_no_open_item_start_one_instead_of_being_refused():
    """The single most likely first interaction: he was told 'take photos and
    send them', so he does exactly that, without typing /newitem."""
    from estate.repository import SubmissionRepository as Subs

    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        Subs(session).close_all("111")
    finally:
        session.close()

    update = _photo_update("111")
    _run(handlers.on_photo(update, _context_returning(b"real-photo-bytes")))

    replies = [c[0][0] for c in update.message.reply_text.call_args_list]
    assert any("started a new item" in r.lower() for r in replies), replies
    assert not any("/newitem" in r for r in replies), "told him he used it wrong"

    session = get_session()
    try:
        open_sub = Subs(session).open_for_user("111")
        assert open_sub is not None and open_sub.item_id
    finally:
        session.close()


def test_saying_done_works_without_the_slash():
    from estate.repository import SubmissionRepository as Subs

    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        Subs(session).close_all("111")
        item = pipeline.start_item(session, owner="telegram:111", prefix="DK")
        Subs(session).start("111", item.item_id)
    finally:
        session.close()

    update = _fake_update("111")
    update.message.text = "done"
    with pytest.raises(ApplicationHandlerStop):
        _run(handlers.on_answer_text(update, MagicMock()))

    replies = [c[0][0] for c in update.message.reply_text.call_args_list]
    # No photos on this item, so the done path should say so -- proving the
    # plain word reached cmd_done rather than being stored as a description.
    assert any("photo" in r.lower() for r in replies), replies


def test_a_real_description_containing_done_is_not_mistaken_for_a_command():
    """The dangerous false positive: swallowing a description as a command."""
    from estate.repository import ItemRepository
    from estate.repository import SubmissionRepository as Subs

    handlers = EstateHandlers(_config.get_settings())
    session = get_session()
    try:
        Subs(session).close_all("111")
        item = pipeline.start_item(session, owner="telegram:111", prefix="DK")
        Subs(session).start("111", item.item_id)
        item_id = item.item_id
    finally:
        session.close()

    update = _fake_update("111")
    update.message.text = ("It's an oak dresser from the spare room, I'm done with it "
                           "and it's in good shape apart from a scratch on top.")
    with pytest.raises(ApplicationHandlerStop):
        _run(handlers.on_answer_text(update, MagicMock()))

    session = get_session()
    try:
        sub = Subs(session).open_for_user("111")
        assert sub is not None, "the submission was finished by a description"
        assert "oak dresser" in (sub.description_hint or "").lower()
        assert ItemRepository(session).get(item_id).status == "Draft"
    finally:
        session.close()


def test_plain_command_matcher_is_conservative():
    from estate.telegram_estate import _plain_command

    assert _plain_command("done") == "done"
    assert _plain_command("Done!") == "done"
    assert _plain_command("  THAT'S IT ") == "done"
    assert _plain_command("start over") == "cancel"
    assert _plain_command("my items") == "myitems"
    assert _plain_command("help") == "help"
    # Prose is never a command, however suggestive.
    assert _plain_command("I am done with the garage and want to start over") == ""
    assert _plain_command("this is a done deal, a lovely finished oak table") == ""
    assert _plain_command("") == ""


def test_an_estate_only_submitter_gets_a_guide_from_start_not_silence():
    """Without this, someone who is only a submitter sends /start and the
    general handler returns nothing at all -- a dead bot, as far as he knows."""
    handlers = EstateHandlers(_config.get_settings())
    update = _fake_update("111")  # submitter, not on TELEGRAM_ALLOWED_USER_IDS
    with pytest.raises(ApplicationHandlerStop):
        _run(handlers.cmd_start(update, MagicMock()))
    text = update.message.reply_text.call_args[0][0]
    assert "photos" in text.lower()
    # Nothing technical leaks into the greeting.
    for jargon in ("/newitem", "provider", "confidence", "worksheet", "SKU", "comparable"):
        assert jargon not in text


def test_a_full_drake_user_still_gets_the_normal_start(monkeypatch):
    handlers = EstateHandlers(_config.get_settings())
    monkeypatch.setattr(handlers.settings, "telegram_allowed_user_ids", "111")
    update = _fake_update("111")
    # Must defer silently so the general D.R.A.K.E. /start still runs.
    _run(handlers.cmd_start(update, MagicMock()))
    update.message.reply_text.assert_not_called()


def test_an_unenrolled_person_is_told_their_own_id_not_just_refused():
    """Enrolment is a chicken and egg: he cannot be allow-listed until Drake
    knows his ID, and he cannot find his ID without being told. A bare refusal
    strands anyone setting this up without the operator present."""
    handlers = EstateHandlers(_config.get_settings())
    update = _fake_update("777000777")  # not on any allowlist

    _run(handlers._deny(update))

    text = update.message.reply_text.call_args[0][0]
    assert "777000777" in text, "he was refused without being told what to send on"
    assert "lost" in text.lower(), "no reassurance that his photos survived"
    # Still refuses: being shown your own ID grants nothing.
    assert not handlers._may_submit("777000777")
