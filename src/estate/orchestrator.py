"""The one item-processing job: photos + a sentence in, a priced record out.

This module is the answer to the project's north star. Everything else in
``estate/`` is a capability; this is the thing that runs them in order, in one
place, so that a submitter sends photos and a sentence and gets back a
researched, priced, prioritised, marketplace-ready record without anybody
opening ChatGPT.

    photos_received -> identifying -> [needs_information] -> researching
                    -> pricing -> generating_listings -> ready_for_review

Design rules, each of which exists because of a specific way this can go wrong
=============================================================================

**Restart-safe.** All job state lives in the database (``processing_stage``,
``processing_attempts``, ``processing_error``), never in memory. A bot that
dies mid-research resumes from the recorded stage instead of losing the item
or silently stalling in a stage nobody is watching.

**Idempotent.** Running the job twice on the same item must not double
anything. Comparables are deduplicated at the repository layer by URL;
listing packages are a dict that is replaced wholesale, never appended to;
pricing is recomputed from the current evidence rather than adjusted.

**Owner facts win.** Anything the owner said -- in their sentence, in an
answer, or via a reviewer edit -- is recorded in ``owner_confirmed_fields``
and is never overwritten by a later automated pass. A re-run must be safe to
trigger at any time, and it would not be if it could quietly replace "yes,
this is mine to sell" with a model's guess.

**A human still decides.** Nothing here approves, prices-for-publication,
lowers a floor, confirms ownership, or publishes. The job's terminal state is
``ready_for_review`` -- a queue, not a decision. Blockers are recorded on the
item so a reviewer sees exactly what is outstanding, and so the automated
path can never mark something ready that is not.

**Failures are recorded, not raised.** A provider outage leaves the photos,
the owner's words, and every field earned so far intact, moves the stage to
``Error``, and increments the attempt counter. Only the exception TYPE and the
stage are stored -- never a message, which can carry a URL with a bearer token
in it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from estate import listing, marketplaces, pricing, research
from estate.repository import CompRepository, ItemRepository, PhotoRepository
from estate.schema import (
    ApprovalStatus,
    ItemStatus,
    ProcessingStage,
    ResearchStatus,
)
from estate._compat import get_logger

logger = get_logger(__name__)

#: How many times the job will retry an item by itself before it stops and
#: waits for a human. Three is enough to ride out a transient provider blip
#: and few enough that a genuinely broken item does not burn tokens all night.
MAX_ATTEMPTS = 3


@dataclass
class JobResult:
    item_id: str = ""
    ok: bool = True
    stage: str = ""
    #: Stages actually executed on this run. Empty on a no-op re-run, which is
    #: how idempotency is asserted in the tests.
    ran: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    message: str = ""


# ---------------------------------------------------------------------------
# Owner-fact protection
# ---------------------------------------------------------------------------

def mark_owner_confirmed(session, item_id: str, *fields: str) -> list:
    """Record that a human supplied these fields, so nothing overwrites them."""
    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return []
    confirmed = list(item.owner_confirmed_fields or [])
    for f in fields:
        if f and f not in confirmed:
            confirmed.append(f)
    item.owner_confirmed_fields = confirmed
    session.commit()
    return confirmed


def _protected(item) -> set:
    """Fields an automated pass must not touch on this item."""
    protected = set(item.owner_confirmed_fields or [])
    # Once a reviewer has approved the numbers, the numbers are theirs.
    if (item.approval_status or "") == ApprovalStatus.APPROVED.value:
        protected.update(
            {"initial_list_price", "expected_sale_price", "floor_price", "current_price",
             "approved_pickup_price", "listing_title", "listing_description"}
        )
    # Ownership is only ever set by a person, in review. Belt and braces: it is
    # in this set even if nothing added it to owner_confirmed_fields.
    protected.add("ownership_approval")
    return protected


def _safe_update(session, item_id: str, actor: str, **fields) -> dict:
    """Apply only the updates that are not protected. Returns what was written."""
    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return {}
    protected = _protected(item)
    allowed = {k: v for k, v in fields.items() if k not in protected}
    if allowed:
        repo.update(item_id, actor=actor, **allowed)
    skipped = sorted(set(fields) - set(allowed))
    if skipped:
        repo.events.record(item_id, "automated_update_skipped", actor=actor, fields=skipped)
    return allowed


def _set_stage(session, item_id: str, stage: str, actor: str = "orchestrator") -> None:
    repo = ItemRepository(session)
    repo.update(item_id, actor=actor, processing_stage=stage,
                last_activity=date.today().isoformat())


def _record_failure(session, item_id: str, stage: str, exc: BaseException) -> None:
    """Persist a failure without ever persisting its message."""
    repo = ItemRepository(session)
    item = repo.get(item_id)
    attempts = int(getattr(item, "processing_attempts", 0) or 0) + 1
    repo.update(
        item_id, actor="orchestrator",
        processing_stage=ProcessingStage.ERROR.value,
        processing_error=type(exc).__name__,
        processing_failed_stage=stage,
        processing_attempts=attempts,
        last_activity=date.today().isoformat(),
    )
    repo.events.record(item_id, "processing_failed", actor="orchestrator",
                       stage=stage, error_type=type(exc).__name__, attempt=attempts)
    logger.error({"action": "orchestrator_stage_failed", "item_id": item_id,
                  "stage": stage, "error_type": type(exc).__name__, "attempt": attempts})


def _clear_failure(session, item_id: str) -> None:
    ItemRepository(session).update(
        item_id, actor="orchestrator", processing_error="", processing_failed_stage="",
    )


# ---------------------------------------------------------------------------
# Stage: research  (evidence normalisation, never invention)
# ---------------------------------------------------------------------------

def run_research(session, item_id: str) -> tuple:
    """Ask the configured provider for comparables and normalise what comes back.

    Returns (summary, warnings). Partial results are always preserved: a
    provider that returns two usable comparables and then fails still leaves
    those two behind. Nothing here fabricates a comparable, and every proposed
    row lands with ``needs_confirmation=True`` so it can raise confidence but
    never unlock approval on its own.
    """
    from estate.research_provider import get_research_provider

    repo = ItemRepository(session)
    comps_repo = CompRepository(session)
    item = repo.get(item_id)
    warnings: list = []

    _set_stage(session, item_id, ProcessingStage.RESEARCHING.value)

    try:
        result = get_research_provider().find_comparables(item)
    except Exception as exc:  # a provider must never take the job down
        logger.error({"action": "research_provider_failed", "item_id": item_id,
                      "error_type": type(exc).__name__})
        repo.update(item_id, actor="orchestrator",
                    research_status=ResearchStatus.FAILED.value)
        warnings.append(
            "Automated research could not run. The item is queued for a human "
            "researcher; nothing already gathered was lost."
        )
        result = None

    added = 0
    rejected = 0
    if result is not None:
        for c in result.comparables:
            # The same standard the manual worksheet import is held to: no URL
            # means it is not evidence, whoever proposed it.
            if not (c.url or "").strip():
                rejected += 1
                continue
            comp, created = comps_repo.add_unique(
                item_id, platform=c.platform, title=c.title, url=c.url, is_sold=c.is_sold,
                price=c.price, shipping_amount=c.shipping_amount, condition=c.condition,
                location=c.location, observed_date=c.observed_date,
                similarities=c.similarities, differences=c.differences,
                relevance=c.relevance, is_placeholder=c.is_placeholder,
                price_type=c.price_type, needs_confirmation=True, source=c.source,
            )
            added += 1 if created else 0
        repo.update(item_id, actor="orchestrator", research_status=result.status)
        if rejected:
            warnings.append(
                "%d proposed comparable(s) had no source link and were discarded."
                % rejected
            )

    # Summarise whatever evidence now exists -- worksheet imports, confirmed
    # rows from an earlier pass, and anything just proposed.
    item = repo.get(item_id)
    comps = _comps(session, item_id)
    summary = research.summarise(item_id, comps, item.condition or "", item.category or "")

    fields = summary.as_item_fields()
    fields["research_confidence"] = summary.confidence
    _safe_update(session, item_id, "orchestrator", **fields)

    blockers = list(summary.gaps)
    repo.update(item_id, actor="orchestrator",
                research_blockers="\n".join(blockers))
    repo.events.record(item_id, "research_ran", actor="orchestrator",
                       comparables_added=added, comparables_rejected=rejected,
                       total_comparables=len(comps), confidence=summary.confidence)
    return summary, warnings


def _comps(session, item_id: str) -> list:
    from estate.approval import _comps_from_db

    return _comps_from_db(session, item_id)


# ---------------------------------------------------------------------------
# Stage: pricing
# ---------------------------------------------------------------------------

def run_pricing(session, item_id: str, summary=None) -> tuple:
    """Turn the evidence into numbers and write them to the item.

    Writes a *recommendation*, never an approval: ``approval_status`` is
    untouched, the floor is a suggestion until a reviewer accepts it, and an
    already-approved item's numbers are protected by ``_safe_update``.
    """
    repo = ItemRepository(session)
    _set_stage(session, item_id, ProcessingStage.PRICING.value)
    item = repo.get(item_id)
    if summary is None:
        summary = research.summarise(
            item_id, _comps(session, item_id), item.condition or "", item.category or ""
        )

    rec = pricing.recommend_price(summary)
    warnings = list(rec.warnings)

    if rec.initial_list_price is None:
        # Not a failure. "We cannot price this yet" is a real, honest answer,
        # and the item still moves forward so a human sees it in the queue.
        repo.update(item_id, actor="orchestrator",
                    pricing_confidence=summary.confidence)
        repo.events.record(item_id, "pricing_skipped", actor="orchestrator",
                           reason="insufficient evidence",
                           comparables=summary.comp_count)
        return rec, warnings

    fields = rec.as_item_fields()
    fields["pricing_confidence"] = rec.confidence
    _safe_update(session, item_id, "orchestrator", **fields)

    item = repo.get(item_id)
    incentive = pricing.compute_pickup_incentive(
        item,
        current_price=item.current_price or rec.initial_list_price,
        stairs=(item.pickup_difficulty in ("Hard", "Specialist Movers")),
        disassembly=(item.pickup_difficulty == "Specialist Movers"),
        urgent=bool(item.move_out_deadline),
    )
    _safe_update(session, item_id, "orchestrator", pickup_incentive=incentive.amount)

    # Expected net: what actually lands in the bank if this sells at the
    # expected price on the recommended channel. This is the number priority
    # ranks on, so it is stored rather than recomputed in four places.
    markets = marketplaces.recommend(item)
    primary = markets.get("primary")
    fee_key = primary.platform.fee_key if primary else ""
    expected = float(rec.expected_sale_price or 0)
    shipping_burden = 0.0
    if bool(item.shipping_feasible) and primary and not primary.platform.local:
        # Shipping we would pay for, roughly: a real quote replaces this at
        # review time. Flagged as an estimate in the warnings, not silently.
        shipping_burden = round(min(60.0, 8.0 + 0.55 * float(item.weight_lbs or 0)), 2)
        warnings.append(
            "Shipping cost is a $%.0f estimate from weight, not a carrier quote."
            % shipping_burden
        )
    net = pricing.estimated_net_proceeds(expected, fee_key, shipping_burden)
    fees = round(max(0.0, expected - shipping_burden - net), 2)
    _safe_update(session, item_id, "orchestrator",
                 estimated_fees=fees, expected_net_proceeds=net)

    repo.events.record(item_id, "priced", actor="orchestrator",
                       list_price=rec.initial_list_price,
                       expected=rec.expected_sale_price, floor=rec.floor_price,
                       confidence=rec.confidence, expected_net=net,
                       basis=rec.basis)
    return rec, warnings


# ---------------------------------------------------------------------------
# Stage: listing packages + website copy
# ---------------------------------------------------------------------------

def run_listing_generation(session, item_id: str, catalog_url: str = "",
                           region: str = "") -> tuple:
    """Generate a copy-ready package per recommended platform, plus website copy.

    Deterministic and template-driven (see ``listing.py``): every claim traces
    to a field a human can check. Stored on the item as a dict, replaced
    wholesale on each run so a retry can never produce two of anything.
    """
    repo = ItemRepository(session)
    _set_stage(session, item_id, ProcessingStage.GENERATING_LISTINGS.value)
    item = repo.get(item_id)
    photos = PhotoRepository(session).for_item(item_id)

    markets = marketplaces.recommend(item)
    packages = listing.build_all(
        item, markets,
        minimum_offer=item.floor_price,
        pickup_price=(
            max(float(item.floor_price or 0),
                float(item.current_price or 0) - float(item.pickup_incentive or 0))
            if item.current_price else None
        ),
        pickup_incentive=float(item.pickup_incentive or 0),
        catalog_url=catalog_url,
        region=region,
    )
    website = listing.build_website_copy(
        item, photos=photos, catalog_url=catalog_url, region=region
    )

    serialised = {}
    for key, package in packages.items():
        serialised[key] = package.to_dict() if hasattr(package, "to_dict") else package
    serialised["website"] = (
        website.to_dict() if hasattr(website, "to_dict") else website
    )

    item = repo.get(item_id)
    item.listing_packages = serialised  # replace, never merge -- idempotency
    session.commit()

    primary = markets.get("primary")
    secondary = markets.get("secondary") or []
    updates = {
        "primary_marketplace": primary.platform.name if primary else "",
        "secondary_marketplaces": ", ".join(f.platform.name for f in secondary),
    }
    hero_key = primary.platform.key if primary else ""
    hero = packages.get(hero_key)
    if hero is not None:
        updates["listing_title"] = getattr(hero, "title", "") or ""
        updates["listing_description"] = getattr(hero, "description", "") or ""
        updates["keywords"] = ", ".join(getattr(hero, "keywords", []) or [])
    _safe_update(session, item_id, "orchestrator", **updates)

    repo.events.record(item_id, "listings_generated", actor="orchestrator",
                       platforms=sorted(serialised), primary=updates["primary_marketplace"])
    return serialised, list(markets.get("warnings") or [])


# ---------------------------------------------------------------------------
# Stage: priority + blockers
# ---------------------------------------------------------------------------

def refresh_priority(session, item_id: str) -> object:
    """Recompute difficulty, blockers, and the priority score for one item."""
    from estate import priority as priority_mod

    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return None

    score = priority_mod.score_item(item)
    repo.update(
        item_id, actor="orchestrator",
        selling_difficulty=priority_mod.selling_difficulty(item),
        shipping_difficulty=priority_mod.shipping_difficulty(item),
        priority_score=score.score,
        priority_reasons=score.explain(),
    )
    return score


def refresh_blockers(session, item_id: str, catalog_url: str = "") -> list:
    """Store what a reviewer would have to fix, without deciding anything."""
    from estate.approval import prepare_review

    repo = ItemRepository(session)
    packet = prepare_review(session, item_id, catalog_url=catalog_url)
    blockers = list(packet.blockers)
    repo.update(item_id, actor="orchestrator", approval_blockers="\n".join(blockers))
    return blockers


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------

def process_item(session, item_id: str, hint: str = "", catalog_url: str = "",
                 region: str = "", move_out_deadline: str = "",
                 force: bool = False) -> JobResult:
    """Run the item to ``ready_for_review``, resuming from wherever it stopped.

    Safe to call repeatedly. Safe to call after a restart. Safe to call on an
    item that is already finished (it becomes a no-op that only refreshes the
    priority score). It never touches an item a human already owns.
    """
    from estate import pipeline

    repo = ItemRepository(session)
    item = repo.get(item_id)
    res = JobResult(item_id=item_id)
    if item is None:
        res.ok = False
        res.message = "Item not found."
        return res

    stage = item.processing_stage or ProcessingStage.PHOTOS_RECEIVED.value

    # A reviewer's decision outranks the pipeline, always.
    if stage in {ProcessingStage.APPROVED.value, ProcessingStage.READY_TO_PUBLISH.value,
                 ProcessingStage.LISTED.value, ProcessingStage.SOLD.value,
                 ProcessingStage.REMOVED.value} and not force:
        res.stage = stage
        res.message = "A person already owns this item; the automated job stood down."
        refresh_priority(session, item_id)
        return res

    attempts = int(item.processing_attempts or 0)
    if stage == ProcessingStage.ERROR.value and attempts >= MAX_ATTEMPTS and not force:
        res.ok = False
        res.stage = stage
        res.message = (
            "This item has failed %d times and is waiting for a person. "
            "Nothing has been lost." % attempts
        )
        return res

    if PhotoRepository(session).count(item_id) == 0:
        res.ok = False
        res.stage = stage
        res.message = "No photos stored for this item yet."
        return res

    # -- 1. Identification ---------------------------------------------------
    identified = bool((item.vision_raw or {}).get("provider"))
    if not identified or force:
        try:
            _ident, ident_res = pipeline.identify_item(session, item_id, hint=hint)
        except Exception as exc:  # identify_item catches its own, but be safe
            _record_failure(session, item_id, ProcessingStage.IDENTIFYING.value, exc)
            res.ok = False
            res.stage = ProcessingStage.ERROR.value
            res.message = ("Could not analyse the photos right now. They are saved, "
                           "so nothing is lost.")
            return res
        res.ran.append(ProcessingStage.IDENTIFYING.value)
        res.warnings.extend(ident_res.warnings)
        if not ident_res.ok:
            res.ok = False
            res.stage = ProcessingStage.ERROR.value
            res.message = ident_res.message
            return res
        item = repo.get(item_id)
        ident_conf = float(
            ((item.vision_raw or {}).get("identification") or {}).get("overall_confidence", 0)
            or 0
        )
        repo.update(item_id, actor="orchestrator", identification_confidence=ident_conf)

    # -- 2. Outstanding questions -------------------------------------------
    item = repo.get(item_id)
    outstanding = [k for k in (item.missing_fields or [])]
    if outstanding:
        _set_stage(session, item_id, ProcessingStage.NEEDS_INFORMATION.value)
        res.stage = ProcessingStage.NEEDS_INFORMATION.value
        res.questions = outstanding
        res.message = "Waiting on a few answers before pricing."
        refresh_priority(session, item_id)
        return res

    # -- 3. Logistics seeding + research + pricing + listings ---------------
    try:
        # finalise_draft owns logistics seeding, the comps worksheet, and the
        # Draft -> Needs Review transition. Calling it here keeps one
        # implementation of those rules rather than a second, drifting copy.
        final = pipeline.finalise_draft(
            session, item_id, move_out_deadline=move_out_deadline,
            catalog_url=catalog_url,
        )
        res.ran.append(ProcessingStage.RESEARCHING.value)
        res.warnings.extend(final.warnings)

        summary, research_warnings = run_research(session, item_id)
        res.warnings.extend(research_warnings)

        _rec, pricing_warnings = run_pricing(session, item_id, summary=summary)
        res.ran.append(ProcessingStage.PRICING.value)
        res.warnings.extend(pricing_warnings)

        run_listing_generation(session, item_id, catalog_url=catalog_url, region=region)
        res.ran.append(ProcessingStage.GENERATING_LISTINGS.value)
    except Exception as exc:
        _record_failure(session, item_id, ProcessingStage.RESEARCHING.value, exc)
        res.ok = False
        res.stage = ProcessingStage.ERROR.value
        res.message = ("Something went wrong while researching this item. Everything "
                       "collected so far is saved and a person will pick it up.")
        return res

    # -- 4. Hand to the review queue ----------------------------------------
    res.blockers = refresh_blockers(session, item_id, catalog_url=catalog_url)
    repo.set_status(item_id, ItemStatus.NEEDS_REVIEW.value, actor="orchestrator",
                    reason="automated processing complete")
    _set_stage(session, item_id, ProcessingStage.READY_FOR_REVIEW.value)
    _clear_failure(session, item_id)
    repo.update(item_id, actor="orchestrator",
                last_processed_at=datetime.now().isoformat(timespec="seconds"))
    refresh_priority(session, item_id)

    res.ran.append(ProcessingStage.READY_FOR_REVIEW.value)
    res.stage = ProcessingStage.READY_FOR_REVIEW.value
    res.message = "Researched, priced, and ready for review."
    repo.events.record(item_id, "processing_complete", actor="orchestrator",
                       blockers=res.blockers, stages=res.ran)
    return res


def sync_stage_with_status(session, item_id: str) -> str:
    """Mirror a reviewer's lifecycle decision into ``processing_stage``.

    Called after ``approval.apply_decision`` so ``/myitems`` does not keep
    saying "ready for review" about something that was approved an hour ago.
    """
    from estate.schema import STATUS_TO_PROCESSING_STAGE

    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return ""
    stage = STATUS_TO_PROCESSING_STAGE.get(item.status or "")
    if stage and stage != item.processing_stage:
        repo.update(item_id, actor="orchestrator", processing_stage=stage,
                    last_activity=date.today().isoformat())
    refresh_priority(session, item_id)
    return stage or (item.processing_stage or "")


def reprioritise_all(session) -> int:
    """Recompute priority for the whole inventory. Cheap, pure, no I/O."""
    items = ItemRepository(session).all()
    for item in items:
        refresh_priority(session, item.item_id)
    return len(items)
