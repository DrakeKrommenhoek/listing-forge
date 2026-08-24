"""Provider-agnostic photo -> item identification.

The contract is deliberately narrow: given a handful of images of ONE physical
object plus whatever the owner said about it in one or two sentences, return a
normalised ``ItemIdentification`` covering identification, condition, and
rough dimensions. Every provider returns the same shape, so the rest of the
pipeline never knows which model ran.

Providers
---------
mock       Deterministic, offline, clearly labelled. Used for tests and demos.
anthropic  Claude vision (default for live use).
openai     GPT vision.

Selection is entirely by environment variable (``ESTATE_VISION_PROVIDER``) --
see ``get_vision_provider()``. Nothing else in the codebase needs to change to
switch providers.

Guardrails
----------
- The model is told to say "unknown" rather than guess. Guessed brands, SKUs,
  and model numbers poison the comparable search, which poisons the price.
- Per-field confidence is returned, and anything below FIELD_CONFIDENCE_FLOOR
  is dropped into ``missing`` so the intake flow asks a human instead.
- The model never sets a price. Pricing comes only from evidence (research.py).
- Condition is never allowed to default to a high grade (Excellent / Like New
  / New Sealed) on thin evidence -- see ``_apply_conservative_condition_rule``.
  This is enforced in code, not just prompted for, because a prompt is not a
  guarantee.
- Every call is timed and its cost estimated, so the operator can see what
  identification is actually costing -- see ``ItemIdentification.cost_usd`` /
  ``processing_seconds``.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from estate.schema import ASKABLE_FIELDS, CATEGORIES, CONDITIONS
from estate._compat import get_logger

logger = get_logger(__name__)

#: Below this per-field confidence we ask a human instead of trusting the model.
FIELD_CONFIDENCE_FLOOR = 0.60

MAX_IMAGES = 8

#: Condition grades a model may not award without strong support. See
#: _apply_conservative_condition_rule. Ordered least to most demanding.
_HIGH_GRADES = ("New / Sealed", "Like New", "Excellent")
#: What a high grade is downgraded to when the evidence does not support it.
_CONSERVATIVE_FALLBACK_GRADE = "Good"
#: A high grade needs at least this many photos actually submitted...
MIN_PHOTOS_FOR_HIGH_GRADE = 5
#: ...AND this much per-field confidence...
MIN_CONDITION_CONFIDENCE_FOR_HIGH_GRADE = 0.75
#: ...AND at least one cited observation. An empty rationale is not evidence.

# Rough, clearly-labelled-as-rough per-call cost estimates in USD, used only
# for the operator-facing cost tracker (ItemIdentification.cost_usd). These
# are NOT billing figures -- check your provider's actual invoice. Updated
# 2026-08-02; vision pricing changes -- re-check before trusting this for
# budgeting.
_COST_PER_CALL_USD = {
    "anthropic": 0.03,  # Claude Sonnet vision, ~8 images + prompt, rough order of magnitude
    "openai": 0.02,     # GPT-4o vision, similar order of magnitude
    "mock": 0.0,
}

SYSTEM_PROMPT = """You identify second-hand household objects from photographs, and the owner's own one or two sentence description, so they can be resold.

Rules you must follow:
1. All photographs show ONE single object. Describe that object only.
2. If you cannot read a brand, model, manufacturer, or SKU from the photos, return "" for it and give it a low confidence. Never guess a brand, model number, or SKU -- an invented model number poisons every downstream price comparison.
3. Weight visible manufacturer labels, model numbers, SKUs, serial numbers, and construction details (joinery, stitching, hardware, printed tags) heavily. If you can read one, quote it exactly.
4. When you are not confident of one exact model, offer 1-3 plausible alternative identifications with your reasoning, rather than forcing a single guess.
5. Report condition from visible evidence only, conservatively. Do not award a top condition grade (New/Sealed, Like New, Excellent) unless the photos clearly show all major surfaces AND you can point to specific evidence for it. When in doubt, grade one notch lower and say why. Distinguish natural material character (wood grain, patina, worn-in leather) from actual defects -- natural character is not a defect.
6. Note anything that suggests a hidden defect you cannot see directly (a repaired-looking area, an unusual gap, a replaced part) separately from confirmed visible defects.
7. Do NOT estimate a price, value, or worth. Pricing is handled elsewhere from real market evidence.
8. Do not describe people, faces, documents, screens, or anything that could be personal information. If a photo contains an address, name, or serial number, note only "identifying label present" and do not transcribe it, EXCEPT a model/SKU number, which is needed for identification and is not personal information.
9. Respond with a single valid JSON object and nothing else."""

USER_TEMPLATE = """Identify this object from the {n} attached photograph(s).

