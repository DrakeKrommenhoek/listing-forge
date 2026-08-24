"""Comparable-price research.

Design position
---------------
This module does NOT invent market evidence. It cannot: there is no code path
that produces a comparable without a URL supplied by a human or a real API.

The MVP flow is human-in-the-loop:

1. ``build_queries()`` produces targeted, platform-specific search strings.
2. ``write_worksheet()`` writes a CSV the researcher fills in from real search
   results (sold/completed listings preferred).
3. ``import_worksheet()`` reads it back, validating every row.
4. ``summarise()`` computes low/median/high, sample size, and a confidence
   score from evidence quality — never from model belief.

``providers`` is the extension point. An eBay Browse/Marketplace-Insights
adapter or a search-API adapter can be registered later without touching the
rest of the pipeline; the worksheet path stays as the fallback.
"""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from estate.paths import item_dir
from estate.schema import PRICE_TYPES, PriceType, PricingConfidence
from estate.settings import load_config

WORKSHEET_COLUMNS = [
    "platform",
    "title",
    "url",
    "sold_or_active",
    "price",
    "price_type",
    "shipping_amount",
    "condition",
    "location",
    "observed_date",
    "similarities",
    "differences",
    "relevance_0_to_1",
    "is_placeholder",
]

#: Platforms whose *sold* data is meaningful for a given category. Used only to
#: order the generated search queries, not to assert anything about the market.
CATEGORY_QUERY_SITES = {
    "Furniture": ["facebook marketplace", "craigslist", "chairish", "ebay"],
    "Appliances": ["facebook marketplace", "craigslist", "offerup"],
    "Electronics": ["ebay sold", "swappa", "facebook marketplace"],
    "Audio / Music Gear": ["reverb sold", "ebay sold", "facebook marketplace"],
    "Tools & Equipment": ["ebay sold", "facebook marketplace", "craigslist"],
    "Outdoor & Garden": ["facebook marketplace", "craigslist", "offerup"],
    "Kitchen & Dining": ["ebay sold", "facebook marketplace"],
    "Home Decor": ["ebay sold", "chairish", "etsy sold"],
    "Art & Collectibles": ["ebay sold", "worthpoint", "heritage auctions"],
    "Books & Media": ["ebay sold", "discogs", "abebooks"],
    "Clothing & Accessories": ["ebay sold", "poshmark sold", "depop", "grailed sold"],
    "Jewelry & Watches": ["ebay sold", "chrono24", "worthpoint"],
    "Sporting Goods": ["ebay sold", "facebook marketplace", "sidelineswap"],
    "Toys & Games": ["ebay sold", "facebook marketplace"],
    "Office & Storage": ["facebook marketplace", "ebay sold"],
    "Vehicles & Trailers": ["facebook marketplace", "craigslist"],
    "Other": ["ebay sold", "facebook marketplace"],
}


@dataclass
class Comparable:
    platform: str = ""
    title: str = ""
    url: str = ""
    is_sold: bool = False
    price: float = 0.0
    shipping_amount: float = 0.0
    condition: str = "Unknown"
    location: str = ""
    observed_date: str = ""
    similarities: str = ""
    differences: str = ""
    relevance: float = 0.5
    is_placeholder: bool = False
    #: See schema.PriceType. EXACT is the only type whose ``price`` is usable
    #: as pricing evidence at face value -- HIDDEN (Best Offer accepted),
    #: ESTIMATED, and UPPER_BOUND all mean the number is not a confirmed sale
    #: price and must never be blended into the low/median/high the same way.
    price_type: str = PriceType.EXACT.value
    #: True for comparables an automated research provider proposed but a
    #: human has not yet confirmed. Mirrors EstateCompORM.needs_confirmation.
    needs_confirmation: bool = False
    source: str = "manual"

    def __post_init__(self) -> None:
        if self.price_type not in PRICE_TYPES:
            self.price_type = PriceType.EXACT.value

    @property
    def has_known_price(self) -> bool:
        """False for HIDDEN comps: the number on the page is not what the
        buyer paid, so it cannot contribute to a price range at all."""
        return self.price_type != PriceType.HIDDEN.value

    @property
    def total_price(self) -> float:
        """Price a buyer actually paid, including shipping.

        Meaningless for HIDDEN comparables (see has_known_price) -- callers
        computing a price range must filter those out first rather than
        relying on this property to do it for them, since a total_price of
        0.0 would otherwise silently drag down a low/median calculation.
        """
        return round(float(self.price or 0) + float(self.shipping_amount or 0), 2)

    def age_days(self, today: date | None = None) -> int | None:
        if not self.observed_date:
            return None
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                d = datetime.strptime(self.observed_date.strip(), fmt).date()
                return max(0, ((today or date.today()) - d).days)
            except ValueError:
                continue
        return None


