"""End-to-end pipeline tests: intake -> identify -> question -> draft -> approve.

Runs entirely offline against a temporary SQLite file and the mock vision
provider. Python 3.10 compatible (see tests/unit/test_estate.py for why).
"""

from __future__ import annotations

import os
import tempfile

import pytest

TMP = tempfile.mkdtemp(prefix="estate-int-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"
os.environ["ESTATE_VISION_PROVIDER"] = "mock"

# The settings object and the SQLAlchemy engine are both process-level
# singletons. If another test module imported first and cached them against the
# repository .env, they must be reset before this module's DATABASE_URL applies.
from estate._compat import config as _config  # noqa: E402
from estate._compat import database as _database  # noqa: E402

_config.get_settings.cache_clear()
_database._engine = None
_database._SessionLocal = None

from estate import approval, pipeline, research  # noqa: E402
from estate.repository import (  # noqa: E402
    CompRepository,
    ItemRepository,
    PhotoRepository,
    SubmissionRepository,
)
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
        pipeline.attach_photo(session, item_id, b"photo-bytes-%d" % i, ext="jpg")


# ---------------------------------------------------------------------------

def test_intake_creates_a_unique_id_and_directory(session):
    a = pipeline.start_item(session, owner="telegram:1")
    b = pipeline.start_item(session, owner="telegram:1")
    assert a.item_id != b.item_id

    from estate import paths

    d = paths.item_dir(a.item_id)
    for sub in ("original", "web", "research", "copy", "approval"):
        assert (d / sub).is_dir()


def test_duplicate_photos_are_ignored(session):
    item = pipeline.start_item(session, owner="telegram:1")
    pipeline.attach_photo(session, item.item_id, b"same")
    _photo, note = pipeline.attach_photo(session, item.item_id, b"same")
    assert note == "duplicate"
    assert pipeline.photo_count(session, item.item_id) == 1


def test_photo_links_stay_in_sync_with_stored_files(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 3)
    fresh = ItemRepository(session).get(item.item_id)
    assert len(fresh.photo_links) == 3
    assert len(PhotoRepository(session).for_item(item.item_id)) == 3


def test_identification_requires_photos(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _ident, result = pipeline.identify_item(session, item.item_id)
    assert result.ok is False
    assert "No photos" in result.message


def test_vision_provider_failure_preserves_submission_and_flags_manual_review(session, monkeypatch):
    """If the vision provider raises (API outage, bad response, whatever),
    the photos must not be lost and the item must be routed to a human for
    manual identification rather than silently stuck or, worse, silently
    treated as identified."""
    from estate import pipeline as pipeline_mod
    from estate.schema import ProcessingStage, ReviewStatus

    class ExplodingProvider:
        def identify(self, photo_paths, hint=""):
            raise RuntimeError("simulated vision API outage")

    monkeypatch.setattr(pipeline_mod, "get_vision_provider", lambda *a, **k: ExplodingProvider())

    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 4)
    ident, result = pipeline.identify_item(session, item.item_id)

    assert ident is None
    assert result.ok is False
    assert "saved" in result.message.lower()  # photos are not lost
    assert pipeline.photo_count(session, item.item_id) == 4  # actually still there

    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.review_status == ReviewStatus.NEEDS_MANUAL_IDENTIFICATION.value
    assert fresh.processing_stage == ProcessingStage.ERROR.value
    events = {e.event_type for e in ItemRepository(session).events.for_item(item.item_id)}
    assert "vision_identify_failed" in events


def test_full_intake_to_needs_review(session):
    item = pipeline.start_item(session, owner="telegram:1", move_out_deadline="2026-09-15")
    _photos(session, item.item_id, 5)

    _ident, result = pipeline.identify_item(session, item.item_id)
    assert result.ok is True
    assert result.questions, "the mock provider should not be confident about anything"

    guard = 0
    while guard < 20:
        guard += 1
        key, question = pipeline.next_question(session, item.item_id)
        if key is None:
            break
        assert question
        pipeline.apply_answer(session, item.item_id, key,
                              "Good" if key == "condition" else "skip")

    final = pipeline.finalise_draft(session, item.item_id)
    assert final.ok
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.status == "Needs Review"
    assert research.worksheet_path(item.item_id).exists()
    summary = pipeline.review_summary(session, item.item_id)
    assert item.item_id in summary
    assert "Drake" in summary  # plain-language confirmation, no jargon


def test_hint_sentence_resolves_fields_vision_left_open(session):
    """The owner's own sentence should pre-answer fields the mock vision
    provider (deliberately) never fills in, so Telegram has fewer questions.

    Uses its own submitter ID. Ownership now carries forward from a
    submitter's recent confirmation (see _inherit_recent_ownership), and the
    estate test modules share one process-wide engine when the whole suite
    runs in one pass — so a test that asserts "ownership is still outstanding"
    must not reuse an ID another test has already confirmed.
    """
    item = pipeline.start_item(session, owner="telegram:hint-only")
    _photos(session, item.item_id, 4)
    hint = (
        "Crate & Barrel wall decoration from the dining room. It is in good "
        "condition and can be shipped if needed."
    )
    _ident, result = pipeline.identify_item(session, item.item_id, hint=hint)
    assert result.ok is True

    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.condition == "Good"
    assert fresh.location_in_house == "Dining Room"
    assert fresh.shipping_feasible is True
    # Ownership is never inferable from a sentence like this one, and must
    # still be on the outstanding list.
    assert "ownership_approval" in fresh.missing_fields
    assert "condition" not in fresh.missing_fields
    assert "location_in_house" not in fresh.missing_fields
    assert "shipping_feasible" not in fresh.missing_fields


def test_boolean_fields_are_always_asked_even_when_vision_is_confident(session):
    """A photo can never establish ownership or shipping willingness -- these
    must be outstanding after every identification, hint or no hint.

    A submitter with no prior confirmation, so nothing can be inherited.
    """
    item = pipeline.start_item(session, owner="telegram:booleans-fresh")
    _photos(session, item.item_id, 4)
    _ident, _result = pipeline.identify_item(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert "ownership_approval" in fresh.missing_fields
    assert "shipping_feasible" in fresh.missing_fields


def test_boolean_answer_accepts_yes_no_and_rejects_junk(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id)
    pipeline.identify_item(session, item.item_id)

    bad = pipeline.apply_answer(session, item.item_id, "ownership_approval", "maybe")
    assert bad.ok is False

    good = pipeline.apply_answer(session, item.item_id, "shipping_feasible", "no")
    assert good.ok is True
    assert ItemRepository(session).get(item.item_id).shipping_feasible is False
    assert "shipping_feasible" not in ItemRepository(session).get(item.item_id).missing_fields


def test_boolean_fields_accept_skip_without_looping_forever(session):
    """Regression guard: 'skip' must not be silently rejected for ownership /
    shipping questions -- that traps the submitter in a question they cannot
    get past, since apply_answer previously never removed the field from
    missing_fields on a rejected answer.

    Its own submitter, so ownership is genuinely asked and genuinely skipped
    rather than inherited from an earlier confirmation.
    """
    item = pipeline.start_item(session, owner="telegram:skip-loop")
    _photos(session, item.item_id)
    pipeline.identify_item(session, item.item_id)

    guard = 0
    while guard < 20:
        guard += 1
        key, _q = pipeline.next_question(session, item.item_id)
        if key is None:
            break
        res = pipeline.apply_answer(session, item.item_id, key, "skip")
        assert res.ok is True
    else:
        pytest.fail("next_question/apply_answer looped without terminating")

    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.missing_fields == []
    # Skipped booleans keep the safe/unconfirmed default -- a reviewer always
    # confirms ownership by hand regardless (see approval gate tests below).
    assert fresh.ownership_approval is False


def test_finalise_does_not_overwrite_an_answered_shipping_feasible(session):
    """Regression guard: finalise_draft used to unconditionally overwrite
    shipping_feasible with a category-level guess, silently discarding an
    owner's own answer (from the hint sentence or a direct yes/no)."""
    item = pipeline.start_item(session, owner="telegram:1")
    ItemRepository(session).update(item.item_id, actor="test", category="Electronics")
    _photos(session, item.item_id, 4)
    # "Furniture"-style category default is False, "Electronics" is True --
    # pick the hint to directly contradict Electronics' own default so an
    # accidental category overwrite is unambiguous.
    pipeline.identify_item(session, item.item_id, hint="Pickup only, too heavy to ship.")
    assert ItemRepository(session).get(item.item_id).shipping_feasible is False

    pipeline.finalise_draft(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.shipping_feasible is False, (
        "finalise_draft overwrote the owner's explicit shipping answer"
    )
    assert fresh.pickup_required is True


def test_finalise_seeds_category_default_only_when_never_asked(session):
    """If shipping_feasible is still outstanding when finalise_draft runs
    (an edge case -- normally the question flow resolves it first), the
    category default is a reasonable seed, clearly not a real answer."""
    item = pipeline.start_item(session, owner="telegram:1")
    ItemRepository(session).update(
        item.item_id, actor="test", category="Electronics",
        missing_fields=["shipping_feasible"],
    )
    _photos(session, item.item_id, 4)
    pipeline.finalise_draft(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.shipping_feasible is True  # Electronics' category default


def test_processing_stage_advances_through_the_pipeline(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 4)
    assert ItemRepository(session).get(item.item_id).processing_stage == "Photos Received"

    pipeline.identify_item(session, item.item_id)
    mid = ItemRepository(session).get(item.item_id)
    assert mid.processing_stage == "Needs Information"  # mock provider leaves fields missing

    guard = 0
    while guard < 20:
        guard += 1
        key, _q = pipeline.next_question(session, item.item_id)
        if key is None:
            break
        pipeline.apply_answer(session, item.item_id, key, "skip")

    pipeline.finalise_draft(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.processing_stage == "Ready for Review"


def test_condition_answer_is_validated(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id)
    pipeline.identify_item(session, item.item_id)
    bad = pipeline.apply_answer(session, item.item_id, "condition", "purple")
    assert bad.ok is False
    good = pipeline.apply_answer(session, item.item_id, "condition", "good")
    assert good.ok is True
    assert ItemRepository(session).get(item.item_id).condition == "Good"


# ---------------------------------------------------------------------------
# Approval gate
# ---------------------------------------------------------------------------

def _ready_item(session, with_comps=True, placeholder=False):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 4)
    ItemRepository(session).update(
        item.item_id, actor="test", item_name="Oak side table", category="Furniture",
        condition="Good", ownership_approval=True, weight_lbs=25,
        dimensions="24 x 24 x 22 in",
        # Publication requires an explicit disclosure -- a blank defects field
        # means "nobody looked", not "nothing wrong". See
        # site.publication_blockers.
        defects="Light ring mark on the top surface. Legs are sound.",
    )
    if with_comps:
        for i in range(4):
            CompRepository(session).add(
                item.item_id, platform="eBay", title="table %d" % i,
                url="https://example.com/%d" % i, is_sold=True, price=90 + i * 5,
                condition="Good", observed_date="2026-07-15", relevance=0.85,
                is_placeholder=placeholder,
            )
    return item


def test_mock_item_name_blocks_approval(session):
    """CLAUDE.md / the spec's approval gate: an item name containing [MOCK]
    must never be approvable, no matter how complete the evidence is."""
    item = _ready_item(session)
    ItemRepository(session).update(item.item_id, actor="test", item_name="[MOCK] Oak side table")
    packet = approval.prepare_review(session, item.item_id)
    assert packet.can_approve is False
    assert any("sample/mock" in b.lower() for b in packet.blockers)

    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is False
    assert "sample/mock" in message.lower()


def test_reviewer_edit_cannot_push_price_below_floor(session):
    """'Never lower below the approved floor' must hold even for a reviewer's
    own manual edit, not just the automated markdown engine."""
    item = _ready_item(session)
    ok, message = approval.apply_decision(
        session, item.item_id, "save_edits", actor="tester",
        edits={"floor_price": "100", "current_price": "80"},
    )
    assert ok is False
    assert "floor" in message.lower()
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.current_price != 80.0  # the bad edit must not have been written


def test_reviewer_edit_at_or_above_floor_is_allowed(session):
    item = _ready_item(session)
    ok, message = approval.apply_decision(
        session, item.item_id, "save_edits", actor="tester",
        edits={"floor_price": "100", "current_price": "150"},
    )
    assert ok is True, message
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.current_price == 150.0
    assert fresh.floor_price == 100.0


def test_review_packet_is_fully_populated_for_a_ready_item(session):
    """The reviewer should only need to: correct fields, confirm ownership,
    add missing measurements, review sources, approve/reject pricing,
    approve/reject publication -- which requires prepare_review to hand back
    everything at once, not piecemeal."""
    item = _ready_item(session)
    packet = approval.prepare_review(session, item.item_id, catalog_url="https://example.com/cat")

    assert packet.item is not None and packet.item.item_id == item.item_id
    assert packet.photos  # identification evidence -- all photos
    assert packet.comps and all(c.url for c in packet.comps)  # sourced comparables
    assert packet.summary is not None and packet.summary.comp_count == len(packet.comps)
    assert packet.price is not None  # pricing recommendation
    assert packet.incentive is not None  # pickup price/incentive
    assert packet.markets  # marketplace recommendations
    assert packet.packages and "website" in packet.packages  # listing + website copy
    assert isinstance(packet.missing, list)  # outstanding info the submitter didn't answer
    assert packet.worksheet_path
    assert packet.can_approve is True  # nothing outstanding for this fixture


def test_low_confidence_fields_surface_in_the_review_packet(session):
    item = _ready_item(session)
    ItemRepository(session).update(
        item.item_id, actor="test",
        vision_raw={"identification": {"confidence": {"model": 0.2, "brand": 0.9}}},
    )
    packet = approval.prepare_review(session, item.item_id)
    assert "model" in packet.low_confidence_fields
    assert "brand" not in packet.low_confidence_fields


def test_approval_blocked_when_only_unconfirmed_comps_exist(session):
    """A future automated research provider's proposed comps (needs_confirmation
    =True) must count toward confidence scoring but never unlock approval on
    their own -- a human has to confirm at least one."""
    item = _ready_item(session, with_comps=False)
    CompRepository(session).add(
        item.item_id, platform="eBay", title="proposed table", url="https://example.com/auto",
        is_sold=True, price=95, condition="Good", observed_date="2026-07-15",
        relevance=0.8, needs_confirmation=True, source="agentic",
    )
    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is False
    assert "confirm" in message.lower()


def test_finalise_draft_sets_research_status_via_the_provider(session):
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 4)
    pipeline.identify_item(session, item.item_id)
    pipeline.finalise_draft(session, item.item_id)
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.research_status == "Queued for Manual Research"
    assert research.worksheet_path(item.item_id).exists()


def test_research_provider_instantiation_failure_falls_back_to_manual_queue(session, monkeypatch):
    """A future research provider that fails to construct (bad config,
    missing key) must never take finalise_draft down with it -- it falls
    back to the safe manual queue, exactly like get_vision_provider falls
    back to mock."""
    from estate import research_provider as research_provider_mod

    class ExplodingProvider:
        def __init__(self):
            raise RuntimeError("simulated bad research provider config")

    monkeypatch.setitem(research_provider_mod._PROVIDERS, "broken", ExplodingProvider)
    provider = research_provider_mod.get_research_provider("broken")
    assert isinstance(provider, research_provider_mod.ManualQueueResearchProvider)

    # And the whole finalise_draft path must still complete normally when
    # ESTATE_RESEARCH_PROVIDER is misconfigured this way.
    item = pipeline.start_item(session, owner="telegram:1")
    _photos(session, item.item_id, 4)
    pipeline.identify_item(session, item.item_id)
    monkeypatch.setattr(
        pipeline, "get_research_provider", lambda: research_provider_mod.get_research_provider("broken")
    )
    result = pipeline.finalise_draft(session, item.item_id)
    assert result.ok
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.research_status == "Queued for Manual Research"


def test_approval_blocked_without_evidence(session):
    item = _ready_item(session, with_comps=False)
    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is False
    assert "No comparable evidence" in message
    assert ItemRepository(session).get(item.item_id).approval_status == "Pending"


def test_approval_blocked_by_placeholder_evidence(session):
    item = _ready_item(session, placeholder=True)
    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is False
    assert "Placeholder" in message


def test_approval_blocked_without_ownership_confirmation(session):
    item = _ready_item(session)
    ItemRepository(session).update(item.item_id, actor="test", ownership_approval=False)
    ok, message = approval.apply_decision(session, item.item_id, "approve", actor="tester")
    assert ok is False
    assert "Ownership" in message


def test_approval_succeeds_with_real_evidence_and_writes_a_record(session):
    item = _ready_item(session)
    ok, message = approval.apply_decision(
        session, item.item_id, "approve", actor="tester", note="looks right"
    )
    assert ok is True, message
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.approval_status == "Approved"
    assert fresh.status == "Approved"
    assert fresh.initial_list_price and fresh.floor_price
    assert fresh.floor_price < fresh.initial_list_price
    assert fresh.primary_marketplace

    from estate import paths

    records = list((paths.item_dir(item.item_id) / "approval").glob("*.json"))
    copies = list((paths.item_dir(item.item_id) / "copy").glob("*.md"))
    assert records, "an approval record must be written to disk"
    assert copies, "listing copy must be written to disk"

    events = {e.event_type for e in ItemRepository(session).events.for_item(item.item_id)}
    assert "approved" in events


def test_reviewer_price_override_is_respected(session):
    item = _ready_item(session)
    ok, _ = approval.apply_decision(
        session, item.item_id, "approve", actor="tester",
        edits={"initial_list_price": "175", "current_price": "175", "floor_price": "120"},
    )
    assert ok is True
    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.initial_list_price == 175.0
    assert fresh.floor_price == 120.0


def test_donate_and_not_for_sale_paths(session):
    donate = _ready_item(session)
    ok, _ = approval.apply_decision(session, donate.item_id, "donate", actor="tester")
    assert ok and ItemRepository(session).get(donate.item_id).status == "Donated"

    keep = _ready_item(session)
    ok, _ = approval.apply_decision(session, keep.item_id, "not_for_sale", actor="tester")
    assert ok and ItemRepository(session).get(keep.item_id).status == "Removed"


def test_specialist_and_more_photos_requests(session):
    item = _ready_item(session)
    approval.apply_decision(session, item.item_id, "specialist", actor="tester")
    assert ItemRepository(session).get(item.item_id).review_status == "Needs Specialist Appraisal"
    approval.apply_decision(session, item.item_id, "request_photos", actor="tester")
    assert ItemRepository(session).get(item.item_id).review_status == "Needs More Photos"


def test_comps_worksheet_import_round_trip(session):
    item = _ready_item(session, with_comps=False)
    path = research.write_worksheet(ItemRepository(session).get(item.item_id))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("eBay,Oak table,https://example.com/x,sold,110,,0,Good,,2026-07-10,same,none,0.9,\n")
        fh.write("eBay,Oak table 2,https://example.com/y,sold,95,,10,Good,,2026-07-02,same,none,0.8,\n")
    count, problems = approval.import_comps_for_item(session, item.item_id)
    assert count == 2 and problems == []
    assert len(CompRepository(session).for_item(item.item_id)) == 2


# ---------------------------------------------------------------------------
# Submission state survives a restart
# ---------------------------------------------------------------------------

def test_open_submission_is_recoverable(session):
    item = pipeline.start_item(session, owner="telegram:99")
    subs = SubmissionRepository(session)
    subs.start("99", item.item_id)

    other = get_session()  # simulates a fresh process after a restart
    try:
        recovered = SubmissionRepository(other).open_for_user("99")
        assert recovered is not None
        assert recovered.item_id == item.item_id
        assert recovered.state == "collecting_photos"
    finally:
        other.close()


def test_processing_and_research_state_survive_a_restart(session):
    """Beyond the submission row itself: processing_stage, research_status,
    and missing_fields all live on the item row, not in memory, so a restart
    mid-pipeline (between /done and the next Telegram message, or during a
    slow research pass) loses no state a fresh process would need to resume
    or explain to the submitter via /myitems."""
    item = pipeline.start_item(session, owner="telegram:55")
    _photos(session, item.item_id, 4)
    pipeline.identify_item(session, item.item_id)
    pipeline.finalise_draft(session, item.item_id)

    other = get_session()  # simulates a fresh process after a restart
    try:
        recovered = ItemRepository(other).get(item.item_id)
        assert recovered is not None
        assert recovered.processing_stage == "Ready for Review"
        assert recovered.research_status == "Queued for Manual Research"
        assert recovered.status == "Needs Review"
    finally:
        other.close()


def test_only_one_submission_is_open_per_user(session):
    subs = SubmissionRepository(session)
    a = pipeline.start_item(session, owner="telegram:77")
    b = pipeline.start_item(session, owner="telegram:77")
    subs.start("77", a.item_id)
    subs.start("77", b.item_id)
    assert subs.open_for_user("77").item_id == b.item_id


def test_website_only_publishes_approved_items(session):
    from estate import site

    approved = _ready_item(session)
    approval.apply_decision(session, approved.item_id, "approve", actor="tester")
    draft = _ready_item(session)

    published = {i.item_id for i in site.collect(session)}
    assert approved.item_id in published
    assert draft.item_id not in published


def test_markdown_clock_starts_when_an_item_is_listed(session):
    """listed_on is stamped once, on the first transition to Listed.

    Regression guard: the markdown script previously used next_markdown_date
    as the listing date, which points forward and produced negative ages.
    """
    from datetime import date

    item = _ready_item(session)
    repo = ItemRepository(session)
    assert repo.get(item.item_id).listed_on == ""

    repo.set_status(item.item_id, "Listed", actor="tester")
    stamped = repo.get(item.item_id).listed_on
    assert stamped == date.today().isoformat()

    repo.set_status(item.item_id, "Offer Received", actor="tester")
    repo.set_status(item.item_id, "Listed", actor="tester")
    assert repo.get(item.item_id).listed_on == stamped, "must not be re-stamped"


# ---------------------------------------------------------------------------
# Intake volume: how many questions someone standing in a room actually gets
#
# The submitter is clearing a house on a deadline. Every question is a reason
# to put the phone down, so the ask-set is configuration and the default is
# "almost nothing". These tests pin the behaviour that makes that safe: the
# owner's own sentence still wins, ownership is still confirmed, and nothing
# here can put an unfinished item in front of a buyer.
# ---------------------------------------------------------------------------


def test_the_default_intake_asks_at_most_dimensions_and_ownership(session):
    """The whole point of the change: photos plus a sentence, not a form."""
    item = pipeline.start_item(session, owner="telegram:volume-1")
    _photos(session, item.item_id, 4)
    pipeline.identify_item(session, item.item_id, hint="A wooden side table.")

    fresh = ItemRepository(session).get(item.item_id)
    assert set(fresh.missing_fields or []) <= {
        "dimensions", "ownership_approval", "shipping_feasible"
    }, fresh.missing_fields
    for noisy in ("brand", "model", "approximate_age", "included_accessories",
                  "location_in_house", "defects"):
        assert noisy not in (fresh.missing_fields or [])


def test_the_owners_sentence_still_wins_over_the_models_guess(session):
    """Regression guard for the bug the narrower ask-set introduced.

    Hint answers used to be applied only to fields still in missing_fields.
    Once almost nothing was missing, someone typing "it's in good condition"
    watched the system record Unknown instead.
    """
    item = pipeline.start_item(session, owner="telegram:volume-2")
    _photos(session, item.item_id, 4)
    pipeline.identify_item(
        session, item.item_id,
        hint="Mine to sell. Oak dresser from the bedroom, good condition, "
             "too heavy to ship.",
    )
    pipeline.apply_hint_answers(
        session, item.item_id,
        "Mine to sell. Oak dresser from the bedroom, good condition, "
        "too heavy to ship.",
    )

    fresh = ItemRepository(session).get(item.item_id)
    assert fresh.condition == "Good"
    assert fresh.location_in_house == "Bedroom"
    assert fresh.shipping_feasible is False
    assert fresh.ownership_approval is True
    assert "ownership_approval" not in (fresh.missing_fields or [])


def test_ownership_is_inherited_from_a_recent_confirmation(session):
    """Asked once an evening, not forty times.

    A question someone has stopped reading is worse than no question, because
    it still looks like diligence.
    """
    first = pipeline.start_item(session, owner="telegram:volume-3")
    _photos(session, first.item_id, 2)
    pipeline.identify_item(session, first.item_id, hint="A lamp.")
    pipeline.apply_answer(session, first.item_id, "ownership_approval", "yes")
    assert ItemRepository(session).get(first.item_id).ownership_approval is True

    second = pipeline.start_item(session, owner="telegram:volume-3")
    _photos(session, second.item_id, 2)
    pipeline.identify_item(session, second.item_id, hint="A different lamp.")

    fresh = ItemRepository(session).get(second.item_id)
    assert fresh.ownership_approval is True
    assert "ownership_approval" not in (fresh.missing_fields or [])


def test_inherited_ownership_is_recorded_as_inherited(session):
    """Inspectable: review can tell "he said yes about this" from "he said
    yes about something else twenty minutes ago"."""
    first = pipeline.start_item(session, owner="telegram:volume-4")
    _photos(session, first.item_id, 2)
    pipeline.identify_item(session, first.item_id, hint="A chair.")
    pipeline.apply_answer(session, first.item_id, "ownership_approval", "yes")

    second = pipeline.start_item(session, owner="telegram:volume-4")
    _photos(session, second.item_id, 2)
    pipeline.identify_item(session, second.item_id, hint="Another chair.")

    events = ItemRepository(session).events.for_item(second.item_id)
    inherited = [e for e in events if e.event_type == "ownership_inherited"]
    assert len(inherited) == 1
    assert inherited[0].detail.get("source_item_id") == first.item_id

    fresh = ItemRepository(session).get(second.item_id)
    assert "ownership_approval" not in (fresh.owner_confirmed_fields or [])


def test_ownership_is_never_inherited_across_submitters(session):
    """One person's confirmation says nothing about another person's item."""
    mine = pipeline.start_item(session, owner="telegram:volume-5")
    _photos(session, mine.item_id, 2)
    pipeline.identify_item(session, mine.item_id, hint="A desk.")
    pipeline.apply_answer(session, mine.item_id, "ownership_approval", "yes")

    theirs = pipeline.start_item(session, owner="telegram:someone-else")
    _photos(session, theirs.item_id, 2)
    pipeline.identify_item(session, theirs.item_id, hint="A different desk.")

    fresh = ItemRepository(session).get(theirs.item_id)
    assert fresh.ownership_approval is False
    assert "ownership_approval" in (fresh.missing_fields or [])


def test_ownership_is_never_inherited_from_an_unconfirmed_item(session):
    never = pipeline.start_item(session, owner="telegram:volume-6")
    _photos(session, never.item_id, 2)
    pipeline.identify_item(session, never.item_id, hint="A shelf.")
    # deliberately never answered

    second = pipeline.start_item(session, owner="telegram:volume-6")
    _photos(session, second.item_id, 2)
    pipeline.identify_item(session, second.item_id, hint="Another shelf.")

    fresh = ItemRepository(session).get(second.item_id)
    assert fresh.ownership_approval is False
    assert "ownership_approval" in (fresh.missing_fields or [])


def test_a_quiet_intake_still_cannot_publish_an_unfinished_item(session):
    """The gates are not what got relaxed.

    Asking fewer questions moves work to review; it does not move it to the
    buyer. An item with no defects disclosure and no confirmed evidence stays
    off the site no matter how smooth its intake was.
    """
    from estate import site

    item = pipeline.start_item(session, owner="telegram:volume-7")
    _photos(session, item.item_id, 3)
    pipeline.identify_item(session, item.item_id, hint="Mine to sell. A stool.")
    ItemRepository(session).update(
        item.item_id, actor="test",
        approval_status="Approved", website_status="Queued", status="Ready to List",
        current_price=40, floor_price=20,
    )

    fresh = ItemRepository(session).get(item.item_id)
    blockers = site.publication_blockers(session, fresh)
    assert blockers
    assert item.item_id not in {i.item_id for i in site.collect(session)}
