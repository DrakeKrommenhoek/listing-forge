"""Exercise the paid vision adapters without paying for them.

`AnthropicVisionProvider.identify` and `OpenAIVisionProvider.identify` are the
code paths that go live the instant an API key is added to `.env` — and until
this module existed, not one line of either had ever been executed by a test.
A bug in request construction or response parsing would have surfaced for the
first time in front of the person the system was built for.

Everything here drives the real adapter with a fake SDK client, so the request
shape, the image encoding, the response parsing, the normalisation and the
guardrails are all genuinely executed. No network call, no API key, no cost.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

TMP = tempfile.mkdtemp(prefix="estate-vis-")
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["ESTATE_INVENTORY_DIR"] = f"{TMP}/inventory"

from estate._compat import config as _config  # noqa: E402

_config.get_settings.cache_clear()

from estate import vision  # noqa: E402

#: A realistic, complete provider response. Deliberately includes markdown
#: fencing, because models wrap JSON in it constantly.
GOOD_PAYLOAD = {
    "item_name": "Teak and iron wall art",
    "category": "Home Decor",
    "brand": "Crate & Barrel",
    "manufacturer": "Crate & Barrel",
    "model": "Marcel",
    "sku": "215141",
    "collection": "Marcel",
    "subcategory": "wall art",
    "label_transcription": "CRATE & BARREL  MARCEL  215141  MADE IN INDIA",
    "country_of_manufacture": "Made in India",
    "shipping_feasible_guess": "likely",
    "materials": "teak wood, iron",
    "color_finish": "natural teak, black iron",
    "style": "mid-century modern",
    "approximate_age": "5-10 years old",
    "description": "A slatted teak panel on a black iron frame.",
    "condition": "Good",
    "condition_observations": "Light surface scuffing on the lower left slat.",
    "defects": "small scuff, lower left",
    "dimensions": "36 x 2 x 24 in",
    "dimensions_source": "estimated",
    "weight_estimate_lbs": 12,
    "fragility": "Medium",
    "included_accessories": "mounting bracket",
    "identifying_details": "Printed maker label on the reverse.",
    "alternative_identifications": [
        {"name": "West Elm slatted wall panel", "reasoning": "Similar slat spacing."}
    ],
    "additional_measurements_needed": ["depth of the mounting bracket"],
    "confidence": {
        "item_name": 0.95, "category": 0.95, "brand": 0.93, "manufacturer": 0.9,
        "model": 0.88, "sku": 0.92, "condition": 0.8, "approximate_age": 0.5,
        "dimensions": 0.4,
    },
    "overall_confidence": 0.9,
    "suggested_photos": ["the mounting hardware"],
    "suggested_questions": ["Do you still have the bracket screws?"],
}


@pytest.fixture()
def photos(tmp_path) -> list:
    """Two tiny real PNG files on disk, so encoding is genuinely exercised."""
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    out = []
    for n in range(2):
        p = tmp_path / f"photo_{n}.png"
        p.write_bytes(data)
        out.append(p)
    return out


# ---------------------------------------------------------------------------
# Fake SDKs
# ---------------------------------------------------------------------------

class _Recorder:
    """Captures the request the adapter built so it can be asserted on."""

    def __init__(self, response):
        self.response = response
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _anthropic_response(text: str, lead_with_non_text: bool = False):
    block = types.SimpleNamespace(type="text", text=text)
    content = [block]
    if lead_with_non_text:
        # Extended thinking puts a non-text block first. Indexing content[0]
        # blindly would raise AttributeError here.
        content = [types.SimpleNamespace(type="thinking", thinking="hmm"), block]
    return types.SimpleNamespace(content=content)


def _install_fake_anthropic(monkeypatch, response):
    recorder = _Recorder(response)
    client = types.SimpleNamespace(messages=recorder)
    module = types.ModuleType("anthropic")
    module.Anthropic = lambda api_key=None: client
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return recorder


def _install_fake_openai(monkeypatch, text: str):
    response = types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=text))]
    )
    recorder = _Recorder(response)
    client = types.SimpleNamespace(
        chat=types.SimpleNamespace(completions=recorder)
    )
    module = types.ModuleType("openai")
    module.OpenAI = lambda api_key=None: client
    monkeypatch.setitem(sys.modules, "openai", module)
    return recorder


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------

def test_anthropic_adapter_builds_a_multi_image_request(monkeypatch, photos):
    recorder = _install_fake_anthropic(
        monkeypatch, _anthropic_response(json.dumps(GOOD_PAYLOAD))
    )
    provider = vision.AnthropicVisionProvider(api_key="test-key", model="claude-test")
    provider.identify(photos, hint="Crate & Barrel wall decoration")

    content = recorder.kwargs["messages"][0]["content"]
    images = [c for c in content if c["type"] == "image"]
    prompts = [c for c in content if c["type"] == "text"]

    # Both photos go in ONE request: the model has to see the label photo and
    # the whole-item photo together to identify anything.
    assert len(images) == 2
    assert len(prompts) == 1
    assert images[0]["source"]["media_type"] == "image/png"
    assert base64.b64decode(images[0]["source"]["data"])  # really encoded
    assert "Crate & Barrel wall decoration" in prompts[0]["text"]
    assert recorder.kwargs["system"] == vision.SYSTEM_PROMPT
    assert recorder.kwargs["model"] == "claude-test"


def test_anthropic_adapter_parses_a_complete_response(monkeypatch, photos):
    _install_fake_anthropic(monkeypatch, _anthropic_response(json.dumps(GOOD_PAYLOAD)))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)

    assert ident.provider == "anthropic"
    assert ident.item_name == "Teak and iron wall art"
    assert ident.brand == "Crate & Barrel"
    assert ident.sku == "215141"
    assert ident.collection == "Marcel"
    assert ident.label_transcription.startswith("CRATE & BARREL")
    assert ident.condition == "Good"
    assert ident.alternative_identifications[0]["name"].startswith("West Elm")
    assert ident.processing_seconds >= 0
    assert ident.cost_usd > 0  # the operator can see what this cost

    fields = ident.to_item_fields()
    assert fields["brand"] == "Crate & Barrel"
    assert fields["sku"] == "215141"

    # Dimensions came back at 0.4 confidence. What happens to it depends
    # entirely on where the floor is set, and the two functions must agree --
    # otherwise a value is neither written nor asked about, and vanishes.
    from estate.vision import compute_missing

    strict = ident.to_item_fields(floor=0.60)
    assert "dimensions" not in strict
    assert "dimensions" in compute_missing(ident, floor=0.60, askable=["dimensions"])

    relaxed = ident.to_item_fields(floor=0.35)
    assert relaxed["dimensions"] == "36 x 2 x 24 in"
    assert "dimensions" not in compute_missing(ident, floor=0.35, askable=["dimensions"])


def test_anthropic_response_leading_with_a_thinking_block_still_parses(monkeypatch, photos):
    """The regression this guards: content[0].text raises AttributeError when
    the response leads with a non-text block, and the submitter is told the
    photos could not be analysed for an item that was identified fine."""
    _install_fake_anthropic(
        monkeypatch,
        _anthropic_response(json.dumps(GOOD_PAYLOAD), lead_with_non_text=True),
    )
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)
    assert ident.item_name == "Teak and iron wall art"


def test_markdown_fenced_json_is_accepted(monkeypatch, photos):
    fenced = "```json\n" + json.dumps(GOOD_PAYLOAD) + "\n```"
    _install_fake_anthropic(monkeypatch, _anthropic_response(fenced))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)
    assert ident.sku == "215141"


def test_prose_around_the_json_is_tolerated(monkeypatch, photos):
    chatty = "Sure! Here's the identification:\n" + json.dumps(GOOD_PAYLOAD) + "\nHope that helps."
    _install_fake_anthropic(monkeypatch, _anthropic_response(chatty))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)
    assert ident.brand == "Crate & Barrel"


def test_unparseable_output_raises_rather_than_inventing_an_item(monkeypatch, photos):
    _install_fake_anthropic(monkeypatch, _anthropic_response("I can't tell what this is."))
    with pytest.raises(ValueError):
        vision.AnthropicVisionProvider(api_key="k").identify(photos)


def test_more_photos_than_the_cap_are_truncated_not_sent(monkeypatch, tmp_path):
    data = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    many = []
    for n in range(vision.MAX_IMAGES + 5):
        p = tmp_path / f"p{n}.png"
        p.write_bytes(data)
        many.append(p)

    recorder = _install_fake_anthropic(
        monkeypatch, _anthropic_response(json.dumps(GOOD_PAYLOAD))
    )
    vision.AnthropicVisionProvider(api_key="k").identify(many)
    images = [c for c in recorder.kwargs["messages"][0]["content"] if c["type"] == "image"]
    assert len(images) == vision.MAX_IMAGES


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def test_openai_adapter_builds_a_multi_image_request(monkeypatch, photos):
    recorder = _install_fake_openai(monkeypatch, json.dumps(GOOD_PAYLOAD))
    provider = vision.OpenAIVisionProvider(api_key="test-key", model="gpt-test")
    provider.identify(photos, hint="a wall hanging")

    messages = recorder.kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == vision.SYSTEM_PROMPT
    user_content = messages[1]["content"]
    images = [c for c in user_content if c["type"] == "image_url"]
    assert len(images) == 2
    assert images[0]["image_url"]["url"].startswith("data:image/png;base64,")
    # JSON mode, so the response is parseable by construction.
    assert recorder.kwargs["response_format"] == {"type": "json_object"}


def test_openai_adapter_parses_a_complete_response(monkeypatch, photos):
    _install_fake_openai(monkeypatch, json.dumps(GOOD_PAYLOAD))
    ident = vision.OpenAIVisionProvider(api_key="k").identify(photos)
    assert ident.provider == "openai"
    assert ident.model == "Marcel"
    assert ident.overall_confidence == 0.9


def test_an_empty_openai_response_does_not_produce_a_blank_item(monkeypatch, photos):
    _install_fake_openai(monkeypatch, None)
    with pytest.raises(ValueError):
        vision.OpenAIVisionProvider(api_key="k").identify(photos)


# ---------------------------------------------------------------------------
# Guardrails hold on the live path too
# ---------------------------------------------------------------------------

def test_an_optimistic_condition_grade_is_capped_on_thin_evidence(monkeypatch, photos):
    """The conservative-condition rule is enforced in code, not just prompted
    for. Two photos and no cited observation cannot support 'Like New'."""
    payload = dict(GOOD_PAYLOAD)
    payload["condition"] = "Like New"
    payload["condition_observations"] = ""
    payload["confidence"] = dict(GOOD_PAYLOAD["confidence"], condition=0.95)

    _install_fake_anthropic(monkeypatch, _anthropic_response(json.dumps(payload)))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)

    assert ident.condition == "Good", "a top grade survived on two photos and no evidence"
    assert ident.condition_capped is True
    assert ident.condition_cap_reason


def test_a_hallucinated_category_is_snapped_to_the_real_vocabulary(monkeypatch, photos):
    payload = dict(GOOD_PAYLOAD, category="Wall Decorations")
    _install_fake_anthropic(monkeypatch, _anthropic_response(json.dumps(payload)))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)
    assert ident.category in vision.CATEGORIES


def test_a_live_provider_never_writes_ownership_or_a_price(monkeypatch, photos):
    """Two things a photo can never establish. Neither may arrive from the
    model, however confidently it asserts them."""
    payload = dict(GOOD_PAYLOAD)
    payload["ownership_approval"] = True
    payload["shipping_feasible"] = True
    payload["estimated_value_usd"] = 400
    payload["price"] = 400

    _install_fake_anthropic(monkeypatch, _anthropic_response(json.dumps(payload)))
    ident = vision.AnthropicVisionProvider(api_key="k").identify(photos)
    fields = ident.to_item_fields()

    for forbidden in ("ownership_approval", "shipping_feasible", "price",
                      "estimated_value_usd", "initial_list_price", "current_price"):
        assert forbidden not in fields, f"{forbidden} reached the item from a photo"


def test_the_factory_falls_back_to_mock_when_the_sdk_is_missing(monkeypatch):
    """What actually happens on a VPS where the key is set but the package
    is not installed: a loud [MOCK] item, never a silent wrong answer."""
    monkeypatch.setitem(sys.modules, "anthropic", None)
    monkeypatch.setattr(
        _config.get_settings(), "estate_vision_provider", "anthropic", raising=False
    )
    provider = vision.get_vision_provider("anthropic")
    assert isinstance(provider, vision.MockVisionProvider)
    assert "init failed" in provider.fallback_reason

    ident = provider.identify([Path(__file__)])
    assert ident.item_name.startswith("[MOCK]")
