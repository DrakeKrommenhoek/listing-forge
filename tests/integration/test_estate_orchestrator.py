"""The core vertical slice: photos + one sentence -> a priced, ranked record.

Everything here runs offline against a temporary SQLite file and the mock
vision provider. No network call, no API key, no paid service, and no
production data is touched.

The point of this module is to hold the north star honest: if these pass, a
submitter can send photos and a sentence and the system does the rest of the
job by itself, up to (but never past) the human approval gate.
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-orch-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"
os.environ["ESTATE_MOVE_OUT_DATE"] = "2026-08-31"

from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import (  # noqa: E402
    orchestrator,
    pipeline,
    priority,
    research,
)
from estate.repository import (  # noqa: E402
    CompRepository,
    ItemRepository,
)
from estate.schema import ProcessingStage  # noqa: E402
from estate._compat import get_session, init_db  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _db():
    init_db()


@pytest.fixture()
def session():
    s = get_session()
    yield s
    s.close()


def _photos(session, item_id, n=4):
    for i in range(n):
        pipeline.attach_photo(session, item_id, b"orch-photo-%d" % i, ext="jpg")


def _comp(session, item_id, url, price=100.0, sold=True, confirmed=True, **kw):
    """A real, sourced comparable, as a human researcher would have entered it."""
    return CompRepository(session).add(
        item_id, platform="ebay", title="Comparable listing", url=url,
        is_sold=sold, price=price, condition="Good", observed_date="2026-07-15",
        relevance=0.9, needs_confirmation=not confirmed, **kw
    )


def _ready_item(session, owner="telegram:orch", comps=3, answer_everything=True):
    """An item taken as far as the automated pipeline can take it alone."""
    item = pipeline.start_item(session, owner=owner)
    _photos(session, item.item_id, 4)
    ItemRepository(session).update(
        item.item_id, actor="test", category="Home Decor", item_name="Teak wall art",
        brand="Crate & Barrel", condition="Good",
    )
    pipeline.identify_item(session, item.item_id, hint="Crate & Barrel wall decoration")
    if answer_everything:
        fresh = ItemRepository(session).get(item.item_id)
        for key in list(fresh.missing_fields or []):
            pipeline.apply_answer(session, item.item_id, key, "yes" if key in
                                  ("ownership_approval", "shipping_feasible") else "skip")
    for n in range(comps):
        _comp(session, item.item_id, f"https://ebay.com/itm/{item.item_id}-{n}",
              price=100.0 + n * 10)
    return item


# ---------------------------------------------------------------------------
# The whole slice
# ---------------------------------------------------------------------------

def test_photos_and_one_sentence_produce_a_priced_ranked_record(session):
    """The north star, asserted end to end."""
    item = _ready_item(session)
    job = orchestrator.process_item(session, item.item_id)

    assert job.ok, job.message
    assert job.stage == ProcessingStage.READY_FOR_REVIEW.value

    fresh = ItemRepository(session).get(item.item_id)
    # Identified
    assert fresh.item_name
    # Researched
    assert fresh.comp_count >= 3
    assert fresh.comp_median
    assert fresh.research_date
    # Priced
    assert fresh.initial_list_price and fresh.expected_sale_price and fresh.floor_price
    assert fresh.floor_price < fresh.expected_sale_price <= fresh.initial_list_price
    assert fresh.expected_net_proceeds is not None
    # Channelled and written
    assert fresh.primary_marketplace
    assert fresh.listing_title
    assert fresh.listing_packages
    assert "website" in fresh.listing_packages
    # Ranked
    assert fresh.priority_score > 0
    assert "Priority" in (fresh.priority_reasons or "")
    # Queued, not decided
    assert fresh.status == "Needs Review"
    assert fresh.approval_status == "Pending"


def test_every_platform_package_is_copy_ready(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    packages = ItemRepository(session).get(item.item_id).listing_packages

    marketplace_keys = [k for k in packages if k != "website"]
    assert marketplace_keys, "no marketplace package was generated"
    for key in marketplace_keys:
        pkg = packages[key]
        assert pkg["title"], f"{key} has no title"
        assert pkg["description"], f"{key} has no description"
        assert pkg["keywords"], f"{key} has no search keywords"
        assert pkg["condition_disclosure"], f"{key} does not disclose condition"
        assert pkg["terms"], f"{key} has no pickup/shipping terms"
        assert pkg["buyer_qa"], f"{key} has no buyer FAQ"

    website = packages["website"]
    assert website["product_title"] and website["description"]
    assert "image_order" in website


def test_ebay_title_respects_the_platform_limit(session):
    from estate import listing

    item = _ready_item(session)
    ItemRepository(session).update(
        item.item_id, actor="test",
        item_name="A deliberately enormous item name " * 8,
    )
    fresh = ItemRepository(session).get(item.item_id)
    assert len(listing.build_title(fresh, "ebay")) <= listing.TITLE_LIMITS["ebay"]


# ---------------------------------------------------------------------------
# Idempotency and restart safety
# ---------------------------------------------------------------------------

def test_reprocessing_does_not_duplicate_comparables_or_listings(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    first = ItemRepository(session).get(item.item_id)
    comps_before = len(CompRepository(session).for_item(item.item_id))
    packages_before = dict(first.listing_packages)

    orchestrator.process_item(session, item.item_id)
    orchestrator.process_item(session, item.item_id)

    after = ItemRepository(session).get(item.item_id)
    assert len(CompRepository(session).for_item(item.item_id)) == comps_before
    assert sorted(after.listing_packages) == sorted(packages_before)
    assert after.comp_count == first.comp_count


def test_duplicate_comparable_urls_are_suppressed(session):
    item = pipeline.start_item(session, owner="telegram:dupe")
    repo = CompRepository(session)
    _first, created_a = repo.add_unique(
        item.item_id, platform="ebay", title="x",
        url="https://www.ebay.com/itm/12345?hash=abc", price=50.0, is_sold=True,
    )
    _second, created_b = repo.add_unique(
        item.item_id, platform="ebay", title="x (again)",
        url="http://ebay.com/itm/12345/", price=50.0, is_sold=True,
    )
    assert created_a is True
    assert created_b is False, "the same listing was stored twice and doubled its weight"
    assert len(repo.for_item(item.item_id)) == 1


def test_a_restart_resumes_from_the_recorded_stage(session):
    """Job state lives in the database, so a fresh session picks it up."""
    item_id = _ready_item(session).item_id
    ItemRepository(session).update(
        item_id, actor="test",
        processing_stage=ProcessingStage.RESEARCHING.value,
    )
    session.close()  # stands in for the bot process dying mid-job

    resumed = get_session()
    try:
        job = orchestrator.process_item(resumed, item_id)
        assert job.ok
        assert ItemRepository(resumed).get(item_id).processing_stage == (
            ProcessingStage.READY_FOR_REVIEW.value
        )
    finally:
        resumed.close()


# ---------------------------------------------------------------------------
# Owner facts are never overwritten
# ---------------------------------------------------------------------------

def test_owner_answers_are_protected_from_later_automated_writes(session):
    item = pipeline.start_item(session, owner="telegram:owner")
    _photos(session, item.item_id, 3)
    pipeline.identify_item(session, item.item_id)
    pipeline.apply_answer(session, item.item_id, "condition", "fair")
    assert "condition" in (ItemRepository(session).get(item.item_id).owner_confirmed_fields or [])

    written = orchestrator._safe_update(
        session, item.item_id, "orchestrator", condition="Excellent", brand="Guess",
    )
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.condition == "Fair", "an automated pass overwrote the owner's own answer"
    assert "condition" not in written
    assert fresh.brand == "Guess", "unprotected fields should still be writable"


def test_ownership_is_never_settable_by_the_automated_path(session):
    item = _ready_item(session)
    orchestrator._safe_update(session, item.item_id, "orchestrator", ownership_approval=True)
    # Whatever the owner said stands; the orchestrator cannot flip it.
    assert "ownership_approval" in orchestrator._protected(
        ItemRepository(session).get(item.item_id)
    )


def test_an_approved_price_is_not_recomputed_on_a_later_run(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    ItemRepository(session).update(
        item.item_id, actor="reviewer", approval_status="Approved",
        initial_list_price=999.0, current_price=999.0, floor_price=500.0,
    )
    orchestrator.run_pricing(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.initial_list_price == 999.0
    assert fresh.floor_price == 500.0


def test_the_job_stands_down_once_a_human_owns_the_item(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    ItemRepository(session).update(
        item.item_id, actor="reviewer", processing_stage=ProcessingStage.APPROVED.value
    )
    job = orchestrator.process_item(session, item.item_id)
    assert job.ran == []
    assert ItemRepository(session).get(item.item_id).processing_stage == (
        ProcessingStage.APPROVED.value
    )


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

def test_vision_failure_saves_everything_and_records_the_stage(session, monkeypatch):
    item = pipeline.start_item(session, owner="telegram:fail")
    _photos(session, item.item_id, 3)

    class Exploding:
        name = "exploding"

        def identify(self, paths, hint=""):
            raise RuntimeError("secret-token-abc123 leaked in this message")

    monkeypatch.setattr(pipeline, "get_vision_provider", lambda *a, **k: Exploding())
    job = orchestrator.process_item(session, item.item_id)

    assert job.ok is False
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.processing_stage == ProcessingStage.ERROR.value
    # The photos survive, and the exception MESSAGE never reaches the database.
    from estate.repository import PhotoRepository

    assert PhotoRepository(session).count(item.item_id) == 3
    assert "secret-token" not in json.dumps(
        {c: str(getattr(fresh, c, "")) for c in
         ("processing_error", "processing_failed_stage", "notes", "description")}
    )


def test_research_failure_preserves_evidence_already_gathered(session, monkeypatch):
    from estate import research_provider as rp_mod

    item = _ready_item(session, comps=2)

    class Exploding:
        name = "exploding"

        def find_comparables(self, item):
            raise RuntimeError("provider down")

    monkeypatch.setattr(rp_mod, "get_research_provider", lambda *a, **k: Exploding())
    summary, warnings = orchestrator.run_research(session, item.item_id)

    assert summary.comp_count == 2, "existing comparables were lost on a provider failure"
    assert any("could not run" in w for w in warnings)
    assert ItemRepository(session).get(item.item_id).research_status == "Failed"


def test_repeated_failures_stop_and_wait_for_a_person(session):
    item = _ready_item(session)
    ItemRepository(session).update(
        item.item_id, actor="test",
        processing_stage=ProcessingStage.ERROR.value,
        processing_attempts=orchestrator.MAX_ATTEMPTS,
    )
    job = orchestrator.process_item(session, item.item_id)
    assert job.ok is False
    assert "waiting for a person" in job.message


# ---------------------------------------------------------------------------
# Evidence rules survive the automated path
# ---------------------------------------------------------------------------

def test_an_active_asking_price_never_counts_as_a_sold_comparable(session):
    item = _ready_item(session, comps=0)
    _comp(session, item.item_id, "https://ebay.com/itm/active-1", price=200.0, sold=False)
    summary, _ = orchestrator.run_research(session, item.item_id)
    assert summary.sold_count == 0
    assert summary.active_count == 1


def test_a_best_offer_sale_never_sets_the_price(session):
    item = _ready_item(session, comps=0)
    _comp(session, item.item_id, "https://ebay.com/itm/bo-1", price=400.0,
          sold=True, price_type="hidden")
    summary, _ = orchestrator.run_research(session, item.item_id)
    rec, _warn = orchestrator.run_pricing(session, item.item_id, summary=summary)
    assert summary.median is None
    assert rec.initial_list_price is None, "priced off a figure nobody actually paid"


def test_unconfirmed_automated_evidence_cannot_unlock_approval(session):
    item = _ready_item(session, comps=0)
    for n in range(4):
        _comp(session, item.item_id, f"https://ebay.com/itm/auto-{n}",
              price=120.0, confirmed=False)
    orchestrator.process_item(session, item.item_id)
    blockers = ItemRepository(session).get(item.item_id).approval_blockers
    assert "awaiting confirmation" in blockers


def test_a_proposed_comparable_without_a_url_is_discarded(session, monkeypatch):
    from estate import research_provider as rp_mod
    from estate.research import Comparable
    from estate.research_provider import ResearchResult

    item = _ready_item(session, comps=0)

    class Sloppy:
        name = "sloppy"

        def find_comparables(self, _item):
            return ResearchResult(
                comparables=[
                    Comparable(platform="ebay", title="no source", url="", price=90.0,
                               is_sold=True),
                    Comparable(platform="ebay", title="sourced",
                               url="https://ebay.com/itm/ok-1", price=90.0, is_sold=True),
                ],
                status="Complete", provider="sloppy",
            )

    monkeypatch.setattr(rp_mod, "get_research_provider", lambda *a, **k: Sloppy())
    _summary, warnings = orchestrator.run_research(session, item.item_id)
    urls = [c.url for c in CompRepository(session).for_item(item.item_id)]
    assert urls == ["https://ebay.com/itm/ok-1"]
    assert any("no source link" in w for w in warnings)


def test_mock_identification_is_blocked_from_publication(session):
    item = pipeline.start_item(session, owner="telegram:mock")
    _photos(session, item.item_id, 3)
    pipeline.identify_item(session, item.item_id)  # mock provider -> "[MOCK] ..."
    fresh = ItemRepository(session).get(item.item_id)
    for key in list(fresh.missing_fields or []):
        pipeline.apply_answer(session, item.item_id, key, "skip")
    orchestrator.process_item(session, item.item_id)
    blockers = ItemRepository(session).get(item.item_id).approval_blockers
    assert "mock" in blockers.lower()


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------

def test_outstanding_questions_pause_the_job_without_losing_anything(session):
    item = pipeline.start_item(session, owner="telegram:q")
    _photos(session, item.item_id, 3)
    job = orchestrator.process_item(session, item.item_id)

    assert job.stage == ProcessingStage.NEEDS_INFORMATION.value
    assert job.questions
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.status == "Draft", "an unanswered item must not reach the review queue"
    assert fresh.priority_score > 0, "it should still be rankable while it waits"


def test_the_owners_sentence_removes_questions_instead_of_asking_them(session):
    item = pipeline.start_item(session, owner="telegram:hint")
    _photos(session, item.item_id, 3)
    orchestrator.process_item(
        session, item.item_id,
        hint="This is mine to sell, it's in good condition and it could be shipped.",
    )
    fresh = ItemRepository(session).get(item.item_id)
    assert "ownership_approval" not in (fresh.missing_fields or [])
    assert fresh.ownership_approval is True


# ---------------------------------------------------------------------------
# External research job contract
# ---------------------------------------------------------------------------

def test_the_research_job_file_states_the_targets_and_the_rules(session):
    item = _ready_item(session, comps=0)
    fresh = ItemRepository(session).get(item.item_id)
    path = research.write_research_job(fresh)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["item_id"] == item.item_id
    assert len(payload["targets"]) >= 10
    assert payload["queries"]
    assert any("Never invent" in r for r in payload["rules"])


def test_imported_results_are_validated_the_same_way_a_worksheet_is(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"comparables": [
        {"platform": "ebay", "title": "no url", "price": 100, "sold_or_active": "sold"},
        {"platform": "ebay", "title": "free", "url": "https://ebay.com/a", "price": 0,
         "sold_or_active": "sold"},
        {"platform": "ebay", "title": "active", "url": "https://ebay.com/b", "price": 250,
         "sold_or_active": "active", "price_type": "exact"},
        {"platform": "ebay", "title": "good", "url": "https://ebay.com/c", "price": 150,
         "sold_or_active": "sold", "price_type": "exact"},
        {"platform": "ebay", "title": "odd", "url": "https://ebay.com/d", "price": 150,
         "sold_or_active": "sold", "price_type": "vibes"},
    ]}), encoding="utf-8")

    comps, problems = research.import_research_results(path)
    by_title = {c.title: c for c in comps}

    assert "no url" not in by_title and "free" not in by_title
    assert len(problems) >= 3
    # An asking price is forced down to a ceiling no matter what the file claims.
    assert by_title["active"].price_type == "upper_bound"
    assert by_title["active"].is_sold is False
    # An unrecognised claim about price quality is downgraded, never promoted.
    assert by_title["odd"].price_type == "estimated"
    assert by_title["good"].price_type == "exact"
    # Nothing imported can unlock approval on its own.
    assert all(c.needs_confirmation for c in comps)


def test_external_provider_queues_when_no_results_file_exists(session):
    from estate.research_provider import ExternalJobResearchProvider

    item = _ready_item(session, comps=0)
    fresh = ItemRepository(session).get(item.item_id)
    result = ExternalJobResearchProvider().find_comparables(fresh)

    assert result.comparables == []
    assert result.status == "Queued for Manual Research"
    assert research.research_job_path(item.item_id).exists()


# ---------------------------------------------------------------------------
# Vision contract readiness (no paid call is ever made)
# ---------------------------------------------------------------------------

def test_the_vision_contract_carries_every_identification_field():
    from estate.vision import USER_TEMPLATE, normalise

    for key in ("collection", "subcategory", "serial_number", "label_transcription",
                "country_of_manufacture", "shipping_feasible_guess", "sku",
                "materials", "color_finish", "style", "weight_estimate_lbs",
                "alternative_identifications", "additional_measurements_needed"):
        assert key in USER_TEMPLATE, f"the vision prompt never asks for {key}"

    ident = normalise(
        {
            "item_name": "Teak wall art", "category": "Home Decor", "brand": "Crate & Barrel",
            "collection": "Marcel", "sku": "215141", "subcategory": "wall art",
            "label_transcription": "CRATE & BARREL  MARCEL  215141",
            "country_of_manufacture": "Made in India",
            "shipping_feasible_guess": "likely",
            "confidence": {"item_name": 0.95, "brand": 0.95, "sku": 0.9},
            "overall_confidence": 0.93,
        },
        "mock", "test-model", photo_count=5,
    )
    report = ident.identification_report()
    assert report["collection"] == "Marcel"
    assert report["label_transcription"].startswith("CRATE & BARREL")
    assert report["shipping_feasible_guess"] == "likely"
    # The model's shipping opinion must never become the owner's answer.
    assert "shipping_feasible" not in ident.to_item_fields()


def test_all_three_vision_providers_are_registered_and_fall_back_safely(monkeypatch):
    from estate import vision

    assert set(vision._PROVIDERS) == {"mock", "anthropic", "openai"}
    # An unknown or unusable provider degrades to mock rather than raising --
    # and mock output is [MOCK]-prefixed, so it is blocked from publication
    # at the data layer even though nothing raised.
    provider = vision.get_vision_provider("does-not-exist")
    assert isinstance(provider, vision.MockVisionProvider)
    assert provider.fallback_reason


def test_the_collection_and_sku_drive_the_comparable_searches(session):
    item = _ready_item(session, comps=0)
    ItemRepository(session).update(item.item_id, actor="test", sku="215141",
                                   collection="Marcel", model="")
    fresh = ItemRepository(session).get(item.item_id)
    queries = research.build_queries(fresh)
    joined = " | ".join(queries).lower()
    assert "215141" in joined
    assert "marcel" in joined


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------

def test_priority_prefers_the_more_valuable_of_two_equal_items(session):
    cheap = _ready_item(session, comps=3)
    dear = _ready_item(session, comps=0)
    for n in range(3):
        _comp(session, dear.item_id, f"https://ebay.com/itm/{dear.item_id}-{n}",
              price=600.0 + n * 10)

    orchestrator.process_item(session, cheap.item_id)
    orchestrator.process_item(session, dear.item_id)

    a = ItemRepository(session).get(cheap.item_id)
    b = ItemRepository(session).get(dear.item_id)
    assert b.priority_score > a.priority_score


def test_priority_is_explained_term_by_term(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    score = priority.score_item(fresh)

    assert abs(score.score - round(sum(score.terms.values()), 1)) < 1.5
    explanation = score.explain()
    for term in ("value", "readiness", "confidence", "ease"):
        assert term in explanation
    assert score.reasons


def test_a_sold_item_drops_out_of_the_priority_ranking(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    ItemRepository(session).set_status(item.item_id, "Sold", actor="test")
    fresh = ItemRepository(session).get(item.item_id)
    assert priority.score_item(fresh).score == 0.0
    assert fresh.item_id not in [i.item_id for i in priority.ranked([fresh])]


def test_urgency_rises_as_the_move_out_date_approaches(session):
    from datetime import date

    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    ItemRepository(session).update(fresh.item_id, actor="test",
                                   move_out_deadline="2026-08-31")
    fresh = ItemRepository(session).get(item.item_id)

    early = priority.score_item(fresh, today=date(2026, 6, 1)).terms["urgency"]
    late = priority.score_item(fresh, today=date(2026, 8, 25)).terms["urgency"]
    overdue = priority.score_item(fresh, today=date(2026, 9, 5)).terms["urgency"]
    assert early < late <= overdue


def test_a_stalled_listing_is_pushed_down_not_up(session):
    from datetime import date, timedelta

    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    ItemRepository(session).update(
        item.item_id, actor="test", inquiry_count=0,
        listed_on=(date.today() - timedelta(days=30)).isoformat(),
    )
    fresh = ItemRepository(session).get(item.item_id)
    score = priority.score_item(fresh)
    assert score.terms.get("stall_penalty", 0) < 0
    assert any("markdown" in r for r in score.reasons)


def test_the_inventory_views_partition_the_work_sensibly(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    items = ItemRepository(session).all()

    ready = priority.view(items, "ready_for_review")
    assert item.item_id in [i.item_id for i in ready]

    # Every view must be defined, callable, and return items sorted by priority.
    for name in priority.VIEWS:
        result = priority.view(items, name)
        scores = [float(i.priority_score or 0) for i in result]
        assert scores == sorted(scores, reverse=True), f"view {name} is not ranked"
        assert name in priority.VIEW_LABELS


def test_ranked_puts_the_highest_priority_item_first(session):
    items = ItemRepository(session).all()
    orchestrator.reprioritise_all(session)
    ranked = priority.ranked(ItemRepository(session).all())
    scores = [float(i.priority_score or 0) for i in ranked]
    assert scores == sorted(scores, reverse=True)
    assert all(i.status not in priority.CLOSED_STATUSES for i in ranked)
    assert len(ranked) <= len(items)


# ---------------------------------------------------------------------------
# Spreadsheet / export
# ---------------------------------------------------------------------------

def test_the_export_carries_every_new_decision_field(session):
    from estate import exporter
    from estate.schema import FIELD_KEYS

    for key in ("priority_score", "expected_net_proceeds", "selling_difficulty",
                "shipping_difficulty", "processing_stage", "identification_confidence",
                "research_confidence", "estimated_fees", "approval_blockers"):
        assert key in FIELD_KEYS, f"{key} is missing from the inventory schema"

    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    out = exporter.export_csv(session, os.path.join(TMP, "inventory.csv"))
    text = out.read_text(encoding="utf-8")
    assert "Priority Score" in text
    assert "Expected Net Proceeds" in text


def test_the_stage_follows_the_reviewers_decision(session):
    item = _ready_item(session)
    orchestrator.process_item(session, item.item_id)
    ItemRepository(session).set_status(item.item_id, "Approved", actor="reviewer")
    orchestrator.sync_stage_with_status(session, item.item_id)
    assert ItemRepository(session).get(item.item_id).processing_stage == (
        ProcessingStage.APPROVED.value
    )