@dataclass
class ResearchSummary:
    item_id: str = ""
    comp_count: int = 0
    low: float | None = None
    median: float | None = None
    high: float | None = None
    confidence: str = PricingConfidence.INSUFFICIENT.value
    confidence_score: float = 0.0
    sold_count: int = 0
    active_count: int = 0
    placeholder_count: int = 0
    #: Comps whose recorded price is not usable as pricing evidence -- almost
    #: always a listing that sold via an accepted Best Offer, where the page
    #: only ever shows the original (unpaid) asking price. See
    #: schema.PriceType.HIDDEN. Excluded from low/median/high entirely.
    hidden_price_count: int = 0
    sources: list = field(default_factory=list)
    research_date: str = ""
    gaps: list = field(default_factory=list)
    recommend_specialist: bool = False
    notes: str = ""

    def as_item_fields(self) -> dict:
        return {
            "comp_low": self.low,
            "comp_median": self.median,
            "comp_high": self.high,
            "comp_count": self.comp_count,
            "sold_comp_count": self.sold_count,
            "comp_sources": self.sources,
            "research_date": self.research_date,
            "pricing_confidence": self.confidence,
        }


# ---------------------------------------------------------------------------
# 1. Query generation
# ---------------------------------------------------------------------------

def build_queries(item, max_queries: int = 8) -> list:
    """Targeted search strings, most specific first.

    ``item`` may be an EstateItemORM or any object with the same attributes.
    """
    brand = (getattr(item, "brand", "") or "").strip()
    model = (getattr(item, "model", "") or "").strip()
    name = (getattr(item, "item_name", "") or "").strip()
    category = (getattr(item, "category", "") or "Other").strip()
    condition = (getattr(item, "condition", "") or "").strip()

    core = " ".join(x for x in (brand, model) if x) or name
    queries: list = []

    def add(q: str) -> None:
        q = " ".join(q.split())
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)

    sku = (getattr(item, "sku", "") or "").strip()
    collection = (getattr(item, "collection", "") or "").strip()

    # A SKU is the strongest identifier there is: it either finds the exact
    # product or it finds nothing, and both answers are useful. It goes first.
    if sku:
        add(f"{brand} {sku}".strip())
        add(f"{sku} sold")
    if brand and model:
        add(f"{brand} {model} sold listings")
        add(f"{brand} {model} completed sold price")
        add(f"site:ebay.com {brand} {model} sold")
    # Sellers list by collection far more consistently than by SKU or model
    # number, so this is usually where the actual sold comparables turn up.
    if brand and collection:
        add(f"{brand} {collection} sold")
    if core and condition and condition != "Unknown":
        add(f"{core} {condition.lower()} condition used price")
    if core:
        add(f"{core} used resale value")
    if name and name != core:
        add(f"{name} {brand} used for sale")

    for site in CATEGORY_QUERY_SITES.get(category, CATEGORY_QUERY_SITES["Other"]):
        add(f"{core or name} {site}")

    return queries[:max_queries]


# ---------------------------------------------------------------------------
# 2/3. Worksheet round-trip
# ---------------------------------------------------------------------------

def worksheet_path(item_id: str) -> Path:
    return item_dir(item_id) / "research" / (f"{item_id}_comps.csv")


