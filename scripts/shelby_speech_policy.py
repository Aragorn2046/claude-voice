"""Shared safety and timeout policy for Shelby speech personas."""

import math
import os
import re


# Clamp Dutch lane synthesis time and reserve nominal room for the English
# translation and Pocket synthesis fallback. Delivery is not time-bounded.
OVERALL_DUTCH_BUDGET_S = 60.0
DUTCH_RESOLVER_TIMEOUT_S = 3.0
ENGLISH_PIN_TRANSLATION_TIMEOUT_S = 12.0
DEFAULT_POCKET_TIMEOUT_S = 27
MAX_DUTCH_TIMEOUT_S = 15.0
DEFAULT_DUTCH_TIMEOUT_S = MAX_DUTCH_TIMEOUT_S
MIN_DUTCH_TIMEOUT_S = 1.0
MIN_DUTCH_LANE_BUDGET_S = 3.0
SHELBY_DUTCH_PERSONAS = frozenset({
    "jarvis",
    "jarvis-app-dynamic",
    "jarvis-app-focus",
    "jarvis-app-lively",
    "jarvis-studio-v2",
    "eva",
    "codex",
})


def normalize_dutch_persona(voice):
    """Normalize a Dutch-lane persona, rejecting non-ASCII name characters."""
    normalized = str(voice or "").strip().casefold()
    return normalized if re.fullmatch(r"[a-z0-9-]+", normalized) else ""


def pocket_synthesis_timeout_s():
    """Return the effective Pocket timeout reserved for English fallback."""
    try:
        return max(1, int(os.environ.get(
            "SHELBY_POCKET_TIMEOUT", str(DEFAULT_POCKET_TIMEOUT_S)
        )))
    except (TypeError, ValueError):
        return DEFAULT_POCKET_TIMEOUT_S


def is_content_voice(voice, configured_content_voice="aragorn"):
    """Return whether a normalized voice name selects a content clone."""
    normalized_voice = str(voice or "").strip().casefold()
    configured_prefix = str(configured_content_voice or "").strip().casefold()
    return bool(
        normalized_voice
        and (
            normalized_voice.startswith("aragorn")
            or (configured_prefix and normalized_voice.startswith(configured_prefix))
        )
    )


def is_shelby_pocket_persona(voice, config=None):
    """Accept only the exact persona IDs served by the Dutch voice lane."""
    return normalize_dutch_persona(voice) in SHELBY_DUTCH_PERSONAS


def remaining_dutch_lane_budget_s(elapsed_s=0.0, reserve_fallback=True):
    """Budget left after resolver and optional English fallback reserves."""
    try:
        elapsed = max(0.0, float(elapsed_s))
    except (TypeError, ValueError):
        elapsed = 0.0
    fallback_reserve = (
        ENGLISH_PIN_TRANSLATION_TIMEOUT_S + pocket_synthesis_timeout_s()
        if reserve_fallback else 0.0
    )
    return (
        OVERALL_DUTCH_BUDGET_S - elapsed - DUTCH_RESOLVER_TIMEOUT_S
        - fallback_reserve
    )


def bounded_dutch_timeout(config, elapsed_s=0.0, reserve_fallback=True):
    """Bound the lane timeout while retaining any requested fallback reserve."""
    remaining = remaining_dutch_lane_budget_s(elapsed_s, reserve_fallback)
    if remaining < MIN_DUTCH_LANE_BUDGET_S:
        return None
    if not isinstance(config, dict):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    try:
        timeout = float(config.get("timeout_s", DEFAULT_DUTCH_TIMEOUT_S))
    except (TypeError, ValueError):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    if not math.isfinite(timeout):
        return min(DEFAULT_DUTCH_TIMEOUT_S, remaining)
    configured_clamp = max(MIN_DUTCH_TIMEOUT_S, min(timeout, MAX_DUTCH_TIMEOUT_S))
    return min(configured_clamp, remaining)
