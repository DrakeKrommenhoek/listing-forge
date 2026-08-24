"""Estate sale system — photo-to-listing pipeline for household move-out sales.

Design constraints (see docs/estate/PROJECT_STATE.md):

- Additive only. This package does not modify existing D.R.A.K.E. behaviour.
  It reuses the existing SQLAlchemy Base, settings object, and Telegram
  Application, but registers its own tables, commands, and config keys.
- Written to run on Python 3.10+ as well as the project's 3.12 target so the
  module can be exercised in constrained environments. Avoid StrEnum and
  datetime.UTC inside this package.
- Never fabricates market evidence. Every comparable must carry a real URL
  supplied by a human or a real API, or be explicitly flagged as a placeholder.
"""

__all__ = ["ESTATE_SCHEMA_VERSION"]

ESTATE_SCHEMA_VERSION = 1
