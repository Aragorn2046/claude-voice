# CASE-O, Shelby's speaking voice

CASE-O (CASE-OMARCHY) is Shelby's default speaking persona. It is the Pocket
TTS predefined voice `javert`, taken from the reference clip
`hf://kyutai/tts-voices/voice-donations/Butter.wav`. The persona id is
`case-o` (`SHELBY_SPEECH_PERSONA`). On day, dawn and dusk that id is
authoritative unless config names another Shelby persona.

JARVIS is kept. The `jarvis` and `jarvis-*` ids stay selectable, and
`jarvis-reference.wav` stays on every voice server. To switch a host back
to JARVIS, set `tts_voice_pocket_en` to `jarvis` in that host's
`scripts/config.json` (per-machine, not committed). That is the key the
hook and the CLI read.

Each voice server needs `case-o-reference.wav` in its voices directory, next
to the kept `jarvis-reference.wav`. Without that file the `case-o` id has
nothing to speak. If Pocket synthesis for the resolved Shelby persona fails
on a server, that same server is tried once more with `jarvis` before the
next server or the Edge fallback. A content voice is never that fallback.

Code defaults are unchanged. When `pocket_speed` and `pocket_gain_db` are
unset, speech stays at 1.0x and 0 dB and ffmpeg is not called.
`scripts/config.example.json` shows `"pocket_speed": 1.0` and
`"pocket_gain_db": 0`. The popup sound, 1.4x and +5.2 dB, is set by the
operator in each host's own gitignored `config.json`, not by code. An
explicit config value wins. Positive gain is limited with
`alimiter=limit=0.97:level=0:latency=1` so the requested boost stays put
and the end of the utterance is not trimmed. If that limiter pass fails,
ffmpeg runs once more without `alimiter`, keeping atempo and volume.
