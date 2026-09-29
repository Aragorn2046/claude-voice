"""Shared safety and timeout policy for Shelby speech personas."""

import math


# Preserve room inside the current 30s Stop hook budget for resolver and pin translation.
STOP_HOOK_BUDGET_S = 30.0
DUTCH_RESOLVER_TIMEOUT_S = 3.0
ENGLISH_PIN_TRANSLATION_TIMEOUT_S = 12.0
MAX_DUTCH_TIMEOUT_S = min(
    15.0,
    STOP_HOOK_BUDGET_S - DUTCH_RESOLVER_TIMEOUT_S - ENGLISH_PIN_TRANSLATION_TIMEOUT_S,
)
DEFAULT_DUTCH_TIMEOUT_S = MAX_DUTCH_TIMEOUT_S
MIN_DUTCH_TIMEOUT_S = 1.0
SHELBY_POCKET_PERSONA_ALIASES = frozenset({"eva", "codex"})


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


def is_shelby_pocket_persona(voice, config):
    """Allow the Jarvis family and configured `eva`/`codex` persona aliases.

    The aliases are derived from non-content `tts_voice_pocket_<role>` keys
    and their configured values, so a content-voice setting cannot authorize
    a clone by itself.
    """
    normalized = str(voice or "").strip().casefold()
    if normalized.startswith("jarvis"):
        return True
    if not isinstance(config, dict):
        return False

    prefix = "tts_voice_pocket_"
    configured_aliases = set()
    for key, value in config.items():
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        role = key[len(prefix):].casefold()
        if role == "content":
            continue
        if role in SHELBY_POCKET_PERSONA_ALIASES:
            configured_aliases.add(role)
        configured_name = str(value or "").strip().casefold()
        if configured_name in SHELBY_POCKET_PERSONA_ALIASES:
            configured_aliases.add(configured_name)
    return normalized in configured_aliases


def bounded_dutch_timeout(config):
    """Read the optional Dutch timeout, defaulting and clamping to 1–15s."""
    if not isinstance(config, dict):
        return DEFAULT_DUTCH_TIMEOUT_S
    try:
        timeout = float(config.get("timeout_s", DEFAULT_DUTCH_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_DUTCH_TIMEOUT_S
    if not math.isfinite(timeout):
        return DEFAULT_DUTCH_TIMEOUT_S
    return max(MIN_DUTCH_TIMEOUT_S, min(timeout, MAX_DUTCH_TIMEOUT_S))
