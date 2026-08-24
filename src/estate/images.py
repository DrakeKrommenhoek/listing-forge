"""Derivative image generation.

Two jobs, one of which is a privacy control rather than a cosmetic one:

1. Resize originals down to web and listing sizes so pages load fast.
2. **Strip EXIF.** Phone photos routinely carry GPS coordinates. Publishing an
   untouched photo of a sofa can publish the seller's home address with it.
   Every derivative is re-encoded from pixel data only, so no metadata
   survives. Originals are never served to the public.

Pillow is imported lazily; without it the pipeline degrades to "originals only,
not web-safe" and says so loudly rather than silently publishing EXIF.
"""

from __future__ import annotations

from pathlib import Path

from estate._compat import get_logger

logger = get_logger(__name__)

WEB_MAX = 1600
THUMB_MAX = 800
LISTING_MAX = 1200
JPEG_QUALITY = 82


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401

        return True
    except ImportError:
        return False


def make_derivative(src: Path, dest: Path, max_edge: int = WEB_MAX,
                    quality: int = JPEG_QUALITY) -> tuple:
    """Write a resized, metadata-free JPEG. Returns (path_or_None, note)."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.error({"action": "image_derivative_skipped", "reason": "pillow_missing"})
        return None, "pillow_missing"

    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)  # honour rotation, then discard EXIF
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # A fresh image object carries no EXIF, IPTC, or XMP.
            clean = Image.new("RGB", im.size)
            clean.putdata(list(im.getdata()))
            clean.save(dest, format="JPEG", quality=quality, optimize=True)
        return dest, "ok"
    except Exception as exc:
        logger.error({"action": "image_derivative_failed", "src": str(src),
                      "error_type": type(exc).__name__})
        return None, type(exc).__name__


def build_web_images(session, item_id: str) -> list:
    """Create web derivatives for every original. Returns web-relative names."""
    from estate import paths
    from estate.repository import PhotoRepository

    repo = PhotoRepository(session)
    originals = repo.for_item(item_id, role="original")
    made = []
    for n, photo in enumerate(originals, start=1):
        src = Path(photo.local_path)
        if not src.exists():
            continue
        slot = "hero" if photo.is_hero else "photo"
        dest = paths.photo_path(item_id, n, slot=slot, ext="jpg", role="web")
        out, note = make_derivative(src, dest, WEB_MAX)
        if out is None:
            continue
        existing = {p.filename for p in repo.for_item(item_id, role="web")}
        if dest.name not in existing:
            repo.add(item_id, role="web", filename=dest.name, local_path=str(dest),
                     sha256=photo.sha256, is_hero=bool(photo.is_hero))
        made.append(dest.name)
    return made
