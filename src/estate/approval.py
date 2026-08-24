"""Human approval gate.

Nothing in this system publishes a price or a listing without a decision
recorded here. ``prepare_review`` is deliberately read-only: it computes the
recommendation fresh every time it is viewed but writes nothing, so looking at
an item can never accidentally price it.

``apply_decision`` is the only function that moves an item into an approved
state, and it records who did it and what the numbers were at that moment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from estate import listing, marketplaces, paths, pricing, research
from estate.repository import CompRepository, ItemRepository
from estate.research import Comparable
from estate.schema import ApprovalStatus, ItemStatus, ReviewStatus

ACTIONS = (
    "approve",
    "save_edits",
    "request_research",
    "request_photos",
    "not_for_sale",
    "donate",
    "specialist",
    "reject",
)


@dataclass
class ReviewPacket:
    item = None
    photos: list = field(default_factory=list)
    comps: list = field(default_factory=list)
    summary = None
    price = None
    incentive = None
    markets: dict = field(default_factory=dict)
    packages: dict = field(default_factory=dict)
    low_confidence_fields: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    worksheet_path: str = ""

    @property
    def can_approve(self) -> bool:
        return not self.blockers


def _comps_from_db(session, item_id: str) -> list:
    out = []
    for c in CompRepository(session).for_item(item_id):
        out.append(
            Comparable(
                platform=c.platform, title=c.title, url=c.url, is_sold=bool(c.is_sold),
                price=float(c.price or 0), shipping_amount=float(c.shipping_amount or 0),
                condition=c.condition, location=c.location, observed_date=c.observed_date,
                similarities=c.similarities, differences=c.differences,
                relevance=float(c.relevance or 0.5), is_placeholder=bool(c.is_placeholder),
                price_type=c.price_type or "exact", needs_confirmation=bool(c.needs_confirmation),
                source=c.source,
            )
        )
    return out


def prepare_review(session, item_id: str, catalog_url: str = "", region: str = "") -> ReviewPacket:
    """Assemble everything a reviewer needs. Read-only."""
    from estate.repository import PhotoRepository

    repo = ItemRepository(session)
    packet = ReviewPacket()
    item = repo.get(item_id)
    if item is None:
        packet.blockers.append("Item not found.")
        return packet

    packet.item = item
    packet.photos = PhotoRepository(session).for_item(item_id)
    packet.comps = _comps_from_db(session, item_id)
    packet.missing = list(item.missing_fields or [])

    packet.summary = research.summarise(
        item_id, packet.comps, item.condition or "", item.category or ""
    )
    packet.price = pricing.recommend_price(packet.summary)

    proposed = packet.price.initial_list_price or item.current_price
    packet.incentive = pricing.compute_pickup_incentive(
        item,
        current_price=proposed,
        stairs=(item.pickup_difficulty in ("Hard", "Specialist Movers")),
        disassembly=(item.pickup_difficulty == "Specialist Movers"),
        urgent=bool(item.move_out_deadline),
    )

    # Marketplace scoring uses the proposed price, not the stored one.
    class _Proxy:
        pass

    proxy = _Proxy()
    for attr in ("category", "weight_lbs", "shipping_feasible", "pickup_required",
                 "brand", "model", "move_out_deadline", "dimensions",
                 "included_accessories", "item_name", "description", "condition",
                 "defects", "approximate_age", "item_id", "people_required",
                 "required_vehicle", "pricing_confidence", "floor_price"):
        setattr(proxy, attr, getattr(item, attr, None))
    proxy.initial_list_price = proposed
    proxy.current_price = proposed
    proxy.floor_price = packet.price.floor_price or item.floor_price

    packet.markets = marketplaces.recommend(proxy)
    packet.packages = listing.build_all(
        proxy, packet.markets,
        minimum_offer=proxy.floor_price,
        pickup_price=packet.incentive.pickup_price,
        pickup_incentive=packet.incentive.amount,
        catalog_url=catalog_url,
        region=region,
    )
    # The Future Only catalogue is its own "platform" -- always generated
    # (not gated on marketplaces.recommend()) so the reviewer sees the
    # website copy for every item, whether or not it is also cross-listed.
    packet.packages["website"] = listing.build_website_copy(
        proxy, photos=packet.photos, catalog_url=catalog_url, region=region,
    )

    # -- what the reviewer must fix before approving -------------------------
    # Per-field confidence lives under vision_raw["identification"]["confidence"]
    # (see vision.ItemIdentification.identification_report()), not at the top
    # level of vision_raw -- this was reading the wrong path and always found
    # an empty dict, so low_confidence_fields never actually surfaced anything.
    vision = item.vision_raw or {}
    identification = vision.get("identification") or {}
    for key, value in (identification.get("confidence") or {}).items():
        try:
            if float(value) < 0.6:
                packet.low_confidence_fields.append(key)
        except (TypeError, ValueError):
            continue

    # A comp only counts toward the evidence requirement once it is a real,
    # sourced, human-confirmed record -- an automated provider's proposed row
    # (needs_confirmation=True) contributes to confidence scoring but must not
    # by itself unlock approval, same as a placeholder.
    confirmed_comps = [
        c for c in packet.comps if c.url and not c.is_placeholder and not c.needs_confirmation
    ]
    if not confirmed_comps:
        if packet.comps:
            packet.blockers.append(
                "No confirmed comparable evidence. Every comparable is either a "
                "placeholder or still awaiting confirmation — review and confirm "
                "at least one before pricing."
            )
        else:
            packet.blockers.append(
                "No comparable evidence recorded. Fill in the comps worksheet before pricing."
            )
    if packet.summary.placeholder_count:
        packet.blockers.append(
            "Placeholder comparables present — this item cannot be approved for publication."
        )
    if not item.ownership_approval:
        packet.blockers.append(
            "Ownership not confirmed. Tick ownership approval before selling this."
        )
    if (item.condition or "Unknown") == "Unknown":
        packet.blockers.append("Condition is still Unknown.")
    if str(item.item_name or "").startswith("[MOCK]"):
        packet.blockers.append("This is sample/mock data and must never be published.")

    packet.worksheet_path = str(research.worksheet_path(item_id))
    return packet


def _sync_stage(session, item_id: str) -> None:
    """Mirror the new lifecycle status into processing_stage.

    Without this, /myitems keeps telling the submitter an item is "ready for
    review" long after a reviewer approved it -- two truths about one item,
    which is exactly the confusion the processing_stage column exists to
    prevent.
    """
    from estate.orchestrator import sync_stage_with_status

    sync_stage_with_status(session, item_id)


def apply_decision(session, item_id: str, action: str, actor: str,
                   edits: dict | None = None, note: str = "",
                   catalog_url: str = "", region: str = "") -> tuple:
    """Record a reviewer decision. Returns (ok, message)."""
    repo = ItemRepository(session)
    item = repo.get(item_id)
    if item is None:
        return False, "Item not found."
    if action not in ACTIONS:
        return False, "Unknown action."

    if edits:
        clean = {}
        for k, v in edits.items():
            if not hasattr(item, k):
                continue
            if k in ("weight_lbs", "comp_low", "comp_median", "comp_high",
                     "initial_list_price", "expected_sale_price", "floor_price",
                     "current_price", "pickup_incentive", "approved_pickup_price",
                     "best_offer", "final_sale_price", "actual_proceeds"):
                try:
                    clean[k] = float(str(v).replace("$", "").replace(",", "")) if str(v).strip() else None
                except ValueError:
                    continue
            elif k in ("people_required", "inquiry_count", "comp_count"):
                try:
                    clean[k] = int(float(v)) if str(v).strip() else 0
                except ValueError:
                    continue
            elif k in ("ownership_approval", "shipping_feasible", "pickup_required"):
                clean[k] = str(v).strip().lower() in ("yes", "true", "on", "1")
            else:
                clean[k] = v
        if clean:
            # "Never lower below the approved floor" is a hard rule, not a
            # suggestion -- it must hold even when the same reviewer editing
            # the price is also the one who could otherwise move both
            # numbers. Only fires when this edit would actually create the
            # violation (current_price/floor_price both resolve to a value).
            resulting_current = clean.get("current_price", item.current_price)
            resulting_floor = clean.get("floor_price", item.floor_price)
            if (
                resulting_current is not None and resulting_floor is not None
                and resulting_current < resulting_floor
            ):
                return False, (
                    "Cannot save: current price $%.2f would be below the floor "
                    "price $%.2f. Raise the floor or raise the price — not both "
                    "at once." % (resulting_current, resulting_floor)
                )
            repo.update(item_id, actor=actor, **clean)
            # A reviewer's edit is a human fact. Record it so a later
            # orchestrator pass -- a re-run, a retry, a resumed job -- can
            # never overwrite it with a recomputed recommendation.
            from estate.orchestrator import mark_owner_confirmed

            mark_owner_confirmed(session, item_id, *clean.keys())
            item = repo.get(item_id)

    if action == "save_edits":
        repo.events.record(item_id, "review_edited", actor=actor, note=note[:400])
        return True, "Changes saved. Item still awaiting approval."

    if action == "request_research":
        repo.update(item_id, actor=actor, review_status=ReviewStatus.NEEDS_MORE_RESEARCH.value)
        repo.events.record(item_id, "research_requested", actor=actor, note=note[:400])
        return True, "Marked as needing more comparable evidence."

    if action == "request_photos":
        repo.update(item_id, actor=actor, review_status=ReviewStatus.NEEDS_MORE_PHOTOS.value)
        repo.events.record(item_id, "photos_requested", actor=actor, note=note[:400])
        return True, "Marked as needing more photographs."

    if action == "specialist":
        repo.update(item_id, actor=actor, review_status=ReviewStatus.NEEDS_SPECIALIST.value)
        repo.events.record(item_id, "specialist_requested", actor=actor, note=note[:400])
        return True, "Flagged for specialist appraisal. Do not list until appraised."

    if action == "not_for_sale":
        repo.update(item_id, actor=actor, approval_status=ApprovalStatus.NOT_FOR_SALE.value,
                    website_status="Hidden")
        repo.set_status(item_id, ItemStatus.REMOVED.value, actor=actor, reason=note or "not for sale")
        _sync_stage(session, item_id)
        return True, "Marked not for sale."

    if action == "donate":
        repo.update(item_id, actor=actor, approval_status=ApprovalStatus.DONATE.value,
                    website_status="Hidden", final_disposition="Donated")
        repo.set_status(item_id, ItemStatus.DONATED.value, actor=actor, reason=note or "donation")
        _sync_stage(session, item_id)
        return True, "Marked for donation."

    if action == "reject":
        repo.update(item_id, actor=actor, approval_status=ApprovalStatus.REJECTED.value)
        repo.set_status(item_id, ItemStatus.DRAFT.value, actor=actor, reason=note or "rejected")
        return True, "Rejected and returned to draft."

    # -- approve -------------------------------------------------------------
    packet = prepare_review(session, item_id, catalog_url=catalog_url, region=region)
    if packet.blockers:
        return False, "Cannot approve: " + " ".join(packet.blockers)

    item = repo.get(item_id)
    updates = dict(packet.summary.as_item_fields())

    # A reviewer's explicit price always wins over the recommendation.
    if item.initial_list_price is None:
        updates.update(packet.price.as_item_fields())
    else:
        updates["current_price"] = item.current_price or item.initial_list_price
        if item.floor_price is None:
            updates["floor_price"] = packet.price.floor_price
        if item.expected_sale_price is None:
            updates["expected_sale_price"] = packet.price.expected_sale_price

    updates["pickup_incentive"] = packet.incentive.amount
    updates["approved_pickup_price"] = packet.incentive.pickup_price
    primary = packet.markets.get("primary")
    updates["primary_marketplace"] = primary.platform.name if primary else ""
    updates["secondary_marketplaces"] = ", ".join(
        f.platform.name for f in packet.markets.get("secondary", [])
    )
    # "website" is always present in packet.packages (see prepare_review) but
    # is a WebsiteCopy, not a marketplace ListingPackage -- it has no .title,
    # so it must never be picked up here even when no marketplace was
    # recommended and it happens to be the only package present.
    first_pkg = next(
        (p for k, p in packet.packages.items() if k != "website"), None
    )
    if first_pkg is not None:
        updates["listing_title"] = first_pkg.title
        updates["listing_description"] = first_pkg.description
        updates["keywords"] = ", ".join(first_pkg.keywords)
    updates["approval_status"] = ApprovalStatus.APPROVED.value
    updates["review_status"] = ReviewStatus.REVIEWED.value
    updates["website_status"] = "Queued"

    repo.update(item_id, actor=actor, **{k: v for k, v in updates.items() if v is not None})
    repo.set_status(item_id, ItemStatus.APPROVED.value, actor=actor, reason=note or "approved")
    _sync_stage(session, item_id)

    item = repo.get(item_id)
    record = {
        "item_id": item_id,
        "approved_by": actor,
        "approved_at": datetime.now().isoformat(timespec="seconds"),
        "note": note,
        "prices": {
            "initial_list_price": item.initial_list_price,
            "expected_sale_price": item.expected_sale_price,
            "floor_price": item.floor_price,
            "current_price": item.current_price,
            "pickup_incentive": item.pickup_incentive,
            "approved_pickup_price": item.approved_pickup_price,
        },
        "evidence": {
            "comp_count": packet.summary.comp_count,
            "sold_count": packet.summary.sold_count,
            "confidence": packet.summary.confidence,
            "confidence_score": packet.summary.confidence_score,
            "sources": packet.summary.sources,
        },
        "marketplaces": {
            "primary": item.primary_marketplace,
            "secondary": item.secondary_marketplaces,
        },
    }
    _write_approval_record(item_id, record)
    _write_listing_packages(item_id, packet.packages)
    repo.events.record(item_id, "approved", actor=actor, **record["prices"])
    return True, "Approved. Item is queued for listing and for the catalogue."


def _write_approval_record(item_id: str, record: dict) -> Path:
    d = paths.item_dir(item_id, create=True) / "approval"
    d.mkdir(parents=True, exist_ok=True)
    out = d / (f"{item_id}_approval_{date.today().isoformat()}.json")
    out.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return out


def _write_listing_packages(item_id: str, packages: dict) -> None:
    d = paths.item_dir(item_id, create=True) / "copy"
    d.mkdir(parents=True, exist_ok=True)
    for key, pkg in packages.items():
        (d / (f"{item_id}_{key}.md")).write_text(pkg.to_markdown(), encoding="utf-8")
    (d / (f"{item_id}_packages.json")).write_text(
        json.dumps({k: v.to_dict() for k, v in packages.items()}, indent=2, default=str),
        encoding="utf-8",
    )


def import_comps_for_item(session, item_id: str, worksheet: str = "") -> tuple:
    """Load the filled worksheet into the database. Returns (count, problems)."""
    path = Path(worksheet) if worksheet else research.worksheet_path(item_id)
    comps, problems = research.import_worksheet(path)
    repo = CompRepository(session)
    repo.clear(item_id)
    for c in comps:
        repo.add(
            item_id, platform=c.platform, title=c.title, url=c.url, is_sold=c.is_sold,
            price=c.price, shipping_amount=c.shipping_amount, condition=c.condition,
            location=c.location, observed_date=c.observed_date,
            similarities=c.similarities, differences=c.differences,
            relevance=c.relevance, is_placeholder=c.is_placeholder,
            price_type=c.price_type, needs_confirmation=c.needs_confirmation, source=c.source,
        )
    ItemRepository(session).events.record(
        item_id, "comps_imported", actor="researcher", count=len(comps),
        problems=problems[:10],
    )
    return len(comps), problems


def review_queue(session) -> list:
    repo = ItemRepository(session)
    order = {s: i for i, s in enumerate(
        [ItemStatus.NEEDS_REVIEW.value, ItemStatus.DRAFT.value, ItemStatus.APPROVED.value,
         ItemStatus.READY_TO_LIST.value, ItemStatus.LISTED.value]
    )}
    items = repo.all()
    return sorted(items, key=lambda i: (order.get(i.status, 99), i.item_id))