{hint_block}Return JSON with exactly this shape:

{{
  "item_name": "short plain-English name a seller would use",
  "category": "one of: {categories}",
  "brand": "",
  "manufacturer": "the company that made it, if different from the brand printed on it",
  "model": "",
  "sku": "manufacturer SKU or part number, ONLY if visible on a label or tag",
  "collection": "the product line or collection this belongs to, e.g. 'Marcel', 'Eames', ONLY if you can support it",
  "subcategory": "a narrower type within the category, e.g. 'wall art', 'dining chair', 'cordless drill'",
  "serial_number": "a serial number ONLY where it is genuinely needed to identify or value the item (tools, instruments, electronics, watches). Otherwise ''",
  "country_of_manufacture": "only if printed on the item or a label, e.g. 'Made in Italy', else ''",
  "label_transcription": "exactly what any manufacturer label, tag, or stamp says, quoted verbatim. This is the single strongest identification evidence there is -- transcribe it even if you cannot interpret it. Omit any personal name or address.",
  "shipping_feasible_guess": "one of: likely, unlikely, unknown -- based only on visible size, weight and fragility. The owner's own answer always overrides this.",
  "approximate_age": "e.g. '1990s', '5-10 years old', or ''",
  "production_period": "the manufacturing run this design belongs to, if identifiable, e.g. '2015-2019', else ''",
  "materials": "primary materials, e.g. 'solid oak, brass hardware'",
  "color_finish": "color and finish, e.g. 'walnut stain, satin lacquer'",
  "style": "design style or era, e.g. 'mid-century modern', 'industrial'",
  "original_use": "what this was designed for",
  "description": "2-4 sentences a buyer would find useful. Materials, style, form, notable features.",
  "condition": "one of: {conditions}",
  "condition_observations": "specific visible evidence supporting the grade you chose",
  "natural_characteristics": "wood grain, patina, worn-in texture -- things that are NOT defects",
  "possible_hidden_defects": "anything that might indicate a defect you cannot fully see, or ''",
  "cleanliness": "one short phrase, e.g. 'clean', 'dusty', 'needs a wipe-down'",
  "structural_condition": "one short phrase on structural soundness",
  "functional_condition": "one short phrase on whether moving/mechanical/electrical parts appear to work, or 'not applicable'",
  "cleaning_recommendations": "what cleaning would improve saleability, or ''",
  "repair_recommendations": "what repair would improve saleability, or ''",
  "condition_price_impact": "one sentence on how the condition affects likely price versus a pristine example",
  "defects": "every visible flaw, comma separated. '' if none visible.",
  "dimensions": "only if a scale reference or printed size is visible, else ''",
  "dimensions_source": "'estimated' if you inferred dimensions from a scale reference, else 'unknown'",
  "weight_estimate_lbs": 0,
  "fragility": "one of: Low, Medium, High",
  "included_accessories": "visible accessories only, else ''",
  "missing_pieces_possible": "parts that a complete example of this item would normally have but that are not visible here, or ''",
  "identifying_details": "logos, maker marks, patterns, distinguishing features -- the EVIDENCE for your identification",
  "alternative_identifications": [
    {{"name": "an alternative plausible identification", "reasoning": "why this is also plausible"}}
  ],
  "additional_measurements_needed": ["measurements the owner should take that the photos do not show"],
  "confidence": {{
    "item_name": 0.0, "category": 0.0, "brand": 0.0, "manufacturer": 0.0,
    "model": 0.0, "sku": 0.0, "condition": 0.0, "approximate_age": 0.0,
    "dimensions": 0.0
  }},
  "overall_confidence": 0.0,
  "suggested_photos": ["which additional shots would most improve identification"],
  "suggested_questions": ["what to ask the owner that photos cannot answer"]
}}"""


@dataclass
class ItemIdentification:
    """Normalised output. ``missing`` drives the follow-up questions."""

    item_name: str = ""
    category: str = ""
    brand: str = ""
    manufacturer: str = ""
    model: str = ""
    sku: str = ""
    #: Product line ("Marcel", "Eames"). Often the difference between finding
    #: the right comparables and finding none, because sellers list by
    #: collection far more consistently than by SKU.
    collection: str = ""
    subcategory: str = ""
    #: Only requested where it genuinely identifies or values the item (tools,
    #: instruments, watches). Never solicited for its own sake -- a serial
    #: number is close to personal data on many household goods.
    serial_number: str = ""
    country_of_manufacture: str = ""
    #: Verbatim label text. The highest-weight evidence a photo can carry, and
    #: the thing a reviewer can check in five seconds against the real object.
    label_transcription: str = ""
    #: The model's read on shippability, from visible size and fragility only.
    #: NEVER written to shipping_feasible -- that field belongs to the owner
    #: (see schema.BOOLEAN_ASKABLE_FIELDS). Recorded as a hint for review.
    shipping_feasible_guess: str = "unknown"
    approximate_age: str = ""
    production_period: str = ""
    materials: str = ""
    color_finish: str = ""
    style: str = ""
    original_use: str = ""
    description: str = ""

    condition: str = "Unknown"
    condition_observations: str = ""
    natural_characteristics: str = ""
    possible_hidden_defects: str = ""
    cleanliness: str = ""
    structural_condition: str = ""
    functional_condition: str = ""
    cleaning_recommendations: str = ""
    repair_recommendations: str = ""
    condition_price_impact: str = ""
    condition_capped: bool = False
    condition_cap_reason: str = ""
    defects: str = ""

    dimensions: str = ""
    dimensions_source: str = "unknown"  # unknown | estimated | official | owner_confirmed
    weight_estimate_lbs: float | None = None
    fragility: str = ""

    included_accessories: str = ""
    missing_pieces_possible: str = ""
    identifying_details: str = ""
    alternative_identifications: list = field(default_factory=list)
    additional_measurements_needed: list = field(default_factory=list)

    confidence: dict = field(default_factory=dict)
    overall_confidence: float = 0.0
    suggested_photos: list = field(default_factory=list)
    suggested_questions: list = field(default_factory=list)
    missing: list = field(default_factory=list)

    provider: str = "mock"
    model_name: str = ""
    processing_seconds: float = 0.0
    cost_usd: float = 0.0
    fallback_used: str = ""  # set when the requested provider could not run
    raw: dict = field(default_factory=dict)

    #: Always carried into the draft so the item is nameable in review, even
    #: when confidence is low. Everything else must clear the floor.
    PROVISIONAL = ("item_name", "category", "description")

    def to_item_fields(self, floor: float | None = None) -> dict:
        """Fields safe to write to the inventory draft.

        Low-confidence values are withheld rather than written, so the review
        screen never shows a guess dressed up as a fact. The exceptions in
        PROVISIONAL are always carried through because an unnamed draft is
        harder to review than a provisionally named one.

        Uses the same configured floor as ``compute_missing``, and that is
        load-bearing rather than tidiness. If this withheld a value that
        ``compute_missing`` had decided was confident enough not to ask about,
        the value would be neither recorded nor asked for — it would simply
        vanish between the two functions. Anything above the floor is kept and
        shown to the reviewer; anything below it is either asked about or
        left blank for the reviewer to fill.
        """
        if floor is None:
            floor, _askable = _configured_intake()
        out = {}
        for key in ("item_name", "category", "brand", "manufacturer", "model", "sku",
                    "collection", "subcategory", "approximate_age", "description",
                    "condition", "defects", "dimensions", "included_accessories"):
            value = getattr(self, key)
            if not value:
                continue
            if key not in self.PROVISIONAL and self.confidence.get(key, 1.0) < floor:
                continue
            out[key] = value
        return out

    def identification_report(self) -> dict:
        """The full identification depth, for vision_raw / the review page."""
        return {
            "sku": self.sku,
            "collection": self.collection,
            "subcategory": self.subcategory,
            "serial_number": self.serial_number,
            "country_of_manufacture": self.country_of_manufacture,
            "label_transcription": self.label_transcription,
            "shipping_feasible_guess": self.shipping_feasible_guess,
            "manufacturer": self.manufacturer,
            "production_period": self.production_period,
            "materials": self.materials,
            "color_finish": self.color_finish,
            "style": self.style,
            "original_use": self.original_use,
            "missing_pieces_possible": self.missing_pieces_possible,
            "identifying_details": self.identifying_details,
            "alternative_identifications": self.alternative_identifications,
            "additional_measurements_needed": self.additional_measurements_needed,
            "confidence": self.confidence,
            "overall_confidence": self.overall_confidence,
        }

    def condition_report(self) -> dict:
        """The full condition depth, for vision_raw / the review page."""
        return {
            "condition": self.condition,
            "condition_observations": self.condition_observations,
            "natural_characteristics": self.natural_characteristics,
            "possible_hidden_defects": self.possible_hidden_defects,
            "cleanliness": self.cleanliness,
            "structural_condition": self.structural_condition,
            "functional_condition": self.functional_condition,
            "cleaning_recommendations": self.cleaning_recommendations,
            "repair_recommendations": self.repair_recommendations,
            "condition_price_impact": self.condition_price_impact,
            "condition_confidence": self.confidence.get("condition", 0.0),
            "condition_capped": self.condition_capped,
            "condition_cap_reason": self.condition_cap_reason,
        }

    def dimensions_report(self) -> dict:
        """Official vs. estimated vs. owner-confirmed, kept explicitly distinct."""
        return {
            "dimensions": self.dimensions,
            "dimensions_source": self.dimensions_source,
            "weight_estimate_lbs": self.weight_estimate_lbs,
            "fragility": self.fragility,
            "additional_measurements_needed": self.additional_measurements_needed,
        }


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _closest(value: str, allowed: list) -> str:
    if not value:
        return ""
    v = value.strip().lower()
    for a in allowed:
        if a.lower() == v:
            return a
    for a in allowed:
        if v in a.lower() or a.lower() in v:
            return a
    return ""


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```\s*$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("model did not return parseable JSON")


def _first_text_block(content) -> str:
    """The first text block of an Anthropic response, whatever else is in it.

    Indexing ``content[0].text`` directly is a live grenade: a response that
    leads with any non-text block (a thinking block, a tool use) raises
    AttributeError, which surfaces to the submitter as "I couldn't analyse
    the photos" for an item the model actually identified perfectly well.
    Scanning for the first block that has text costs nothing and cannot fail
    that way.
    """
    for block in content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            return text
        if isinstance(block, dict) and str(block.get("text", "")).strip():
            return str(block["text"])
    return ""


def _apply_conservative_condition_rule(ident: ItemIdentification, photo_count: int) -> None:
    """Enforce "never default to Excellent" in code, not just in the prompt.

    A high grade (New/Sealed, Like New, Excellent) is only honoured when there
    were enough photos to plausibly show every side, the model's own
    condition confidence clears a real bar, AND the model actually cited
    observations supporting it. Anything short of all three is downgraded to
    "Good" -- never silently accepted, and never downgraded further than Good
    by this rule alone (a model that says "Poor" with weak evidence stays
    "Poor"; understating condition costs nothing, overstating it costs a
    refund).
    """
    if ident.condition not in _HIGH_GRADES:
        return

    reasons = []
    if photo_count < MIN_PHOTOS_FOR_HIGH_GRADE:
        reasons.append(
            "only %d photo(s); %d+ needed to award %s"
            % (photo_count, MIN_PHOTOS_FOR_HIGH_GRADE, ident.condition)
        )
    condition_conf = ident.confidence.get("condition", 0.0)
    if condition_conf < MIN_CONDITION_CONFIDENCE_FOR_HIGH_GRADE:
        reasons.append(
            "condition confidence %.2f below the %.2f bar for %s"
            % (condition_conf, MIN_CONDITION_CONFIDENCE_FOR_HIGH_GRADE, ident.condition)
        )
    if not ident.condition_observations.strip():
        reasons.append("no supporting observation was cited")

    if reasons:
        ident.condition_capped = True
        ident.condition_cap_reason = "; ".join(reasons)
        ident.condition = _CONSERVATIVE_FALLBACK_GRADE
        ident.confidence["condition"] = min(condition_conf, MIN_CONDITION_CONFIDENCE_FOR_HIGH_GRADE)


def normalise(data: dict, provider: str, model_name: str, photo_count: int = 0) -> ItemIdentification:
    """Coerce any provider's raw dict into the canonical shape."""

    def s(key: str) -> str:
        v = data.get(key, "")
        return v.strip() if isinstance(v, str) else ""

    def lst(key: str) -> list:
        v = data.get(key, [])
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    def alt_ids(key: str) -> list:
        v = data.get(key, [])
        if not isinstance(v, list):
            return []
        out = []
        for entry in v:
            if isinstance(entry, dict) and entry.get("name"):
                out.append({"name": str(entry["name"]).strip(),
                           "reasoning": str(entry.get("reasoning", "")).strip()})
            elif isinstance(entry, str) and entry.strip():
                out.append({"name": entry.strip(), "reasoning": ""})
        return out

    def num(key: str) -> float | None:
        v = data.get(key)
        if v in (None, "", 0):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    conf_raw = data.get("confidence") or {}
    conf = {}
    if isinstance(conf_raw, dict):
        for k, v in conf_raw.items():
            try:
                conf[str(k)] = max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                continue

    ident = ItemIdentification(
        item_name=s("item_name"),
        category=_closest(s("category"), CATEGORIES) or "Other",
        brand=s("brand"),
        manufacturer=s("manufacturer"),
        model=s("model"),
        sku=s("sku"),
        collection=s("collection"),
        subcategory=s("subcategory"),
        serial_number=s("serial_number"),
        country_of_manufacture=s("country_of_manufacture"),
        label_transcription=s("label_transcription"),
        shipping_feasible_guess=_closest(
            s("shipping_feasible_guess"), ["likely", "unlikely", "unknown"]
        ) or "unknown",
        approximate_age=s("approximate_age"),
        production_period=s("production_period"),
        materials=s("materials"),
        color_finish=s("color_finish"),
        style=s("style"),
        original_use=s("original_use"),
        description=s("description"),
        condition=_closest(s("condition"), CONDITIONS) or "Unknown",
        condition_observations=s("condition_observations"),
        natural_characteristics=s("natural_characteristics"),
        possible_hidden_defects=s("possible_hidden_defects"),
        cleanliness=s("cleanliness"),
        structural_condition=s("structural_condition"),
        functional_condition=s("functional_condition"),
        cleaning_recommendations=s("cleaning_recommendations"),
        repair_recommendations=s("repair_recommendations"),
        condition_price_impact=s("condition_price_impact"),
        defects=s("defects"),
        dimensions=s("dimensions"),
        dimensions_source=_closest(s("dimensions_source"), ["estimated", "official", "unknown"])
        or ("estimated" if s("dimensions") else "unknown"),
        weight_estimate_lbs=num("weight_estimate_lbs"),
        fragility=_closest(s("fragility"), ["Low", "Medium", "High"]),
        included_accessories=s("included_accessories"),
        missing_pieces_possible=s("missing_pieces_possible"),
        identifying_details=s("identifying_details"),
        alternative_identifications=alt_ids("alternative_identifications"),
        additional_measurements_needed=lst("additional_measurements_needed"),
        confidence=conf,
        suggested_photos=lst("suggested_photos"),
        suggested_questions=lst("suggested_questions"),
        provider=provider,
        model_name=model_name,
        raw=data if isinstance(data, dict) else {},
    )

    try:
        ident.overall_confidence = max(0.0, min(1.0, float(data.get("overall_confidence", 0.0))))
    except (TypeError, ValueError):
        ident.overall_confidence = 0.0
    if not ident.overall_confidence and conf:
        ident.overall_confidence = round(sum(conf.values()) / len(conf), 3)

    _apply_conservative_condition_rule(ident, photo_count)
    ident.missing = compute_missing(ident)
    return ident


