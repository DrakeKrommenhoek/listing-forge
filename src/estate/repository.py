"""Data access for the estate sale system.

Thin, explicit repositories. No lazy magic — every write commits and every
state change writes an EstateEventORM row so the audit trail is complete.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from estate.ids import next_item_id
from estate.models import (
    EstateCompORM,
    EstateEventORM,
    EstateInquiryORM,
    EstateItemORM,
    EstatePhotoORM,
    EstateSubmissionORM,
)
from estate.schema import FIELD_KEYS


class EventLog:
    def __init__(self, session: Session):
        self.s = session

    def record(self, item_id: str, event_type: str, actor: str = "system", **detail: Any) -> None:
        self.s.add(
            EstateEventORM(
                item_id=item_id or "", event_type=event_type, actor=actor, detail=detail or {}
            )
        )
        self.s.commit()

    def for_item(self, item_id: str) -> list:
        return list(
            self.s.execute(
                select(EstateEventORM)
                .where(EstateEventORM.item_id == item_id)
                .order_by(EstateEventORM.created_at)
            ).scalars()
        )


class ItemRepository:
    def __init__(self, session: Session):
        self.s = session
        self.events = EventLog(session)

    # -- create / read ------------------------------------------------------

    def create(self, owner: str, prefix: str = "DK", **fields: Any) -> EstateItemORM:
        item_id = next_item_id(self.s, prefix=prefix)
        item = EstateItemORM(item_id=item_id, submission_owner=owner, status="Draft")
        for k, v in fields.items():
            if k in FIELD_KEYS or hasattr(item, k):
                setattr(item, k, v)
        self.s.add(item)
        self.s.commit()
        self.events.record(item_id, "item_created", actor=owner)
        return item

    def get(self, item_id: str) -> EstateItemORM | None:
        return self.s.get(EstateItemORM, item_id)

    def all(self) -> list:
        return list(
            self.s.execute(
                select(EstateItemORM).order_by(EstateItemORM.item_id)
            ).scalars()
        )

    def by_status(self, *statuses: str) -> list:
        return [i for i in self.all() if i.status in statuses]

    def needing_review(self) -> list:
        return self.by_status("Needs Review", "Draft")

    # -- update -------------------------------------------------------------

    def update(self, item_id: str, actor: str = "system", **fields: Any) -> EstateItemORM | None:
        item = self.get(item_id)
        if item is None:
            return None
        changed = {}
        for k, v in fields.items():
            if hasattr(item, k) and getattr(item, k) != v:
                changed[k] = v
                setattr(item, k, v)
        if changed:
            self.s.commit()
            self.events.record(item_id, "item_updated", actor=actor, changed=list(changed))
        return item

    def set_status(self, item_id: str, status: str, actor: str = "system",
                   reason: str = "") -> EstateItemORM | None:
        item = self.get(item_id)
        if item is None:
            return None
        prior = item.status
        item.status = status
        # Start the markdown clock the first time an item actually goes live.
        if status == "Listed" and not item.listed_on:
            item.listed_on = date.today().isoformat()
        self.s.commit()
        self.events.record(
            item_id, "status_changed", actor=actor, **{"from": prior, "to": status, "reason": reason}
        )
        return item


class PhotoRepository:
    def __init__(self, session: Session):
        self.s = session

    def add(self, item_id: str, **fields: Any) -> EstatePhotoORM:
        existing = self.for_item(item_id)
        photo = EstatePhotoORM(item_id=item_id, sort_order=len(existing), **fields)
        if not existing:
            photo.is_hero = True
        self.s.add(photo)
        self.s.commit()
        return photo

    def exists_hash(self, item_id: str, sha256: str) -> bool:
        if not sha256:
            return False
        return bool(
            self.s.execute(
                select(EstatePhotoORM.id).where(
                    EstatePhotoORM.item_id == item_id,
                    EstatePhotoORM.sha256 == sha256,
                    EstatePhotoORM.role == "original",
                )
            ).first()
        )

    def for_item(self, item_id: str, role: str = "original") -> list:
        return list(
            self.s.execute(
                select(EstatePhotoORM)
                .where(EstatePhotoORM.item_id == item_id, EstatePhotoORM.role == role)
                .order_by(EstatePhotoORM.sort_order)
            ).scalars()
        )

    def count(self, item_id: str, role: str = "original") -> int:
        return len(self.for_item(item_id, role))


class CompRepository:
    def __init__(self, session: Session):
        self.s = session

    def add(self, item_id: str, **fields: Any) -> EstateCompORM:
        comp = EstateCompORM(item_id=item_id, **fields)
        self.s.add(comp)
        self.s.commit()
        return comp

    @staticmethod
    def fingerprint(url: str = "", platform: str = "", title: str = "",
                    price: float | None = None) -> str:
        """Identity of a comparable, for duplicate suppression.

        The URL is the identity when there is one -- normalised so that a
        tracking query string, a trailing slash, or http/https do not make the
        same eBay listing look like two independent data points and quietly
        double its weight in the median. Without a URL (which the pipeline
        rejects anyway) we fall back to platform+title+price.
        """
        clean = (url or "").strip().lower()
        if clean:
            clean = clean.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            for prefix in ("https://", "http://"):
                if clean.startswith(prefix):
                    clean = clean[len(prefix):]
            if clean.startswith("www."):
                clean = clean[4:]
            return "url:" + clean
        return "sig:%s|%s|%s" % (
            (platform or "").strip().lower(),
            " ".join((title or "").strip().lower().split()),
            ("%.2f" % float(price)) if price else "",
        )

    def add_unique(self, item_id: str, **fields: Any) -> tuple:
        """Add a comparable unless an equivalent one is already recorded.

        Returns ``(comp, created)``. This is what makes a retry of the research
        stage idempotent: re-running a provider that returns the same five
        listings must not turn them into ten and inflate the sample size the
        confidence score is built on.
        """
        target = self.fingerprint(
            fields.get("url", ""), fields.get("platform", ""),
            fields.get("title", ""), fields.get("price"),
        )
        for existing in self.for_item(item_id):
            if self.fingerprint(existing.url, existing.platform, existing.title,
                                existing.price) == target:
                return existing, False
        return self.add(item_id, **fields), True

    def for_item(self, item_id: str) -> list:
        return list(
            self.s.execute(
                select(EstateCompORM)
                .where(EstateCompORM.item_id == item_id)
                .order_by(EstateCompORM.created_at)
            ).scalars()
        )

    def clear(self, item_id: str) -> int:
        rows = self.for_item(item_id)
        for r in rows:
            self.s.delete(r)
        self.s.commit()
        return len(rows)


class SubmissionRepository:
    """One open submission per Telegram user; survives process restarts."""

    def __init__(self, session: Session):
        self.s = session

    def open_for_user(self, user_id: str) -> EstateSubmissionORM | None:
        return self.s.execute(
            select(EstateSubmissionORM)
            .where(
                EstateSubmissionORM.telegram_user_id == str(user_id),
                EstateSubmissionORM.is_open.is_(True),
            )
            .order_by(EstateSubmissionORM.created_at.desc())
        ).scalars().first()

    def start(self, user_id: str, item_id: str) -> EstateSubmissionORM:
        self.close_all(user_id)
        sub = EstateSubmissionORM(
            telegram_user_id=str(user_id), item_id=item_id, state="collecting_photos"
        )
        self.s.add(sub)
        self.s.commit()
        return sub

    def save(self, sub: EstateSubmissionORM) -> EstateSubmissionORM:
        sub.updated_at = datetime.now()
        self.s.commit()
        return sub

    def close_all(self, user_id: str) -> int:
        rows = list(
            self.s.execute(
                select(EstateSubmissionORM).where(
                    EstateSubmissionORM.telegram_user_id == str(user_id),
                    EstateSubmissionORM.is_open.is_(True),
                )
            ).scalars()
        )
        for r in rows:
            r.is_open = False
        self.s.commit()
        return len(rows)


class InquiryRepository:
    def __init__(self, session: Session):
        self.s = session

    def add(self, item_id: str, **fields: Any) -> EstateInquiryORM:
        inq = EstateInquiryORM(item_id=item_id, **fields)
        self.s.add(inq)
        item = self.s.get(EstateItemORM, item_id)
        if item is not None:
            item.inquiry_count = (item.inquiry_count or 0) + 1
            if inq.offer_amount and (item.best_offer is None or inq.offer_amount > item.best_offer):
                item.best_offer = inq.offer_amount
        self.s.commit()
        return inq

    def for_item(self, item_id: str) -> list:
        return list(
            self.s.execute(
                select(EstateInquiryORM).where(EstateInquiryORM.item_id == item_id)
            ).scalars()
        )
