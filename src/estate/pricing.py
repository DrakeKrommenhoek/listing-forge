"""Pricing, pickup incentive, and markdown engine.

Three hard rules, enforced in code rather than in documentation:

1. A price is never derived from a model's opinion — only from the comparable
   set produced by ``research.py``.
2. ``current_price`` can never fall below ``floor_price``. Every function that
   lowers a price clamps against the floor.
3. Nothing here publishes anything. These are recommendations that a human
   approves in ``approval.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from estate.schema import PricingConfidence
from estate.settings import load_config
from estate.settings import move_out_date as move_out_date_default


def _round_to(value: float, step: int) -> float:
    if step <= 0:
        return round(float(value), 2)
    return float(int(round(float(value) / step)) * step)


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------------------------------
# Price bands
# ---------------------------------------------------------------------------

@dataclass
class PriceRecommendation:
    initial_list_price: float | None = None
    expected_sale_price: float | None = None
    floor_price: float | None = None
    basis: str = ""
    confidence: str = PricingConfidence.INSUFFICIENT.value
    warnings: list = field(default_factory=list)
    publishable: bool = False

    def as_item_fields(self) -> dict:
        return {
            "initial_list_price": self.initial_list_price,
            "expected_sale_price": self.expected_sale_price,
            "floor_price": self.floor_price,
            "current_price": self.initial_list_price,
        }


def recommend_price(summary, cfg: dict | None = None) -> PriceRecommendation:
    """Turn a ResearchSummary into a list/expected/floor triple.

    Returns an empty recommendation (with warnings) when the evidence is too
    thin to price at all. That is a deliberate outcome, not a failure.
    """
    cfg = cfg or load_config()
    b = cfg["price_bands"]
    rec = PriceRecommendation(confidence=summary.confidence)

    if not summary.median or summary.comp_count == 0:
        rec.warnings.append(
            "No comparable evidence — no price can be recommended. "
            "Fill in the comps worksheet or request a specialist review."
        )
        return rec

    median = float(summary.median)
    low = float(summary.low or median)

    list_mult = float(b["initial_list_multiplier"])
    exp_mult = float(b["expected_sale_multiplier"])
    floor_mult = float(
        b["low_confidence_floor_multiplier"]
        if summary.confidence in (PricingConfidence.LOW.value,
                                  PricingConfidence.INSUFFICIENT.value)
        else b["floor_multiplier"]
    )

    step = int(b.get("round_to", 5))
    min_price = float(b.get("min_price", 5))

    rec.initial_list_price = max(min_price, _round_to(median * list_mult, step))
    rec.expected_sale_price = max(min_price, _round_to(median * exp_mult, step))
    # The floor is anchored to the LOWER of (median x floor multiplier) and the
    # cheapest real comparable, so we never floor above what the market clears.
    rec.floor_price = max(min_price, _round_to(min(median * floor_mult, low * 0.9), step))

    if rec.floor_price >= rec.expected_sale_price:
        rec.floor_price = max(min_price, _round_to(rec.expected_sale_price * 0.75, step))

    rec.basis = (
        "median of %d comparable(s): low $%.0f / median $%.0f / high $%.0f; "
        "%d completed sale(s)"
        % (summary.comp_count, summary.low or 0, median, summary.high or 0, summary.sold_count)
    )

    if summary.confidence in (PricingConfidence.LOW.value, PricingConfidence.INSUFFICIENT.value):
        rec.warnings.append(
            f"Pricing confidence is {summary.confidence}. Treat these numbers as a starting point "
            "for discussion, not a valuation."
        )
    if summary.placeholder_count:
        rec.warnings.append(
            "Placeholder comparables present — this price MUST NOT be published."
        )
    if summary.sold_count == 0:
        rec.warnings.append(
            "Derived entirely from asking prices; real clearing prices are "
            "usually lower."
        )
    rec.warnings.extend(summary.gaps)

    rec.publishable = (
        summary.placeholder_count == 0
        and summary.comp_count >= int(cfg["confidence"].get("sample_size_min", 3))
    )
    return rec


# ---------------------------------------------------------------------------
# Pickup incentive
# ---------------------------------------------------------------------------

@dataclass
class PickupIncentive:
    amount: float = 0.0
    pickup_price: float | None = None
    factors: list = field(default_factory=list)
    capped: bool = False

    def explain(self) -> str:
        if not self.amount:
            return "No pickup incentive — this item is easy to move or ship."
        return "Local pickup discount ${:.0f} ({}).".format(self.amount, "; ".join(self.factors))


def _cubic_feet(dimensions: str) -> float:
    """Best-effort volume from a free-text dimension string. 0 if unparseable."""
    import re

    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", dimensions or "")][:3]
    if len(nums) < 3:
        return 0.0
    unit_inches = "cm" not in (dimensions or "").lower()
    vol = nums[0] * nums[1] * nums[2]
    return vol / 1728.0 if unit_inches else vol / 28316.8


def compute_pickup_incentive(
    item,
    current_price: float | None = None,
    stairs: bool = False,
    disassembly: bool = False,
    difficult_access: bool = False,
    avoided_disposal: bool = False,
    urgent: bool = False,
    cfg: dict | None = None,
) -> PickupIncentive:
    """Dollar discount for local pickup, derived from cost genuinely avoided.

    The incentive exists because a heavy, awkward item that we would otherwise
    pay to ship, move, or dispose of is worth real money to hand to a buyer who
    carries it away themselves.
    """
    cfg = cfg or load_config()
    p = cfg["pickup_incentive"]
    out = PickupIncentive()
    if not p.get("enabled", True):
        return out

    price = float(current_price if current_price is not None else (item.current_price or 0) or 0)
    amount = float(p.get("base", 0))

    weight = float(getattr(item, "weight_lbs", None) or 0)
    threshold = float(p.get("weight_threshold_lbs", 30))
    if weight > threshold:
        add = (weight - threshold) * float(p.get("per_lb_over_30", 0.35))
        amount += add
        out.factors.append(f"{weight:.0f} lb (+${add:.0f})")

    cuft = _cubic_feet(getattr(item, "dimensions", "") or "")
    if cuft >= float(p.get("oversize_cuft_threshold", 8)):
        amount += float(p["oversize_bonus"])
        out.factors.append("oversize {:.0f} cu ft (+${:.0f})".format(cuft, p["oversize_bonus"]))

    if stairs:
        amount += float(p["stairs_bonus"])
        out.factors.append("stairs (+${:.0f})".format(p["stairs_bonus"]))
    if disassembly:
        amount += float(p["disassembly_bonus"])
        out.factors.append("needs disassembly (+${:.0f})".format(p["disassembly_bonus"]))
    if int(getattr(item, "people_required", 1) or 1) >= 2:
        amount += float(p["two_person_bonus"])
        out.factors.append("two-person lift (+${:.0f})".format(p["two_person_bonus"]))
    if "truck" in (getattr(item, "required_vehicle", "") or "").lower():
        amount += float(p["truck_required_bonus"])
        out.factors.append("truck required (+${:.0f})".format(p["truck_required_bonus"]))
    if difficult_access:
        amount += float(p["difficult_access_bonus"])
        out.factors.append("difficult access (+${:.0f})".format(p["difficult_access_bonus"]))
    if avoided_disposal:
        amount += float(p["avoided_disposal_bonus"])
        out.factors.append("avoids disposal fee (+${:.0f})".format(p["avoided_disposal_bonus"]))
    if urgent:
        amount += float(p["urgency_bonus"])
        out.factors.append("removal urgency (+${:.0f})".format(p["urgency_bonus"]))

    cap_pct = float(p.get("max_pct_of_price", 0.25))
    cap_abs = float(p.get("max_absolute", 150))
    cap = min(cap_abs, price * cap_pct) if price else cap_abs
    if amount > cap:
        amount = cap
        out.capped = True
        out.factors.append(f"capped at ${cap:.0f}")

    out.amount = max(0.0, _round_to(amount, int(p.get("round_to", 5))))

    floor = float(getattr(item, "floor_price", None) or 0)
    if price:
        candidate = price - out.amount
        if floor and candidate < floor:
            # Never let the pickup price break the floor.
            out.amount = max(0.0, _round_to(price - floor, int(p.get("round_to", 5))))
            candidate = price - out.amount
            out.capped = True
        out.pickup_price = max(0.0, candidate)
    return out


# ---------------------------------------------------------------------------
# Markdown engine
# ---------------------------------------------------------------------------

@dataclass
class MarkdownDecision:
    should_mark_down: bool = False
    new_price: float | None = None
    step_pct: float = 0.0
    total_markdown_pct: float = 0.0
    next_markdown_date: str = ""
    reasons: list = field(default_factory=list)
    at_floor: bool = False


def evaluate_markdown(
    item,
    listed_on=None,
    today: date | None = None,
    move_out_date=None,
    cfg: dict | None = None,
) -> MarkdownDecision:
    """Decide whether today is a markdown day and by how much.

    Signals: days listed, inquiries, offers, item value, pickup difficulty,
    pricing confidence, and how close the move-out deadline is.
    """
    cfg = cfg or load_config()
    m = cfg["markdown"]
    mods = m.get("modifiers", {})
    d = MarkdownDecision()

    if not m.get("enabled", True):
        d.reasons.append("Markdowns disabled in config.")
        return d

    today = today or date.today()
    current = float(getattr(item, "current_price", None) or 0)
    initial = float(getattr(item, "initial_list_price", None) or current)
    floor = float(getattr(item, "floor_price", None) or 0)

    if not current:
        d.reasons.append("No current price set.")
        return d
    if floor and current <= floor:
        d.at_floor = True
        d.reasons.append(f"Already at the floor price of ${floor:.0f} — no further markdown.")
        return d

    listed = _parse_date(listed_on)
    if listed is None:
        d.reasons.append("Item has no listing date; markdown clock has not started.")
        return d

    days_listed = (today - listed).days
    first_after = int(m.get("first_markdown_after_days", 10))
    interval = max(1, int(m.get("interval_days", 7)))

    if days_listed < first_after:
        d.next_markdown_date = (listed + timedelta(days=first_after)).isoformat()
        d.reasons.append(
            "Listed %d day(s); first markdown is at day %d." % (days_listed, first_after)
        )
        return d

    cycles = 1 + (days_listed - first_after) // interval
    d.next_markdown_date = (
        listed + timedelta(days=first_after + cycles * interval)
    ).isoformat()

    step = float(m.get("base_step_pct", 0.10))
    inquiries = int(getattr(item, "inquiry_count", 0) or 0)
    best_offer = float(getattr(item, "best_offer", None) or 0)

    if inquiries == 0 and cycles >= 2:
        step += float(mods.get("no_inquiries_after_two_cycles", 0))
        d.reasons.append("No inquiries after %d markdown cycles." % cycles)
    if inquiries >= 3 and not best_offer:
        step += float(mods.get("many_inquiries_no_offers", 0))
        d.reasons.append("%d inquiries but no offers — priced above interest." % inquiries)
    if best_offer and floor and best_offer >= floor:
        step += float(mods.get("has_offer_above_floor", 0))
        d.reasons.append(f"A live offer of ${best_offer:.0f} is above the floor — slow down.")
    if initial >= float(m.get("high_value_threshold", 400)):
        step += float(mods.get("high_value_item", 0))
        d.reasons.append("High-value item — smaller steps protect margin.")
    if getattr(item, "pricing_confidence", "") in (
        PricingConfidence.LOW.value, PricingConfidence.INSUFFICIENT.value
    ):
        step += float(mods.get("low_confidence_pricing", 0))
        d.reasons.append("Low pricing confidence — the market will tell us faster.")
    if getattr(item, "pickup_difficulty", "") in ("Hard", "Specialist Movers"):
        step += float(mods.get("hard_pickup", 0))
        d.reasons.append("Hard pickup narrows the buyer pool.")

    # An explicit argument wins; otherwise fall back to the configured hard
    # deadline so urgency is never silently switched off by a missing env var.
    deadline = _parse_date(move_out_date or move_out_date_default())
    if deadline:
        days_left = (deadline - today).days
        urgent_bump = float(mods.get("urgent_deadline", 0))
        if days_left <= int(m.get("deadline_endgame_days", 7)):
            # The endgame must never be gentler than the merely-urgent band, so
            # the urgent bump applies first and the endgame percentage acts as a
            # floor on top of it, not as a replacement for it.
            step += urgent_bump
            step = max(step, float(m.get("deadline_endgame_step_pct", 0.20)))
            d.reasons.append(
                "%d day(s) to move-out — endgame pricing." % max(days_left, 0)
            )
        elif days_left <= int(m.get("urgent_deadline_days", 21)):
            step += urgent_bump
            d.reasons.append("%d day(s) to move-out — accelerating." % days_left)

    step = max(0.0, step)
    d.step_pct = round(step, 4)

    candidate = current * (1.0 - step)
    total_pct = 1.0 - (candidate / initial) if initial else 0.0
    max_total = float(m.get("max_total_markdown_pct", 0.55))
    if total_pct > max_total:
        # The cap limits how far below the ORIGINAL list price we go. Never let
        # it raise the price: if the current price already sits below the cap
        # line (usually after a manual edit), the floor becomes the only
        # binding constraint and we simply hold.
        candidate = min(initial * (1.0 - max_total), current)
        d.reasons.append("Clamped to the %.0f%% maximum total markdown." % (max_total * 100))

    if floor and candidate < floor:
        candidate = floor
        d.at_floor = True
        d.reasons.append(f"Clamped to the floor price of ${floor:.0f}.")

    candidate = _round_to(candidate, int(cfg["price_bands"].get("round_to", 5)))
    if floor:
        candidate = max(candidate, floor)

    if candidate >= current:
        d.reasons.append("Computed markdown would not lower the price; holding.")
        return d

    d.should_mark_down = True
    d.new_price = candidate
    d.total_markdown_pct = round(1.0 - (candidate / initial), 4) if initial else 0.0
    return d


def estimated_net_proceeds(price: float, platform_key: str, shipping_cost: float = 0.0,
                           cfg: dict | None = None) -> float:
    """Rough net after platform fee. Fee table is unverified — see config."""
    cfg = cfg or load_config()
    fee_rate = float(cfg.get("fees", {}).get(platform_key, 0.0) or 0.0)
    return round(max(0.0, float(price) * (1.0 - fee_rate) - float(shipping_cost or 0)), 2)


def price_display(item, incentive: PickupIncentive | None = None) -> dict:
    """The four numbers a reviewer and a buyer need to see together."""
    current = float(getattr(item, "current_price", None) or 0)
    floor = float(getattr(item, "floor_price", None) or 0)
    inc = incentive.amount if incentive else float(getattr(item, "pickup_incentive", 0) or 0)
    return {
        "standard_price": current,
        "local_pickup_price": max(floor, current - inc) if current else None,
        "floor_price": floor,
        "next_markdown_date": getattr(item, "next_markdown_date", "") or "not scheduled",
        "pickup_incentive": inc,
    }