def _configured_intake() -> tuple:
    """(confidence floor, askable fields) for this deployment.

    Read lazily and defensively. This module runs in tests, in the demo, and
    on a machine with no ``.env`` at all, and a missing configuration must
    fall back to the old behaviour rather than raise mid-submission.
    """
    try:
        from estate._compat import get_settings

        settings = get_settings()
        return float(settings.estate_field_confidence_floor), settings.estate_askable()
    except Exception:  # noqa: BLE001 - configuration must never break intake
        return FIELD_CONFIDENCE_FLOOR, list(ASKABLE_FIELDS)


def compute_missing(ident: ItemIdentification, floor: float | None = None,
                    askable: list | None = None) -> list:
    """Which askable fields the model could not settle confidently.

    Only fields this deployment is actually willing to ask about can end up
    here — see ``Settings.estate_askable``. The submitter is someone clearing
    a house while standing in a room; every question is a reason to put the
    phone down. Anything not asked keeps the model's own answer and is shown
    to the reviewer as low-confidence, which is a correction rather than an
    interrogation.

    ``floor`` and ``askable`` are injectable so the behaviour is testable
    without touching the environment.
    """
    if floor is None or askable is None:
        configured_floor, configured_askable = _configured_intake()
        floor = configured_floor if floor is None else floor
        askable = configured_askable if askable is None else askable

    missing = []
    for key in ASKABLE_FIELDS:
        if key not in askable:
            continue
        value = getattr(ident, key, "")
        conf = ident.confidence.get(key, 0.0)
        if key == "condition":
            if not value or value == "Unknown" or conf < floor:
                missing.append(key)
        elif key == "defects":
            # Defects are never assumed when we are asking at all. If defects
            # is not in `askable`, the model's observation stands and the
            # reviewer confirms it before publication -- site.py refuses to
            # publish an item whose defects field is still blank either way.
            missing.append(key)
        elif key == "location_in_house":
            missing.append(key)  # a photo can never tell us this
        elif not value or conf < floor:
            missing.append(key)
    return missing


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class VisionProvider(ABC):
    name = "abstract"

    @abstractmethod
    def identify(self, image_paths: list, hint: str = "") -> ItemIdentification: ...

    @staticmethod
    def _prompt(n: int, hint: str) -> str:
        hint_block = (f"The owner says: {hint.strip()}\n\n") if hint.strip() else ""
        return USER_TEMPLATE.format(
            n=n,
            hint_block=hint_block,
            categories=", ".join(CATEGORIES),
            conditions=", ".join(CONDITIONS),
        )

    @staticmethod
    def _encode(path: Path) -> tuple:
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        if mime not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
            mime = "image/jpeg"
        return mime, base64.b64encode(Path(path).read_bytes()).decode("ascii")


