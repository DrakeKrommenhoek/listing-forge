"""Canonical field list, statuses, and vocabularies for the estate inventory.

This module is the single source of truth. The SQLAlchemy model, the
spreadsheet exporter, the review UI, and the website generator all derive
their column sets from INVENTORY_FIELDS so they can never drift apart.
"""

from dataclasses import dataclass
from enum import Enum


class ItemStatus(str, Enum):
    """Primary lifecycle status. Order here is the intended progression."""

    DRAFT = "Draft"
    NEEDS_REVIEW = "Needs Review"
    APPROVED = "Approved"
    READY_TO_LIST = "Ready to List"
    LISTED = "Listed"
    OFFER_RECEIVED = "Offer Received"
    PICKUP_SCHEDULED = "Pickup Scheduled"
    SHIPPING = "Shipping"
    SOLD = "Sold"
    DONATED = "Donated"
    REMOVED = "Removed"


STATUS_ORDER = [s.value for s in ItemStatus]

#: Statuses whose items may appear on the public website. APPROVED is included
#: because the catalogue is our own shop window — an item is shown there as soon
#: as a human has signed off on its price, without waiting for a marketplace
#: listing to go up.
PUBLISHABLE_STATUSES = {
    ItemStatus.APPROVED.value,
    ItemStatus.READY_TO_LIST.value,
    ItemStatus.LISTED.value,
    ItemStatus.OFFER_RECEIVED.value,
    ItemStatus.PICKUP_SCHEDULED.value,
    ItemStatus.SHIPPING.value,
    ItemStatus.SOLD.value,
}

#: Statuses that mean "a human has signed off on the price".
APPROVED_STATUSES = {
    ItemStatus.APPROVED.value,
    ItemStatus.READY_TO_LIST.value,
    ItemStatus.LISTED.value,
    ItemStatus.OFFER_RECEIVED.value,
    ItemStatus.PICKUP_SCHEDULED.value,
    ItemStatus.SHIPPING.value,
    ItemStatus.SOLD.value,
}


class Condition(str, Enum):
    NEW_SEALED = "New / Sealed"
    LIKE_NEW = "Like New"
    EXCELLENT = "Excellent"
    GOOD = "Good"
    FAIR = "Fair"
    POOR = "Poor"
    FOR_PARTS = "For Parts / Repair"
    UNKNOWN = "Unknown"


CONDITIONS = [c.value for c in Condition]


