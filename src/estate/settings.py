"""Tunable configuration for pricing, markdown, and pickup incentives.

Read from ``estate/config/pricing.json`` on every call so thresholds can be
changed without a restart. Falls back to the shipped defaults if the file is
missing or malformed — a bad edit must never take the bot down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from estate._compat import get_logger

logger = get_logger(__name__)

DEFAULT_PATH = Path("estate/config/pricing.json")

_FALLBACK: dict = {
    "price_bands": {
        "initial_list_multiplier": 1.15,
        "expected_sale_multiplier": 0.92,
        "floor_multiplier": 0.60,
        "low_confidence_floor_multiplier": 0.45,
        "round_to": 5,
        "min_price": 5,
    },
    "confidence": {
        "weights": {"sample_size": 0.25, "similarity": 0.25, "recency": 0.20,
                    "sold_evidence": 0.20, "condition_match": 0.10},
        "sample_size_full_at": 6,
        "sample_size_min": 3,
        "recency_full_days": 45,
        "recency_zero_days": 365,
        "thresholds": {"high": 0.75, "medium": 0.55, "low": 0.30},
        "placeholder_caps_at": "Low",
    },
    "markdown": {
        "enabled": True,
        "first_markdown_after_days": 10,
        "interval_days": 7,
        "base_step_pct": 0.10,
        "max_total_markdown_pct": 0.55,
        "modifiers": {},
        "high_value_threshold": 400,
        "urgent_deadline_days": 21,
        "deadline_endgame_days": 7,
        "deadline_endgame_step_pct": 0.20,
    },
    "pickup_incentive": {
        "enabled": True, "base": 0, "per_lb_over_30": 0.35, "weight_threshold_lbs": 30,
        "oversize_cuft_threshold": 8, "oversize_bonus": 15, "stairs_bonus": 20,
        "disassembly_bonus": 25, "two_person_bonus": 25, "truck_required_bonus": 30,
        "difficult_access_bonus": 15, "avoided_disposal_bonus": 20, "urgency_bonus": 15,
        "max_pct_of_price": 0.25, "max_absolute": 150, "round_to": 5,
    },
    "fees": {},
    "deadline": {"move_out_date": ""},
}


def move_out_date() -> str:
    """The hard deadline, env first, then config, then empty.

    Returned as an ISO date string. Empty means "no deadline configured", which
    switches off every urgency behaviour in the markdown engine.
    """
    import os

    from estate._compat import get_settings

    env = (os.environ.get("ESTATE_MOVE_OUT_DATE") or "").strip()
    if env:
        return env
    try:
        configured = (get_settings().estate_move_out_date or "").strip()
    except Exception:
        configured = ""
    if configured:
        return configured
    return str(load_config().get("deadline", {}).get("move_out_date", "") or "")


def config_path() -> Path:
    return Path(os.environ.get("ESTATE_PRICING_CONFIG", str(DEFAULT_PATH)))


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return _FALLBACK
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.error({"action": "pricing_config_invalid", "error_type": type(exc).__name__,
                      "path": str(p)})
        return _FALLBACK
    merged = json.loads(json.dumps(_FALLBACK))
    for section, values in data.items():
        if section.startswith("_"):
            continue
        if isinstance(values, dict) and isinstance(merged.get(section), dict):
            merged[section].update({k: v for k, v in values.items() if not k.startswith("_")})
        else:
            merged[section] = values
    return merged


def get(section: str, key: str, default: Any = None) -> Any:
    return load_config().get(section, {}).get(key, default)