class MockVisionProvider(VisionProvider):
    """Offline provider. Output is deterministic and clearly marked as mock.

    Used by the sample-item demo and the test suite so no API key, network
    call, or invented market data is ever required to exercise the pipeline.
    """

    name = "mock"

    #: The demo harness sets this to script specific fixtures by filename stem.
    fixtures: dict = {}

    def identify(self, image_paths: list, hint: str = "") -> ItemIdentification:
        start = time.monotonic()
        paths = [Path(p) for p in image_paths][:MAX_IMAGES]
        stem = paths[0].stem if paths else ""
        for key, payload in self.fixtures.items():
            if key in stem or key in hint:
                ident = normalise(payload, "mock", "mock-vision-v1", photo_count=len(paths))
                ident.raw["_mock"] = True
                ident.processing_seconds = round(time.monotonic() - start, 4)
                ident.cost_usd = 0.0
                return ident
        data = {
            "item_name": "[MOCK] Unidentified household item",
            "category": "Other",
            "brand": "",
            "model": "",
            "description": "[MOCK OUTPUT — no vision model was called] "
                           "%d photo(s) received for this item." % len(paths),
            "condition": "Unknown",
            "condition_observations": "",
            "defects": "",
            "confidence": {"item_name": 0.1, "category": 0.1, "brand": 0.0,
                           "model": 0.0, "condition": 0.0},
            "overall_confidence": 0.1,
            "suggested_photos": ["brand or model label", "full item, straight on"],
            "suggested_questions": ["What is this item?"],
        }
        ident = normalise(data, "mock", "mock-vision-v1", photo_count=len(paths))
        ident.raw["_mock"] = True
        ident.processing_seconds = round(time.monotonic() - start, 4)
        ident.cost_usd = 0.0
        return ident


