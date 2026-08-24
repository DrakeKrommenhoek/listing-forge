"""Transparent priority scoring and the inventory views built on top of it.

Why a formula and not a model
-----------------------------
Priority decides what a human spends their limited evenings on before
August 31. If that ordering is a black box, it stops being trusted the first
time it puts a $15 lamp above a $400 dresser, and then the whole inventory
gets ignored in favour of a mental list. So the score is a small, additive,
fully explained formula: every point that is added or removed writes its own
sentence into ``reasons``, and ``explain()`` reproduces the arithmetic.

The score is 0-100. Higher means "work on this first".

The formula
-----------
Start at 0 and add:

1. **Value** (0-40). The single biggest term, because the point of the
   exercise is money. Log-scaled on expected net proceeds so the gap between
   $20 and $80 matters more than the gap between $600 and $700 -- diminishing
   returns are real, and a linear term makes one antique drown out forty
   sellable items.
2. **Readiness** (0-20). How close the item is to actually earning: an item
   ready for review is one decision from being listed, so it outranks an item
   that has not been identified yet at equal value. Already-listed items score
   low here on purpose -- they are working without you.
3. **Confidence** (0-15). Pricing you can defend converts faster. Weak
   evidence is not a reason to ignore an item, so this term is small.
4. **Ease of sale** (0-10). Easy pickup, shippable, mainstream category.
5. **Urgency** (0-15). Ramps as the move-out deadline approaches, and only
   for items that are not yet sold or removed. Past the deadline it saturates.

Then subtract:

6. **Stall penalty** (0-10). An item that has sat listed with no inquiries is
   telling you the price is wrong, which is a markdown decision, not a
   "work on it" decision.

Deliberately *not* in the formula: how long the item has been in the system
(that is what the stall term measures, and only once it is listed), and
anything derived from a model's opinion of desirability.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from estate.schema import (
    APPROVED_STATUSES,
    ItemStatus,
    PricingConfidence,
    ProcessingStage,
)

#: Maximum points each term can contribute. Changing these changes the
#: ordering; they are here rather than inline so a tuning decision is one
#: visible edit with a test behind it.
WEIGHTS = {
    "value": 40.0,
    "readiness": 20.0,
    "confidence": 15.0,
    "ease": 10.0,
    "urgency": 15.0,
    "stall_penalty": 10.0,
}

#: Net proceeds at which the value term saturates. Above this, extra dollars
#: stop buying priority -- a $2,000 item and a $900 item both simply go first.
VALUE_SATURATION_USD = 800.0

#: Readiness points by processing stage. An item waiting on the OWNER scores
#: above one waiting on the pipeline, because the owner is the bottleneck the
#: system cannot clear by itself.
READINESS_BY_STAGE = {
    ProcessingStage.READY_FOR_REVIEW.value: 1.00,
    ProcessingStage.APPROVED.value: 0.90,
    ProcessingStage.READY_TO_PUBLISH.value: 0.90,
    ProcessingStage.NEEDS_INFORMATION.value: 0.75,
    ProcessingStage.ERROR.value: 0.70,
    ProcessingStage.GENERATING_LISTINGS.value: 0.55,
    ProcessingStage.PRICING.value: 0.50,
    ProcessingStage.RESEARCHING.value: 0.45,
    ProcessingStage.IDENTIFYING.value: 0.30,
    ProcessingStage.PHOTOS_RECEIVED.value: 0.30,
    ProcessingStage.LISTED.value: 0.20,
    ProcessingStage.SOLD.value: 0.0,
    ProcessingStage.REMOVED.value: 0.0,
}

CONFIDENCE_POINTS = {
    PricingConfidence.HIGH.value: 1.0,
    PricingConfidence.MEDIUM.value: 0.7,
    PricingConfidence.LOW.value: 0.35,
    PricingConfidence.INSUFFICIENT.value: 0.0,
}

#: Statuses where priority is meaningless -- the item is off the board.
CLOSED_STATUSES = {
    ItemStatus.SOLD.value,
    ItemStatus.DONATED.value,
    ItemStatus.REMOVED.value,
}


@dataclass
class PriorityScore:
    score: float = 0.0
    terms: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)

    def explain(self) -> str:
        """The arithmetic, reproduced. This is what gets stored on the item."""
        lines = [f"Priority {self.score:.0f}/100"]
        for name, value in self.terms.items():
            sign = "+" if value >= 0 else ""
            lines.append(f"  {sign}{value:.1f}  {name}")
        lines.extend("  - " + r for r in self.reasons)
        return "\n".join(lines)


def _days_until(deadline, today: date | None = None) -> int | None:
    if not deadline:
        return None
    today = today or date.today()
    if isinstance(deadline, datetime):
        deadline = deadline.date()
    if isinstance(deadline, date):
        return (deadline - today).days
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return (datetime.strptime(str(deadline).strip(), fmt).date() - today).days
        except (ValueError, TypeError):
            continue
    return None


def days_until_move_out(item, today: date | None = None) -> int | None:
    """Days left before the item has to be gone. None when no deadline is set."""
    from estate.settings import move_out_date as configured

    return _days_until(getattr(item, "move_out_deadline", "") or configured(), today)


def expected_value(item) -> float:
    """The money figure priority is built on, in order of trustworthiness.

    Never invents a number: an item with no pricing evidence contributes 0 to
    the value term rather than a guess, which is why an unpriced item still
    surfaces -- via the readiness and urgency terms, not a fabricated price.
    """
    for attr in ("expected_net_proceeds", "expected_sale_price", "current_price",
                 "initial_list_price", "comp_median"):
        value = getattr(item, attr, None)
        if value:
            return float(value)
    return 0.0


def selling_difficulty(item) -> str:
    """Easy / Moderate / Hard, from buyer pool and evidence quality."""
    points = 0
    if not (getattr(item, "brand", "") or getattr(item, "model", "")):
        points += 1  # unbranded goods are harder to search for and harder to trust
    if getattr(item, "pricing_confidence", "") in (
        PricingConfidence.LOW.value, PricingConfidence.INSUFFICIENT.value
    ):
        points += 1
    if (getattr(item, "condition", "") or "Unknown") in ("Fair", "Poor", "For Parts / Repair"):
        points += 1
    if getattr(item, "pickup_difficulty", "") in ("Hard", "Specialist Movers"):
        points += 1
    price = expected_value(item)
    if price and price >= 400:
        points += 1  # a bigger cheque means a smaller pool of willing buyers
    return "Easy" if points <= 1 else ("Moderate" if points <= 2 else "Hard")


def shipping_difficulty(item) -> str:
    """How much of the buyer pool is reachable beyond driving distance."""
    if not bool(getattr(item, "shipping_feasible", False)):
        return "Local Only"
    weight = float(getattr(item, "weight_lbs", None) or 0)
    if weight and weight > 50:
        return "Local Only"
    if weight and weight > 20:
        return "Ships With Effort"
    if int(getattr(item, "people_required", 1) or 1) >= 2:
        return "Ships With Effort"
    return "Ships Easily"


def score_item(item, today: date | None = None) -> PriorityScore:
    """Compute the 0-100 priority score and the reasons behind every term."""
    out = PriorityScore()
    today = today or date.today()
    status = getattr(item, "status", "") or ""
    stage = getattr(item, "processing_stage", "") or ProcessingStage.PHOTOS_RECEIVED.value

    if status in CLOSED_STATUSES:
        out.reasons.append(f"{status} — no longer needs attention.")
        out.terms["closed"] = 0.0
        return out

    # 1. Value ---------------------------------------------------------------
    value = expected_value(item)
    if value > 0:
        ratio = math.log1p(min(value, VALUE_SATURATION_USD)) / math.log1p(VALUE_SATURATION_USD)
        term = round(WEIGHTS["value"] * ratio, 1)
        out.reasons.append(f"Expected value ${value:.0f}.")
    else:
        term = 0.0
        out.reasons.append("No pricing evidence yet, so value contributes nothing.")
    out.terms["value"] = term

    # 2. Readiness -----------------------------------------------------------
    readiness = READINESS_BY_STAGE.get(stage, 0.3)
    out.terms["readiness"] = round(WEIGHTS["readiness"] * readiness, 1)
    if stage == ProcessingStage.NEEDS_INFORMATION.value:
        out.reasons.append("Waiting on an answer from you — the system cannot move it alone.")
    elif stage == ProcessingStage.READY_FOR_REVIEW.value:
        out.reasons.append("One review away from being listable.")
    elif stage == ProcessingStage.LISTED.value:
        out.reasons.append("Already listed — it is working without you.")

    # 3. Confidence ----------------------------------------------------------
    confidence = CONFIDENCE_POINTS.get(getattr(item, "pricing_confidence", "") or "", 0.0)
    out.terms["confidence"] = round(WEIGHTS["confidence"] * confidence, 1)
    if confidence == 0.0:
        out.reasons.append("Pricing is not yet defensible; evidence is the next job.")

    # 4. Ease of sale --------------------------------------------------------
    ease_map = {"Easy": 1.0, "Moderate": 0.55, "Hard": 0.2}
    ease = ease_map.get(selling_difficulty(item), 0.55)
    out.terms["ease"] = round(WEIGHTS["ease"] * ease, 1)

    # 5. Urgency -------------------------------------------------------------
    days_left = days_until_move_out(item, today)
    if days_left is None:
        out.terms["urgency"] = 0.0
        out.reasons.append("No move-out deadline configured, so urgency is not scored.")
    else:
        # 60+ days out: nothing. Ramps linearly to full at the deadline and
        # stays there afterwards -- an overdue item never becomes less urgent.
        ramp = 1.0 if days_left <= 0 else max(0.0, min(1.0, (60 - days_left) / 60.0))
        out.terms["urgency"] = round(WEIGHTS["urgency"] * ramp, 1)
        if days_left <= 0:
            out.reasons.append("Past the move-out date — it has to go.")
        elif days_left <= 14:
            out.reasons.append(f"{days_left} day(s) to move-out.")

    # 6. Stall penalty -------------------------------------------------------
    listed_on = getattr(item, "listed_on", "") or ""
    days_listed = None
    if listed_on:
        gone = _days_until(listed_on, today)
        days_listed = -gone if gone is not None else None
    if days_listed and days_listed >= 14 and not int(getattr(item, "inquiry_count", 0) or 0):
        out.terms["stall_penalty"] = -WEIGHTS["stall_penalty"]
        out.reasons.append(
            f"Listed {days_listed} days with no inquiries — this needs a markdown, "
            "not more work."
        )
    elif int(getattr(item, "inquiry_count", 0) or 0) >= 1 and status in APPROVED_STATUSES:
        out.terms["buyer_interest"] = 5.0
        out.reasons.append(
            "%d buyer inquiry(ies) — a live conversation beats a cold listing."
            % int(item.inquiry_count)
        )

    out.score = round(max(0.0, min(100.0, sum(out.terms.values()))), 1)
    return out


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _has_blockers(item) -> bool:
    return bool((getattr(item, "approval_blockers", "") or "").strip())


#: name -> predicate. Every view is a plain filter over the inventory so the
#: same list can be produced by the CLI, the review UI, and a Telegram reply
#: without three copies of the logic drifting apart.
VIEWS = {
    "top_value": lambda i: i.status not in CLOSED_STATUSES and expected_value(i) > 0,
    "high_value_not_ready": lambda i: (
        i.status not in CLOSED_STATUSES
        and expected_value(i) >= 150
        and i.status not in APPROVED_STATUSES
    ),
    "quick_wins": lambda i: (
        i.status not in CLOSED_STATUSES
        and selling_difficulty(i) == "Easy"
        and expected_value(i) >= 40
    ),
    "needs_information": lambda i: (
        i.processing_stage == ProcessingStage.NEEDS_INFORMATION.value
    ),
    "needs_research": lambda i: (
        i.status not in CLOSED_STATUSES
        and (i.research_status or "") in (
            "Not Started", "Queued for Manual Research", "In Progress",
            "Needs More Evidence", "Failed",
        )
    ),
    "ready_for_review": lambda i: (
        i.processing_stage == ProcessingStage.READY_FOR_REVIEW.value
        and i.status not in APPROVED_STATUSES
    ),
    "approved_not_listed": lambda i: (
        i.status in (ItemStatus.APPROVED.value, ItemStatus.READY_TO_LIST.value)
    ),
    "needs_markdown": lambda i: (
        i.status == ItemStatus.LISTED.value
        and bool(i.next_markdown_date)
        and (_days_until(i.next_markdown_date) or 1) <= 0
    ),
    "bundle_candidates": lambda i: (
        i.status not in CLOSED_STATUSES and 0 < expected_value(i) < 25
    ),
    "donation_candidates": lambda i: (
        i.status not in CLOSED_STATUSES
        and expected_value(i) > 0
        and expected_value(i) < 15
        and selling_difficulty(i) == "Hard"
    ),
    "processing_failed": lambda i: i.processing_stage == ProcessingStage.ERROR.value,
    "at_risk": lambda i: (
        i.status not in CLOSED_STATUSES
        and i.status not in APPROVED_STATUSES
        and (days_until_move_out(i) is not None and days_until_move_out(i) <= 14)
    ),
}

VIEW_LABELS = {
    "top_value": "Highest expected net proceeds",
    "high_value_not_ready": "High-value items not yet ready",
    "quick_wins": "Quick wins",
    "needs_information": "Needs information from the owner",
    "needs_research": "Needs research",
    "ready_for_review": "Ready for review",
    "approved_not_listed": "Approved but not listed",
    "needs_markdown": "Needs markdown",
    "bundle_candidates": "Low-value items to bundle",
    "donation_candidates": "Donation or removal candidates",
    "processing_failed": "Processing failed — needs a human",
    "at_risk": "At risk of missing the move-out date",
}


def view(items: list, name: str, limit: int = 0) -> list:
    """Items matching a named view, highest priority first."""
    predicate = VIEWS.get(name)
    if predicate is None:
        return []
    selected = [i for i in items if predicate(i)]
    selected.sort(key=lambda i: float(getattr(i, "priority_score", 0) or 0), reverse=True)
    return selected[:limit] if limit else selected


def ranked(items: list, limit: int = 0) -> list:
    """The whole open inventory, highest priority first."""
    open_items = [i for i in items if (getattr(i, "status", "") or "") not in CLOSED_STATUSES]
    open_items.sort(
        key=lambda i: (float(getattr(i, "priority_score", 0) or 0), expected_value(i)),
        reverse=True,
    )
    return open_items[:limit] if limit else open_items
