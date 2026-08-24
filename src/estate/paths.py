"""Standardised on-disk layout for every item.

    inventory/<ITEM_ID>/
        original/     untouched photos exactly as received
        web/          resized, stripped-of-EXIF images for the website
        listing/      marketplace-sized images
        research/     comps worksheets and research notes
        copy/         generated listing packages (one JSON + one .md per platform)
        approval/     approval record (who approved what, when, at what price)
        item.json     denormalised snapshot of the inventory row

Filename convention:

    <ITEM_ID>_<NN>_<slot>.<ext>          e.g. DK-202608-004_01_hero.jpg

``slot`` is one of the photography checklist slots (hero, front, back, side-l,
side-r, top, bottom, label, serial, accessories, dimensions, defect, detail).
Unclassified photos use ``photo``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

PHOTO_SLOTS = [
    "hero",
    "front",
    "back",
    "side-l",
    "side-r",
    "top",
    "bottom",
    "label",
    "serial",
    "accessories",
    "dimensions",
    "defect",
    "detail",
    "photo",
]

SUBDIRS = ("original", "web", "listing", "research", "copy", "approval")

_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def inventory_root() -> Path:
    """Root of the inventory tree. Override with ESTATE_INVENTORY_DIR."""
    return Path(os.environ.get("ESTATE_INVENTORY_DIR", "./data/inventory")).resolve()


def item_dir(item_id: str, create: bool = False) -> Path:
    d = inventory_root() / safe_component(item_id)
    if create:
        for sub in SUBDIRS:
            (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def photo_path(item_id: str, index: int, slot: str = "photo", ext: str = "jpg",
               role: str = "original") -> Path:
    slot = slot if slot in PHOTO_SLOTS else "photo"
    ext = _SAFE.sub("", ext).lstrip(".").lower() or "jpg"
    name = f"{safe_component(item_id)}_{index:02d}_{slot}.{ext}"
    return item_dir(item_id) / role / name


def safe_component(value: str) -> str:
    """Reject path traversal and shell-hostile characters in an ID."""
    cleaned = _SAFE.sub("_", (value or "").strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"unsafe path component: {value!r}")
    return cleaned


def relative_photo_url(path: Path) -> str:
    """Path relative to the inventory root, for storage in photo_links."""
    try:
        return str(path.resolve().relative_to(inventory_root())).replace(os.sep, "/")
    except ValueError:
        return str(path).replace(os.sep, "/")