class AnthropicVisionProvider(VisionProvider):
    name = "anthropic"
    default_model = "claude-sonnet-5"

    def __init__(self, api_key: str = "", model: str = ""):
        import anthropic

        from estate._compat import get_settings

        settings = get_settings()
        self.model = model or settings.estate_vision_model or self.default_model
        self._client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key)

    def identify(self, image_paths: list, hint: str = "") -> ItemIdentification:
        start = time.monotonic()
        paths = [Path(p) for p in image_paths][:MAX_IMAGES]
        content: list = []
        for p in paths:
            mime, b64 = self._encode(p)
            content.append(
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime, "data": b64}}
            )
        content.append({"type": "text", "text": self._prompt(len(paths), hint)})

        msg = self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        text = _first_text_block(msg.content)
        ident = normalise(_extract_json(text), "anthropic", self.model, photo_count=len(paths))
        ident.processing_seconds = round(time.monotonic() - start, 3)
        ident.cost_usd = _COST_PER_CALL_USD.get("anthropic", 0.0)
        return ident


class OpenAIVisionProvider(VisionProvider):
    name = "openai"
    default_model = "gpt-4o"

    def __init__(self, api_key: str = "", model: str = ""):
        from openai import OpenAI

        from estate._compat import get_settings

        settings = get_settings()
        self.model = model or settings.estate_vision_model or self.default_model
        self._client = OpenAI(api_key=api_key or settings.openai_api_key)

    def identify(self, image_paths: list, hint: str = "") -> ItemIdentification:
        start = time.monotonic()
        paths = [Path(p) for p in image_paths][:MAX_IMAGES]
        content: list = [{"type": "text", "text": self._prompt(len(paths), hint)}]
        for p in paths:
            mime, b64 = self._encode(p)
            content.append(
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}}
            )
        resp = self._client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
        )
        text = resp.choices[0].message.content or ""
        ident = normalise(_extract_json(text), "openai", self.model, photo_count=len(paths))
        ident.processing_seconds = round(time.monotonic() - start, 3)
        ident.cost_usd = _COST_PER_CALL_USD.get("openai", 0.0)
        return ident


