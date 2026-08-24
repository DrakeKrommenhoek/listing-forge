"""Item ID allocation.

Format: ``DK-YYYYMM-NNN`` — human-readable, sortable, and short enough to say
out loud on the phone or type into an email subject line.

The counter is per-month and derived from what already exists in the database,
so IDs never collide even across restarts. Allocation takes a row lock via a
single INSERT; a UNIQUE primary key on estate_items is the final guarantee.
"""

from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

ID_RE = re.compile(r"^([A-Z]{2,4})-(\d{6})-(\d{3,})$")


def is_valid_item_id(value: str) -> bool:
    return bool(ID_RE.match(value or ""))


def next_item_id(session: Session, prefix: str = "DK", now: datetime | None = None) -> str:
    """Return the next unused item ID for the current month."""
    from estate.models import EstateItemORM

    now = now or datetime.now()
    period = now.strftime("%Y%m")
    stem = f"{prefix}-{period}-"

    rows = session.execute(
        select(EstateItemORM.item_id).where(EstateItemORM.item_id.like(stem + "%"))
    ).scalars().all()

    highest = 0
    for rid in rows:
        m = ID_RE.match(rid)
        if m:
            highest = max(highest, int(m.group(3)))
    return f"{stem}{highest + 1:03d}"


def count_items(session: Session) -> int:
    from estate.models import EstateItemORM

    return int(session.execute(select(func.count(EstateItemORM.item_id))).scalar() or 0)
