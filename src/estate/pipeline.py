"""Photo -> draft item pipeline.

Transport-independent on purpose: the Telegram adapter, the CLI, and the tests
all drive the same functions. Nothing in here knows what Telegram is.

Flow
----
start_item()      allocate ID, create directories, create Draft row
attach_photo()    persist bytes, dedupe by hash, record the row
identify_item()   run the vision provider, write confident fields to the draft
next_question()   the single most useful thing to ask a human right now
apply_answer()    record an answer and advance
finalise_draft()  seed logistics defaults, generate the comps worksheet, and
                  move the item to Needs Review
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from estate import paths, research
from estate.hint_parser import parse_hint
from estate.repository import CompRepository, ItemRepository, PhotoRepository
from estate.research_provider import get_research_provider
from estate.schema import (
    BOOLEAN_ASKABLE_FIELDS,
    FIELD_QUESTIONS,
    NO_WORDS,
    NONE_WORDS,
    SKIP_WORDS,
    YES_WORDS,
    ItemStatus,
)
from estate.vision import get_vision_provider
from estate._compat import get_logger

logger = get_logger(__name__)

MIN_PHOTOS = 3
IDEAL_PHOTOS = 8

#: Seed values by category for logistics fields a photo cannot answer.
#: (weight_lbs, vehicle, people, pickup_difficulty, shipping_feasible)
#: These are starting points a reviewer confirms — never presented as measured.
CATEGORY_LOGISTICS = {
    "Furniture": (60.0, "SUV or truck", 2, "Moderate", False),
    "Appliances": (120.0, "Truck or van", 2, "Hard", False),
    "Electronics": (12.0, "Car", 1, "Easy", True),
    "Audio / Music Gear": (20.0, "Car", 1, "Easy", True),
    "Tools & Equipment": (25.0, "Car", 1, "Easy", True),
    "Outdoor & Garden": (45.0, "SUV or truck", 2, "Moderate", False),
    "Kitchen & Dining": (8.0, "Car", 1, "Easy", True),
    "Home Decor": (10.0, "Car", 1, "Easy", True),
    "Art & Collectibles": (10.0, "Car", 1, "Easy", True),
    "Books & Media": (5.0, "Car", 1, "Easy", True),
    "Clothing & Accessories": (3.0, "Car", 1, "Easy", True),
    "Jewelry & Watches": (1.0, "Car", 1, "Easy", True),
    "Sporting Goods": (20.0, "Car", 1, "Easy", True),
    "Toys & Games": (8.0, "Car", 1, "Easy", True),
    "Office & Storage": (30.0, "SUV or truck", 2, "Moderate", False),
    "Vehicles & Trailers": (500.0, "Tow vehicle", 2, "Specialist Movers", False),
    "Other": (15.0, "Car", 1, "Easy", True),
}


@dataclass
class IntakeResult:
    item_id: str = ""
    ok: bool = True
    message: str = ""
    questions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Start
# ---------------------------------------------------------------------------

def start_item(session, owner: str, prefix: str = "DK", move_out_deadline: str = ""):
    """Allocate an ID, create the directory tree, and open a Draft row."""
    repo = ItemRepository(session)
    item = repo.create(
        owner=owner,
        prefix=prefix,
        status=ItemStatus.DRAFT.value,
        move_out_deadline=move_out_deadline or "",
    )
    paths.item_dir(item.item_id, create=True)
    return item


# ---------------------------------------------------------------------------
# 2. Photos
# ---------------------------------------------------------------------------

def attach_photo(session, item_id: str, data: bytes, ext: str = "jpg",
                 telegram_file_id: str = "", media_group_id: str = "",
                 slot: str = "photo", caption: str = "") -> tuple:
    """Persist one photo. Returns (photo_or_None, note).

    Duplicate bytes are silently ignored — Telegram re-sends the same file when
    a user forwards an album twice, and a duplicate is never useful evidence.
    """
    photos = PhotoRepository(session)
    digest = hashlib.sha256(data).hexdigest()
    if photos.exists_hash(item_id, digest):
        return None, "duplicate"

    index = photos.count(item_id) + 1
    target = paths.photo_path(item_id, index, slot=slot, ext=ext, role="original")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    photo = photos.add(
        item_id,
        role="original",
        filename=target.name,
        local_path=str(target),
        telegram_file_id=telegram_file_id,
        media_group_id=media_group_id,
        sha256=digest,
        caption=caption,
    )

    # Keep photo_links on the item in sync so the spreadsheet export is correct.
    item = ItemRepository(session).get(item_id)
    if item is not None:
        links = list(item.photo_links or [])
        rel = paths.relative_photo_url(target)
        if rel not in links:
            links.append(rel)
            item.photo_links = links
            session.commit()
    return photo, "stored"


def photo_count(session, item_id: str) -> int:
    return PhotoRepository(session).count(item_id)


# ---------------------------------------------------------------------------
# 3. Identification
# ---------------------------------------------------------------------------

def identify_item(session, item_id: str, hint: str = "", provider_name: str = ""):
    """Run the vision provider and write only confident fields to the draft.

    ``hint`` is the owner's own one-or-two sentence description, passed
    straight to the vision model as context and also, separately, run through
    ``hint_parser`` by the caller to pre-answer follow-up questions. Vision
    failure never loses the submission: the photos stay saved, the item is
    flagged for manual identification, and processing_stage becomes Error.
    """
    from estate.schema import ProcessingStage, ReviewStatus

    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return None, IntakeResult(item_id=item_id, ok=False, message="Item not found.")

    photo_paths = [Path(p.local_path) for p in PhotoRepository(session).for_item(item_id)
                   if p.local_path and Path(p.local_path).exists()]
    result = IntakeResult(item_id=item_id)
    if not photo_paths:
        result.ok = False
        result.message = "No photos stored for this item yet."
        return None, result

    repo.update(item_id, actor="system", processing_stage=ProcessingStage.IDENTIFYING.value)

    provider = get_vision_provider(provider_name)
    try:
        ident = provider.identify(photo_paths, hint=hint)
    except Exception as exc:
        logger.error({"action": "vision_identify_failed", "item_id": item_id,
                      "error_type": type(exc).__name__})
        result.ok = False
        result.message = ("Could not analyse the photos right now. They are saved, "
                          "so nothing is lost — this item is waiting for review.")
        repo.update(item_id, actor="system",
                    review_status=ReviewStatus.NEEDS_MANUAL_IDENTIFICATION.value,
                    processing_stage=ProcessingStage.ERROR.value)
        repo.events.record(item_id, "vision_identify_failed", actor="system",
                           error_type=type(exc).__name__, hint_given=bool(hint))
        return None, result

    fields = ident.to_item_fields()
    repo.update(item_id, actor=f"vision:{ident.provider}", **fields)
    item = repo.get(item_id)
    item.vision_raw = {
        "provider": ident.provider,
        "model": ident.model_name,
        "processing_seconds": ident.processing_seconds,
        "cost_usd": ident.cost_usd,
        "fallback_used": ident.fallback_used or getattr(provider, "fallback_reason", ""),
        "identification": ident.identification_report(),
        "condition": ident.condition_report(),
        "dimensions": ident.dimensions_report(),
        "raw": ident.raw,
    }

    # A photo can never establish who owns an item or whether they are
    # willing to ship it -- these are always outstanding after vision runs,
    # regardless of confidence, unless the owner's own sentence already
    # settled them (handled just below), or -- for ownership only -- the same
    # person already confirmed it recently (see _inherit_recent_ownership).
    missing = list(ident.missing)
    for boolean_field in BOOLEAN_ASKABLE_FIELDS:
        if boolean_field not in missing:
            missing.append(boolean_field)
    item.missing_fields = missing
    session.commit()

    _inherit_recent_ownership(session, item_id)
    item = repo.get(item_id)

    repo.events.record(item_id, "vision_identified", actor=f"vision:{ident.provider}",
                       provider=ident.provider, confidence=ident.overall_confidence,
                       missing=missing, cost_usd=ident.cost_usd,
                       processing_seconds=ident.processing_seconds)

    # Resolve as much of the remaining missing list as the owner's own
    # sentence already answered, so Dad is never asked something he already
    # told us. Only ever fills a gap vision left open -- never overrides a
    # field vision was confident enough to set itself.
    if hint:
        hint_res = apply_hint_answers(session, item_id, hint)
        result.warnings.extend(hint_res.warnings)

    item = repo.get(item_id)
    result.questions = list(item.missing_fields or [])
    # NEEDS_INFORMATION is the human-visible /myitems signal that the item is
    # waiting on the submitter, not on the pipeline itself. finalise_draft()
    # advances this to RESEARCHING and then READY_FOR_REVIEW once questions
    # are answered and /done is sent again.
    if result.questions:
        repo.update(item_id, actor="system",
                    processing_stage=ProcessingStage.NEEDS_INFORMATION.value)
    result.message = ident.item_name or "Unidentified item"
    if ident.overall_confidence < 0.4:
        result.warnings.append("Low identification confidence — more photos would help.")
    if ident.condition_capped:
        result.warnings.append(
            "Condition graded conservatively (%s): %s" % (ident.condition, ident.condition_cap_reason)
        )
    if ident.suggested_photos:
        result.warnings.append("Helpful extra shots: " + "; ".join(ident.suggested_photos[:3]))
    return ident, result


def _inherit_recent_ownership(session, item_id: str) -> bool:
    """Carry a recent ownership confirmation forward to this item.

    Ownership is a hard publication gate and is never inferred from a
    photograph — that does not change. What changes is how often it is asked.
    Someone clearing a house answers "yes, this is mine to sell" for the first
    item and then answers it thirty-nine more times, which teaches them to tap
    through the question rather than read it. A question people have stopped
    reading is worse than no question, because it still looks like diligence.

    So: if the same submitter explicitly confirmed ownership within
    ``estate_ownership_confirm_hours``, this item inherits that and the
    question is not asked again. Set the setting to 0 to restore per-item
    asking.

    The inheritance is recorded as its own event naming the item it came
    from, and is deliberately NOT marked owner-confirmed — so review can tell
    "he said yes about this item" apart from "he said yes about something
    else twenty minutes ago". Every action stays inspectable.

    Returns True when a confirmation was inherited.
    """
    from datetime import datetime, timedelta

    from estate._compat import get_settings

    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None or item.ownership_approval:
        return False
    if "ownership_approval" not in (item.missing_fields or []):
        return False

    try:
        window_hours = int(get_settings().estate_ownership_confirm_hours)
    except Exception:  # noqa: BLE001 - configuration must never break intake
        window_hours = 0
    if window_hours <= 0:
        return False

    owner = (item.submission_owner or "").strip()
    if not owner:
        return False

    cutoff = datetime.now() - timedelta(hours=window_hours)
    source = None
    for other in repo.all():
        if other.item_id == item_id or not other.ownership_approval:
            continue
        if (other.submission_owner or "").strip() != owner:
            continue
        submitted = other.date_submitted
        if not isinstance(submitted, datetime) or submitted < cutoff:
            continue
        if source is None or submitted > source.date_submitted:
            source = other

    if source is None:
        return False

    repo.update(item_id, actor="ownership_inheritance", ownership_approval=True)
    fresh = repo.get(item_id)
    fresh.missing_fields = [
        k for k in (fresh.missing_fields or []) if k != "ownership_approval"
    ]
    session.commit()
    repo.events.record(
        item_id,
        "ownership_inherited",
        actor="ownership_inheritance",
        source_item_id=source.item_id,
        window_hours=window_hours,
    )
    return True


def apply_hint_answers(session, item_id: str, hint_text: str) -> IntakeResult:
    """Resolve fields from the owner's own sentence, deterministically.

    Deliberately conservative (see hint_parser's own docstring): a field is
    written only when the regex/keyword extraction is unambiguous. Anything
    the sentence does not clearly answer is left alone.

    **The owner's own words win over the model's guess.** This used to apply
    only to fields still in ``missing_fields`` — i.e. only where vision had
    already admitted low confidence. That was safe when almost everything was
    missing, but once the ask-set narrowed, hardly anything was, and someone
    typing "it's in good condition" watched the system record Unknown.

    A person describing an object they own is better evidence than a model
    looking at a photograph of it, so the extraction now applies regardless
    of confidence, and everything it writes is marked owner-confirmed so no
    later automated pass can quietly overwrite it.
    """
    repo = ItemRepository(session)
    item = repo.get(item_id)
    res = IntakeResult(item_id=item_id)
    if item is None:
        res.ok = False
        res.message = "Item not found."
        return res

    extraction = parse_hint(hint_text)
    answers = extraction.as_answers()
    missing = list(item.missing_fields or [])
    resolved = {k: v for k, v in answers.items() if v is not None and v != ""}
    if resolved:
        repo.update(item_id, actor="hint_parser", **resolved)
        # These came out of the owner's own words. Record them so a later
        # automated pass can never quietly replace them with a model guess.
        from estate.orchestrator import mark_owner_confirmed

        mark_owner_confirmed(session, item_id, *resolved.keys())
        remaining = [k for k in missing if k not in resolved]
        item = repo.get(item_id)
        item.missing_fields = remaining
        session.commit()
        repo.events.record(item_id, "hint_parsed", actor="hint_parser",
                           resolved=list(resolved), patterns=extraction.matched_patterns)
        res.warnings.append(
            "From your description, I already have: " + ", ".join(sorted(resolved))
        )
    res.questions = list(item.missing_fields or [])
    return res


# ---------------------------------------------------------------------------
# 4. Questions
# ---------------------------------------------------------------------------

def next_question(session, item_id: str) -> tuple:
    """Return (field_key, question_text) or (None, None) when done."""
    item = ItemRepository(session).get(item_id)
    if item is None:
        return None, None
    for key in list(item.missing_fields or []):
        if key in FIELD_QUESTIONS:
            return key, FIELD_QUESTIONS[key]
    return None, None


def apply_answer(session, item_id: str, field_key: str, answer: str) -> IntakeResult:
    """Record one answer. 'skip' leaves the field blank but stops asking."""
    repo = ItemRepository(session)
    item = repo.get(item_id)
    res = IntakeResult(item_id=item_id)
    if item is None:
        res.ok = False
        res.message = "Item not found."
        return res

    raw = (answer or "").strip()
    lowered = raw.lower()

    if field_key in BOOLEAN_ASKABLE_FIELDS and lowered in SKIP_WORDS:
        # Ownership and shipping both default to the safe/unconfirmed value
        # already (False), and a reviewer always confirms ownership by hand
        # before anything is approved regardless of what this field shows --
        # so "skip" is allowed rather than trapping the submitter in a
        # question they are unable or unwilling to answer right now.
        value = ""
    elif field_key in BOOLEAN_ASKABLE_FIELDS:
        if lowered in YES_WORDS:
            value = True
        elif lowered in NO_WORDS:
            value = False
        else:
            res.ok = False
            res.message = "Please answer yes or no, or say: skip"
            return res
    elif lowered in SKIP_WORDS:
        value = ""
    elif field_key in ("defects", "included_accessories") and lowered in NONE_WORDS:
        value = "None"
    elif field_key == "condition":
        from estate.schema import CONDITIONS
        match = ""
        for c in CONDITIONS:
            if lowered and (lowered in c.lower() or c.lower().startswith(lowered)):
                match = c
                break
        if not match:
            res.ok = False
            res.message = ("I didn't catch that. Please answer with one of: "
                           "like new, excellent, good, fair, or poor.")
            return res
        value = match
    else:
        value = raw

    # Booleans are meaningful answers even when False ("not shippable", "not
    # my item to sell") -- only skip the write for the empty-string sentinel
    # that "skip" produces on free-text fields.
    if value != "":
        repo.update(item_id, actor="submitter", **{field_key: value})
        # A direct answer from the submitter is the strongest fact we have
        # about this item. Protect it from every later automated write.
        from estate.orchestrator import mark_owner_confirmed

        mark_owner_confirmed(session, item_id, field_key)

    remaining = [k for k in (item.missing_fields or []) if k != field_key]
    item = repo.get(item_id)
    item.missing_fields = remaining
    session.commit()
    res.questions = remaining
    return res


# ---------------------------------------------------------------------------
# 5. Finalise
# ---------------------------------------------------------------------------

def finalise_draft(session, item_id: str, move_out_deadline: str = "",
                   catalog_url: str = "") -> IntakeResult:
    """Seed logistics, write the comps worksheet, and hand off for review."""
    from estate.schema import ProcessingStage

    repo = ItemRepository(session)
    item = repo.get(item_id)
    res = IntakeResult(item_id=item_id)
    if item is None:
        res.ok = False
        res.message = "Item not found."
        return res

    repo.update(item_id, actor="system", processing_stage=ProcessingStage.RESEARCHING.value)

    weight, vehicle, people, difficulty, shippable = CATEGORY_LOGISTICS.get(
        item.category or "Other", CATEGORY_LOGISTICS["Other"]
    )
    updates = {}
    if not item.weight_lbs:
        updates["weight_lbs"] = weight
    if not item.required_vehicle:
        updates["required_vehicle"] = vehicle
    if not item.people_required or item.people_required == 1:
        updates["people_required"] = people
    if item.pickup_difficulty in ("", "Easy"):
        updates["pickup_difficulty"] = difficulty
    # shipping_feasible is one of the two fields the intake flow always asks
    # about (see BOOLEAN_ASKABLE_FIELDS): the owner's own answer -- whether
    # resolved from the hint sentence, an explicit yes/no, or a deliberate
    # skip -- must never be silently overwritten by a category guess here.
    # The category default only applies in the edge case where finalise_draft
    # runs without that question ever having been reached at all.
    if "shipping_feasible" in (item.missing_fields or []):
        updates["shipping_feasible"] = bool(shippable)
    updates["pickup_required"] = not bool(updates.get("shipping_feasible", item.shipping_feasible))
    if move_out_deadline and not item.move_out_deadline:
        updates["move_out_deadline"] = move_out_deadline
    updates["review_status"] = "Not Reviewed"
    updates["approval_status"] = "Pending"
    repo.update(item_id, actor="system", **{k: v for k, v in updates.items() if v is not None})

    item = repo.get(item_id)
    provider = get_research_provider()
    research_result = provider.find_comparables(item)
    worksheet = research.worksheet_path(item_id)

    # Any comparables an automated provider proposes are recorded as
    # unconfirmed evidence -- they feed confidence scoring but the approval
    # gate ignores them until a human confirms each one (see
    # approval.prepare_review). The manual-queue default never returns any.
    if research_result.comparables:
        comp_repo = CompRepository(session)
        for c in research_result.comparables:
            # add_unique, not add: finalise_draft can legitimately run twice on
            # the same item (a resumed submission, an orchestrator retry), and
            # the same comparable arriving twice must not inflate the sample
            # size that pricing confidence is scored from.
            comp_repo.add_unique(
                item_id, platform=c.platform, title=c.title, url=c.url, is_sold=c.is_sold,
                price=c.price, shipping_amount=c.shipping_amount, condition=c.condition,
                location=c.location, observed_date=c.observed_date,
                similarities=c.similarities, differences=c.differences,
                relevance=c.relevance, is_placeholder=c.is_placeholder,
                price_type=c.price_type, needs_confirmation=True, source=c.source,
            )

    repo.update(item_id, actor="system", research_status=research_result.status)
    repo.set_status(item_id, ItemStatus.NEEDS_REVIEW.value, actor="system",
                    reason="intake complete")
    repo.update(item_id, actor="system",
                processing_stage=ProcessingStage.READY_FOR_REVIEW.value)
    repo.events.record(item_id, "worksheet_created", actor="system", path=str(worksheet),
                       research_provider=research_result.provider,
                       proposed_comparables=len(research_result.comparables))

    res.message = "Draft created and queued for review."
    res.warnings.append(
        "Weight, vehicle, and helper count are category estimates seeded by the "
        "system. Confirm them during review."
    )
    if photo_count(session, item_id) < MIN_PHOTOS:
        res.warnings.append(
            "Only %d photo(s) — %d or more identifies items far more reliably."
            % (photo_count(session, item_id), MIN_PHOTOS)
        )
    return res


def review_summary(session, item_id: str) -> str:
    """The plain-language confirmation the submitter receives. No jargon."""
    item = ItemRepository(session).get(item_id)
    if item is None:
        return "That item could not be found."
    n = photo_count(session, item_id)
    lines = [
        f"Saved. Item {item.item_id}",
        "",
        item.item_name or "Item name to be confirmed",
    ]
    if item.brand or item.model:
        lines.append(" ".join(x for x in (item.brand, item.model) if x))
    lines.append("Condition: %s" % (item.condition or "to be confirmed"))
    if item.location_in_house:
        lines.append(f"Room: {item.location_in_house}")
    lines.append("Photos saved: %d" % n)

    # What the automated job actually worked out, in the submitter's language.
    # Deliberately framed as a suggestion throughout: nothing here is a price
    # anyone has agreed to, and the message must never imply otherwise.
    if item.initial_list_price:
        lines.append("")
        lines.append("Suggested asking price: $%.0f" % item.initial_list_price)
        if item.expected_sale_price:
            lines.append("Likely to sell around: $%.0f" % item.expected_sale_price)
        if item.pricing_confidence:
            lines.append("Confidence in that figure: %s" % item.pricing_confidence)
    if item.primary_marketplace:
        lines.append("Best place to sell it: %s" % item.primary_marketplace)

    outstanding = [b for b in (item.approval_blockers or "").split("\n") if b.strip()]
    if outstanding:
        lines.append("")
        lines.append("Still to sort out before it can go live:")
        for blocker in outstanding[:4]:
            lines.append("  - " + blocker)

    lines.append("")
    lines.append("Drake will price it and send it for approval before anything is listed.")
    lines.append("Send /newitem when you're ready for the next one.")
    return "\n".join(lines)
