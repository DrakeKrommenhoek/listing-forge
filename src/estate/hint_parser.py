"""Extract structured answers from the owner's one-or-two sentence description.

The whole point of asking for a sentence instead of a form is that Dad
shouldn't have to answer questions the sentence already answered. This module
is deliberately NOT a model call: it is fast, offline, deterministic, and
testable regex/keyword matching over a short, predictable vocabulary. That
keeps the cost and latency of every submission low regardless of which vision
provider is configured, and means the extraction behaviour never drifts.

It is intentionally conservative. A missed extraction just means one more
Telegram question gets asked, which costs Dad ten seconds. A wrong extraction
could mislead pricing, a buyer disclosure, or the ownership gate, so every
pattern here is written to require a fairly explicit phrase before it commits
to an answer, and ``ownership_approval`` in particular only ever resolves on
an unambiguous statement.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from estate.schema import CONDITIONS


@dataclass
class HintExtraction:
    condition: str | None = None
    shipping_feasible: bool | None = None
    ownership_approval: bool | None = None
    approximate_age: str | None = None
    location_in_house: str | None = None
    defects: str | None = None
    brand_guess: str | None = None
    matched_patterns: list = field(default_factory=list)

    def as_answers(self) -> dict:
        """Field-key -> extracted value, for whichever fields resolved."""
        out = {}
        if self.condition:
            out["condition"] = self.condition
        if self.shipping_feasible is not None:
            out["shipping_feasible"] = self.shipping_feasible
        if self.ownership_approval is not None:
            out["ownership_approval"] = self.ownership_approval
        if self.approximate_age:
            out["approximate_age"] = self.approximate_age
        if self.location_in_house:
            out["location_in_house"] = self.location_in_house
        if self.defects:
            out["defects"] = self.defects
        return out


# ---------------------------------------------------------------------------
# Shipping willingness
# ---------------------------------------------------------------------------

_SHIP_YES_PATTERNS = [
    r"\bcan\s+(?:be\s+)?ship(?:ped)?\b",
    r"\bwilling\s+to\s+ship\b",
    r"\bhappy\s+to\s+ship\b",
    r"\bship\s+(?:it\s+)?if\s+(?:needed|necessary)\b",
    r"\bcould\s+ship\b",
]
_SHIP_NO_PATTERNS = [
    r"\bpickup\s*only\b",
    r"\bpick[\s-]?up\s*only\b",
    r"\bwon'?t\s+ship\b",
    r"\bcan'?t\s+ship\b",
    r"\bcannot\s+ship\b",
    r"\bno\s+shipping\b",
    r"\btoo\s+(?:heavy|big|large|fragile)\s+to\s+ship\b",
    r"\blocal\s+pickup\s+only\b",
]


def _match_any(patterns: list, text_lower: str) -> str | None:
    for pat in patterns:
        m = re.search(pat, text_lower)
        if m:
            return m.group(0)
    return None


def _extract_shipping(text_lower: str, matched: list) -> bool | None:
    hit = _match_any(_SHIP_NO_PATTERNS, text_lower)
    if hit:
        matched.append("shipping_feasible=False (%r)" % hit)
        return False
    hit = _match_any(_SHIP_YES_PATTERNS, text_lower)
    if hit:
        matched.append("shipping_feasible=True (%r)" % hit)
        return True
    return None


# ---------------------------------------------------------------------------
# Condition
# ---------------------------------------------------------------------------

# Ordered most-specific first so "like new" is not swallowed by a looser "new"
# match, etc. Mapped to the exact Condition enum values.
_CONDITION_PATTERNS = [
    (r"\bnew\s+(?:in\s+(?:the\s+)?box|sealed|never\s+(?:used|opened))\b", "New / Sealed"),
    (r"\blike\s+new\b", "Like New"),
    (r"\bbarely\s+used\b", "Like New"),
    (r"\bexcellent\s+condition\b", "Excellent"),
    (r"\bgreat\s+condition\b", "Excellent"),
    (r"\bgood\s+(?:working\s+)?condition\b", "Good"),
    (r"\bworks?\s+(?:fine|well|great|perfectly)\b", "Good"),
    (r"\bfair\s+condition\b", "Fair"),
    (r"\bshows?\s+(?:its\s+)?age\b", "Fair"),
    (r"\bworn\b", "Fair"),
    (r"\bpoor\s+condition\b", "Poor"),
    (r"\bbeat(?:en)?[\s-]?up\b", "Poor"),
    (r"\bbroken\b", "For Parts / Repair"),
    (r"\bdoesn'?t\s+work\b", "For Parts / Repair"),
    (r"\bfor\s+parts\b", "For Parts / Repair"),
    (r"\bnot\s+working\b", "For Parts / Repair"),
]


def _extract_condition(text_lower: str, matched: list) -> str | None:
    for pattern, grade in _CONDITION_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            matched.append("condition=%r (%r)" % (grade, m.group(0)))
            assert grade in CONDITIONS
            return grade
    return None


# ---------------------------------------------------------------------------
# Defects
# ---------------------------------------------------------------------------

_DEFECT_KEYWORDS = (
    "scratch", "scratches", "scuff", "scuffed", "chip", "chipped", "crack",
    "cracked", "stain", "stained", "dent", "dented", "tear", "torn", "rip",
    "ripped", "missing", "broken", "damage", "damaged", "wobbly", "loose",
    "squeak", "squeaks", "faded", "rust", "rusted", "peeling",
)

_DEFECT_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|,\s*|;\s*|\s+(?:and|but)\s+")


def _extract_defects(text: str, text_lower: str, matched: list) -> str | None:
    if not any(kw in text_lower for kw in _DEFECT_KEYWORDS):
        return None
    # Keep whichever clause(s) actually contain a defect keyword, rather than
    # the whole sentence -- the rest is usually identification/shipping talk
    # that does not belong in a defects field.
    clauses = [c.strip() for c in _DEFECT_SENTENCE_SPLIT.split(text) if c.strip()]
    hits = [c for c in clauses if any(kw in c.lower() for kw in _DEFECT_KEYWORDS)]
    if not hits:
        hits = [text.strip()]
    matched.append("defects extracted from %d clause(s)" % len(hits))
    return "; ".join(hits)[:500]


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------

_AGE_PATTERNS = [
    r"\bfrom\s+the\s+(\d{2,4}s)\b",
    r"\b(\d{4})s\b",
    r"\b(?:about|around|roughly|approximately|~)\s*(\d{1,2}\+?\s*years?\s*old)\b",
    r"\b(\d{1,2}\+?\s*years?\s*old)\b",
    r"\bbought\s+(?:it\s+)?(?:in\s+|around\s+)?(\d{4})\b",
    r"\b(brand\s*new)\b",
    r"\b(antique)\b",
    r"\b(vintage)\b",
]


def _extract_age(text_lower: str, matched: list) -> str | None:
    for pattern in _AGE_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            value = m.group(1) if m.groups() else m.group(0)
            matched.append("approximate_age=%r" % value)
            return value
    return None


# ---------------------------------------------------------------------------
# Location / origin
# ---------------------------------------------------------------------------

_ROOMS = (
    "dining room", "living room", "family room", "bedroom", "master bedroom",
    "guest room", "kitchen", "garage", "basement", "attic", "office", "study",
    "den", "hallway", "closet", "porch", "patio", "backyard", "yard",
    "laundry room", "mudroom", "sunroom", "bathroom",
)


def _extract_location(text_lower: str, matched: list) -> str | None:
    # Longest match first, so "master bedroom" is not swallowed by the
    # shorter "bedroom" pattern it happens to contain.
    for room in sorted(_ROOMS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(room) + r"\b", text_lower):
            matched.append("location_in_house=%r" % room)
            return room.title()
    return None


# ---------------------------------------------------------------------------
# Ownership -- deliberately the most conservative extractor here
# ---------------------------------------------------------------------------

_OWNERSHIP_YES_PATTERNS = [
    r"\bit'?s\s+mine\b",
    r"\bmine\s+to\s+sell\b",
    r"\bi\s+own\s+(?:it|this)\b",
    r"\bmy\s+own\b",
    r"\bbelongs?\s+to\s+me\b",
]
_OWNERSHIP_NO_PATTERNS = [
    r"\bnot\s+(?:sure\s+(?:if|whether)\s+)?(?:mine|i\s+can\s+sell)\b",
    r"\bborrowed\b",
    r"\bsomeone\s+else'?s\b",
    r"\bbelongs?\s+to\s+(?:my|someone|a\s+friend|a\s+relative)\b",
    r"\bneed\s+to\s+ask\b",
    r"\bnot\s+my\s+(?:item|thing)\b",
]


def _extract_ownership(text_lower: str, matched: list) -> bool | None:
    hit = _match_any(_OWNERSHIP_NO_PATTERNS, text_lower)
    if hit:
        matched.append("ownership_approval=False (%r)" % hit)
        return False
    hit = _match_any(_OWNERSHIP_YES_PATTERNS, text_lower)
    if hit:
        matched.append("ownership_approval=True (%r)" % hit)
        return True
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_hint(text: str) -> HintExtraction:
    """Best-effort, conservative extraction from a free-text description.

    Never raises. An empty or unparseable string simply yields an
    HintExtraction with everything None, which resolves nothing and changes
    no behaviour -- the pipeline falls back to asking every question, exactly
    as it did before this module existed.
    """
    text = (text or "").strip()
    if not text:
        return HintExtraction()

    text_lower = text.lower()
    matched: list = []

    return HintExtraction(
        condition=_extract_condition(text_lower, matched),
        shipping_feasible=_extract_shipping(text_lower, matched),
        ownership_approval=_extract_ownership(text_lower, matched),
        approximate_age=_extract_age(text_lower, matched),
        location_in_house=_extract_location(text_lower, matched),
        defects=_extract_defects(text, text_lower, matched),
        matched_patterns=matched,
    )
