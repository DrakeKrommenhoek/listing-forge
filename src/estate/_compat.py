"""Standalone stand-ins for the four things this package borrowed from
Operation D.R.A.K.E. when it lived at src/operation_drake/estate/.

This package was never run outside that host app. These shims exist so the
code is importable and readable on its own, NOT because they were exercised
standalone. See ../../README.md ("What would need to change to run this
again") before trusting any of this in production.

Replaced:
  - operation_drake.config.get_settings      -> get_settings() (env-driven)
  - operation_drake.storage.database.*       -> get_session() / init_db()
  - operation_drake.observability.logging    -> get_logger()
  - operation_drake.models.database.Base     -> Base (local declarative base)

A few tests reach into the host app's config/database modules directly
(`from estate._compat import config as _config`, `... storage import
database as _database`) to monkeypatch settings or reset a test database
between runs. `config` and `database` below are minimal module-like objects
standing in for that so the import lines resolve; the tests were not
re-verified against them.
"""

from __future__ import annotations

import logging
import os
import types
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Local declarative base. The original shared one SQLAlchemy Base with
    the rest of D.R.A.K.E. so estate tables lived in the same agent.db file
    alongside the bot's own tables. Standalone, this package owns its own
    database file instead."""


@dataclass
class Settings:
    """Flat stand-in for the one property (`estate_askable`) and the dozen
    `estate_*` fields the original operation_drake.config.Settings class
    carried. Values come from ESTATE_* environment variables — see
    .env.example for the full list and what each one does."""

    estate_enabled: bool = field(default_factory=lambda: os.getenv("ESTATE_ENABLED", "true").lower() == "true")
    estate_id_prefix: str = field(default_factory=lambda: os.getenv("ESTATE_ID_PREFIX", "IT"))
    estate_inventory_dir: str = field(default_factory=lambda: os.getenv("ESTATE_INVENTORY_DIR", "./data/inventory"))
    estate_vision_provider: str = field(default_factory=lambda: os.getenv("ESTATE_VISION_PROVIDER", "mock"))
    estate_vision_model: str = field(default_factory=lambda: os.getenv("ESTATE_VISION_MODEL", ""))
    estate_research_provider: str = field(default_factory=lambda: os.getenv("ESTATE_RESEARCH_PROVIDER", "manual_queue"))
    estate_allowed_submitter_ids: str = field(default_factory=lambda: os.getenv("ESTATE_ALLOWED_SUBMITTER_IDS", ""))
    estate_reviewer_ids: str = field(default_factory=lambda: os.getenv("ESTATE_REVIEWER_IDS", ""))
    estate_move_out_date: str = field(default_factory=lambda: os.getenv("ESTATE_MOVE_OUT_DATE", ""))
    estate_selling_email: str = field(default_factory=lambda: os.getenv("ESTATE_SELLING_EMAIL", ""))
    estate_catalog_url: str = field(default_factory=lambda: os.getenv("ESTATE_CATALOG_URL", ""))
    estate_brand_name: str = field(default_factory=lambda: os.getenv("ESTATE_BRAND_NAME", "The Collection"))
    estate_pickup_region: str = field(default_factory=lambda: os.getenv("ESTATE_PICKUP_REGION", ""))
    estate_review_port: int = field(default_factory=lambda: int(os.getenv("ESTATE_REVIEW_PORT", "8010")))
    estate_review_token: str = field(default_factory=lambda: os.getenv("ESTATE_REVIEW_TOKEN", ""))
    estate_field_confidence_floor: float = field(
        default_factory=lambda: float(os.getenv("ESTATE_FIELD_CONFIDENCE_FLOOR", "0.35"))
    )
    estate_ask_fields: str = field(default_factory=lambda: os.getenv("ESTATE_ASK_FIELDS", "dimensions"))
    estate_ownership_confirm_hours: int = field(
        default_factory=lambda: int(os.getenv("ESTATE_OWNERSHIP_CONFIRM_HOURS", "12"))
    )

    def estate_submitters(self) -> set[str]:
        return {u.strip() for u in self.estate_allowed_submitter_ids.split(",") if u.strip()}

    def estate_reviewers(self) -> set[str]:
        return {u.strip() for u in self.estate_reviewer_ids.split(",") if u.strip()}

    def estate_askable(self) -> list[str]:
        from estate.schema import ASKABLE_FIELDS

        wanted = {f.strip() for f in self.estate_ask_fields.split(",") if f.strip()}
        return [f for f in ASKABLE_FIELDS if f in wanted]


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Module-like object so `from estate._compat import config as _config` reads
# naturally where a test previously did `from operation_drake import config`.
config = types.SimpleNamespace(get_settings=get_settings)


_DB_PATH = os.getenv("ESTATE_DATABASE_PATH", "./data/listing_forge.db")
_engine = create_engine(f"sqlite:///{_DB_PATH}")
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def get_engine():
    return _engine


def init_db() -> None:
    Base.metadata.create_all(bind=_engine)


@contextmanager
def get_session() -> Session:
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# Same rationale as `config` above, for `from estate._compat import database as _database`.
database = types.SimpleNamespace(get_session=get_session, init_db=init_db, DATABASE_PATH=_DB_PATH)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger
