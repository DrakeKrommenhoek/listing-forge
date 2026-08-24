"""The additive migration must survive meeting the real VPS database.

This session added fourteen columns to ``estate_items``. Every one of them
works perfectly against a database created fresh by ``create_all()`` and would
crash on the first query against the production database on the VPS, which
already has these tables from an earlier deploy -- ``create_all()`` creates
missing tables but never adds a column to an existing one.

So the thing worth testing is not the happy path. It is: build a table with
the OLD column set, run the migration, and prove the new columns arrive, the
existing rows survive with their values intact, and a second run is a no-op.
"""

from __future__ import annotations

import os
import tempfile

import pytest
from sqlalchemy import create_engine, inspect, text

TMP = tempfile.mkdtemp(prefix="estate-migrate-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TMP}/migrate.db")

from estate.migrations import ensure_estate_schema  # noqa: E402

#: A deliberately old, pre-this-session shape of the table.
LEGACY_DDL = """
CREATE TABLE estate_items (
    item_id VARCHAR PRIMARY KEY,
    item_name VARCHAR DEFAULT '',
    category VARCHAR DEFAULT '',
    brand VARCHAR DEFAULT '',
    model VARCHAR DEFAULT '',
    condition VARCHAR DEFAULT 'Unknown',
    status VARCHAR DEFAULT 'Draft',
    current_price FLOAT,
    notes TEXT DEFAULT ''
)
"""

NEW_COLUMNS = [
    "processing_stage", "identification_confidence", "research_confidence",
    "estimated_fees", "expected_net_proceeds", "selling_difficulty",
    "shipping_difficulty", "priority_score", "priority_reasons",
    "research_blockers", "approval_blockers", "last_activity",
    "processing_attempts", "processing_error", "processing_failed_stage",
    "last_processed_at", "owner_confirmed_fields", "collection", "subcategory",
]


@pytest.fixture()
def legacy_engine(tmp_path):
    engine = create_engine("sqlite:///%s" % (tmp_path / "legacy.db"))
    with engine.begin() as conn:
        conn.execute(text(LEGACY_DDL))
        conn.execute(text(
            "INSERT INTO estate_items (item_id, item_name, status, current_price, notes) "
            "VALUES ('DK-202601-001', 'Existing item', 'Listed', 125.0, 'keep me')"
        ))
    return engine


def test_the_migration_adds_every_new_column_without_touching_a_row(legacy_engine):
    added = ensure_estate_schema(legacy_engine)

    columns = {c["name"] for c in inspect(legacy_engine).get_columns("estate_items")}
    for column in NEW_COLUMNS:
        assert column in columns, f"{column} would crash on the real database"
        assert f"estate_items.{column}" in added

    with legacy_engine.begin() as conn:
        row = conn.execute(text(
            "SELECT item_name, status, current_price, notes FROM estate_items"
        )).fetchone()
    assert row == ("Existing item", "Listed", 125.0, "keep me")


def test_new_columns_land_with_the_same_default_a_new_row_would_get(legacy_engine):
    ensure_estate_schema(legacy_engine)
    with legacy_engine.begin() as conn:
        stage, score, attempts = conn.execute(text(
            "SELECT processing_stage, priority_score, processing_attempts FROM estate_items"
        )).fetchone()
    # An existing item must not read back as an empty stage, which nothing
    # downstream knows how to display.
    assert stage == "Photos Received"
    assert score == 0
    assert attempts == 0


def test_running_the_migration_twice_changes_nothing(legacy_engine):
    first = ensure_estate_schema(legacy_engine)
    second = ensure_estate_schema(legacy_engine)
    assert first, "the first run should have had work to do"
    assert second == [], "the migration is not idempotent"


def test_the_migration_never_touches_a_table_it_does_not_own(legacy_engine):
    with legacy_engine.begin() as conn:
        conn.execute(text("CREATE TABLE tasks (id VARCHAR PRIMARY KEY, title VARCHAR)"))
    before = {c["name"] for c in inspect(legacy_engine).get_columns("tasks")}
    ensure_estate_schema(legacy_engine)
    after = {c["name"] for c in inspect(legacy_engine).get_columns("tasks")}
    assert before == after