def write_worksheet(item, queries: list | None = None) -> Path:
    """Write an empty, self-documenting comps worksheet for a human to fill."""
    item_dir(item.item_id, create=True)
    path = worksheet_path(item.item_id)
    queries = queries if queries is not None else build_queries(item)

    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow([f"# COMPARABLES WORKSHEET — {item.item_id} — {item.item_name}"])
        w.writerow(["# Fill one row per real listing you find. Do not invent rows."])
        w.writerow(["# sold_or_active: 'sold' (completed sale) or 'active' (asking price)."])
        w.writerow(["# Sold evidence is worth far more than asking prices."])
        w.writerow(["# price_type: 'exact' (default -- the shown price is the real sale price),"])
        w.writerow(["#   'hidden' (sold via an ACCEPTED BEST OFFER -- the real price is not"])
        w.writerow(["#   shown anywhere; put the asking price here anyway but it will be"])
        w.writerow(["#   excluded from the price range), 'estimated' (a plausible figure with"])
        w.writerow(["#   no listing page to point to), or 'upper_bound' (an active asking"])
        w.writerow(["#   price -- a ceiling, not a result). Leave blank for 'exact'."])
        w.writerow(["# relevance_0_to_1: 1.0 = same model, same condition. 0.3 = loosely similar."])
        w.writerow(["# is_placeholder: leave blank. Set to 'yes' ONLY for demo/mock rows."])
        w.writerow(["# Suggested searches:"])
        for q in queries:
            w.writerow(["#   " + q])
        w.writerow([])
        w.writerow(WORKSHEET_COLUMNS)
    return path


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in {"y", "yes", "true", "1", "sold", "x"}


def import_worksheet(path: Path | str) -> tuple:
    """Read a filled worksheet. Returns (comparables, problems)."""
    path = Path(path)
    comps: list = []
    problems: list = []
    if not path.exists():
        return comps, [f"worksheet not found: {path}"]

    with path.open("r", newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh)
                if r and not (r[0] or "").strip().startswith("#") and any(c.strip() for c in r)]

    if not rows:
        return comps, ["worksheet is empty"]
    header = [h.strip().lower() for h in rows[0]]
    if header[:3] != WORKSHEET_COLUMNS[:3]:
        return comps, ["worksheet header does not match the expected columns"]

    for n, row in enumerate(rows[1:], start=2):
        rec = dict(zip(header, [c.strip() for c in row] + [""] * len(header)))
        if not rec.get("url") and not _truthy(rec.get("is_placeholder", "")):
            problems.append("row %d: no URL — a comparable without a source is not evidence" % n)
            continue
        try:
            price = float(str(rec.get("price", "")).replace("$", "").replace(",", "") or 0)
        except ValueError:
            problems.append("row %d: price is not a number" % n)
            continue
        if price <= 0:
            problems.append("row %d: price must be greater than zero" % n)
            continue
        try:
            shipping = float(str(rec.get("shipping_amount", "")).replace("$", "") or 0)
        except ValueError:
            shipping = 0.0
        try:
            relevance = float(rec.get("relevance_0_to_1") or 0.5)
        except ValueError:
            relevance = 0.5

        price_type = (rec.get("price_type", "") or "").strip().lower() or PriceType.EXACT.value
        if price_type not in PRICE_TYPES:
            problems.append(
                "row %d: unknown price_type %r, treated as 'exact' -- expected one of %s"
                % (n, price_type, ", ".join(PRICE_TYPES))
            )
            price_type = PriceType.EXACT.value

        comps.append(
            Comparable(
                platform=rec.get("platform", ""),
                title=rec.get("title", ""),
                url=rec.get("url", ""),
                is_sold=_truthy(rec.get("sold_or_active", "")),
                price=price,
                shipping_amount=shipping,
                condition=rec.get("condition", "Unknown") or "Unknown",
                location=rec.get("location", ""),
                observed_date=rec.get("observed_date", ""),
                similarities=rec.get("similarities", ""),
                differences=rec.get("differences", ""),
                relevance=max(0.0, min(1.0, relevance)),
                is_placeholder=_truthy(rec.get("is_placeholder", "")),
                price_type=price_type,
                source="manual",
            )
        )
    return comps, problems


# ---------------------------------------------------------------------------
# 3b. External research job contract
# ---------------------------------------------------------------------------
#
# The machine-readable half of the worksheet. A worksheet is for a person; a
# job file is for whatever automated researcher gets built or hired later --
# an eBay API client, an agentic browser, a contractor's script. Both end up
# in the same place, through the same validation, with the same
# needs_confirmation flag, so no future provider gets a private back door
# into the evidence table.

