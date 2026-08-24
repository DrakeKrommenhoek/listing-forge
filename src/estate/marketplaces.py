"""Marketplace recommendation.

A rules engine, not a model call. Each platform declares what it is actually
good at; an item is scored against every platform and the best fits are
returned with the reasoning attached, so a human can disagree with a specific
factor rather than with a black box.

Fee figures were checked on 2026-08-02 (see estate/config/pricing.json and
docs/estate/MARKETPLACES.md for sources). Platforms change fees and policies
frequently — ``fee_verified_on`` is carried through to the review screen so a
stale number is visible rather than silently trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from estate.settings import load_config

FEES_VERIFIED_ON = "2026-08-02"


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    #: Categories this platform genuinely serves well.
    categories: tuple
    min_price: float = 0.0
    max_price: float = 10_000_000.0
    #: True if the platform is primarily local/in-person.
    local: bool = False
    #: True if the platform expects the seller to ship.
    ships: bool = True
    #: Max practical shipped weight in lbs (soft signal, not a hard rule).
    max_ship_lbs: float = 70.0
    fee_key: str = ""
    effort: int = 2          # 1 = trivial listing, 4 = photography + measurements + shipping
    time_to_sell_days: int = 21
    #: Relative audience size, 0-15. Offsets the specialist bonus so a broad
    #: marketplace is not out-scored by a narrow one purely for being narrow.
    reach: int = 5
    #: True only for genuinely category-native marketplaces (Reverb for gear,
    #: Discogs for records). A generalist with a restricted category list, such
    #: as Craigslist, is NOT a specialist.
    specialist: bool = False
    audience: str = ""
    notes: str = ""


ALL_CATEGORIES = ("*",)

PLATFORMS = [
    Platform(
        "facebook_marketplace", "Facebook Marketplace", ALL_CATEGORIES,
        min_price=5, local=True, ships=False, fee_key="facebook_marketplace_local",
        reach=15, effort=1, time_to_sell_days=10,
        audience="Local buyers, broadest reach of any local channel",
        notes="Best default for anything bulky, heavy, or low-value-per-pound. "
              "No fee on local cash sales. Expect lowball offers and no-shows.",
    ),
    Platform(
        "offerup", "OfferUp", ALL_CATEGORIES,
        min_price=5, local=True, ships=False, fee_key="offerup_local",
        reach=7, effort=1, time_to_sell_days=14,
        audience="Local buyers, skews toward furniture, appliances, tools",
        notes="Useful as a secondary local listing. Smaller audience than Facebook.",
    ),
    Platform(
        "craigslist", "Craigslist", (
            "Furniture", "Appliances", "Tools & Equipment", "Outdoor & Garden",
            "Office & Storage", "Vehicles & Trailers", "Sporting Goods", "Other",
        ),
        min_price=20, local=True, ships=False, fee_key="craigslist",
        reach=7, effort=1, time_to_sell_days=14,
        audience="Local, older-skewing, strong for tools and large items",
        notes="Free for most categories. Higher scam volume — cash in person only.",
    ),
    Platform(
        "nextdoor", "Nextdoor", (
            "Furniture", "Home Decor", "Kitchen & Dining", "Outdoor & Garden",
            "Toys & Games", "Books & Media", "Office & Storage", "Other",
        ),
        min_price=5, max_price=400, local=True, ships=False, fee_key="nextdoor",
        reach=4, effort=1, time_to_sell_days=12,
        audience="Immediate neighbours — highest trust, smallest pool",
        notes="Excellent for fast, easy pickups of modest-value items.",
    ),
    Platform(
        "ebay", "eBay", ALL_CATEGORIES,
        min_price=15, ships=True, max_ship_lbs=70, fee_key="ebay",
        reach=14, effort=3, time_to_sell_days=21,
        audience="National buyers; the deepest market for identifiable models",
        notes="The right answer when the item has a searchable brand + model. "
              "Also the best source of sold-price evidence.",
    ),
    Platform(
        "poshmark", "Poshmark", ("Clothing & Accessories", "Jewelry & Watches"),
        min_price=15, ships=True, max_ship_lbs=10, fee_key="poshmark",
        specialist=True, reach=8, effort=2, time_to_sell_days=30,
        audience="Women's mainstream and contemporary brands",
        notes="Highest fee of the clothing platforms but includes the shipping label.",
    ),
    Platform(
        "depop", "Depop", ("Clothing & Accessories",),
        min_price=10, ships=True, max_ship_lbs=10, fee_key="depop",
        specialist=True, reach=7, effort=2, time_to_sell_days=30,
        audience="Under-30 buyers; vintage, Y2K, distinctive pieces",
        notes="Lowest fees of the clothing platforms. Weak for plain or classic items.",
    ),
    Platform(
        "grailed", "Grailed", ("Clothing & Accessories",),
        min_price=40, ships=True, max_ship_lbs=10, fee_key="grailed",
        specialist=True, reach=6, effort=3, time_to_sell_days=30,
        audience="Menswear, streetwear, designer",
        notes="Only worth it for recognised menswear labels.",
    ),
    Platform(
        "reverb", "Reverb", ("Audio / Music Gear",),
        min_price=25, ships=True, max_ship_lbs=100, fee_key="reverb",
        specialist=True, reach=9, effort=3, time_to_sell_days=25,
        audience="Musicians and gear collectors, worldwide",
        notes="Clearly the right home for instruments, amps, pedals, and studio gear.",
    ),
    Platform(
        "discogs", "Discogs", ("Books & Media",),
        min_price=5, ships=True, max_ship_lbs=20, fee_key="discogs",
        specialist=True, reach=6, effort=3, time_to_sell_days=45,
        audience="Record and CD collectors",
        notes="Only for music media. Requires exact pressing identification.",
    ),
    Platform(
        "chairish", "Chairish", ("Furniture", "Home Decor", "Art & Collectibles"),
        min_price=250, ships=True, max_ship_lbs=500, fee_key="chairish",
        specialist=True, reach=5, effort=4, time_to_sell_days=60,
        audience="Design-led buyers seeking vintage and designer furniture",
        notes="Commission is 30-40% and items are curated on submission. "
              "Only worth it for genuinely designer or antique pieces.",
    ),
    Platform(
        "etsy", "Etsy", ("Art & Collectibles", "Home Decor", "Jewelry & Watches"),
        min_price=15, ships=True, max_ship_lbs=30, fee_key="etsy",
        specialist=True, reach=8, effort=3, time_to_sell_days=45,
        audience="Handmade and vintage buyers",
        notes="Vintage items must be 20+ years old to qualify.",
    ),
]

PLATFORMS_BY_KEY = {p.key: p for p in PLATFORMS}

#: Categories where a specialist channel usually beats a general marketplace.
SPECIALIST_HINTS = {
    "Art & Collectibles": "For anything that may be genuinely valuable, get an auction-house "
                          "or appraiser opinion before listing on a general marketplace.",
    "Jewelry & Watches": "Have precious metal and stones assessed by a jeweller before pricing. "
                         "Photos cannot establish authenticity or carat weight.",
    "Vehicles & Trailers": "Title transfer rules vary by state. Confirm paperwork before advertising.",
}


@dataclass
class PlatformFit:
    platform: Platform
    score: float = 0.0
    reasons: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    estimated_fee_pct: float = 0.0

    @property
    def viable(self) -> bool:
        return not self.blockers


def _matches_category(p: Platform, category: str) -> bool:
    return p.categories == ALL_CATEGORIES or category in p.categories


def score_platform(p: Platform, item, cfg: dict | None = None) -> PlatformFit:
    cfg = cfg or load_config()
    fit = PlatformFit(platform=p)
    fit.estimated_fee_pct = float(cfg.get("fees", {}).get(p.fee_key, 0.0) or 0.0)

    category = getattr(item, "category", "") or "Other"
    price = float(getattr(item, "initial_list_price", None)
                  or getattr(item, "current_price", None) or 0)
    weight = float(getattr(item, "weight_lbs", None) or 0)
    shipping_feasible = bool(getattr(item, "shipping_feasible", False))
    pickup_required = bool(getattr(item, "pickup_required", True))

    # --- blockers -----------------------------------------------------------
    if not _matches_category(p, category):
        fit.blockers.append(f"does not serve the {category} category")
    if price and price < p.min_price:
        fit.blockers.append(f"below this platform's practical minimum of ${p.min_price:.0f}")
    if price and price > p.max_price:
        fit.blockers.append(f"above this platform's practical ceiling of ${p.max_price:.0f}")
    if p.ships and not p.local:
        if pickup_required and not shipping_feasible:
            fit.blockers.append("item is pickup-only and this platform expects shipping")
        elif weight and weight > p.max_ship_lbs:
            fit.blockers.append(
                f"{weight:.0f} lb exceeds what ships economically here (~{p.max_ship_lbs:.0f} lb)"
            )
    if fit.blockers:
        return fit

    # --- positives ----------------------------------------------------------
    score = 50.0

    if p.specialist:
        score += 14
        fit.reasons.append(f"category-native marketplace for {category}")
    score += p.reach
    if p.reach >= 13:
        fit.reasons.append("largest buyer pool of any option here")

    if p.local and (pickup_required or (weight and weight >= 50)):
        score += 20
        fit.reasons.append("local pickup avoids shipping this item entirely")
    if not p.local and shipping_feasible and getattr(item, "brand", "") and getattr(item, "model", ""):
        score += 15
        fit.reasons.append("identifiable brand + model sells well to a national audience")

    fee_penalty = fit.estimated_fee_pct * 60
    score -= fee_penalty
    if fit.estimated_fee_pct == 0:
        fit.reasons.append("no platform fee on a local sale")
    elif fit.estimated_fee_pct >= 0.25:
        fit.reasons.append("high commission (%.0f%%) — only worth it for the right item"
                           % (fit.estimated_fee_pct * 100))

    score -= (p.effort - 1) * 4
    if p.effort <= 1:
        fit.reasons.append("very low listing effort")

    score -= p.time_to_sell_days * 0.25
    if p.time_to_sell_days <= 12:
        fit.reasons.append("typically sells quickly")

    # Value alignment: cheap items should not go to slow, high-effort platforms.
    if price and price < 60 and (p.effort >= 3 or fit.estimated_fee_pct >= 0.15):
        score -= 15
        fit.reasons.append("low sale value does not justify the fees or effort here")
    if price and price >= 300 and not p.local and fit.estimated_fee_pct < 0.15:
        score += 8
        fit.reasons.append("good economics at this price point")

    # Move-out urgency favours fast local channels.
    if getattr(item, "move_out_deadline", "") and p.local:
        score += 6
        fit.reasons.append("fast local channel suits the move-out deadline")

    fit.score = round(max(0.0, score), 1)
    return fit


def recommend(item, top_n: int = 4, cfg: dict | None = None) -> dict:
    """Return primary + secondary marketplace recommendations with reasoning."""
    cfg = cfg or load_config()
    fits = [score_platform(p, item, cfg) for p in PLATFORMS]
    viable = sorted([f for f in fits if f.viable], key=lambda f: f.score, reverse=True)
    rejected = [f for f in fits if not f.viable]

    primary = viable[0] if viable else None
    secondary = viable[1:top_n]

    warnings = []
    category = getattr(item, "category", "") or "Other"
    if category in SPECIALIST_HINTS:
        warnings.append(SPECIALIST_HINTS[category])
    if not viable:
        warnings.append(
            "No marketplace scored as a good fit. This usually means the item is "
            "very low value, pickup-only and oversized, or miscategorised. "
            "Consider donation or a local free-cycle listing."
        )
    warnings.append(
        f"Platform fees last verified {FEES_VERIFIED_ON}. Confirm current rates before relying on "
        "net-proceeds estimates."
    )

    return {
        "primary": primary,
        "secondary": secondary,
        "rejected": rejected,
        "warnings": warnings,
        "fees_verified_on": FEES_VERIFIED_ON,
    }


def summary_text(result: dict) -> str:
    lines = []
    p = result.get("primary")
    if p:
        lines.append(f"Primary: {p.platform.name} (score {p.score:.0f}, est. fee {p.estimated_fee_pct * 100:.1f}%)")
        for r in p.reasons[:3]:
            lines.append("  - " + r)
    else:
        lines.append("Primary: none recommended")
    if result.get("secondary"):
        lines.append("Also list on: " + ", ".join(f.platform.name for f in result["secondary"]))
    return "\n".join(lines)
