"""Listing copy generation.

Deterministic and template-driven on purpose. Listing copy has to be
*accurate* far more than it has to be clever: every claim here traces back to a
field a human filled in or approved. A model that improvises adjectives will
eventually improvise a feature the item does not have, and that turns into a
refund and a bad review.

The copy is generated per platform because the constraints really do differ:
title length, whether shipping is assumed, how buyers search, and what tone
reads as normal on that platform.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from estate.marketplaces import PLATFORMS_BY_KEY
from estate.pricing import estimated_net_proceeds

#: Practical title limits. Where a platform is generous we still keep titles
#: readable rather than stuffed.
TITLE_LIMITS = {
    "ebay": 80,
    "facebook_marketplace": 100,
    "offerup": 50,
    "craigslist": 70,
    "nextdoor": 60,
    "poshmark": 50,
    "depop": 65,
    "grailed": 60,
    "reverb": 80,
    "discogs": 80,
    "chairish": 100,
    "etsy": 140,
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "for", "with", "in", "on", "to", "is",
    "it", "this", "that", "very", "some", "any", "from", "by", "as", "at",
}


@dataclass
class ListingPackage:
    platform_key: str = ""
    platform_name: str = ""
    title: str = ""
    description: str = ""
    condition_disclosure: str = ""
    dimensions: str = ""
    accessories: str = ""
    terms: str = ""
    bundle_language: str = ""
    catalog_language: str = ""
    keywords: list = field(default_factory=list)
    list_price: float | None = None
    minimum_offer: float | None = None
    pickup_price: float | None = None
    estimated_net: float | None = None
    buyer_qa: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [
            f"# {self.platform_name} — {self.title}",
            "",
            f"**List price:** ${_money(self.list_price)}   **Minimum approved offer:** ${_money(self.minimum_offer)}",
        ]
        if self.pickup_price:
            lines.append(f"**Local pickup price:** ${_money(self.pickup_price)}")
        if self.estimated_net is not None:
            lines.append(f"**Estimated net after fees:** ${_money(self.estimated_net)}")
        lines += ["", "## Title", self.title, "", "## Description", self.description, ""]
        if self.keywords:
            lines += ["## Search keywords", ", ".join(self.keywords), ""]
        if self.buyer_qa:
            lines.append("## Suggested answers to common questions")
            for q, a in self.buyer_qa:
                lines.append(f"**{q}**")
                lines.append(a)
                lines.append("")
        if self.warnings:
            lines.append("## Warnings")
            for w in self.warnings:
                lines.append("- " + w)
        return "\n".join(lines)


def _money(v) -> str:
    return "—" if v in (None, "") else (f"{float(v):.0f}" if float(v) == int(float(v))
                                        else f"{float(v):.2f}")


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut or text[:limit]


def build_title(item, platform_key: str) -> str:
    """Front-load the words buyers actually search for."""
    parts = []
    for value in (getattr(item, "brand", ""), getattr(item, "model", "")):
        if value and value.strip():
            parts.append(value.strip())
    name = (getattr(item, "item_name", "") or "").strip()
    if name and name.lower() not in " ".join(parts).lower():
        parts.append(name)

    tail = []
    cond = (getattr(item, "condition", "") or "").strip()
    if cond and cond not in ("Unknown",):
        tail.append(cond)
    if platform_key in ("facebook_marketplace", "offerup", "craigslist", "nextdoor"):
        if getattr(item, "pickup_required", False):
            tail.append("Local Pickup")
    dims = (getattr(item, "dimensions", "") or "").strip()
    if dims and platform_key in ("facebook_marketplace", "craigslist", "chairish"):
        tail.append(dims)

    title = " ".join(parts)
    if tail:
        title = title + " — " + " · ".join(tail)
    return _clip(title, TITLE_LIMITS.get(platform_key, 80)) or "Untitled item"


def build_keywords(item, limit: int = 15) -> list:
    seed = " ".join(str(getattr(item, f, "") or "") for f in
                    ("brand", "model", "item_name", "category", "description",
                     "included_accessories"))
    words = [w.lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]{1,}", seed)]
    out: list = []
    for w in words:
        if w in STOPWORDS or len(w) < 3 or w in out:
            continue
        out.append(w)
    brand = (getattr(item, "brand", "") or "").strip().lower()
    model = (getattr(item, "model", "") or "").strip().lower()
    if brand and model:
        out.insert(0, f"{brand} {model}")
    return out[:limit]


def condition_disclosure(item) -> str:
    """Always disclose. Silence about defects is what generates disputes."""
    cond = (getattr(item, "condition", "") or "Unknown").strip()
    defects = (getattr(item, "defects", "") or "").strip()
    age = (getattr(item, "approximate_age", "") or "").strip()

    lines = [f"Condition: {cond}."]
    if age:
        lines.append(f"Approximate age: {age}.")
    if defects and defects.lower() not in ("none", "no", "n/a"):
        lines.append(f"Please note these flaws: {defects}.")
    else:
        lines.append(
            "No defects were noted during inspection. This is a used household "
            "item and may show normal wear consistent with its age."
        )
    lines.append("Photographs are of the actual item, not a stock image.")
    return " ".join(lines)


def build_terms(item, platform_key: str, pickup_price=None, pickup_incentive: float = 0.0,
                region: str = "") -> str:
    p = PLATFORMS_BY_KEY.get(platform_key)
    local = bool(p and p.local)
    pickup_required = bool(getattr(item, "pickup_required", True))
    ships = bool(getattr(item, "shipping_feasible", False))
    people = int(getattr(item, "people_required", 1) or 1)
    vehicle = (getattr(item, "required_vehicle", "") or "").strip()

    lines = []
    if local or pickup_required or not ships:
        where = (f" in {region}") if region else ""
        lines.append(f"Local pickup{where}. Exact address is shared once a pickup time is confirmed.")
        if people >= 2:
            lines.append("Please bring a second person — this is a two-person lift.")
        if vehicle:
            # Phrased without an article so acronyms ("SUV") read correctly.
            lines.append(f"Vehicle needed: {vehicle}.")
        lines.append("Cash or an instant payment app on pickup. No holds without a deposit.")
    if ships and p and p.ships and not p.local:
        lines.append("Ships from the US. Buyer pays actual shipping unless stated otherwise; "
                     "item is packed carefully and dispatched within 2 business days.")
    if pickup_incentive and pickup_price:
        lines.append(f"Collecting it yourself? The local pickup price is ${_money(pickup_price)}, a saving of ${_money(pickup_incentive)}.")
    return " ".join(lines)


def bundle_language(catalog_url: str = "") -> str:
    base = ("Buying more than one item? We are clearing a whole house and are happy "
            "to do a bundle price — ask and we will put a number together.")
    if catalog_url:
        base += f" The full catalogue is at {catalog_url}."
    return base


def catalog_language(item, catalog_url: str = "") -> str:
    """The one-line blurb used on the website catalogue card."""
    bits = [x for x in (getattr(item, "brand", ""), getattr(item, "item_name", "")) if x]
    lead = " ".join(bits) or "Household item"
    desc = (getattr(item, "description", "") or "").strip()
    first = desc.split(".")[0].strip() if desc else ""
    tail = (". " + first + ".") if first else "."
    ref = " Reference {}.".format(getattr(item, "item_id", ""))
    return f"{lead}{tail}{ref}"


def buyer_qa(item, pickup_price=None, minimum_offer=None) -> list:
    """Pre-written, honest answers to the questions that always get asked."""
    dims = (getattr(item, "dimensions", "") or "").strip() or "not yet measured — ask and we will measure it"
    acc = (getattr(item, "included_accessories", "") or "").strip() or "nothing beyond what is pictured"
    return [
        ("Is this still available?",
         "Yes, it is available as of today. It is also listed elsewhere, so "
         "availability can change — first confirmed pickup takes it."),
        ("What are the exact dimensions?",
         f"Dimensions: {dims}."),
        ("What is included?",
         f"Included: {acc}."),
        ("What condition is it really in?",
         condition_disclosure(item)),
        ("Would you take less?",
         "The listed price already reflects what comparable items have sold for. "
         + (f"We can consider offers at or above ${_money(minimum_offer)}."
            if minimum_offer else "Reasonable offers are considered.")),
        ("Can you deliver?",
         "Pickup only for this item. "
         + (f"Local pickup price is ${_money(pickup_price)}." if pickup_price else "")),
        ("Can you hold it until the weekend?",
         "We can hold for 24 hours with a confirmed pickup time, or longer with a deposit."),
    ]


def build_package(item, platform_key: str, minimum_offer=None, pickup_price=None,
                  pickup_incentive: float = 0.0, catalog_url: str = "",
                  region: str = "") -> ListingPackage:
    p = PLATFORMS_BY_KEY.get(platform_key)
    pkg = ListingPackage(
        platform_key=platform_key,
        platform_name=p.name if p else platform_key,
    )
    pkg.title = build_title(item, platform_key)
    pkg.condition_disclosure = condition_disclosure(item)
    pkg.dimensions = (getattr(item, "dimensions", "") or "").strip()
    pkg.accessories = (getattr(item, "included_accessories", "") or "").strip()
    pkg.terms = build_terms(item, platform_key, pickup_price, pickup_incentive, region)
    pkg.bundle_language = bundle_language(catalog_url)
    pkg.catalog_language = catalog_language(item, catalog_url)
    pkg.keywords = build_keywords(item)
    pkg.list_price = getattr(item, "current_price", None) or getattr(item, "initial_list_price", None)
    pkg.minimum_offer = minimum_offer if minimum_offer is not None else getattr(item, "floor_price", None)
    pkg.pickup_price = pickup_price
    if pkg.list_price and p:
        pkg.estimated_net = estimated_net_proceeds(pkg.list_price, p.fee_key)
    pkg.buyer_qa = buyer_qa(item, pickup_price, pkg.minimum_offer)

    body = [(getattr(item, "description", "") or "").strip()]
    if pkg.dimensions:
        body.append(f"Dimensions: {pkg.dimensions}.")
    weight = getattr(item, "weight_lbs", None)
    if weight:
        body.append(f"Weight: approximately {_money(weight)} lb.")
    if pkg.accessories:
        body.append(f"Included: {pkg.accessories}.")
    body.append(pkg.condition_disclosure)
    if pkg.terms:
        body.append(pkg.terms)
    body.append(pkg.bundle_language)
    body.append("Item reference: {}".format(getattr(item, "item_id", "")))

    if platform_key in ("facebook_marketplace", "offerup", "nextdoor"):
        # Short, scannable, mobile-first.
        pkg.description = "\n\n".join(x for x in body if x)
    elif platform_key == "craigslist":
        pkg.description = "\n\n".join(x for x in body if x)
    elif platform_key in ("depop", "poshmark", "grailed"):
        tags = " ".join("#" + re.sub(r"[^a-z0-9]", "", k) for k in pkg.keywords[:6] if k)
        pkg.description = "\n\n".join(x for x in body if x) + "\n\n" + tags
    else:
        pkg.description = "\n\n".join(x for x in body if x)

    # --- honesty guards -----------------------------------------------------
    if not getattr(item, "floor_price", None):
        pkg.warnings.append("No floor price set — do not accept offers until one is approved.")
    if getattr(item, "pricing_confidence", "") in ("Low", "Insufficient Evidence"):
        pkg.warnings.append(
            "Pricing confidence is {}. Review the price before posting.".format(getattr(item, "pricing_confidence", ""))
        )
    if not pkg.dimensions:
        pkg.warnings.append("No dimensions recorded — buyers will ask, and it slows sales.")
    if (getattr(item, "condition", "") or "Unknown") == "Unknown":
        pkg.warnings.append("Condition is still Unknown — must be set before listing.")
    if str(getattr(item, "item_name", "")).startswith("[MOCK]"):
        pkg.warnings.append("MOCK ITEM — sample data. Do not post this listing.")
    return pkg


def build_all(item, recommendation: dict, minimum_offer=None, pickup_price=None,
              pickup_incentive: float = 0.0, catalog_url: str = "",
              region: str = "") -> dict:
    """One package per recommended platform, keyed by platform key."""
    keys = []
    if recommendation.get("primary"):
        keys.append(recommendation["primary"].platform.key)
    keys += [f.platform.key for f in recommendation.get("secondary", [])]
    return {
        k: build_package(item, k, minimum_offer, pickup_price, pickup_incentive,
                         catalog_url, region)
        for k in keys
    }


# ---------------------------------------------------------------------------
# Future Only website copy
# ---------------------------------------------------------------------------
#
# Distinct in tone and shape from the marketplace ListingPackage above: the
# catalogue is our own shop window, so the copy reads as a premium, editorial
# description rather than a marketplace posting -- but it is built from
# exactly the same human-approved fields (condition_disclosure, dimensions,
# accessories, price). No adjective here is invented; "premium" describes the
# writing style, not an inflated claim about the item.

@dataclass
class WebsiteCopy:
    product_title: str = ""
    subtitle: str = ""
    description: str = ""
    key_details: list = field(default_factory=list)
    condition_statement: str = ""
    dimensions: str = ""
    shipping_statement: str = ""
    website_price: float | None = None
    category: str = ""
    search_tags: list = field(default_factory=list)
    #: Photo filenames in the order they should appear on the item page,
    #: hero image first.
    image_order: list = field(default_factory=list)
    hero_image: str = ""
    bundle_statement: str = ""
    contact_cta: str = ""
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        lines = [f"# {self.product_title}"]
        if self.subtitle:
            lines.append(f"*{self.subtitle}*")
        lines += ["", self.description, ""]
        if self.key_details:
            lines.append("## Key details")
            lines += ["- " + d for d in self.key_details]
            lines.append("")
        lines += [
            f"**Condition:** {self.condition_statement}",
            f"**Dimensions:** {self.dimensions or 'Not yet measured'}",
            f"**Shipping / pickup:** {self.shipping_statement}",
            f"**Website price:** ${_money(self.website_price)}",
            f"**Category:** {self.category}",
            "",
        ]
        if self.search_tags:
            lines += ["## Search tags", ", ".join(self.search_tags), ""]
        if self.image_order:
            lines += [
                "## Image order",
                ", ".join(self.image_order),
                f"Hero image: {self.hero_image or self.image_order[0]}",
                "",
            ]
        if self.bundle_statement:
            lines += ["## Bundle", self.bundle_statement, ""]
        if self.contact_cta:
            lines += ["## Contact", self.contact_cta, ""]
        if self.warnings:
            lines.append("## Warnings")
            lines += ["- " + w for w in self.warnings]
        return "\n".join(lines)


def _key_details(item) -> list:
    details = []
    age = (getattr(item, "approximate_age", "") or "").strip()
    if age:
        details.append(f"Approximate age: {age}")
    accessories = (getattr(item, "included_accessories", "") or "").strip()
    if accessories and accessories.lower() not in ("none", "no", "n/a"):
        details.append(f"Included: {accessories}")
    weight = getattr(item, "weight_lbs", None)
    if weight:
        details.append(f"Weight: approximately {_money(weight)} lb")
    if getattr(item, "pickup_required", False) and int(getattr(item, "people_required", 1) or 1) >= 2:
        details.append("Two-person lift for collection")
    vehicle = (getattr(item, "required_vehicle", "") or "").strip()
    if vehicle and getattr(item, "pickup_required", False):
        details.append(f"Vehicle needed for collection: {vehicle}")
    return details


def _shipping_statement(item, region: str = "") -> str:
    ships = bool(getattr(item, "shipping_feasible", False))
    pickup_required = bool(getattr(item, "pickup_required", True))
    where = f" in {region}" if region else ""
    if pickup_required or not ships:
        return f"Local pickup only{where}. Exact location shared once a time is confirmed."
    return f"Can be shipped, or collected locally{where} at a reduced price."


def _image_order(photos: list | None) -> tuple:
    """(ordered filenames, hero filename). Honours EstatePhotoORM.is_hero
    when set; otherwise falls back to stored sort_order, i.e. upload order."""
    if not photos:
        return [], ""
    ordered = sorted(
        photos,
        key=lambda p: (0 if getattr(p, "is_hero", False) else 1, getattr(p, "sort_order", 0)),
    )
    names = [getattr(p, "filename", "") for p in ordered if getattr(p, "filename", "")]
    hero = next((getattr(p, "filename", "") for p in ordered if getattr(p, "is_hero", False)), "")
    return names, (hero or (names[0] if names else ""))


def build_website_copy(item, photos: list | None = None, catalog_url: str = "",
                       region: str = "") -> WebsiteCopy:
    """The Future Only catalogue listing for one approved item.

    ``item`` needs the same attributes approval.prepare_review's proxy
    object already carries (item_name, brand, model, category, condition,
    defects, approximate_age, dimensions, weight_lbs, included_accessories,
    description, current_price / initial_list_price, shipping_feasible,
    pickup_required, people_required, required_vehicle, item_id).
    """
    copy = WebsiteCopy()

    brand = (getattr(item, "brand", "") or "").strip()
    name = (getattr(item, "item_name", "") or "").strip() or "Untitled item"
    copy.product_title = (
        f"{brand} {name}" if brand and brand.lower() not in name.lower() else name
    )

    model = (getattr(item, "model", "") or "").strip()
    age = (getattr(item, "approximate_age", "") or "").strip()
    cond = (getattr(item, "condition", "") or "Unknown").strip()
    subtitle_bits = [x for x in (model, age) if x]
    copy.subtitle = ", ".join(subtitle_bits) if subtitle_bits else cond

    copy.category = (getattr(item, "category", "") or "Other").strip()
    copy.condition_statement = condition_disclosure(item)
    copy.dimensions = (getattr(item, "dimensions", "") or "").strip()
    copy.shipping_statement = _shipping_statement(item, region)
    copy.website_price = getattr(item, "current_price", None) or getattr(item, "initial_list_price", None)
    copy.key_details = _key_details(item)
    copy.search_tags = build_keywords(item)
    copy.bundle_statement = bundle_language(catalog_url)
    copy.contact_cta = (
        "Interested? Send an enquiry below with your name and the best way to "
        f"reach you, quoting reference {getattr(item, 'item_id', '')}."
    )
    copy.image_order, copy.hero_image = _image_order(photos)

    desc = (getattr(item, "description", "") or "").strip()
    body = [desc] if desc else [
        f"A {cond.lower()} {name.lower()}" + (f" from {brand}." if brand else ".")
    ]
    if copy.key_details:
        body.append(" ".join(copy.key_details) + ".")
    body.append(copy.condition_statement)
    copy.description = " ".join(x for x in body if x)

    if (getattr(item, "condition", "") or "Unknown") == "Unknown":
        copy.warnings.append("Condition is still Unknown — must be set before publishing.")
    if not copy.dimensions:
        copy.warnings.append("No dimensions recorded — add before publishing if possible.")
    if not copy.image_order:
        copy.warnings.append("No photos available for this listing.")
    if str(getattr(item, "item_name", "")).startswith("[MOCK]"):
        copy.warnings.append("MOCK ITEM — sample data. Do not publish this copy.")
    return copy