#: What the research job asks for, in priority order. Written into every job
#: file so the researcher (human or not) is aiming at the same targets the
#: pricing engine actually rewards, rather than whatever is easiest to find.
RESEARCH_TARGETS = [
    "manufacturer product page for the exact model",
    "archived manufacturer page (web.archive.org) if the product is discontinued",
    "exact-model listings with a CONFIRMED completed sale and a visible sale price",
    "exact-model completed listings where the sale price is hidden (accepted Best Offer)",
    "exact-model ACTIVE listings (asking prices — ceilings, not results)",
    "same-collection or same-series completed sales",
    "closely related same-brand completed sales",
    "original retail price, with a source",
    "current platform fees for the likely selling channel",
    "shipping or freight constraints for an item this size and weight",
]

#: Every field an imported comparable may carry. Anything else in the file is
#: ignored rather than silently stored.
RESULT_FIELDS = [
    "platform", "title", "url", "sold_or_active", "price_type", "price",
    "shipping_amount", "condition", "observed_date", "location",
    "similarities", "differences", "relevance_0_to_1", "source_quality",
    "research_notes",
]


def research_job_path(item_id: str) -> Path:
    return item_dir(item_id) / "research" / (f"{item_id}_research_job.json")


def research_results_path(item_id: str) -> Path:
    return item_dir(item_id) / "research" / (f"{item_id}_research_results.json")


