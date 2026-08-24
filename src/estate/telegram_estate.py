"""Telegram intake for the estate sale system.

Registered as an *additional* handler set on the existing D.R.A.K.E.
Application. It does not replace or modify any existing handler:

- Photo messages had no handler before, so ``filters.PHOTO`` is free.
- Text is intercepted in handler group -1 only while a submission is actively
  waiting for an answer, then ``ApplicationHandlerStop`` prevents fall-through.
  At every other moment text behaves exactly as it did before.

Conversation design
-------------------
The submitter is not a technical user. There are four things to know:

    (send photos)          starts an item automatically
    (send a sentence)      context, so fewer questions come back
    "done"                 finished with this item
    "start over"           scrap this one

Slash commands (/newitem, /done, /cancel) all still work, but nobody has
to know they exist -- see PLAIN_DONE and _plain_command. The submitter is
someone's dad clearing a house, not an operator reading a manual.

Everything else — IDs, providers, confidence, worksheets — stays invisible.
State lives in the database, not in memory, so a restart mid-submission loses
nothing.
"""

from __future__ import annotations

import asyncio
import time

from telegram import Update
from telegram.ext import (
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from estate._compat import get_settings
from estate import pipeline
from estate.repository import ItemRepository, SubmissionRepository
from estate.schema import ItemStatus
from estate._compat import get_logger
from estate._compat import get_session

logger = get_logger(__name__)

DOWNLOAD_RETRIES = 3
DOWNLOAD_BACKOFF_SECONDS = 1.5
MAX_PHOTO_BYTES = 20 * 1024 * 1024

STATE_PHOTOS = "collecting_photos"
STATE_ANSWERING = "answering"
STATE_DONE = "complete"

WELCOME = (
    "Let's add an item.\n\n"
    f"Take {pipeline.MIN_PHOTOS}-{pipeline.IDEAL_PHOTOS} photos of it and send them to me:\n"
    "  - the whole thing from the front\n"
    "  - the back and both sides\n"
    "  - any brand or model label\n"
    "  - anything broken, chipped, or worn\n"
    "  - anything that comes with it\n\n"
    "Then send a sentence or two about it -- what you think it is, where it "
    "came from, its condition, and whether you're able to ship it if a buyer "
    "isn't local. Anything you tell me here is one less question I'll ask "
    "you later.\n\n"
    "When you're finished with this item, just say: done"
)

#: Shown when photos arrive with no item open. The submitter did the natural
#: thing -- took photos and sent them -- so the system starts the item for
#: them rather than telling them they used it wrong.
AUTO_STARTED = (
    "Got it, I've started a new item for these photos.\n\n"
    "Send as many angles as you like -- especially any brand or model label, "
    "and anything chipped or worn. Then tell me in a sentence what it is.\n\n"
    "When you're finished with this item, just say: done"
)

#: The whole system, for someone who will never read documentation. Sent on
#: /start and /help to a submitter who is not a general D.R.A.K.E. user.
DAD_GUIDE = (
    "Hello! I help sell things.\n\n"
    "Here's all there is to it:\n\n"
    "1. Take a few photos of ONE item and send them to me.\n"
    "2. Type a sentence about it -- what it is, what shape it's in, and "
    "whether you could post it to a buyer who isn't local.\n"
    "3. Say: done\n\n"
    "That's it. I'll work out what it is, what it's worth, where to sell it, "
    "and write the advert. Drake checks everything before anything goes on "
    "sale, so you can't break anything.\n\n"
    "Then just start the next item by sending its photos.\n\n"
    "Other things you can say:\n"
    "  my items  -- see everything you've sent me\n"
    "  start over  -- scrap the item you're in the middle of\n"
    "  help  -- show this again"
)

#: Plain words a non-technical submitter will actually type, mapped to the
#: command they meant. Matched only against a SHORT whole message (see
#: _plain_command) so a description like "I'm done clearing out the garage"
#: is still treated as the description it is.
PLAIN_DONE = {
    "done", "done!", "im done", "i'm done", "that's it", "thats it", "finished",
    "all done", "that's all", "thats all", "ok done", "okay done", "next",
    "complete", "finish", "send it", "go", "that is it",
}
PLAIN_CANCEL = {
    "start over", "cancel", "scrap it", "scrap this", "delete this",
    "forget it", "restart", "start again", "never mind", "nevermind",
}
PLAIN_MYITEMS = {"my items", "myitems", "my stuff", "what have i sent",
                 "list", "my list"}
PLAIN_HELP = {"help", "what do i do", "how does this work", "?", "how do i do this"}

#: Longest a message may be to count as a plain command rather than a
#: description. "done" is a command; a forty-word paragraph that happens to
#: contain the word "done" is not.
PLAIN_COMMAND_MAX_CHARS = 24


def _plain_command(text: str) -> str:
    """Map a short plain-English message to a command name, or ''.

    Deliberately conservative. A submitter who types a real description must
    never have it swallowed as a command, so anything longer than a few words
    is treated as prose no matter what it contains.
    """
    cleaned = " ".join((text or "").strip().lower().split())
    cleaned = cleaned.rstrip(".!")
    if not cleaned or len(cleaned) > PLAIN_COMMAND_MAX_CHARS:
        return ""
    if cleaned in PLAIN_DONE:
        return "done"
    if cleaned in PLAIN_CANCEL:
        return "cancel"
    if cleaned in PLAIN_MYITEMS:
        return "myitems"
    if cleaned in PLAIN_HELP:
        return "help"
    return ""


def _seconds() -> float:
    return time.monotonic()


class EstateHandlers:
    """Attach estate intake to an existing telegram.ext Application."""

    def __init__(self, settings=None):
        self.settings = settings or get_settings()
        # Throttles the per-photo acknowledgement so an 8-photo album does not
        # produce 8 replies. Keyed by telegram user id.
        self._last_ack: dict = {}

    # -- registration -------------------------------------------------------

    def register(self, app) -> None:
        app.add_handler(CommandHandler("newitem", self.cmd_newitem))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("myitems", self.cmd_myitems))
        app.add_handler(CommandHandler("estate", self.cmd_estate_status))
        app.add_handler(MessageHandler(filters.PHOTO, self.on_photo))
        # Group -1 runs before the default handlers (group 0), where the
        # general D.R.A.K.E. /done (channels/telegram.py) is registered.
        # PTB runs at most one handler per group per update -- within a
        # single group, the FIRST added handler that matches wins and the
        # rest in that group are never even reached. /done is registered
        # here in an earlier group specifically so it gets first look; each
        # handler below only raises ApplicationHandlerStop when it actually
        # consumed the update, so an unmatched case (e.g. /done with no
        # estate submission open) falls through to group 0 unchanged and the
        # original D.R.A.K.E. /done still works exactly as before this
        # module existed.
        app.add_handler(CommandHandler("done", self.cmd_done), group=-1)
        # /start and /help exist in group 0 but silently return for anyone
        # outside the general D.R.A.K.E. allowlist -- so a submitter who is
        # only an estate user would send /start and get NOTHING back, which
        # is the worst possible first impression. These take over for exactly
        # that person and defer for everyone else.
        app.add_handler(CommandHandler("start", self.cmd_start), group=-1)
        app.add_handler(CommandHandler("help", self.cmd_help), group=-1)
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_answer_text), group=-1
        )
        logger.info({"action": "estate_handlers_registered"})

    # -- authorisation ------------------------------------------------------

    def _may_submit(self, user_id: str) -> bool:
        allowed = self.settings.estate_submitters()
        return (user_id in allowed) if allowed else False

    async def _deny(self, update: Update) -> None:
        """Refuse, but hand the person the one thing that unblocks them.

        Enrolment is a chicken and egg: the operator cannot allow-list someone
        until they know that person's numeric Telegram ID, and the person
        cannot discover their own ID without being told where to look. The
        old message was a dead end -- someone setting this up alone, without
        the operator sitting next to them, simply stopped there.

        Showing a person their OWN user ID grants nothing (it is not a
        secret, and @userinfobot will tell anyone the same number), but it
        turns the dead end into a step they can complete on their own.
        """
        uid = str(update.effective_user.id)
        logger.info({"action": "estate_unauthorised", "user_id": uid})
        await update.message.reply_text(
            "Almost there — this account just needs to be switched on first.\n\n"
            "Send this number to the person who set this up:\n\n"
            f"    {uid}\n\n"
            "Once they've added it, come back and send your photos. "
            "Nothing you've sent so far was lost."
        )

    def _is_estate_only_user(self, user_id: str) -> bool:
        """True for someone who may submit items but is not a D.R.A.K.E. user.

        That is the person the estate greeting is written for. Drake himself
        is on both lists and must keep getting the normal assistant greeting,
        so this deliberately returns False for him.
        """
        if not self._may_submit(user_id):
            return False
        return user_id not in self.settings.allowed_user_ids()

    # -- commands -----------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Greet an estate-only submitter; defer for everyone else."""
        uid = str(update.effective_user.id)
        if not self._is_estate_only_user(uid):
            return  # fall through to the general D.R.A.K.E. /start
        await update.message.reply_text(DAD_GUIDE)
        raise ApplicationHandlerStop

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._is_estate_only_user(uid):
            return  # fall through to the general D.R.A.K.E. /help
        await update.message.reply_text(DAD_GUIDE)
        raise ApplicationHandlerStop

    async def cmd_newitem(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return await self._deny(update)

        loop = asyncio.get_event_loop()
        try:
            item_id = await loop.run_in_executor(None, self._start_submission, uid)
        except Exception as exc:
            logger.error({"action": "estate_newitem_failed", "error_type": type(exc).__name__})
            return await update.message.reply_text(
                "Something went wrong starting a new item. Please try /newitem again."
            )
        await update.message.reply_text(WELCOME)
        logger.info({"action": "estate_submission_started", "item_id": item_id, "user_id": uid})

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return await self._deny(update)
        loop = asyncio.get_event_loop()
        closed = await loop.run_in_executor(None, self._cancel_submission, uid)
        if closed:
            await update.message.reply_text(
                "Scrapped that one — nothing was listed. Send photos whenever you're "
                "ready for the next item."
            )
        else:
            await update.message.reply_text(
                "There was nothing in progress. Just send photos when you're ready."
            )

    async def cmd_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /done for an active estate submission, otherwise defer.

        Registered ahead of the general D.R.A.K.E. /done (see register()).
        The routing rule is deliberately based on submission state, not on
        estate authorisation: if this Telegram user has no open estate
        submission, this /done is not ours, so we return without replying or
        raising ApplicationHandlerStop and PTB falls through to group 0's
        original handler exactly as if the estate system were not installed.
        Every branch that DOES apply to an open submission raises
        ApplicationHandlerStop after replying, so the general /done never
        also fires for the same update.
        """
        uid = str(update.effective_user.id)
        loop = asyncio.get_event_loop()
        state = await loop.run_in_executor(None, self._peek_submission, uid)
        if state is None:
            return  # not ours -- let the general D.R.A.K.E. /done handle it

        if not self._may_submit(uid):
            # An open submission exists but the user is no longer allow-listed.
            # This IS an estate /done, so it is denied explicitly rather than
            # silently handed to a general-purpose handler that knows nothing
            # about the submission.
            await self._deny(update)
            raise ApplicationHandlerStop

        item_id, sub_state, count = state

        if sub_state == STATE_ANSWERING:
            await update.message.reply_text(
                "Almost there — just answer the last question, or type: skip"
            )
            raise ApplicationHandlerStop
        if count == 0:
            await update.message.reply_text(
                "I haven't received any photos yet. Send me a few photos of the item first."
            )
            raise ApplicationHandlerStop
        if count < pipeline.MIN_PHOTOS:
            await update.message.reply_text(
                "Got %d photo%s. A couple more would help me identify it, but I'll "
                "work with this." % (count, "" if count == 1 else "s")
            )

        await update.message.reply_text("Thanks. Looking at the photos now...")
        try:
            first_q = await loop.run_in_executor(None, self._identify, uid, item_id)
        except Exception as exc:
            logger.error({"action": "estate_identify_failed", "item_id": item_id,
                          "error_type": type(exc).__name__})
            await update.message.reply_text(
                "I couldn't analyse the photos just now, but they are safely saved. "
                "I'll flag this item for a person to look at."
            )
            raise ApplicationHandlerStop

        if first_q:
            await update.message.reply_text(first_q)
        else:
            await self._finish(update, uid, item_id)
        raise ApplicationHandlerStop

    async def cmd_myitems(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return await self._deny(update)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._my_items, uid)
        await update.message.reply_text(text)

    async def cmd_estate_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return await self._deny(update)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._status_text)
        await update.message.reply_text(text)

    # -- photo intake -------------------------------------------------------

    async def on_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return await self._deny(update)

        loop = asyncio.get_event_loop()
        state = await loop.run_in_executor(None, self._peek_submission, uid)
        if state is None:
            # Photos with nothing open. Somebody who has been told "take
            # photos of the thing and send them" will do exactly that and
            # will not have typed /newitem first -- so start the item for
            # them instead of rejecting a perfectly good photo. This is the
            # single most likely way a non-technical submitter uses the bot.
            try:
                item_id = await loop.run_in_executor(None, self._start_submission, uid)
            except Exception as exc:
                logger.error({"action": "estate_autostart_failed",
                              "error_type": type(exc).__name__})
                return await update.message.reply_text(
                    "I couldn't start a new item just now. Please send that photo again."
                )
            logger.info({"action": "estate_submission_autostarted",
                         "item_id": item_id, "user_id": uid})
            await update.message.reply_text(AUTO_STARTED)
            sub_state = STATE_PHOTOS
        else:
            item_id, sub_state, _count = state

        if sub_state == STATE_ANSWERING:
            return await update.message.reply_text(
                "I already have the photos for this one. Answer the question above, "
                "or say: skip"
            )
        if sub_state != STATE_PHOTOS:
            return await update.message.reply_text(
                "I already have the photos for this one. Say: done"
            )

        # Telegram sends several resolutions; the last entry is the largest.
        photo = update.message.photo[-1]
        if photo.file_size and photo.file_size > MAX_PHOTO_BYTES:
            return await update.message.reply_text(
                "That photo is too large for me to save. Try sending it again at "
                "normal quality."
            )

        data = await self._download_with_retry(context, photo.file_id)
        if data is None:
            return await update.message.reply_text(
                "That photo didn't come through. Please send it again."
            )

        media_group = update.message.media_group_id or ""
        caption = (update.message.caption or "")[:200]
        try:
            note, total = await loop.run_in_executor(
                None, self._store_photo, uid, item_id, data, photo.file_id, media_group, caption
            )
        except Exception as exc:
            logger.error({"action": "estate_photo_store_failed", "item_id": item_id,
                          "error_type": type(exc).__name__})
            return await update.message.reply_text(
                "I couldn't save that photo. Please send it again."
            )

        if note == "duplicate":
            return

        # One acknowledgement per album, or at most one every few seconds.
        key = media_group or uid
        now = _seconds()
        last = self._last_ack.get(key, 0.0)
        if now - last >= 2.5:
            self._last_ack[key] = now
            if total >= pipeline.MIN_PHOTOS:
                await update.message.reply_text(
                    "Got %d photos. Send more if you like, or say: done" % total
                )
            else:
                await update.message.reply_text(
                    "Got %d. Keep going — %d or more is ideal."
                    % (total, pipeline.MIN_PHOTOS)
                )

    async def _download_with_retry(self, context, file_id: str):
        """Telegram file fetches fail transiently; retry with backoff."""
        for attempt in range(1, DOWNLOAD_RETRIES + 1):
            try:
                tg_file = await context.bot.get_file(file_id)
                buf = await tg_file.download_as_bytearray()
                return bytes(buf)
            except Exception as exc:
                logger.error({"action": "estate_photo_download_retry", "attempt": attempt,
                              "error_type": type(exc).__name__})
                if attempt == DOWNLOAD_RETRIES:
                    return None
                await asyncio.sleep(DOWNLOAD_BACKOFF_SECONDS * attempt)
        return None

    # -- answer capture -----------------------------------------------------

    async def on_answer_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Consume plain text only while an item is waiting for an answer."""
        if not update.message or not update.effective_user:
            return
        uid = str(update.effective_user.id)
        if not self._may_submit(uid):
            return  # fall through to the normal D.R.A.K.E. text handler

        loop = asyncio.get_event_loop()
        state = await loop.run_in_executor(None, self._peek_submission, uid)
        if state is None:
            return
        item_id, sub_state, _ = state

        text = update.message.text or ""

        # A submitter who was shown "just say: done" will say exactly that,
        # not "/done". Short plain-English equivalents are honoured as the
        # commands they obviously are, at any point in the submission.
        plain = _plain_command(text)
        if plain == "done":
            await self.cmd_done(update, context)
            raise ApplicationHandlerStop
        if plain == "cancel":
            await self.cmd_cancel(update, context)
            raise ApplicationHandlerStop
        if plain == "myitems":
            await self.cmd_myitems(update, context)
            raise ApplicationHandlerStop
        if plain == "help":
            await update.message.reply_text(DAD_GUIDE)
            raise ApplicationHandlerStop

        if sub_state == STATE_PHOTOS:
            # This is the owner's one-or-two sentence description, sent
            # sometime between the photos and being finished. It is never a
            # general chat message while an item is actively being
            # photographed, so it is captured here and never allowed to fall
            # through to the normal D.R.A.K.E. handler.
            if text.strip():
                await loop.run_in_executor(None, self._store_hint, uid, item_id, text)
                await update.message.reply_text(
                    "Noted, thank you. Send more photos if you have them, "
                    "or say: done"
                )
            raise ApplicationHandlerStop

        if sub_state != STATE_ANSWERING:
            return

        answer = text
        try:
            reply, done = await loop.run_in_executor(
                None, self._apply_answer, uid, item_id, answer
            )
        except Exception as exc:
            logger.error({"action": "estate_answer_failed", "item_id": item_id,
                          "error_type": type(exc).__name__})
            await update.message.reply_text(
                "I didn't manage to save that. Please type it once more."
            )
            raise ApplicationHandlerStop

        if done:
            await self._finish(update, uid, item_id)
        elif reply:
            await update.message.reply_text(reply)
        raise ApplicationHandlerStop

    async def _finish(self, update: Update, uid: str, item_id: str) -> None:
        loop = asyncio.get_event_loop()
        try:
            summary = await loop.run_in_executor(None, self._finalise, uid, item_id)
        except Exception as exc:
            logger.error({"action": "estate_finalise_failed", "item_id": item_id,
                          "error_type": type(exc).__name__})
            return await update.message.reply_text(
                "Your photos and answers are saved. I hit a snag finishing up, so "
                "a person will take it from here."
            )
        await update.message.reply_text(summary)

    # -----------------------------------------------------------------------
    # Synchronous workers (run in an executor; SQLAlchemy is not async here)
    # -----------------------------------------------------------------------

    def _start_submission(self, uid: str) -> str:
        session = get_session()
        try:
            item = pipeline.start_item(
                session,
                owner=f"telegram:{uid}",
                prefix=self.settings.estate_id_prefix,
                move_out_deadline=self.settings.estate_move_out_date,
            )
            SubmissionRepository(session).start(uid, item.item_id)
            return item.item_id
        finally:
            session.close()

    def _cancel_submission(self, uid: str) -> bool:
        session = get_session()
        try:
            subs = SubmissionRepository(session)
            sub = subs.open_for_user(uid)
            if sub is None:
                return False
            if sub.item_id:
                repo = ItemRepository(session)
                item = repo.get(sub.item_id)
                if item is not None and item.status == ItemStatus.DRAFT.value:
                    repo.set_status(sub.item_id, ItemStatus.REMOVED.value,
                                    actor=f"telegram:{uid}", reason="submitter cancelled")
            subs.close_all(uid)
            return True
        finally:
            session.close()

    def _peek_submission(self, uid: str):
        """Returns (item_id, state, photo_count) or None. Also recovers after
        a restart: an open submission is always resumable."""
        session = get_session()
        try:
            sub = SubmissionRepository(session).open_for_user(uid)
            if sub is None:
                return None
            return sub.item_id, sub.state, pipeline.photo_count(session, sub.item_id)
        finally:
            session.close()

    def _store_photo(self, uid: str, item_id: str, data: bytes, file_id: str,
                     media_group: str, caption: str):
        session = get_session()
        try:
            _photo, note = pipeline.attach_photo(
                session, item_id, data, ext="jpg", telegram_file_id=file_id,
                media_group_id=media_group, caption=caption,
            )
            total = pipeline.photo_count(session, item_id)
            sub = SubmissionRepository(session).open_for_user(uid)
            if sub is not None:
                sub.photo_count = total
                sub.last_media_group_id = media_group
                SubmissionRepository(session).save(sub)
            return note, total
        finally:
            session.close()

    def _store_hint(self, uid: str, item_id: str, text: str) -> None:
        """Accumulate free text sent while photos are still being collected.

        Dad might send the description as one message or split across a
        couple -- either way it is appended, not replaced, so nothing he
        types gets lost.
        """
        session = get_session()
        try:
            subs = SubmissionRepository(session)
            sub = subs.open_for_user(uid)
            if sub is None:
                return
            existing = (sub.description_hint or "").strip()
            piece = text.strip()[:500]
            sub.description_hint = (existing + " " + piece).strip() if existing else piece
            subs.save(sub)
        finally:
            session.close()

    def _identify(self, uid: str, item_id: str):
        """Run vision, then return the first question (or None if complete)."""
        session = get_session()
        try:
            subs = SubmissionRepository(session)
            sub = subs.open_for_user(uid)
            hint = (sub.description_hint or "") if sub is not None else ""
            _ident, result = pipeline.identify_item(session, item_id, hint=hint)
            sub = subs.open_for_user(uid)
            if not result.ok:
                if sub is not None:
                    sub.state = STATE_ANSWERING
                    sub.pending_questions = []
                    subs.save(sub)
                return None
            key, question = pipeline.next_question(session, item_id)
            if sub is not None:
                sub.state = STATE_ANSWERING if key else STATE_DONE
                sub.pending_questions = list(result.questions)
                subs.save(sub)
            if not key:
                return None
            lead = f"I think this is: {result.message}\n\n" if result.message else ""
            return lead + question
        finally:
            session.close()

    def _apply_answer(self, uid: str, item_id: str, answer: str):
        session = get_session()
        try:
            key, _q = pipeline.next_question(session, item_id)
            if key is None:
                return "", True
            res = pipeline.apply_answer(session, item_id, key, answer)
            if not res.ok:
                return res.message, False
            next_key, next_q = pipeline.next_question(session, item_id)
            subs = SubmissionRepository(session)
            sub = subs.open_for_user(uid)
            if sub is not None:
                answers = dict(sub.answers or {})
                answers[key] = answer[:300]
                sub.answers = answers
                sub.state = STATE_ANSWERING if next_key else STATE_DONE
                subs.save(sub)
            if next_key:
                return next_q, False
            return "", True
        finally:
            session.close()

    def _finalise(self, uid: str, item_id: str) -> str:
        """Run the full item-processing job, not just the intake tail.

        This is the point where "photos and a sentence go in" becomes "a
        researched, priced, prioritised, marketplace-ready record comes out":
        the orchestrator seeds logistics, runs research, prices from the
        evidence, generates every listing package, records the outstanding
        blockers, and scores the item against the rest of the inventory --
        all before the submitter's confirmation message is written.
        """
        from estate import orchestrator

        session = get_session()
        try:
            job = orchestrator.process_item(
                session, item_id,
                move_out_deadline=self.settings.estate_move_out_date,
                catalog_url=self.settings.estate_catalog_url,
            )
            SubmissionRepository(session).close_all(uid)
            summary = pipeline.review_summary(session, item_id)
            if not job.ok:
                # The photos, the sentence, and every field earned so far are
                # already saved. Say so plainly rather than implying loss.
                summary += "\n\n" + job.message
            if self.settings.estate_catalog_url:
                summary += f"\nReview page: {self.settings.estate_catalog_url}"
            return summary
        finally:
            session.close()

    def _my_items(self, uid: str) -> str:
        """List the submitter's items, including where automated processing
        currently stands for anything not yet in front of a human reviewer.

        identify_item() and the research/pricing steps it triggers can run
        well after the Telegram reply that started them, so this is the one
        place the submitter can check on work still in progress after a
        restart or a slow research pass -- it reads processing_stage, which
        persists in the database rather than in memory.
        """
        from estate.schema import (
            FIELD_QUESTIONS,
            PROCESSING_STAGE_MESSAGES,
        )

        session = get_session()
        try:
            owner = f"telegram:{uid}"
            items = [i for i in ItemRepository(session).all()
                     if i.submission_owner == owner]
            if not items:
                return "You haven't added any items yet. Send me photos of something to start."
            # Highest priority first: the list is only useful if the thing at
            # the top is the thing worth doing next.
            items.sort(key=lambda i: float(i.priority_score or 0), reverse=True)
            lines = ["Your items (%d), most worth your time first:" % len(items), ""]
            for i in items[:15]:
                price = (f"${i.current_price:.0f}") if i.current_price else "price pending"
                # processing_stage is now the primary signal at every point in
                # the item's life, not just while it is still a Draft: it is
                # the one column that tells the submitter what is happening
                # and who is holding the item right now.
                stage_msg = PROCESSING_STAGE_MESSAGES.get(
                    i.processing_stage or "", i.processing_stage or i.status
                )
                line = "{} - {} - {} - {}".format(
                    i.item_id, i.item_name or "unnamed", stage_msg, price
                )
                if i.processing_stage == "Needs Information":
                    outstanding = [
                        FIELD_QUESTIONS.get(k, k) for k in (i.missing_fields or [])
                    ][:1]
                    if outstanding:
                        line += "\n      -> " + outstanding[0]
                lines.append(line)
            return "\n".join(lines)
        finally:
            session.close()

    def _status_text(self) -> str:
        session = get_session()
        try:
            items = ItemRepository(session).all()
            counts: dict = {}
            for i in items:
                counts[i.status] = counts.get(i.status, 0) + 1
            sold = sum(float(i.final_sale_price or 0) for i in items if i.status == "Sold")
            lines = ["Estate sale status", "", "Items: %d" % len(items)]
            for status in sorted(counts):
                lines.append("  %s: %d" % (status, counts[status]))
            lines.append("")
            lines.append(f"Sold to date: ${sold:.0f}")
            if self.settings.estate_move_out_date:
                lines.append(f"Move-out: {self.settings.estate_move_out_date}")
            return "\n".join(lines)
        finally:
            session.close()