class PricingConfidence(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"
    INSUFFICIENT = "Insufficient Evidence"


CONFIDENCE_LEVELS = [c.value for c in PricingConfidence]


class PriceType(str, Enum):
    """How trustworthy a comparable's recorded price actually is.

    A completed sale is only real pricing evidence when the number shown is
    the number that actually changed hands. Two common cases quietly break
    that assumption and must never be silently treated as EXACT:

    - A listing sold via an accepted Best Offer. The page shows the ORIGINAL
      asking price, not what the buyer actually paid -- that figure is never
      published. Mark these HIDDEN; the price is not usable for pricing math,
      only as evidence that the item does sell.
    - An automated research provider proposing a plausible-but-unconfirmed
      figure (no direct listing page to point to). Mark these ESTIMATED.

    An active (not yet sold) asking price is a ceiling on value, not a
    result -- mark these UPPER_BOUND so they are never blended into a median
    the way a real completed sale would be.
    """

    EXACT = "exact"
    HIDDEN = "hidden"
    ESTIMATED = "estimated"
    UPPER_BOUND = "upper_bound"


PRICE_TYPES = [p.value for p in PriceType]


class ReviewStatus(str, Enum):
    NOT_REVIEWED = "Not Reviewed"
    IN_REVIEW = "In Review"
    NEEDS_MORE_PHOTOS = "Needs More Photos"
    NEEDS_MORE_RESEARCH = "Needs More Research"
    NEEDS_SPECIALIST = "Needs Specialist Appraisal"
    NEEDS_MANUAL_IDENTIFICATION = "Needs Manual Identification"
    REVIEWED = "Reviewed"


class ProcessingStage(str, Enum):
    """Where the automated pipeline is on ONE item, before a human is involved.

    Distinct from ``status`` (schema.ItemStatus), which is the human-facing
    lifecycle. This is what /myitems reports back to the submitter while the
    system is still working, and it is what survives a bot restart so a
    resumed process can tell where it left off.
    """

    PHOTOS_RECEIVED = "Photos Received"
    IDENTIFYING = "Identifying"
    NEEDS_INFORMATION = "Needs Information"
    RESEARCHING = "Researching"
    PRICING = "Pricing"
    GENERATING_LISTINGS = "Generating Listings"
    READY_FOR_REVIEW = "Ready for Review"
    APPROVED = "Approved"
    READY_TO_PUBLISH = "Ready to Publish"
    LISTED = "Listed"
    SOLD = "Sold"
    REMOVED = "Removed"
    ERROR = "Error"


PROCESSING_STAGES = [s.value for s in ProcessingStage]

#: The automated portion of the pipeline, in the order the orchestrator walks
#: it. Everything after READY_FOR_REVIEW is driven by a human decision, not by
#: ``orchestrator.process_item``.
AUTOMATED_STAGE_ORDER = [
    ProcessingStage.PHOTOS_RECEIVED.value,
    ProcessingStage.IDENTIFYING.value,
    ProcessingStage.NEEDS_INFORMATION.value,
    ProcessingStage.RESEARCHING.value,
    ProcessingStage.PRICING.value,
    ProcessingStage.GENERATING_LISTINGS.value,
    ProcessingStage.READY_FOR_REVIEW.value,
]

#: Stages the automated pipeline must never move an item out of, because a
#: human owns the item from here on. ``orchestrator.process_item`` refuses to
#: run on an item sitting in one of these.
HUMAN_OWNED_STAGES = {
    ProcessingStage.APPROVED.value,
    ProcessingStage.READY_TO_PUBLISH.value,
    ProcessingStage.LISTED.value,
    ProcessingStage.SOLD.value,
    ProcessingStage.REMOVED.value,
}

#: Map from a human-facing ItemStatus to the processing stage that mirrors it,
#: so ``/myitems`` keeps telling one coherent story after a reviewer acts.
STATUS_TO_PROCESSING_STAGE = {
    ItemStatus.APPROVED.value: ProcessingStage.APPROVED.value,
    ItemStatus.READY_TO_LIST.value: ProcessingStage.READY_TO_PUBLISH.value,
    ItemStatus.LISTED.value: ProcessingStage.LISTED.value,
    ItemStatus.OFFER_RECEIVED.value: ProcessingStage.LISTED.value,
    ItemStatus.PICKUP_SCHEDULED.value: ProcessingStage.LISTED.value,
    ItemStatus.SHIPPING.value: ProcessingStage.LISTED.value,
    ItemStatus.SOLD.value: ProcessingStage.SOLD.value,
    ItemStatus.DONATED.value: ProcessingStage.REMOVED.value,
    ItemStatus.REMOVED.value: ProcessingStage.REMOVED.value,
}

#: Plain-language, non-technical status lines shown to the submitter in
#: Telegram. Never mention providers, confidence scores, or internals.
PROCESSING_STAGE_MESSAGES = {
    ProcessingStage.PHOTOS_RECEIVED.value: "photos received, about to take a look",
    ProcessingStage.IDENTIFYING.value: "figuring out what it is",
    ProcessingStage.NEEDS_INFORMATION.value: "needs a bit more info from you",
    ProcessingStage.RESEARCHING.value: "checking what similar items have sold for",
    ProcessingStage.PRICING.value: "working out what to ask for it",
    ProcessingStage.GENERATING_LISTINGS.value: "writing the listings",
    ProcessingStage.READY_FOR_REVIEW.value: "ready for review",
    ProcessingStage.APPROVED.value: "approved — ready to post",
    ProcessingStage.READY_TO_PUBLISH.value: "ready to publish",
    ProcessingStage.LISTED.value: "listed and waiting for a buyer",
    ProcessingStage.SOLD.value: "sold",
    ProcessingStage.REMOVED.value: "removed from the sale",
    ProcessingStage.ERROR.value: "hit a snag — a person will take a look",
}


class ResearchStatus(str, Enum):
    """Spreadsheet-facing summary of the comparable-research state."""

    NOT_STARTED = "Not Started"
    QUEUED_FOR_MANUAL_RESEARCH = "Queued for Manual Research"
    IN_PROGRESS = "In Progress"
    NEEDS_MORE_EVIDENCE = "Needs More Evidence"
    COMPLETE = "Complete"
    FAILED = "Failed"


class ApprovalStatus(str, Enum):
    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    NOT_FOR_SALE = "Not For Sale"
    DONATE = "Donate"


class WebsiteStatus(str, Enum):
    HIDDEN = "Hidden"
    QUEUED = "Queued"
    PUBLISHED = "Published"
    SOLD_SHOWN = "Sold (shown)"


class PickupDifficulty(str, Enum):
    EASY = "Easy"
    MODERATE = "Moderate"
    HARD = "Hard"
    SPECIALIST = "Specialist Movers"


CATEGORIES = [
    "Furniture",
    "Appliances",
    "Electronics",
    "Audio / Music Gear",
    "Tools & Equipment",
    "Outdoor & Garden",
    "Kitchen & Dining",
    "Home Decor",
    "Art & Collectibles",
    "Books & Media",
    "Clothing & Accessories",
    "Jewelry & Watches",
    "Sporting Goods",
    "Toys & Games",
    "Office & Storage",
    "Vehicles & Trailers",
    "Other",
]


@dataclass(frozen=True)
class Field:
    """One inventory column.

    key:      python/db attribute name
    label:    spreadsheet + UI header
    group:    logical section, used for grouping in the review UI
    kind:     text | longtext | number | money | percent | date | bool | choice | url | urls
    choices:  allowed values when kind == "choice"
    note:     help text shown in the spreadsheet's Field Guide tab
    """

    key: str
    label: str
    group: str
    kind: str = "text"
    choices: tuple = ()
    note: str = ""


G_ID = "Identification"
G_PHYS = "Physical"
G_LOG = "Logistics"
G_RES = "Research"
G_PRICE = "Pricing"
G_MKT = "Marketing"
G_WF = "Workflow"
G_OUT = "Outcome"

INVENTORY_FIELDS = [
    # --- Identification -----------------------------------------------------
    Field("item_id", "Item ID", G_ID, "text", note="Immutable. Format: DK-YYYYMM-NNN."),
    Field("item_name", "Item Name", G_ID, "text"),
    Field("category", "Category", G_ID, "choice", tuple(CATEGORIES)),
    Field("brand", "Brand", G_ID),
    Field("model", "Model", G_ID),
    Field("sku", "SKU", G_ID, note="Manufacturer SKU or part number, when a label or listing confirms one."),
    Field("collection", "Collection", G_ID,
          note="Product line or series. Often finds comparables when the SKU cannot."),
    Field("subcategory", "Subcategory", G_ID),
    Field("manufacturer", "Manufacturer", G_ID, note="May differ from Brand (e.g. licensed or house-brand goods)."),
    Field("approximate_age", "Approximate Age", G_ID, note="Free text, e.g. '~1995' or '5-10 yrs'."),
    Field("description", "Description", G_ID, "longtext"),
    # --- Physical -----------------------------------------------------------
    Field("condition", "Condition", G_PHYS, "choice", tuple(CONDITIONS)),
    Field("defects", "Defects", G_PHYS, "longtext", note="Always disclose. Empty means 'none observed', not 'none'."),
    Field("dimensions", "Dimensions", G_PHYS, note="L x W x H with units."),
    Field("weight_lbs", "Weight (lbs)", G_PHYS, "number"),
    Field("included_accessories", "Included Accessories", G_PHYS, "longtext"),
    Field("location_in_house", "Location in House", G_PHYS),
    Field("photo_links", "Photo Links", G_PHYS, "urls"),
    # --- Logistics ----------------------------------------------------------
    Field("date_submitted", "Date Submitted", G_LOG, "date"),
    Field("submission_owner", "Submission Owner", G_LOG),
    Field("ownership_approval", "Ownership Approval", G_LOG, "bool",
          note="Confirms the submitter has the right to sell this item."),
    Field("shipping_feasible", "Shipping Feasibility", G_LOG, "bool"),
    Field("pickup_required", "Pickup Requirement", G_LOG, "bool"),
    Field("pickup_difficulty", "Pickup Difficulty", G_LOG, "choice",
          tuple(d.value for d in PickupDifficulty)),
    Field("required_vehicle", "Required Vehicle", G_LOG),
    Field("people_required", "People Required", G_LOG, "number"),
    Field("move_out_deadline", "Move-Out Deadline", G_LOG, "date"),
    # --- Research -----------------------------------------------------------
    Field("comp_low", "Comparable Low Price", G_RES, "money"),
    Field("comp_median", "Comparable Median Price", G_RES, "money"),
    Field("comp_high", "Comparable High Price", G_RES, "money"),
    Field("comp_count", "Comparable Sample Size", G_RES, "number"),
    Field("sold_comp_count", "Sold Comparable Count", G_RES, "number",
          note="Of the sample above, how many are confirmed completed sales rather than asking prices."),
    Field("comp_sources", "Comparable Source Links", G_RES, "urls"),
    Field("research_date", "Research Date", G_RES, "date"),
    Field("pricing_confidence", "Pricing Confidence", G_RES, "choice", tuple(CONFIDENCE_LEVELS)),
    Field("identification_confidence", "Identification Confidence", G_RES, "number",
          note="0-1, straight from the vision provider. Not a pricing signal."),
    Field("research_confidence", "Research Confidence", G_RES, "choice",
          tuple(CONFIDENCE_LEVELS),
          note="How good the comparable EVIDENCE is, before pricing maths is applied."),
    # --- Pricing ------------------------------------------------------------
    Field("initial_list_price", "Initial List Price", G_PRICE, "money"),
    Field("expected_sale_price", "Expected Selling Price", G_PRICE, "money"),
    Field("floor_price", "Floor Price", G_PRICE, "money",
          note="Hard minimum. The markdown engine will never go below this."),
    Field("current_price", "Current Price", G_PRICE, "money"),
    Field("pickup_incentive", "Pickup Incentive", G_PRICE, "money",
          note="Dollar discount for local pickup, derived from avoided cost."),
    Field("approved_pickup_price", "Approved Pickup Price", G_PRICE, "money"),
    Field("markdown_pct", "Markdown Percentage", G_PRICE, "percent"),
    Field("next_markdown_date", "Next Markdown Date", G_PRICE, "date"),
    Field("estimated_fees", "Estimated Fees", G_PRICE, "money",
          note="Platform commission on the recommended primary channel, at the expected sale price."),
    Field("expected_net_proceeds", "Expected Net Proceeds", G_PRICE, "money",
          note="Expected sale price minus estimated fees and shipping burden. The number "
               "that decides which item is worth working on next."),
    # --- Marketing ----------------------------------------------------------
    Field("primary_marketplace", "Best Primary Marketplace", G_MKT),
    Field("secondary_marketplaces", "Secondary Marketplaces", G_MKT),
    Field("listing_title", "Listing Title", G_MKT),
    Field("listing_description", "Listing Description", G_MKT, "longtext"),
    Field("keywords", "Keywords", G_MKT),
    # --- Workflow -----------------------------------------------------------
    Field("research_status", "Research Status", G_WF, "choice",
          tuple(s.value for s in ResearchStatus)),
    Field("review_status", "Review Status", G_WF, "choice",
          tuple(s.value for s in ReviewStatus)),
    Field("approval_status", "Approval Status", G_WF, "choice",
          tuple(s.value for s in ApprovalStatus)),
    Field("website_status", "Website Status", G_WF, "choice",
          tuple(s.value for s in WebsiteStatus)),
    Field("listing_urls", "Listing URLs", G_WF, "urls"),
    Field("status", "Status", G_WF, "choice", tuple(STATUS_ORDER),
          note="Primary lifecycle status."),
    Field("processing_stage", "Processing Stage", G_WF, "choice", tuple(PROCESSING_STAGES),
          note="Where the automated pipeline is, as distinct from the human lifecycle status."),
    Field("selling_difficulty", "Selling Difficulty", G_WF, "choice",
          ("Easy", "Moderate", "Hard"),
          note="How hard this is to turn into cash: buyer pool, evidence quality, price band."),
    Field("shipping_difficulty", "Shipping Difficulty", G_WF, "choice",
          ("Ships Easily", "Ships With Effort", "Local Only"),
          note="Derived from weight, size, and the owner's shipping answer."),
    Field("priority_score", "Priority Score", G_WF, "number",
          note="0-100. Higher means work on this first. See priority.py for the formula; "
               "priority_reasons records why."),
    Field("priority_reasons", "Priority Reasons", G_WF, "longtext",
          note="Plain-language breakdown of the priority score. Never a black box."),
    Field("research_blockers", "Research Blockers", G_WF, "longtext"),
    Field("approval_blockers", "Approval Blockers", G_WF, "longtext"),
    Field("last_activity", "Last Activity", G_WF, "date"),
    # --- Outcome ------------------------------------------------------------
    Field("inquiry_count", "Inquiry Count", G_OUT, "number"),
    Field("best_offer", "Best Offer", G_OUT, "money"),
    Field("buyer", "Buyer", G_OUT),
    Field("fulfilment_status", "Pickup or Shipping Status", G_OUT),
    Field("final_sale_price", "Final Sale Price", G_OUT, "money"),
    Field("actual_proceeds", "Actual Proceeds", G_OUT, "money",
          note="Final sale price minus platform fees and shipping cost."),
    Field("final_disposition", "Final Disposition", G_OUT),
    Field("notes", "Notes", G_OUT, "longtext"),
]

FIELDS_BY_KEY = {f.key: f for f in INVENTORY_FIELDS}
FIELD_KEYS = [f.key for f in INVENTORY_FIELDS]
FIELD_LABELS = [f.label for f in INVENTORY_FIELDS]


def fields_in_group(group: str):
    return [f for f in INVENTORY_FIELDS if f.group == group]


GROUPS = [G_ID, G_PHYS, G_LOG, G_RES, G_PRICE, G_MKT, G_WF, G_OUT]

#: Fields the intake flow will ask the submitter about when the vision model
#: cannot determine them confidently. Ordered by how much they move price.
ASKABLE_FIELDS = [
    "brand",
    "model",
    "condition",
    "defects",
    "dimensions",
    "included_accessories",
    "approximate_age",
    "location_in_house",
]

#: Fields that are never inferable from photos at all -- a photo cannot tell
#: you who owns an item or whether the owner is willing to ship it. These are
#: added to every item's missing-information list by the pipeline (not by the
#: vision model), and are answered yes/no rather than free text. The hint
#: parser resolves most of these from the owner's own one-sentence
#: description before a Telegram question is ever sent.
BOOLEAN_ASKABLE_FIELDS = ["ownership_approval", "shipping_feasible"]

YES_WORDS = {"yes", "y", "yeah", "yep", "sure", "correct", "true", "definitely", "of course"}
NO_WORDS = {"no", "n", "nope", "not really", "false", "nah"}

#: Human-friendly question text used by the Telegram flow. Kept deliberately
#: plain — the submitter is not a technical user.
FIELD_QUESTIONS = {
    "brand": "What brand is it? (If you don't know, just say: skip)",
    "model": "Do you know the model name or number? (or say: skip)",
    "condition": "How would you describe the condition — like new, excellent, good, fair, or poor?",
    "defects": "Any scratches, chips, stains, missing parts, or things that don't work? (or say: none)",
    "dimensions": "Roughly how big is it? (length x width x height, or say: skip)",
    "included_accessories": "Does anything come with it — cords, remotes, manuals, extra parts? (or say: none)",
    "approximate_age": "Roughly how old is it? (or say: skip)",
    "location_in_house": "Which room is it in?",
    "ownership_approval": "Is this yours to sell -- not borrowed, and not someone else's? (yes/no)",
    "shipping_feasible": "Could this be shipped in a box if a buyer isn't local, or is it pickup only? (yes/no)",
}

SKIP_WORDS = {"skip", "s", "n/a", "na", "dunno", "don't know", "dont know", "no idea", "-"}
NONE_WORDS = {"none", "no", "nothing", "nope", "n"}