def write_research_job(item, queries: list | None = None) -> Path:
    """Describe the evidence this item needs, in a form a machine can consume."""
    item_dir(item.item_id, create=True)
    path = research_job_path(item.item_id)
    payload = {
        "schema_version": 1,
        "item_id": item.item_id,
        "created": date.today().isoformat(),
        "item": {
            "item_name": getattr(item, "item_name", ""),
            "brand": getattr(item, "brand", ""),
            "model": getattr(item, "model", ""),
            "sku": getattr(item, "sku", ""),
            "manufacturer": getattr(item, "manufacturer", ""),
            "category": getattr(item, "category", ""),
            "condition": getattr(item, "condition", ""),
            "dimensions": getattr(item, "dimensions", ""),
            "approximate_age": getattr(item, "approximate_age", ""),
        },
        "targets": RESEARCH_TARGETS,
        "queries": queries if queries is not None else build_queries(item),
        "result_fields": RESULT_FIELDS,
        "rules": [
            "Never invent a listing. An empty result is a correct result.",
            "Every comparable must carry a real, working URL to the listing page.",
            "sold_or_active must be 'sold' only for a CONFIRMED completed sale.",
            "An accepted Best Offer is price_type 'hidden': the shown price is "
            "not what was paid and must not be treated as the sale price.",
            "An active asking price is price_type 'upper_bound', never 'exact'.",
            "Results are proposals. A human confirms each one before it can "
            "affect an approval.",
        ],
        "results_file": str(research_results_path(item.item_id)),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def import_research_results(path: Path | str) -> tuple:
    """Read an external researcher's results file. Returns (comparables, problems).

    Held to exactly the same standard as ``import_worksheet``, plus two rules
    that only matter for automated sources:

    - an ``active`` row is forced to ``price_type='upper_bound'`` and
      ``is_sold=False`` regardless of what the file says, because an asking
      price is not a result and a provider must not be able to launder one
      into the sold-comparable count;
    - an unrecognised ``price_type`` is downgraded to ``estimated``, not
      promoted to ``exact`` -- an unparseable claim about price quality is
      evidence of low quality, not of high.
    """
    path = Path(path)
    comps: list = []
    problems: list = []
    if not path.exists():
        return comps, [f"results file not found: {path}"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return comps, ["results file is not readable JSON: %s" % type(exc).__name__]

    rows = payload.get("comparables") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return comps, ["results file has no 'comparables' list"]

    for n, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            problems.append("row %d: not an object" % n)
            continue
        url = str(row.get("url", "") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            problems.append(
                "row %d: no usable source URL — a comparable without a source is "
                "not evidence" % n
            )
            continue
        try:
            price = float(str(row.get("price", "")).replace("$", "").replace(",", "") or 0)
        except (ValueError, TypeError):
            problems.append("row %d: price is not a number" % n)
            continue
        if price <= 0:
            problems.append("row %d: price must be greater than zero" % n)
            continue
        try:
            shipping = float(str(row.get("shipping_amount", "")).replace("$", "") or 0)
        except (ValueError, TypeError):
            shipping = 0.0
        try:
            relevance = float(row.get("relevance_0_to_1") or 0.5)
        except (ValueError, TypeError):
            relevance = 0.5

        is_sold = str(row.get("sold_or_active", "")).strip().lower() in ("sold", "yes", "true", "1")
        price_type = str(row.get("price_type", "") or "").strip().lower()
        if price_type not in PRICE_TYPES:
            if price_type:
                problems.append(
                    "row %d: unknown price_type %r, downgraded to 'estimated'"
                    % (n, price_type)
                )
            price_type = PriceType.ESTIMATED.value
        if not is_sold:
            # An asking price is a ceiling. This is not negotiable by the file.
            price_type = PriceType.UPPER_BOUND.value

        notes = " ".join(
            str(row.get(k, "")).strip() for k in ("research_notes", "source_quality")
        ).strip()
        comps.append(
            Comparable(
                platform=str(row.get("platform", "") or ""),
                title=str(row.get("title", "") or ""),
                url=url,
                is_sold=is_sold,
                price=price,
                shipping_amount=shipping,
                condition=str(row.get("condition", "") or "Unknown") or "Unknown",
                location=str(row.get("location", "") or ""),
                observed_date=str(row.get("observed_date", "") or ""),
                similarities=str(row.get("similarities", "") or ""),
                differences=" ".join(
                    x for x in (str(row.get("differences", "") or ""), notes) if x
                ).strip(),
                relevance=max(0.0, min(1.0, relevance)),
                is_placeholder=False,
                price_type=price_type,
                needs_confirmation=True,
                source=str(payload.get("source", "external_job"))
                if isinstance(payload, dict) else "external_job",
            )
        )
    return comps, problems


# ---------------------------------------------------------------------------
# 4. Summary + confidence
# ---------------------------------------------------------------------------

def _condition_match_score(comps: list, item_condition: str) -> float:
    if not comps:
        return 0.0
    target = (item_condition or "").strip().lower()
    if not target or target == "unknown":
        return 0.3
    hits = sum(1 for c in comps if (c.condition or "").strip().lower() == target)
    partial = sum(1 for c in comps if (c.condition or "").strip().lower() not in ("", "unknown"))
    if not partial:
        return 0.2
    return round(min(1.0, (hits + 0.4 * (partial - hits)) / len(comps)), 3)


def score_confidence(comps: list, item_condition: str = "", cfg: dict | None = None) -> tuple:
    """Return (score 0..1, label). Evidence quality only — no model opinion."""
    cfg = cfg or load_config()
    c = cfg["confidence"]
    w = c["weights"]

    if not comps:
        return 0.0, PricingConfidence.INSUFFICIENT.value

    n = len(comps)
    sample = min(1.0, n / float(c.get("sample_size_full_at", 6)))

    similarity = sum(x.relevance for x in comps) / n

    ages = [x.age_days() for x in comps]
    known = [a for a in ages if a is not None]
    if known:
        full = float(c.get("recency_full_days", 45))
        zero = float(c.get("recency_zero_days", 365))
        per = [1.0 if a <= full else max(0.0, 1.0 - (a - full) / max(1.0, zero - full))
               for a in known]
        recency = sum(per) / len(per) * (len(known) / float(n))
    else:
        recency = 0.0

    sold = sum(1 for x in comps if x.is_sold) / float(n)
    cond = _condition_match_score(comps, item_condition)

    score = (
        w["sample_size"] * sample
        + w["similarity"] * similarity
        + w["recency"] * recency
        + w["sold_evidence"] * sold
        + w["condition_match"] * cond
    )
    score = round(max(0.0, min(1.0, score)), 3)

    th = c["thresholds"]
    if n < int(c.get("sample_size_min", 3)):
        label = PricingConfidence.LOW.value
    elif score >= th["high"]:
        label = PricingConfidence.HIGH.value
    elif score >= th["medium"]:
        label = PricingConfidence.MEDIUM.value
    elif score >= th["low"]:
        label = PricingConfidence.LOW.value
    else:
        label = PricingConfidence.INSUFFICIENT.value

    # Placeholder or mock evidence can never produce a trustworthy label.
    if any(x.is_placeholder for x in comps):
        cap = c.get("placeholder_caps_at", "Low")
        order = [PricingConfidence.INSUFFICIENT.value, PricingConfidence.LOW.value,
                 PricingConfidence.MEDIUM.value, PricingConfidence.HIGH.value]
        if order.index(label) > order.index(cap):
            label = cap

    return score, label


def summarise(item_id: str, comps: list, item_condition: str = "",
              category: str = "", cfg: dict | None = None) -> ResearchSummary:
    cfg = cfg or load_config()
    summary = ResearchSummary(item_id=item_id, research_date=date.today().isoformat())
    summary.comp_count = len(comps)
    summary.sold_count = sum(1 for c in comps if c.is_sold)
    summary.active_count = summary.comp_count - summary.sold_count
    summary.placeholder_count = sum(1 for c in comps if c.is_placeholder)
    summary.hidden_price_count = sum(1 for c in comps if not c.has_known_price)
    summary.sources = [c.url for c in comps if c.url]

    # HIDDEN comps (Best Offer accepted) prove the item sells but their price
    # is not the real sale price -- they must never enter the price range.
    priced = [c for c in comps if c.has_known_price]
    if priced:
        prices = sorted(c.total_price for c in priced)
        summary.low = round(prices[0], 2)
        summary.high = round(prices[-1], 2)
        summary.median = round(statistics.median(prices), 2)

    summary.confidence_score, summary.confidence = score_confidence(comps, item_condition, cfg)

    # Explain exactly what is missing so the reviewer can act on it.
    min_n = int(cfg["confidence"].get("sample_size_min", 3))
    if summary.comp_count == 0:
        summary.gaps.append("No comparables recorded at all.")
    elif summary.comp_count < min_n:
        summary.gaps.append(
            "Only %d comparable(s); %d is the minimum for a usable range."
            % (summary.comp_count, min_n)
        )
    if summary.sold_count == 0 and summary.comp_count:
        summary.gaps.append(
            "No completed sales — every comparable is an asking price, which "
            "systematically overstates value."
        )
    if summary.placeholder_count:
        summary.gaps.append(
            "%d placeholder/mock row(s) present. Confidence is capped and this "
            "price must not be published." % summary.placeholder_count
        )
    if summary.hidden_price_count:
        summary.gaps.append(
            "%d comparable(s) sold via an accepted Best Offer — the displayed "
            "price is not the real sale price and was excluded from the range."
            % summary.hidden_price_count
        )
    if summary.comp_count and summary.low is None:
        summary.gaps.append(
            "Every comparable's price is hidden (Best Offer) or otherwise "
            "unusable — there is evidence this item sells, but no usable "
            "price range. Needs at least one comparable with a real price."
        )
    undated = sum(1 for c in comps if c.age_days() is None)
    if undated:
        summary.gaps.append("%d comparable(s) have no observed date; recency unscored." % undated)
    if comps and summary.high and summary.low and summary.low > 0:
        if summary.high / summary.low >= 4:
            summary.gaps.append(
                "Comparable spread is very wide (%.0fx). The set probably mixes "
                "different models or conditions." % (summary.high / summary.low)
            )

    specialist_categories = {"Art & Collectibles", "Jewelry & Watches"}
    if category in specialist_categories and summary.confidence in (
        PricingConfidence.LOW.value, PricingConfidence.INSUFFICIENT.value
    ):
        summary.recommend_specialist = True
        summary.gaps.append(
            f"Category '{category}' with weak comparables — recommend a specialist "
            "appraisal before listing."
        )
    if summary.median and summary.median >= 1000 and summary.confidence != PricingConfidence.HIGH.value:
        summary.recommend_specialist = True
        summary.gaps.append(
            "Median comparable is over $1,000 with less-than-high confidence — "
            "worth a specialist opinion."
        )

    return summary


def comps_to_dicts(comps: list) -> list:
    return [asdict(c) for c in comps]
