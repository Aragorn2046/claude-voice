# CASE-O, Shelby's speaking voice

CASE-O (CASE-OMARCHY) is Shelby's default speaking persona. It is the Pocket
TTS predefined voice `javert`, taken from the reference clip
`hf://kyutai/tts-voices/voice-donations/Butter.wav`. The persona id is
`case-o` (`SHELBY_SPEECH_PERSONA`). On day, dawn and dusk that id is
authoritative unless config names another Shelby persona.

JARVIS is kept. The `jarvis` and `jarvis-*` ids stay selectable, and
`jarvis-reference.wav` stays on every voice server. To switch a host back,
set `tts_voice_pocket_en` and `tts_voice_pocket_codex` to `jarvis` in that
host's `scripts/config.json` (per-machine, not committed).

Each voice server needs `case-o-reference.wav` in its voices directory, next
to the kept `jarvis-reference.wav`. Without that file the `case-o` id has
nothing to speak.

CASE-O matches the popup voice. Hosts that leave `pocket_speed` and
`pocket_gain_db` unset still get 1.4 and 5.2 dB, the popup filter
`atempo=1.4,volume=5.2dB`. `scripts/config.example.json` sets those same
values. An explicit config value wins, so `pocket_speed` 1.0 and
`pocket_gain_db` 0 restore the previous speed and level. Positive gain is
limited with `alimiter=limit=0.97:level=0:latency=1` so the requested boost
stays put and the end of the utterance is not trimmed.