_PROVIDERS = {
    "mock": MockVisionProvider,
    "anthropic": AnthropicVisionProvider,
    "openai": OpenAIVisionProvider,
}


def get_vision_provider(name: str = "", **kwargs: Any) -> VisionProvider:
    """Factory, selected purely by ESTATE_VISION_PROVIDER (or an explicit name).

    Falls back to mock -- never to a silent wrong answer -- when the requested
    provider is unknown, its SDK is missing, or its API key is absent. Mock
    output is always prefixed [MOCK] and blocked from publication, so the
    fallback is loud at the data layer even though it is quiet at the API
    layer (no exception reaches the caller).
    """
    from estate._compat import get_settings

    key = (name or get_settings().estate_vision_provider or "mock").strip().lower()
    cls = _PROVIDERS.get(key)
    if cls is None:
        logger.error({"action": "vision_provider_unknown", "requested": key})
        provider = MockVisionProvider()
        provider.fallback_reason = "unknown provider %r" % key
        return provider
    try:
        return cls(**kwargs)
    except Exception as exc:  # missing key, missing SDK, bad config
        logger.error(
            {"action": "vision_provider_init_failed", "provider": key,
             "error_type": type(exc).__name__}
        )
        provider = MockVisionProvider()
        provider.fallback_reason = "%s init failed: %s" % (key, type(exc).__name__)
        return provider
