"""Multi-item bundle pricing for the public catalogue.

"What's your price for all three?" is the single most common question in a
house clearance, so the catalogue answers it before it is asked. This module
owns the private half of that answer: reading the tunable tier table out of
``estate/config/pricing.json`` and deciding, per item, how deep a discount
that item can absorb.

The floor rule, which is absolute
---------------------------------
A bundle discount may never take any selected item below its approved
``floor_price``. That is a human-approved number and nothing automated is
allowed to undercut it — not the markdown engine, and not this.

The obvious implementation — publish each item's maximum permissible
discount so the browser can compute the binding constraint — is wrong,
because ``max_discount = 1 - floor/price`` is algebraically equivalent to
publishing the floor price itself. A buyer with a calculator would read every
floor off the page.

So instead each item is published with a **band**: the index of the deepest
configured tier that item can absorb without breaching its floor. With the
default three-tier table a band is one of 0, 1, 2, 3 — roughly two bits,
telling a reader only "this item can bear at least a 10% discount", never
what the floor is. The discount applied to a basket is then

    min( tier for the number of items , min over items of tier[band] )

which is the binding constraint across the basket, exactly as required, and
is computed identically on the server, in the browser, and in the emitted
inquiry function.

Proof the floor holds. Let item *i* have public price ``p_i``, floor ``f_i``
and headroom ``h_i = 1 - f_i/p_i``. ``band_i`` is chosen so that
``tier[band_i] <= h_i``. The applied discount ``d <= tier[band_i] <= h_i``
for every selected item, so the effective price
``p_i * (1 - d) >= p_i * (f_i / p_i) = f_i``. The floor holds item by item,
and therefore holds for the total.

The arithmetic itself lives in ``inquiry_validation.bundle_quote`` rather
than here, because that module is copied verbatim into the public serverless
bundle and must stay import-free. Keeping one implementation there means the
number quoted on the page, the number recomputed when the inquiry arrives,
and the number that reaches Telegram cannot drift apart.

Nothing here commits to a price. Every figure this module produces is
indicative and is labelled as such wherever it is displayed; a human still
confirms on reply, exactly as everywhere else in this system.
"""

from __future__ import annotations

from estate import settings as estate_settings
from estate.inquiry_validation import (
    bundle_quote,
    normalise_tiers,
    tier_discount_for_count,
)

__all__ = [
    "DEFAULT_TIERS",
    "bundle_config",
    "bundle_quote",
    "discount_band",
    "headroom",
    "max_discount_for_band",
    "normalise_tiers",
    "tier_discount_for_count",
]

#: Used when ``estate/config/pricing.json`` has no ``bundle`` section at all.
#: Matches the shipped configuration so a missing section behaves like the
#: documented default rather than silently switching bundling off.
DEFAULT_TIERS = (
    {"min_items": 2, "discount_pct": 0.05},
    {"min_items": 3, "discount_pct": 0.10},
    {"min_items": 5, "discount_pct": 0.15},
)


def bundle_config() -> dict:
    """The ``bundle`` block of the pricing config, normalised and safe.

    A malformed or missing section degrades to the shipped defaults rather
    than raising: a bad edit to a tunable file must never break a build, and
    a build that silently produced *deeper* discounts than intended would be
    far worse than one that produced the documented ones.
    """
    raw = estate_settings.load_config().get("bundle") or {}
    if not isinstance(raw, dict):
        raw = {}

    tiers = normalise_tiers(raw.get("tiers") or DEFAULT_TIERS)
    if not tiers:
        tiers = normalise_tiers(DEFAULT_TIERS)

    try:
        max_items = int(raw.get("max_items", 25))
    except (TypeError, ValueError):
        max_items = 25
    try:
        round_to = int(raw.get("round_to", 5))
    except (TypeError, ValueError):
        round_to = 5

    return {
        "enabled": bool(raw.get("enabled", True)),
        "tiers": tiers,
        "max_items": max(1, min(max_items, 100)),
        "round_to": max(0, round_to),
    }


def headroom(price, floor) -> float:
    """How much of ``price`` can be discounted before ``floor`` is breached.

    Returned as a fraction in [0, 1]. A missing or non-positive price has no
    headroom at all — an item with no published price cannot be discounted,
    because there is nothing to discount from. A missing floor is treated as
    *no* headroom too: an item whose floor has not been approved has not been
    priced by a human, and guessing on its behalf is precisely the thing this
    system does not do.
    """
    try:
        p = float(price or 0)
        f = float(floor or 0)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0 or f <= 0:
        return 0.0
    if f >= p:
        return 0.0
    return 1.0 - (f / p)


#: Comparison tolerance for the band calculation, in discount-fraction space.
#:
#: ``1 - 90/100`` is ``0.09999999999999998`` in binary floating point, so an
#: item priced at $100 with a $90 floor would fail an exact ``0.10 <= room``
#: test and silently drop a whole tier. That is the safe direction, but
#: losing a documented 10% discount to the sixteenth decimal place is a bug
#: rather than caution. 1e-9 of a fraction is well under a cent on any price
#: this system will ever see, and the displayed bundle total is rounded
#: *upward* afterwards, which absorbs it many times over.
_BAND_EPSILON = 1e-9


def discount_band(price, floor, tiers=None) -> int:
    """The deepest tier index this item can absorb without breaching its floor.

    0 means "cannot be discounted at all"; ``len(tiers)`` means "can absorb
    the deepest configured tier". This integer is the only bundle-related
    fact ever published about an item.
    """
    tiers = normalise_tiers(tiers) if tiers is not None else bundle_config()["tiers"]
    room = headroom(price, floor)
    band = 0
    for index, tier in enumerate(tiers, start=1):
        if tier["discount_pct"] <= room + _BAND_EPSILON:
            band = index
        else:
            break
    return band


def max_discount_for_band(band: int, tiers=None) -> float:
    """The discount fraction a given band permits. Band 0 permits nothing."""
    tiers = normalise_tiers(tiers) if tiers is not None else bundle_config()["tiers"]
    try:
        band = int(band)
    except (TypeError, ValueError):
        return 0.0
    if band <= 0 or not tiers:
        return 0.0
    return float(tiers[min(band, len(tiers)) - 1]["discount_pct"])
