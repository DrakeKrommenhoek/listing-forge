"""SQLAlchemy tables for the estate sale system.

All tables are prefixed ``estate_`` and attach to the existing project Base so
``init_db()`` creates them alongside the rest of D.R.A.K.E. No existing table
is touched.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from estate._compat import Base


def _uid() -> str:
    return str(uuid.uuid4())


class EstateItemORM(Base):
    """One sellable item. Columns mirror schema.INVENTORY_FIELDS exactly."""

    __tablename__ = "estate_items"

    item_id: Mapped[str] = mapped_column(String, primary_key=True)

    # Identification
    item_name: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")
    brand: Mapped[str] = mapped_column(String, default="")
    model: Mapped[str] = mapped_column(String, default="")
    sku: Mapped[str] = mapped_column(String, default="")
    #: Product line or series ("Marcel", "Eames"). Sellers list by collection
    #: far more consistently than by SKU, so this materially widens the
    #: comparable search when a SKU finds nothing.
    collection: Mapped[str] = mapped_column(String, default="")
    subcategory: Mapped[str] = mapped_column(String, default="")
    manufacturer: Mapped[str] = mapped_column(String, default="")
    approximate_age: Mapped[str] = mapped_column(String, default="")
    description: Mapped[str] = mapped_column(Text, default="")

    # Physical
    condition: Mapped[str] = mapped_column(String, default="Unknown")
    defects: Mapped[str] = mapped_column(Text, default="")
    dimensions: Mapped[str] = mapped_column(String, default="")
    weight_lbs: Mapped[float | None] = mapped_column(Float, nullable=True)
    included_accessories: Mapped[str] = mapped_column(Text, default="")
    location_in_house: Mapped[str] = mapped_column(String, default="")
    photo_links: Mapped[list] = mapped_column(JSON, default=list)

    # Logistics
    date_submitted: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    submission_owner: Mapped[str] = mapped_column(String, default="")
    ownership_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    shipping_feasible: Mapped[bool] = mapped_column(Boolean, default=False)
    pickup_required: Mapped[bool] = mapped_column(Boolean, default=True)
    pickup_difficulty: Mapped[str] = mapped_column(String, default="Easy")
    required_vehicle: Mapped[str] = mapped_column(String, default="")
    people_required: Mapped[int] = mapped_column(Integer, default=1)
    move_out_deadline: Mapped[str] = mapped_column(String, default="")

    # Research
    comp_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_median: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    comp_count: Mapped[int] = mapped_column(Integer, default=0)
    sold_comp_count: Mapped[int] = mapped_column(Integer, default=0)
    comp_sources: Mapped[list] = mapped_column(JSON, default=list)
    research_date: Mapped[str] = mapped_column(String, default="")
    pricing_confidence: Mapped[str] = mapped_column(String, default="Insufficient Evidence")
    #: Automated background job stage -- see schema.ProcessingStage. Distinct
    #: from `status`, which is the human-facing lifecycle (Draft, Needs
    #: Review, Approved, ...). This tracks what the pipeline is doing right
    #: now, before a human ever sees the item, and is what /myitems reports.
    processing_stage: Mapped[str] = mapped_column(String, default="Photos Received")
    #: 0..1, copied straight from the vision provider's overall_confidence.
    #: Deliberately separate from pricing_confidence: knowing exactly what an
    #: item is tells you nothing about whether you know what it is worth.
    identification_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    #: Quality of the comparable EVIDENCE on its own terms, before pricing
    #: maths. pricing_confidence can be lower (placeholders cap it) but never
    #: meaningfully higher.
    research_confidence: Mapped[str] = mapped_column(String, default="Insufficient Evidence")

    # Pricing
    initial_list_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    floor_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pickup_incentive: Mapped[float] = mapped_column(Float, default=0.0)
    approved_pickup_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    markdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    next_markdown_date: Mapped[str] = mapped_column(String, default="")
    estimated_fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_net_proceeds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Marketing
    primary_marketplace: Mapped[str] = mapped_column(String, default="")
    secondary_marketplaces: Mapped[str] = mapped_column(String, default="")
    listing_title: Mapped[str] = mapped_column(String, default="")
    listing_description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[str] = mapped_column(Text, default="")

    # Workflow
    research_status: Mapped[str] = mapped_column(String, default="Not Started")
    review_status: Mapped[str] = mapped_column(String, default="Not Reviewed")
    approval_status: Mapped[str] = mapped_column(String, default="Pending")
    website_status: Mapped[str] = mapped_column(String, default="Hidden")
    listing_urls: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String, default="Draft")
    selling_difficulty: Mapped[str] = mapped_column(String, default="")
    shipping_difficulty: Mapped[str] = mapped_column(String, default="")
    #: 0..100, recomputed by priority.score_item() every time the orchestrator
    #: touches the item. priority_reasons is the human-readable justification;
    #: the score is never allowed to be an unexplained number.
    priority_score: Mapped[float] = mapped_column(Float, default=0.0)
    priority_reasons: Mapped[str] = mapped_column(Text, default="")
    research_blockers: Mapped[str] = mapped_column(Text, default="")
    approval_blockers: Mapped[str] = mapped_column(Text, default="")
    last_activity: Mapped[str] = mapped_column(String, default="")

    # Outcome
    inquiry_count: Mapped[int] = mapped_column(Integer, default=0)
    best_offer: Mapped[float | None] = mapped_column(Float, nullable=True)
    buyer: Mapped[str] = mapped_column(String, default="")
    fulfilment_status: Mapped[str] = mapped_column(String, default="")
    final_sale_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    actual_proceeds: Mapped[float | None] = mapped_column(Float, nullable=True)
    final_disposition: Mapped[str] = mapped_column(String, default="")
    notes: Mapped[str] = mapped_column(Text, default="")

    # Internal (not exported to the spreadsheet)
    #: The date the item actually went live on a marketplace. Distinct from
    #: next_markdown_date, which points forward. The markdown clock starts here.
    listed_on: Mapped[str] = mapped_column(String, default="")
    vision_raw: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    listing_packages: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Orchestrator retry state. Kept in the database, not in memory, so a
    #: restart mid-job resumes instead of starting over or silently stalling.
    processing_attempts: Mapped[int] = mapped_column(Integer, default=0)
    #: Error TYPE and stage only -- never a message, which could carry a
    #: credential or a private path out of a provider exception.
    processing_error: Mapped[str] = mapped_column(String, default="")
    processing_failed_stage: Mapped[str] = mapped_column(String, default="")
    last_processed_at: Mapped[str] = mapped_column(String, default="")
    #: Fields a human has explicitly confirmed or edited. The orchestrator
    #: will never overwrite one of these with a model guess on a re-run.
    owner_confirmed_fields: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class EstatePhotoORM(Base):
    """One stored photograph belonging to an item."""

    __tablename__ = "estate_photos"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    item_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    role: Mapped[str] = mapped_column(String, default="original")  # original|web|listing
    filename: Mapped[str] = mapped_column(String, default="")
    local_path: Mapped[str] = mapped_column(String, default="")
    telegram_file_id: Mapped[str] = mapped_column(String, default="")
    media_group_id: Mapped[str] = mapped_column(String, default="")
    sha256: Mapped[str] = mapped_column(String, default="", index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_hero: Mapped[bool] = mapped_column(Boolean, default=False)
    caption: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EstateCompORM(Base):
    """One comparable listing used as pricing evidence.

    ``is_placeholder`` must be True for any row that is not real observed
    market data. The pricing engine refuses to raise confidence above LOW when
    placeholders are present.
    """

    __tablename__ = "estate_comps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    item_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    platform: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, default="")
    condition: Mapped[str] = mapped_column(String, default="Unknown")
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    shipping_amount: Mapped[float] = mapped_column(Float, default=0.0)
    observed_date: Mapped[str] = mapped_column(String, default="")
    url: Mapped[str] = mapped_column(String, default="")
    location: Mapped[str] = mapped_column(String, default="")
    is_sold: Mapped[bool] = mapped_column(Boolean, default=False)
    #: How trustworthy the recorded price actually is. See
    #: research.PriceType. A listing that sold via an accepted Best Offer
    #: never yields an "exact" price -- the asking price shown on the page is
    #: not what changed hands, and the real figure is not published.
    price_type: Mapped[str] = mapped_column(String, default="exact")
    similarities: Mapped[str] = mapped_column(Text, default="")
    differences: Mapped[str] = mapped_column(Text, default="")
    relevance: Mapped[float] = mapped_column(Float, default=0.5)  # 0..1
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    #: True for comparables an automated research provider proposed. They
    #: count toward the evidence count and confidence score but are excluded
    #: from the approval gate's evidence check until a human confirms them --
    #: same spirit as is_placeholder, but for "plausible, unverified" rather
    #: than "known fake".
    needs_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String, default="manual")  # manual|ebay_api|search_api|agentic
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EstateSubmissionORM(Base):
    """An in-progress Telegram intake session.

    Persisted so the flow survives a bot restart. One open submission per
    Telegram user at a time.
    """

    __tablename__ = "estate_submissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    telegram_user_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    item_id: Mapped[str] = mapped_column(String, default="", index=True)
    state: Mapped[str] = mapped_column(String, default="collecting_photos")
    photo_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_questions: Mapped[list] = mapped_column(JSON, default=list)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    #: The owner's own one-or-two sentence description, accumulated from any
    #: free text sent while photos are still being collected (state
    #: "collecting_photos"). This is what lets Dad answer "Crate & Barrel wall
    #: decoration ... good condition ... can be shipped" once, up front, instead
    #: of via follow-up questions -- it is fed to the vision provider as
    #: context and separately run through hint_parser to pre-resolve as many
    #: of ASKABLE_FIELDS / BOOLEAN_ASKABLE_FIELDS as it plausibly can.
    description_hint: Mapped[str] = mapped_column(Text, default="")
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_media_group_id: Mapped[str] = mapped_column(String, default="")
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class EstateEventORM(Base):
    """Append-only audit trail: every state change, approval, markdown, inquiry."""

    __tablename__ = "estate_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    item_id: Mapped[str] = mapped_column(String, index=True, default="")
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str] = mapped_column(String, default="system")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class EstateInquiryORM(Base):
    """A buyer inquiry routed in from the selling inbox or the website form."""

    __tablename__ = "estate_inquiries"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uid)
    item_id: Mapped[str] = mapped_column(String, index=True, default="")
    channel: Mapped[str] = mapped_column(String, default="email")
    buyer_name: Mapped[str] = mapped_column(String, default="")
    buyer_contact: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")
    offer_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    label: Mapped[str] = mapped_column(String, default="New Inquiry")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
