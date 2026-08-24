"""Additive-only schema migration for the estate tables.

``Base.metadata.create_all()`` (used by ``init_db()``) creates tables that do
not exist yet, but it does **not** add columns to a table that already exists.
Every session that adds a field to ``EstateItemORM`` or ``EstateCompORM`` would
otherwise work perfectly against a fresh database and then crash with
``OperationalError: no such column`` the moment it runs against the real
database on the VPS, which already has these tables from an earlier deploy.

``ensure_estate_schema()`` closes that gap the only way that is safe for
production data: it introspects the live table via ``PRAGMA table_info`` and
issues ``ALTER TABLE ... ADD COLUMN`` for anything the ORM model declares that
the database does not yet have. It never drops, renames, or alters an existing
column, and it never touches a row. Run on every startup; a no-op when nothing
changed.

This is SQLite-specific (the project's only supported database — see
CLAUDE.md). ``ADD COLUMN`` with a constant default is a fast, non-rewriting
operation in SQLite as long as the default is a fixed literal, which is true
for every column on these models.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from estate._compat import get_logger

logger = get_logger(__name__)

# Tables this migration is allowed to touch. Deliberately explicit rather than
# "every table on Base" so a future non-estate model can never be affected by
# a bug here.
_MANAGED_TABLES = (
    "estate_items",
    "estate_photos",
    "estate_comps",
    "estate_submissions",
    "estate_events",
    "estate_inquiries",
)


def _sqlite_default_literal(column) -> str:
    """A safe, fixed-value SQL literal for ALTER TABLE ... ADD COLUMN.

    SQLite requires ADD COLUMN defaults to be a constant, not an expression, so
    server_default/onupdate callables (e.g. func.now()) cannot be used as-is.
    New columns default to NULL, 0, '', or empty JSON, matching the Python-side
    default the ORM applies to new rows going forward; existing rows simply get
    the SQL-level default until something writes to them.
    """
    from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text

    # Prefer the ORM's own scalar Python-side default when there is one, so a
    # pre-existing row ends up with the same value a brand-new row would get
    # (e.g. processing_stage="Photos Received" rather than ""). Only scalar
    # literals are safe here -- callables (default=list, default=dict) are
    # handled by type below instead.
    default = getattr(column, "default", None)
    if default is not None and not getattr(default, "is_callable", True):
        arg = default.arg
        if isinstance(arg, bool):
            return "1" if arg else "0"
        if isinstance(arg, (int, float)):
            return str(arg)
        if isinstance(arg, str):
            return "'%s'" % arg.replace("'", "''")

    py_type = column.type
    if isinstance(py_type, Boolean):
        return "0"
    if isinstance(py_type, Integer):
        return "0"
    if isinstance(py_type, Float):
        return "NULL" if column.nullable else "0"
    if isinstance(py_type, JSON):
        return "'[]'" if column.default and column.default.arg is list else "'{}'"
    if isinstance(py_type, DateTime):
        return "NULL"
    if isinstance(py_type, (String, Text)):
        return "''"
    return "NULL"


def ensure_estate_schema(engine: Engine) -> list[str]:
    """Add any columns the ORM models declare that the live tables lack.

    Returns the list of "table.column" additions made, for logging. Safe to
    call on every process start; does nothing when the schema already matches.
    """
    from estate.models import Base as _EstateBase  # noqa: F401
    from estate._compat import Base

    added: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in _MANAGED_TABLES:
                continue
            if table.name not in existing_tables:
                # Brand-new table: create_all() already handled it, or will on
                # the next call. Nothing to migrate.
                continue

            live_columns = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in live_columns:
                    continue
                default_sql = _sqlite_default_literal(column)
                ddl = 'ALTER TABLE "%s" ADD COLUMN "%s" %s DEFAULT %s' % (
                    table.name, column.name, column.type.compile(dialect=conn.dialect),
                    default_sql,
                )
                conn.execute(text(ddl))
                added.append("%s.%s" % (table.name, column.name))

    if added:
        logger.info({"action": "estate_schema_migrated", "columns_added": added})
    return added
