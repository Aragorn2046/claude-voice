#!/usr/bin/env python3
"""Claude Code Stop hook — multi-engine TTS with language detection.

Engines: edge (default/free), elevenlabs (premium/streaming), kokoro (local/free)
Features:
  - Lockfile prevents dual-session double-playback
  - Auto-detects Dutch content → switches to Dutch voice
  - Engine switchable via config or /tts command
  - Remote audio: auto-discovers receiver (SSH, MOSH, any remote access)
  - SIGTERM-safe: cleans up lockfile and exits silently when killed

Receives JSON on stdin with last_assistant_message. Extracts the <voice>...</voice>
block, sanitizes it for speech, and plays it.
"""

import asyncio
import fcntl
import json
import os
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time

IS_MACOS = platform.system() == "Darwin"

# Ensure WSLg PulseAudio is available (WSL only)
if not IS_MACOS and not os.environ.get("PULSE_SERVER") and os.path.exists("/mnt/wslg/PulseServer"):
    os.environ["PULSE_SERVER"] = "unix:/mnt/wslg/PulseServer"

# Paths
CONFIG_PATH = os.path.expanduser("~/projects/claude-voice/scripts/config.json")
LOCKFILE_PATH = "/tmp/sonia-tts.lock"
LOG_PATH = "/tmp/claude-tts.log"

# Global lock handle for SIGTERM cleanup
_lock_fd = None

# Defaults (overridden by config.json)
DEFAULTS = {
    "tts_engine": "edge",
    "tts_speed": "+30%",
    "tts_voice_edge_en": "en-GB-SoniaNeural",
    "tts_voice_edge_nl": "nl-NL-FennaNeural",
    "tts_voice_elevenlabs_en": "rTWLXOmnw0ckuMBnjFoZ",  # Day Voice
    "tts_voice_elevenlabs_nl": "rTWLXOmnw0ckuMBnjFoZ",  # Day Voice (multilingual)
    "elevenlabs_model": "eleven_turbo_v2_5",
    "elevenlabs_api_key_env": "ELEVENLABS_API_KEY",
    "elevenlabs_api_key": "",
    "tts_voice_kokoro_en": "af_heart",
    "tts_voice_kokoro_nl": "af_heart",
}

EDGE_SOURCE_PERSONAS = {
    "day": {"en": "en-GB-RyanNeural", "nl": "nl-NL-MaartenNeural"},
    "dawn": {"en": "en-US-AriaNeural", "nl": "nl-NL-FennaNeural"},
    "dusk": {"en": "en-GB-SoniaNeural", "nl": "nl-BE-DenaNeural"},
}

KOKORO_SOURCE_PERSONAS = {
    "day": "am_michael",
    "dawn": "af_heart",
    "dusk": "bf_emma",
}


def log(msg: str):
    """Write to log file instead of stderr (prevents bleeding into Claude Code output)."""
    try:
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def handle_sigterm(signum, frame):
    """Clean up lockfile and exit silently on SIGTERM (from timeout or voice-shutup)."""
    global _lock_fd
    release_lock(_lock_fd)
    os._exit(0)


def load_config() -> dict:
    """Load config, merging with defaults."""
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


_SESSION_ID = ""


def _open_lockfile():
    fd = os.open(LOCKFILE_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    return os.fdopen(fd, "r+")


def _stamp_lockfile(lock_fd):
    lock_fd.seek(0)
    lock_fd.truncate()
    lock_fd.write(f"{os.getpid()}\n{_SESSION_ID}\n")
    lock_fd.flush()


def acquire_lock():
    """Acquire lockfile to prevent dual-session double-playback.
    Returns lock file handle or None if another instance is speaking."""
    global _lock_fd
    lock_fd = None
    try:
        lock_fd = _open_lockfile()
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _stamp_lockfile(lock_fd)
        _lock_fd = lock_fd
        return lock_fd
    except (IOError, OSError):
        if lock_fd:
            try:
                lock_fd.close()
            except OSError:
                pass
        # Another instance holds the lock — decide whether to steal it.
        # Etiquette: only steal if (a) holder PID is gone, (b) holder is THIS
        # session_id (so we replace our own stale lock), or (c) lockfile is
        # stale (mtime > 180s — likely a wedged hook). Otherwise yield silently
        # so a parallel session doesn't truncate our sibling's playback.
        old_pid, old_sid = None, ""
        try:
            with open(LOCKFILE_PATH) as f:
                lines = f.read().splitlines()
            old_pid = int(lines[0].strip()) if lines else None
            old_sid = lines[1].strip() if len(lines) > 1 else ""
        except (ValueError, OSError, FileNotFoundError, IndexError):
            pass
        holder_alive = False
        if old_pid:
            try:
                os.kill(old_pid, 0)
                holder_alive = True
            except OSError:
                holder_alive = False
        try:
            lock_age = time.time() - os.path.getmtime(LOCKFILE_PATH)
        except OSError:
            lock_age = 9999
        same_session = bool(old_sid) and old_sid == _SESSION_ID
        may_steal = (not holder_alive) or same_session or lock_age > 180
        if not may_steal:
            log(f"Yielding lock: holder pid={old_pid} sid={old_sid[:8]} age={lock_age:.1f}s")
            return None
        if holder_alive and old_pid:
            try:
                os.kill(old_pid, 9)
            except OSError:
                pass
        # Try again
        lock_fd = None
        try:
            lock_fd = _open_lockfile()
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            _stamp_lockfile(lock_fd)
            _lock_fd = lock_fd
            return lock_fd
        except (IOError, OSError):
            if lock_fd:
                try:
                    lock_fd.close()
                except OSError:
                    pass
            return None


def release_lock(lock_fd):
    """Release lockfile."""
    global _lock_fd
    if lock_fd:
        try:
            lock_fd.seek(0)
            lock_fd.truncate()
            lock_fd.flush()
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except (IOError, OSError):
            pass
    _lock_fd = None


# Speakable-summary shape:
#   - Always whole sentences, never mid-cut.
#   - Target 1-2 sentences (quick headsup), absolute max 4.
#   - Soft char target ~280 = ~4 average sentences. If chosen sentences would
#     exceed it, drop sentences from the front until they fit.
#   - If even the LAST single sentence is itself too long to be a clean headsup,
#     scan earlier sentences for a shorter conclusion-shaped line that fits;
#     last resort is a generic "Update ready." rather than truncating mid-word.
# Voice output is a HEADS-UP, not a readout (Codex handoff 2026-06-13, relay #29):
# a deterministic cap so even an over-long <voice> block is reduced to one short
# highlight. ~260 chars / 2 sentences max, status sentences preferred.
SUMMARY_TARGET_CHARS = 250
SUMMARY_MAX_SENTENCES = 2
SUMMARY_HARD_CHARS = 260            # absolute ceiling; never spoken past this
SUMMARY_SOFT_LONG_SENTENCE = 260   # a single sentence longer than this isn't headsup-shaped

# Sentences carrying status/priority signal are preferred over filler when
# choosing what to speak (done / verified / pushed / open ends / needs-you).
_PRIORITY_RE = re.compile(
    r"\b(done|completed?|finished|fixed|wired|installed|verified|tested|pushed|"
    r"committed|shipped|deployed|merged|works|working|ready|blocked|failing|"
    r"failed|error|open end|remaining|next|action needed|needs? you|your call|"
    r"confirm|approve|waiting)\b",
    re.IGNORECASE,
)


def _hard_cap(text: str, limit: int = SUMMARY_HARD_CHARS) -> str:
    """Force text to <= limit chars, cutting at a sentence (then word) boundary
    so the spoken output never trails off mid-word."""
    text = re.sub(r'\s+', ' ', text).strip()
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = head.rsplit('.', 1)[0].strip()
    if len(cut) < 80:
        cut = head.rsplit(' ', 1)[0].strip()
    return cut.rstrip(',:;') + '.'

# Regexes for content that must NEVER reach the speaker (defense against the
# P1 leak findings in 2026-05-19 codex review: unterminated fences, raw
# secrets, bearer-shaped tokens, internal hostnames).
_SECRET_PATTERNS = (
    re.compile(r'\b(?:sk|pk|rk|xoxb|xoxp|xoxa|ghp|gho|ghs|ghr|github_pat|glpat|key|tok|tkn)[_-][A-Za-z0-9_\-]{12,}\b', re.IGNORECASE),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\b'),  # JWT
    re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b'),  # email
    re.compile(r'\b(?:[A-Za-z0-9-]+\.)+(?:internal|local|lan|tail[a-z0-9]+\.ts\.net|onion|consul)\b', re.IGNORECASE),
    re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'),  # IPv4
    re.compile(r'\bBearer\s+[A-Za-z0-9._\-]+', re.IGNORECASE),
    re.compile(r'\b[a-f0-9]{32,}\b'),  # long hex (hashes, hex secrets)
)


def extract_voice_block(text: str) -> str:
    """Extract content from <voice>...</voice> tags.

    Fallback: if no <voice> block exists (agent forgot to emit one), derive a
    speakable summary from the visible prose so TTS never goes silent. Aragorn
    2026-05-19: compliance drifted to ~1-18% across sessions and the static
    "Response ready." beacon was firing on nearly every turn (burned 111% of
    monthly ElevenLabs quota on 15-char repeats). Replaced beacon with the
    prose-summarization the docstring originally promised but never implemented.
    Hardened 2026-05-19 after codex adversarial review — handles unterminated
    fences/tags, redacts secret-shaped tokens, picks whole sentences (never
    truncates mid-sentence), caps at 4 sentences.
    """
    # Case-insensitive, attribute-tolerant match for <voice ...>...</voice>.
    match = re.search(r'<voice\b[^>]*>(.*?)</voice\s*>', text, re.DOTALL | re.IGNORECASE)
    if match:
        return _shape_speakable(match.group(1).strip())

    return _derive_summary_from_prose(text)


def extract_voice_lang(text: str):
    """Parse an explicit language declaration from the <voice> tag, e.g.
    <voice lang="nl">. Returns 'nl' or 'en' when declared, None otherwise.

    The agent declaring its own language beats the marker-word heuristic in
    detect_language(), which misfires on short blocks (<3 words => always
    'en') and on Dutch text dense with technical English terms (<15% marker
    ratio => 'en' voice reading Dutch = garbled). S749, 2026-06-10.
    """
    match = re.search(r'<voice\b[^>]*\blang\s*=\s*["\']?([a-zA-Z]{2})', text, re.IGNORECASE)
    if match:
        lang = match.group(1).lower()
        if lang in ('nl', 'en'):
            return lang
    return None


def _strip_unbalanced_block(t: str, open_re: str, close_re: str) -> str:
    """Strip matched open/close pairs first, then drop any tail from a leftover
    open marker to end-of-text. Prevents unterminated ```/``` or `<tool_result>`
    blocks from leaking their contents into the speakable summary.
    """
    t = re.sub(f'{open_re}.*?{close_re}', ' ', t, flags=re.DOTALL)
    t = re.sub(f'{open_re}.*\\Z', ' ', t, flags=re.DOTALL)
    return t


def _split_sentences(t: str) -> list:
    """Split on sentence boundaries, preserve terminal punctuation."""
    parts = re.split(r'(?<=[.!?])\s+', t)
    return [p.strip() for p in parts if p.strip()]


def _shape_speakable(text: str) -> str:
    """Take an already-cleaned string and return a whole-sentence summary that:
       - Ends on a real sentence boundary (no mid-word cuts).
       - Has at most SUMMARY_MAX_SENTENCES sentences.
       - Fits roughly within SUMMARY_TARGET_CHARS for cost control.
       - Falls back to "Update ready." rather than emit a mid-sentence fragment.
    """
    t = re.sub(r'\s+', ' ', text).strip()
    if not t:
        return "Done."

    sentences = _split_sentences(t)
    sentences = [s for s in sentences if len(s) >= 4]
    if not sentences:
        return "Done."

    # If text has no terminal punctuation, the whole thing is one "sentence".
    # Hard-cap it rather than drop the message.
    if len(sentences) == 1 and not sentences[0].endswith(('.', '!', '?')):
        return _hard_cap(sentences[0] if sentences[0].endswith(('.', '!', '?'))
                         else sentences[0] + ".")

    # Already heads-up-shaped (<= max sentences, within the hard cap)? Speak it
    # whole — don't trim a perfectly short 2-sentence block down to one sentence
    # just because the other lacks a status keyword.
    whole = " ".join(sentences)
    if len(sentences) <= SUMMARY_MAX_SENTENCES and len(whole) <= SUMMARY_HARD_CHARS:
        return _hard_cap(whole)

    # Longer block: prefer sentences carrying status/priority signal; if none, all.
    prioritized = [s for s in sentences if _PRIORITY_RE.search(s)]
    pool = prioritized if prioritized else sentences

    # Build from the FRONT (lede-first), in document order. A heads-up leads with
    # the headline ("Done. Main result: X. Open end: Y"), so the first status
    # sentence is the one to speak — NOT the trailing meta/sign-off line.
    picked = []
    char_count = 0
    for s in pool:
        if len(picked) >= SUMMARY_MAX_SENTENCES:
            break
        # Skip individual sentences too long to be a headsup (hard-capped below
        # if they're all we have).
        if len(s) > SUMMARY_SOFT_LONG_SENTENCE:
            if picked:
                break
            continue
        prospective = char_count + len(s) + (1 if picked else 0)
        if picked and prospective > SUMMARY_TARGET_CHARS:
            break
        picked.append(s)
        char_count = prospective
        # Prefer terse: stop after 1 sentence if it's already a substantial headsup.
        if len(picked) == 1 and len(s) >= 120:
            break

    if not picked:
        # Everything exceeded the headsup length — hard-cap the first (or first
        # priority) sentence at a clean boundary rather than dropping the message.
        return _hard_cap(pool[0])

    return _hard_cap(" ".join(picked))


def _derive_summary_from_prose(text: str) -> str:
    """Strip non-speakable elements + redact secrets, then return a whole-
    sentence summary (1-2 sentences typical, max 4, never mid-cut)."""
    t = text

    t = _strip_unbalanced_block(t, r'```', r'```')
    t = re.sub(r'`[^`\n]*`', ' ', t)

    for tag in ('thinking', 'system-reminder', 'function_calls', 'function_results',
                'tool_use', 'tool_result', 'parameter', 'antml:function_calls',
                'antml:invoke', 'antml:parameter', 'voice'):
        t = _strip_unbalanced_block(t, rf'<{tag}\b[^>]*>', rf'</{tag}\s*>')

    t = re.sub(r'</?[a-zA-Z][^>]*>', ' ', t)
    t = re.sub(r'^[\s]*[#>\-\*\+]+\s*', ' ', t, flags=re.MULTILINE)
    t = re.sub(r'^\s*\|.*\|\s*$', ' ', t, flags=re.MULTILINE)
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'[~/][\w./-]+', ' ', t)
    t = re.sub(r'\b[\w-]+\.(?:py|sh|js|ts|md|json|toml|yaml|yml|html|css|log|jsonl|txt)\b',
               ' ', t)

    for pat in _SECRET_PATTERNS:
        t = pat.sub(' ', t)

    return _shape_speakable(t)


def sanitize_for_speech(text: str) -> str:
    """Strip formatting artifacts that sound weird when spoken."""
    text = text.replace('\\n', ' ')
    text = text.replace('\\t', ' ')
    text = text.replace('\\', '')
    text = re.sub(r'[`*_#\[\](){}|~>]', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[~/][a-zA-Z0-9_./-]+', '', text)
    text = re.sub(r'/[a-z_-]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def detect_language(text: str) -> str:
    """Simple heuristic to detect Dutch vs English.
    Returns 'nl' for Dutch, 'en' for everything else."""
    dutch_markers = [
        'dat', 'het', 'een', 'van', 'zijn', 'voor', 'niet', 'maar', 'ook',
        'dit', 'wat', 'aan', 'nog', 'wel', 'naar', 'hier', 'alle', 'waar',
        'moet', 'heel', 'geen', 'klaar', 'gedaan', 'goed', 'alles', 'even',
        'staat', 'wordt', 'wil', 'kan', 'heb', 'bij', 'mij', 'jij', 'zij',
        'zit', 'daar', 'dus', 'nou', 'laten', 'kijk', 'beetje', 'eigenlijk',
    ]
    words = text.lower().split()
    if len(words) < 3:
        return 'en'
    dutch_count = sum(1 for w in words if w.rstrip('.,!?:;') in dutch_markers)
    ratio = dutch_count / len(words)
    return 'nl' if ratio > 0.15 else 'en'


def play_audio_file(filepath: str):
    """Play an audio file using the platform-appropriate player."""
    if IS_MACOS:
        subprocess.run(["afplay", filepath], capture_output=True, timeout=30)
    else:
        # Convert to raw PCM and use paplay (WSL/Linux)
        import soundfile as sf
        import numpy as np
        data, srate = sf.read(filepath)
        pcm = (data * 32767).astype(np.int16).tobytes()
        channels = 1 if data.ndim == 1 else data.shape[1]
        play_raw_pcm(pcm, srate, channels)


def play_raw_pcm(pcm_data: bytes, srate: int, channels: int):
    """Play raw PCM data via paplay (WSL/Linux only)."""
    if IS_MACOS:
        # Write to temp wav and play with afplay
        import struct
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        try:
            # Write WAV header + data
            bits_per_sample = 16
            byte_rate = srate * channels * bits_per_sample // 8
            block_align = channels * bits_per_sample // 8
            data_size = len(pcm_data)
            tmp.write(b'RIFF')
            tmp.write(struct.pack('<I', 36 + data_size))
            tmp.write(b'WAVE')
            tmp.write(b'fmt ')
            tmp.write(struct.pack('<IHHIIHH', 16, 1, channels, srate, byte_rate, block_align, bits_per_sample))
            tmp.write(b'data')
            tmp.write(struct.pack('<I', data_size))
            tmp.write(pcm_data)
            tmp.close()
            subprocess.run(["afplay", tmp.name], capture_output=True, timeout=30)
        finally:
            os.unlink(tmp.name)
        return
    subprocess.run(
        ["paplay", "--raw", f"--rate={srate}", f"--channels={channels}", "--format=s16le"],
        input=pcm_data, capture_output=True, timeout=30
    )


async def speak_edge(text: str, voice: str, speed: str, remote_target: str = None,
                     play_local: bool = True, remote_requires_off_lan: bool = False,
                     remote_fallback_target: str = None):
    """Edge TTS — free, cloud-based.

    play_local: if False, do NOT fall through to local playback. Used when the
    machine's default output is a virtual device with no working forwarding
    chain — playing would generate silent audio.
    """
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=speed)
        await communicate.save(tmp_path)

        if remote_target:
            import soundfile as sf
            import numpy as np
            data, srate = sf.read(tmp_path)
            pcm = (data * 32767).astype(np.int16).tobytes()
            channels = 1 if data.ndim == 1 else data.shape[1]
            wav_data = make_wav(pcm, srate, channels)
            if send_audio_remote(
                wav_data, remote_target, require_off_lan=remote_requires_off_lan,
                fallback_target=remote_fallback_target,
            ):
                return
            if not play_local:
                log("Remote send failed and play_local=False — not falling back to local")
                return

        if not play_local:
            return

        if IS_MACOS:
            play_audio_file(tmp_path)
        else:
            import soundfile as sf
            import numpy as np
            data, srate = sf.read(tmp_path)
            pcm = (data * 32767).astype(np.int16).tobytes()
            channels = 1 if data.ndim == 1 else data.shape[1]
            play_raw_pcm(pcm, srate, channels)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# Silence padding (ms) prepended/appended to synthesized PCM. Covers audio-device
# warmup (start clip — the dropped first word, e.g. "Done") and buffer drain on
# stream end (tail clip — the cut-off last word). Applies to both the remote-WAV
# path and local afplay/paplay. Tunable via config keys tts_pad_lead_ms / tts_pad_tail_ms.
PAD_LEAD_MS = 300
PAD_TAIL_MS = 500


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str,
                              speed: float = 1.0, remote_target: str = None,
                              play_local: bool = True, lead_ms: int = PAD_LEAD_MS,
                              tail_ms: int = PAD_TAIL_MS, primer_amp: int = 0,
                              remote_requires_off_lan: bool = False,
                              remote_fallback_target: str = None):
    """ElevenLabs with streaming via raw HTTP — supports speed parameter.

    play_local: if False, do NOT fall through to local playback.
    Caller should already have called find_audible_path() and gated the
    ElevenLabs API call entirely if neither remote nor local were audible.
    """
    import requests

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=pcm_24000"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "text": text,
        "model_id": model,
    }
    if speed != 1.0:
        body["speed"] = speed

    resp = requests.post(url, json=body, headers=headers, stream=True, timeout=(10, 90))
    resp.raise_for_status()

    if remote_target:
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        pcm_data = pad_pcm(pcm_data, 24000, 1, lead_ms, tail_ms, primer_amp)
        wav_data = make_wav(pcm_data, 24000, 1)
        if send_audio_remote(
            wav_data, remote_target, require_off_lan=remote_requires_off_lan,
            fallback_target=remote_fallback_target,
        ):
            return
        if not play_local:
            log("Remote send failed and play_local=False — not falling back to local")
            return
        # play_local=True: fall through to local playback below
        play_raw_pcm(pcm_data, 24000, 1)
        return

    if not play_local:
        return

    if IS_MACOS:
        # Collect all PCM data, write to temp WAV, play with afplay
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        pcm_data = pad_pcm(pcm_data, 24000, 1, lead_ms, tail_ms, primer_amp)
        play_raw_pcm(pcm_data, 24000, 1)
    else:
        # Stream directly to paplay (WSL/Linux)
        proc = subprocess.Popen(
            ["paplay", "--raw", "--rate=24000", "--channels=1", "--format=s16le"],
            stdin=subprocess.PIPE
        )
        try:
            for chunk in resp.iter_content(chunk_size=4096):
                if chunk:
                    proc.stdin.write(chunk)
            proc.stdin.close()
            # 180s = ~3 min of speech. Was 30s, which clipped any reply
            # over ~470 chars mid-sentence. voice-shutup.sh still kills the
            # process by name on the next user prompt, so this only governs
            # the natural end-of-playback wait.
            proc.wait(timeout=180)
        except Exception:
            proc.kill()


def speak_kokoro(text: str, voice: str, speed: float = 1.0):
    """Kokoro TTS — local, free, good quality."""
    try:
        from kokoro import KPipeline
        import numpy as np
    except ImportError:
        log("Kokoro not installed, falling back to Edge TTS")
        return False

    try:
        lang = 'a'  # American English default
        if detect_language(text) == 'nl':
            lang = 'a'  # Kokoro doesn't have native Dutch yet, use English

        pipeline = KPipeline(lang_code=lang)
        samples_list = []

        for _, _, audio in pipeline(text, voice=voice, speed=speed):
            if audio is not None:
                samples_list.append(audio.numpy() if hasattr(audio, 'numpy') else audio)

        if not samples_list:
            return False

        import numpy as np
        audio_data = np.concatenate(samples_list)
        pcm = (audio_data * 32767).astype(np.int16).tobytes()
        play_raw_pcm(pcm, 24000, 1)
        return True
    except Exception as e:
        log(f"Kokoro error: {e}")
        return False


REMOTE_AUDIO_PORT = 12345

def _pad_wav_tail(wav_data: bytes, tail_ms: int = 1000) -> bytes:
    """Append PCM silence so Windows playback drains after the final phoneme."""
    import io
    import wave

    if tail_ms <= 0:
        return wav_data
    try:
        source = io.BytesIO(wav_data)
        with wave.open(source, "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(params.nframes)
        silent_frames = int(params.framerate * tail_ms / 1000)
        silence = b"\x00" * silent_frames * params.nchannels * params.sampwidth
        output = io.BytesIO()
        with wave.open(output, "wb") as writer:
            writer.setparams(params)
            writer.writeframes(frames + silence)
        return output.getvalue()
    except Exception as e:
        log(f"Could not pad Pocket WAV tail: {e}")
        return wav_data


def speak_pocket(text: str, voice: str, remote_target: str = None, play_local: bool = True,
                 base_url: str = "http://127.0.0.1:8933",
                 fallback_local: bool | None = None, tail_ms: int = 1000,
                 remote_requires_off_lan: bool = False,
                 remote_fallback_target: str = None) -> bool:
    """Local pocket-tts daemon (com.shelby.pocket-tts, Day). Returns True on success.

    English-only engine — callers must route 'nl' elsewhere. WAV comes back
    whole (no streaming), so latency ≈ generation time (~1-2s for a voice block).
    """
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(
            f"{base_url}/tts",
            data=json.dumps({"text": text, "voice": voice}).encode(),
            headers={"Content-Type": "application/json"})
        # Cloned voices can take longer than eight seconds on a cold model.
        # Keep this aligned with the Codex adapter so neither runtime silently
        # falls back to a generic Edge voice for the same machine identity.
        timeout = int(os.environ.get("SHELBY_POCKET_TIMEOUT", "27"))
        wav_data = urllib.request.urlopen(req, timeout=timeout).read()
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        log(f"pocket-tts request failed: {e}")
        return False
    if not wav_data or len(wav_data) < 1000:
        log(f"pocket-tts returned suspiciously small payload ({len(wav_data)} bytes)")
        return False
    wav_data = _pad_wav_tail(wav_data, tail_ms=tail_ms)
    if fallback_local is None:
        fallback_local = play_local

    ok = False
    remote_ok = False
    if remote_target:
        remote_ok = send_audio_remote(
            wav_data, remote_target, require_off_lan=remote_requires_off_lan,
            fallback_target=remote_fallback_target,
        )
        ok = remote_ok or ok
        if remote_ok and not play_local:
            return True

    should_play_local = play_local or (bool(remote_target) and not remote_ok and fallback_local)
    if should_play_local:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_data)
                tmp_path = f.name
            play_audio_file(tmp_path)
            ok = True
        except Exception as e:
            log(f"pocket-tts local playback failed: {e}")
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    return ok


# Audible-path cache — avoids re-probing receivers on every turn.
# Bust with: rm /tmp/voice-audible-cache.json
AUDIBLE_CACHE_PATH = "/tmp/voice-audible-cache.json"
AUDIBLE_CACHE_TTL = 30  # seconds


def _probe_url(url: str, timeout: float = 0.5) -> bool:
    """Quick HTTP GET probe. Returns True on 2xx, False otherwise."""
    import urllib.request
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


ROUTE_LEGACY = "legacy"
ROUTE_ORIGIN = "origin"
ROUTE_DUSK = "dusk"


def _source_machine(cfg: dict) -> str:
    """Return a stable fleet identity; config is authoritative over hostname."""
    import socket

    configured = str(cfg.get("source_machine") or cfg.get("machine") or "").strip().lower()
    if configured:
        return configured
    return socket.gethostname().split(".", 1)[0].strip().lower()


def _fetch_health_payload(url: str, timeout: float, attempts: int) -> dict | None:
    """Fetch one versioned receiver payload with bounded transient retries."""
    import urllib.request

    payload = None
    for _attempt in range(max(1, attempts)):
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if not 200 <= resp.status < 300:
                    return None
                body = resp.read(65536)
            payload = json.loads(body)
            break
        except Exception:
            continue

    return payload if isinstance(payload, dict) else None


def _receiver_health(url: str, expected_receiver: str, timeout: float = 1.0,
                     attempts: int = 2, home_network_names: tuple = ("AragornHQ",),
                     max_location_age_seconds: float = 30.0) -> dict | None:
    """Validate a Dusk receiver's identity, readiness, and tri-state location."""
    payload = _fetch_health_payload(url, timeout, attempts)
    if not payload or payload.get("schema_version") != "shelby.voice-receiver.health.v1":
        return None
    receiver_id = str(payload.get("receiver_id") or "").split(".", 1)[0].strip().lower()
    if receiver_id != expected_receiver.strip().lower():
        return None
    if payload.get("audio_ready") is not True or payload.get("location_ready") is not True:
        return None
    if not isinstance(payload.get("home_lan"), bool):
        return None
    profiles = payload.get("network_profiles")
    if not isinstance(profiles, list) or not profiles or not all(
        isinstance(name, str) and name.strip() for name in profiles
    ):
        return None
    physical_profiles = payload.get("physical_network_profiles")
    if not isinstance(physical_profiles, list) or not all(
        isinstance(name, str) and name.strip() for name in physical_profiles
    ):
        return None
    if any(name not in profiles for name in physical_profiles):
        return None
    age = payload.get("location_age_seconds")
    if not isinstance(age, (int, float)) or not 0 <= age <= max_location_age_seconds:
        return None
    home_names = {str(name).strip().casefold() for name in home_network_names if str(name).strip()}
    computed_home = any(name.strip().casefold() in home_names for name in physical_profiles)
    if payload["home_lan"] != computed_home:
        return None
    state = payload.get("location_state")
    if state not in {"home", "away"}:
        return None
    if state == "home" and not payload["home_lan"]:
        return None
    if state == "away" and (payload["home_lan"] or not physical_profiles):
        return None
    return payload


def _origin_receiver_health(url: str, expected_receiver: str,
                            timeout: float = 0.75, attempts: int = 2):
    """Return valid JSON, 'legacy', False-invalid, or None-unavailable."""
    import urllib.request

    for _attempt in range(max(1, attempts)):
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if not 200 <= response.status < 300:
                    return False
                body = response.read(65536)
                headers = getattr(response, "headers", None)
                content_type = str(headers.get("Content-Type", "")) if headers else ""
        except Exception:
            continue
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            text = body.decode("utf-8", errors="replace").strip()
            if text == "OK" and (not content_type or content_type.lower().startswith("text/plain")):
                return "legacy"
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != "shelby.voice-receiver.health.v1":
            return False
        receiver_id = str(payload.get("receiver_id") or "").split(".", 1)[0].strip().lower()
        if receiver_id != expected_receiver.strip().lower():
            return False
        if payload.get("audio_ready") is not True:
            return False
        return payload
    return None


def _is_loopback_url(url: str) -> bool:
    import urllib.parse

    try:
        host = (urllib.parse.urlparse(url).hostname or "").casefold()
    except ValueError:
        return False
    return host in {"127.0.0.1", "::1", "localhost"}


def resolve_dusk_presence_route(cfg: dict) -> tuple[str, str | None]:
    """Return an authoritative legacy/origin/Dusk route decision.

    When enabled, ambiguous or unavailable Dusk state fails safely to the
    originating machine. Only a versioned, identity-checked Dusk receiver can
    redirect Day or Dawn off AragornHQ.
    """
    policy = cfg.get("dusk_presence_routing")
    if not isinstance(policy, dict) or not policy.get("enabled", False):
        return ROUTE_LEGACY, None

    source = _source_machine(cfg)
    origin_target = str(policy.get("origin_target") or "").strip() or None
    if source == "dusk":
        log("Source is Dusk — keeping Dusk voice on Dusk")
        return ROUTE_ORIGIN, origin_target
    if source not in {"day", "dawn"}:
        log(f"Unknown source machine {source!r} — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target

    off_lan_target = str(policy.get("off_lan_target") or policy.get("away_target") or "").strip()
    if not off_lan_target:
        log("Dusk off-LAN target is unset — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target
    health_url = str(
        policy.get("off_lan_health_url")
        or policy.get("away_health_url")
        or off_lan_target.replace("/tts", "/health")
    )
    timeout = float(policy.get("probe_timeout_seconds", 1.0))
    attempts = int(policy.get("probe_attempts", 2))
    home_network_names = tuple(policy.get("home_network_names", ["AragornHQ"]))
    max_location_age = float(policy.get("max_location_age_seconds", 30.0))
    health = _receiver_health(
        health_url, expected_receiver="dusk", timeout=timeout, attempts=attempts,
        home_network_names=home_network_names,
        max_location_age_seconds=max_location_age,
    )
    if health is None:
        log("Dusk location/readiness is unknown — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target
    if health["home_lan"]:
        log("Dusk receiver reports AragornHQ — keeping TTS on the origin")
        return ROUTE_ORIGIN, origin_target

    log(f"Dusk receiver reports off AragornHQ LAN — routing audio to {off_lan_target}")
    return ROUTE_DUSK, off_lan_target


def resolve_dusk_presence_target(cfg: dict) -> str | None:
    """Compatibility wrapper returning only a confirmed Dusk remote target."""
    route, target = resolve_dusk_presence_route(cfg)
    return target if route == ROUTE_DUSK else None


def is_local_output_real() -> bool:
    """True if the machine's default audio output is a real speaker, False if a
    virtual device (BlackHole, monitor, null sink, loopback) that needs an
    external forwarding chain to be audible.

    Conservative default: True on any detection error — better to attempt
    playback than to silently suppress when we're unsure.
    """
    if IS_MACOS:
        try:
            result = subprocess.run(
                ["system_profiler", "SPAudioDataType"],
                capture_output=True, text=True, timeout=3
            )
            current_device = None
            for line in result.stdout.splitlines():
                stripped = line.strip()
                if stripped.endswith(":") and line.startswith("        ") and not line.startswith("          "):
                    current_device = stripped.rstrip(":")
                if "Default Output Device: Yes" in line and current_device:
                    return not any(v in current_device for v in
                                   ("BlackHole", "Virtual", "Loopback", "Aggregate", "Multi-Output"))
        except Exception:
            return True
        return True
    else:
        try:
            result = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=2)
            for line in result.stdout.splitlines():
                if line.startswith("Default Sink:"):
                    sink = line.split(":", 1)[1].strip().lower()
                    return not any(v in sink for v in ("null", "monitor", "dummy"))
        except Exception:
            return True
        return True


def find_audible_path(cfg: dict) -> tuple:
    """Determine whether TTS will actually be heard, and where.

    Returns (remote_url, play_local):
      - remote_url: str | None — confirmed-healthy receiver URL, else None
      - play_local: bool — True iff local playback would be audible
                    (real speaker, OR virtual output with healthy forwarding)

    If both falsy → suppress TTS entirely (saves ElevenLabs tokens).

    Cached for `audible_path_cache_ttl_seconds` (default 30s).
    """
    now = time.time()
    cache_ttl = cfg.get("audible_path_cache_ttl_seconds", AUDIBLE_CACHE_TTL)
    policy = cfg.get("dusk_presence_routing")
    policy_enabled = isinstance(policy, dict) and policy.get("enabled", False)

    if not policy_enabled:
        try:
            with open(AUDIBLE_CACHE_PATH) as f:
                cache = json.load(f)
            if now - cache.get("ts", 0) < cache_ttl:
                return cache.get("remote_url"), bool(cache.get("play_local", False))
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    remote_url = None
    local_ips = get_local_ips()

    if cfg.get("remote_audio", False):
        route, policy_target = resolve_dusk_presence_route(cfg)
        if route == ROUTE_DUSK:
            remote_url = policy_target
        elif route == ROUTE_ORIGIN:
            if policy_target:
                health_url = policy_target.replace("/tts", "/health")
                source = _source_machine(cfg)
                origin_health = _origin_receiver_health(
                    health_url,
                    expected_receiver=source,
                    timeout=float(policy.get("origin_probe_timeout_seconds", 0.75)),
                    attempts=int(policy.get("origin_probe_attempts", 2)),
                )
                legacy_origin_ok = (
                    origin_health == "legacy"
                    and _is_loopback_url(policy_target)
                    and policy.get("allow_legacy_loopback_origin_health", True)
                )
                if isinstance(origin_health, dict) or legacy_origin_ok:
                    remote_url = policy_target
                    log(f"Using same-machine receiver {policy_target}")
                else:
                    log(f"Same-machine receiver {policy_target} unavailable — trying direct local playback")
        else:
            cfg_target = (cfg.get("remote_audio_target") or "").strip()
            if cfg_target:
                health_url = cfg_target.replace("/tts", "/health")
                if _probe_url(health_url, timeout=0.5):
                    remote_url = cfg_target
                    log(f"Explicit remote_audio_target {cfg_target} healthy")
                else:
                    log(f"Explicit remote_audio_target {cfg_target} DOWN — falling through to receivers list")

        if route == ROUTE_LEGACY and not remote_url:
            for recv in cfg.get("remote_audio_receivers", []):
                ip = (recv.get("ip") or "").strip()
                if not ip or ip in local_ips:
                    continue
                if recv.get("protocol", "http") != "http":
                    continue
                port = recv.get("port", REMOTE_AUDIO_PORT)
                if _probe_url(f"http://{ip}:{port}/health", timeout=0.5):
                    remote_url = f"http://{ip}:{port}/tts"
                    log(f"Auto-discovered receiver: {recv.get('name', ip)} ({remote_url})")
                    break

    play_local = False
    if is_local_output_real():
        play_local = True
    else:
        fwd = cfg.get("local_audio_forward")
        if fwd:
            ip = (fwd.get("ip") or "").strip()
            port = fwd.get("port", REMOTE_AUDIO_PORT)
            health_url = fwd.get("health_url") or f"http://{ip}:{port}/health"
            if _probe_url(health_url, timeout=0.5):
                fwd_tts_url = f"http://{ip}:{port}/tts"
                if remote_url and remote_url == fwd_tts_url:
                    play_local = False
                    log(f"Remote target == forward target ({fwd_tts_url}) — dropping local to avoid double playback")
                else:
                    play_local = True
                    log(f"Virtual local output, forwarding to {fwd.get('name', ip)} healthy — local audible")
            else:
                log(f"Virtual local output, forwarding target {fwd.get('name', ip)} DOWN — local muted")
        else:
            log("Virtual local output with no local_audio_forward configured — local muted")

    if not policy_enabled:
        try:
            with open(AUDIBLE_CACHE_PATH, "w") as f:
                json.dump({"ts": now, "remote_url": remote_url, "play_local": play_local}, f)
        except Exception:
            pass

    return remote_url, play_local


def get_local_ips() -> set:
    """Get this machine's IP addresses to avoid sending audio to ourselves."""
    import socket
    ips = {"127.0.0.1", "::1"}
    try:
        # Get all addresses for this hostname
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    # Also try connecting to a remote address to find our Tailscale IP
    for probe in ["100.77.19.108", "100.99.87.61", "100.96.47.104"]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((probe, 80))
            ips.add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
    return ips


def get_remote_audio_target(cfg: dict) -> str | None:
    """If remote_audio is enabled, find the right receiver automatically.

    Works with SSH, MOSH, Zellij, tmux, or any remote access method.

    Strategy:
    1. REMOTE_AUDIO_TARGET env var (hard override)
    2. If SSH_CLIENT set, prefer receiver matching SSH origin IP
    3. Auto-discover: probe all receivers, skip self, first healthy wins
    """
    if not cfg.get("remote_audio", False):
        return None

    port = cfg.get("remote_audio_port", REMOTE_AUDIO_PORT)

    # 1. Env var override (for edge cases or debugging)
    env_target = os.environ.get("REMOTE_AUDIO_TARGET", "")
    if env_target:
        return env_target if env_target.startswith("http") else f"http://{env_target}:{port}/tts"

    # 2. Explicit config target (set by /tts dawn|dusk|local)
    cfg_target = cfg.get("remote_audio_target", "")
    if cfg_target:
        log(f"Using explicit remote_audio_target from config: {cfg_target}")
        return cfg_target

    # 2. Build receiver list from config
    receivers = cfg.get("remote_audio_receivers", [
        {"name": "Dawn", "ip": "100.77.19.108", "port": 12345},
        {"name": "Dusk", "ip": "100.99.87.61", "port": 12345},
    ])

    # 3. Filter out ourselves — never send audio to the machine generating it
    local_ips = get_local_ips()
    receivers = [r for r in receivers if r.get("ip", "") not in local_ips]

    if not receivers:
        log("No remote receivers after filtering out local IPs")
        return None

    # 4. If SSH_CLIENT is set, sort to prefer the receiver matching the SSH origin
    ssh_client = os.environ.get("SSH_CLIENT", "")
    ssh_origin_ip = ssh_client.split()[0] if ssh_client else ""
    if ssh_origin_ip:
        receivers = sorted(receivers, key=lambda r: r.get("ip", "") != ssh_origin_ip)

    # 5. Probe receivers — first healthy one wins
    import urllib.request
    for recv in receivers:
        ip = recv.get("ip", "")
        rport = recv.get("port", port)
        url = f"http://{ip}:{rport}/health"
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=0.5) as resp:
                if resp.status == 200:
                    target = f"http://{ip}:{rport}/tts"
                    log(f"Auto-discovered receiver: {recv.get('name', ip)} ({target})")
                    return target
        except Exception:
            continue

    # 6. Last resort: if SSH_CLIENT is set, try that IP directly
    if ssh_origin_ip and ssh_origin_ip not in local_ips:
        log(f"No healthy receiver found, trying SSH_CLIENT IP {ssh_origin_ip}")
        return f"http://{ssh_origin_ip}:{port}/tts"

    log("No remote receiver found")
    return None


def _poll_delivery_status(requests_module, status_url: str,
                          delays=(0.0, 0.15, 0.35, 0.7)) -> bool | None:
    """Return True for accepted playback, False for terminal failure, else None."""
    for delay in delays:
        if delay:
            time.sleep(delay)
        try:
            response = requests_module.get(status_url, timeout=(1, 2))
            if response.status_code == 404:
                continue
            response.raise_for_status()
            status = str(response.json().get("status") or "")
        except Exception:
            continue
        if status in {"playing", "played"}:
            return True
        if status in {"failed", "rejected", "cancelled"}:
            return False
        # accepted/reserved/missing are non-terminal: playback may still start.
    return None


def _cancel_reserved_delivery(requests_module, cancel_url: str) -> bool | None:
    """Cancel a reservation. False means it was already accepted; None unknown."""
    try:
        response = requests_module.delete(cancel_url, timeout=(1, 2))
        payload = response.json() if response.content else {}
        status = str(payload.get("status") or "")
        if response.status_code == 200 and status == "cancelled":
            return True
        if response.status_code == 409 or status in {"accepted", "playing", "played"}:
            return False
    except Exception:
        return None
    return None


def send_audio_remote(wav_data: bytes, target_url: str,
                      require_off_lan: bool = False,
                      fallback_target: str = None) -> bool:
    """Deliver WAV exactly once using reserve, upload, reconcile, and cancel."""
    import uuid
    import requests

    def definitive_failure(reason: str) -> bool:
        log(reason)
        if fallback_target and fallback_target != target_url:
            log(f"Failing over definitively rejected audio to origin {fallback_target}")
            return send_audio_remote(wav_data, fallback_target)
        return False

    delivery_id = uuid.uuid4().hex
    headers = {
        "Content-Type": "audio/wav",
        "X-Shelby-Delivery-ID": delivery_id,
    }
    if require_off_lan:
        headers["X-Shelby-Require-Off-Lan"] = "1"
    receiver_base = target_url.rsplit("/", 1)[0]
    status_url = receiver_base + f"/delivery/{delivery_id}"
    reserve_url = status_url + "/reserve"

    # Reserving never starts playback. If it fails ambiguously, origin fallback
    # is safe because no audio bytes have been transmitted yet.
    legacy_loopback = False
    try:
        reserve = requests.post(
            reserve_url, data=b"", headers=headers, timeout=(1, 2)
        )
        if reserve.status_code in {404, 405}:
            legacy_loopback = _is_loopback_url(target_url) and not require_off_lan
            if not legacy_loopback:
                return definitive_failure(
                    f"Receiver at {target_url} lacks exact-once protocol — using origin"
                )
        elif not 200 <= reserve.status_code < 300:
            return definitive_failure(
                f"Receiver reservation rejected ({reserve.status_code}) — using origin"
            )
    except Exception as e:
        return definitive_failure(
            f"Receiver reservation failed ({target_url}): {e} — using origin"
        )

    if legacy_loopback:
        try:
            response = requests.post(
                target_url, data=wav_data, headers=headers, timeout=(2, 10)
            )
            response.raise_for_status()
            log(f"Sent {len(wav_data)} bytes to legacy loopback receiver")
            return True
        except Exception as e:
            return definitive_failure(
                f"Legacy loopback delivery failed ({e}) — using direct local playback"
            )

    try:
        response = requests.post(
            target_url, data=wav_data, headers=headers, timeout=(2, 10)
        )
        if not 200 <= response.status_code < 300:
            # The receiver contract guarantees that 4xx/5xx responses happen
            # before acceptance. Cancel the unused reservation best-effort.
            _cancel_reserved_delivery(requests, status_url)
            return definitive_failure(
                f"Remote delivery rejected ({response.status_code}) — using origin"
            )
        result = _poll_delivery_status(requests, status_url, delays=(0.0, 0.05, 0.1))
        if result is False:
            return definitive_failure(
                f"Delivery {delivery_id[:8]} failed before playback — using origin"
            )
        log(f"Sent {len(wav_data)} bytes to {target_url} delivery={delivery_id[:8]}")
        return True
    except Exception as e:
        log(f"Remote delivery outcome ambiguous ({e}) — reconciling")

    reconciled = _poll_delivery_status(requests, status_url)
    if reconciled is not None:
        log(f"Delivery {delivery_id[:8]} reconciliation={reconciled}")
        return reconciled if reconciled else definitive_failure(
            f"Delivery {delivery_id[:8]} failed terminally — using origin"
        )

    cancelled = _cancel_reserved_delivery(requests, status_url)
    if cancelled is True:
        return definitive_failure(
            f"Delivery {delivery_id[:8]} cancelled before acceptance — using origin"
        )
    if cancelled is False:
        log(f"Delivery {delivery_id[:8]} was already accepted — suppressing duplicate local play")
        return True

    # If neither status nor cancellation can be observed, an accepted upload
    # is indistinguishable from a network outage. Favor exact-once behavior.
    log(f"Delivery {delivery_id[:8]} remains ambiguous — suppressing duplicate local play")
    return True


def make_wav(pcm_data: bytes, srate: int = 24000, channels: int = 1) -> bytes:
    """Convert raw PCM s16le data to WAV format in memory."""
    import struct
    bits_per_sample = 16
    byte_rate = srate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)
    header = b'RIFF'
    header += struct.pack('<I', 36 + data_size)
    header += b'WAVE'
    header += b'fmt '
    header += struct.pack('<IHHIIHH', 16, 1, channels, srate, byte_rate, block_align, bits_per_sample)
    header += b'data'
    header += struct.pack('<I', data_size)
    return header + pcm_data


def _make_primer(lead_ms: int, amp: int, srate: int = 24000, channels: int = 1) -> bytes:
    """Build a fade-in low-amplitude noise lead. The Dawn receiver chain
    (BlackHole -> VBAN -> Voicemeeter) has a noise gate / VAD that strips
    leading SILENCE and then eats ~1.2s of real speech while the device opens
    (proven 2026-06-13: 1500ms vs 3000ms silence lead both lost numbers 1-6).
    A non-silent primer trips the gate open BEFORE the words start; the linear
    fade-in from zero avoids an abrupt 'static' onset. amp<=0 disables it."""
    import array, random
    n = srate * channels * max(0, lead_ms) // 1000
    if n <= 0 or amp <= 0:
        return b'\x00' * (srate * channels * 2 // 1000 * max(0, lead_ms))
    rnd = random.Random(0x5E1B)  # deterministic seed; reproducible primer
    # The gate needs immediate full-level signal from t=0 to open in time — a
    # full-duration fade-in keeps it quiet too long and the gate stays shut
    # (proven 2026-06-13: amp-1100 fade lost numbers 1-6). So hold full
    # amplitude across the body, with only short anti-click ramps at the edges.
    edge = min(max(1, n // 8), srate // 100)  # ~10ms attack/release, capped
    out = array.array('h', bytes(2 * n))
    for i in range(n):
        if i < edge:
            gain = (i + 1) / edge
        elif i >= n - edge:
            gain = (n - i) / edge
        else:
            gain = 1.0
        out[i] = int(rnd.randint(-amp, amp) * gain)
    return out.tobytes()


def pad_pcm(pcm_data: bytes, srate: int = 24000, channels: int = 1,
            lead_ms: int = PAD_LEAD_MS, tail_ms: int = PAD_TAIL_MS,
            primer_amp: int = 0) -> bytes:
    """Prepend a lead (faded-noise primer if primer_amp>0, else silence) and
    append trailing silence, so the receiver's gate/drain doesn't clip speech."""
    bytes_per_ms = srate * channels * 2 // 1000
    if primer_amp > 0 and lead_ms > 0:
        lead = _make_primer(lead_ms, primer_amp, srate, channels)
    else:
        lead = b'\x00' * (bytes_per_ms * max(0, lead_ms))
    tail = b'\x00' * (bytes_per_ms * max(0, tail_ms))
    return lead + pcm_data + tail


def _resolve_elevenlabs_key(cfg: dict) -> str:
    """Resolve the ElevenLabs API key. Prefer config, then the inherited env,
    then ~/.secrets/elevenlabs.env. The secrets-file fallback makes the voice
    consistent across sessions whose launching shell never sourced the env
    (which otherwise silently degraded to the Edge fallback voice)."""
    env_name = cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY")
    key = cfg.get("elevenlabs_api_key") or os.environ.get(env_name, "")
    if key:
        return key
    secrets_path = os.path.expanduser("~/.secrets/elevenlabs.env")
    try:
        with open(secrets_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == env_name:
                    v = v.strip().strip('"').strip("'")
                    if v:
                        os.environ.setdefault(env_name, v)
                        log(f"Loaded {env_name} from {secrets_path} (env was empty)")
                        return v
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"Could not read {secrets_path}: {e}")
    return ""


ELEVENLABS_LOG = os.path.expanduser("~/claude-voice-venv/elevenlabs-usage.log")
ELEVENLABS_QUOTA_CACHE = "/tmp/elevenlabs-quota-cache.json"
ELEVENLABS_QUOTA_TTL = 600  # 10 minutes


def _get_cached_quota(api_key: str) -> dict:
    """Get ElevenLabs quota, using a time-throttled cache to avoid per-call API hits.
    Only calls /v1/user/subscription if cache is stale (>10 min) or missing."""
    now = time.time()

    # Try reading cache
    try:
        with open(ELEVENLABS_QUOTA_CACHE) as f:
            cache = json.load(f)
        if now - cache.get("ts", 0) < ELEVENLABS_QUOTA_TTL:
            return cache
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

    # Cache stale or missing — fetch from API
    try:
        import requests
        resp = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                            headers={"xi-api-key": api_key}, timeout=5)
        if resp.status_code == 401:
            # API key lacks user_read permission — cache this so we don't retry constantly
            log("ElevenLabs quota check: 401 (key lacks user_read permission)")
            used, limit, pct, reset_date = "no_perm", "no_perm", "n/a", "n/a"
        else:
            data = resp.json()
            used = data.get("character_count", "?")
            limit = data.get("character_limit", "?")
            pct = f"{used/limit*100:.1f}%" if isinstance(used, int) and isinstance(limit, int) else "?"
            reset = data.get("next_character_count_reset_unix", 0)
            from datetime import datetime
            reset_date = datetime.fromtimestamp(reset).strftime("%Y-%m-%d") if reset else "?"
    except Exception:
        used, limit, pct, reset_date = "?", "?", "?", "?"

    result = {"ts": now, "used": used, "limit": limit, "pct": pct, "reset_date": reset_date}

    # Write cache (best-effort)
    try:
        with open(ELEVENLABS_QUOTA_CACHE, "w") as f:
            json.dump(result, f)
    except Exception:
        pass

    return result


def log_elevenlabs_usage(chars_this_call: int, api_key: str):
    """Log character usage with throttled quota check (every 10 min, not every call)."""
    quota = _get_cached_quota(api_key)
    used = quota.get("used", "?")
    limit = quota.get("limit", "?")
    pct = quota.get("pct", "?")
    reset_date = quota.get("reset_date", "?")

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | +{chars_this_call} chars | {used}/{limit} ({pct}) | resets {reset_date}\n"
    try:
        with open(ELEVENLABS_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    log(f"ElevenLabs: {used}/{limit} ({pct}), resets {reset_date}")


def speak(text: str, cfg: dict, lang_hint: str = None):
    """Route to the configured TTS engine with language detection.

    lang_hint: explicit language declared in the <voice lang="..."> tag.
    When present it overrides detect_language()'s heuristic.

    Audibility pre-check: if neither a healthy remote receiver NOR an audible
    local output exists, suppress the entire TTS call. This is the load-bearing
    cost-saver — it prevents ElevenLabs character spend on audio that wouldn't
    be heard anywhere (default output is BlackHole with no forwarding alive,
    all remote receivers down, etc.).
    """
    engine = cfg.get("tts_engine", "edge")
    lang = lang_hint if lang_hint in ('nl', 'en') else detect_language(text)
    speed = cfg.get("tts_speed", "+30%")

    remote_target, play_local = find_audible_path(cfg)
    if not remote_target and not play_local:
        log(f"No audible path — suppressing TTS for {len(text)} chars "
            f"(engine={engine}, lang={lang}) — saved API call")
        return

    if remote_target and play_local:
        log(f"Audible: remote={remote_target} + local")
    elif remote_target:
        log(f"Audible: remote={remote_target} (local muted)")
    else:
        log(f"Audible: local only (no remote receiver)")

    t0 = time.time()
    policy = cfg.get("dusk_presence_routing") if isinstance(
        cfg.get("dusk_presence_routing"), dict
    ) else {}
    off_lan_target = str(
        policy.get("off_lan_target") or policy.get("away_target") or ""
    ).strip()
    remote_requires_off_lan = bool(
        remote_target and off_lan_target and remote_target == off_lan_target
        and _source_machine(cfg) in {"day", "dawn"}
    )
    remote_fallback_target = (
        str(policy.get("origin_target") or "").strip() or None
        if remote_requires_off_lan else None
    )

    if engine == "pocket":
        # pocket-tts is English-only — Dutch routes to the next engine in line.
        if lang != "en":
            engine = cfg.get("engine_fallback", {}).get("pocket", "edge")
            log(f"pocket-tts has no '{lang}' support — falling back to {engine}")
        else:
            # Single-playback default (2026-07-06): remote receiver + local output
            # both audible at the desk = double voice. Remote (the desk receiver)
            # wins; set pocket_play_both=true to restore dual output.
            local_fallback = play_local
            pocket_play_local = play_local
            if remote_target and play_local and not cfg.get("pocket_play_both", False):
                pocket_play_local = False
            configured_voice = cfg.get("tts_voice_pocket_en", "eva")
            expected_voices = {"day": "jarvis", "dawn": "eva", "dusk": "eva-ru"}
            voice = os.environ.get("SHELBY_TTS_POCKET_VOICE") or expected_voices.get(
                _source_machine(cfg), configured_voice
            )
            if voice != configured_voice:
                log(f"Corrected Pocket persona {configured_voice!r} -> {voice!r} for source machine")
            base_url = cfg.get("pocket_tts_url", "http://127.0.0.1:8933")
            if speak_pocket(text, voice, remote_target=remote_target,
                            play_local=pocket_play_local, base_url=base_url,
                            fallback_local=local_fallback,
                            tail_ms=int(cfg.get("pocket_tail_ms", 1000)),
                            remote_requires_off_lan=remote_requires_off_lan,
                            remote_fallback_target=remote_fallback_target):
                mode = ("remote+local" if remote_target and pocket_play_local
                        else ("remote" if remote_target else "local"))
                log(f"TTS (pocket/{lang}/{mode}): {time.time()-t0:.2f}s, {len(text)} chars, $0")
                return
            engine = cfg.get("engine_fallback", {}).get("pocket", "edge")
            log(f"pocket-tts failed — falling back to {engine}")

    if engine == "elevenlabs":
        api_key = _resolve_elevenlabs_key(cfg)
        if not api_key:
            log("No ElevenLabs API key, falling back to Edge")
            engine = "edge"
        else:
            # Quota gate (S749): the May-2026 cap-out showed calls still hitting
            # ElevenLabs at 200% of quota — there was no pre-call check, forcing a
            # manual config flip to edge. Degrade automatically at >=95% instead;
            # the 10-min quota cache keeps this nearly free. Recovers by itself
            # after the monthly reset.
            quota = _get_cached_quota(api_key)
            _used, _limit = quota.get("used"), quota.get("limit")
            if isinstance(_used, int) and isinstance(_limit, int) and _used >= 0.95 * _limit:
                log(f"ElevenLabs quota {_used}/{_limit} ({_used/_limit*100:.0f}%) — degrading to edge until reset")
                engine = "edge"
        if engine == "elevenlabs" and api_key:
            source = _source_machine(cfg)
            voice_key = f"tts_voice_elevenlabs_{source}_{lang}"
            voice_id = os.environ.get("SHELBY_TTS_ELEVENLABS_VOICE_ID") or cfg.get(voice_key)
            if not voice_id:
                log(f"No source-specific ElevenLabs persona for {source}/{lang} — falling back to Edge")
                engine = "edge"
        if engine == "elevenlabs" and api_key:
            model = cfg.get("elevenlabs_model", "eleven_turbo_v2_5")
            el_speed = cfg.get("elevenlabs_speed", 1.0)
            lead_ms = int(cfg.get("tts_pad_lead_ms", PAD_LEAD_MS))
            tail_ms = int(cfg.get("tts_pad_tail_ms", PAD_TAIL_MS))
            primer_amp = int(cfg.get("tts_lead_primer_amp", 0))
            try:
                speak_elevenlabs_streaming(text, voice_id, model, api_key, speed=el_speed,
                                           remote_target=remote_target, play_local=play_local,
                                           lead_ms=lead_ms, tail_ms=tail_ms, primer_amp=primer_amp,
                                           remote_requires_off_lan=remote_requires_off_lan,
                                           remote_fallback_target=remote_fallback_target)
            except Exception as e:
                # Codex review 2026-07-06: an unhandled raise here used to kill the
                # hook with no audio at all — degrade to edge instead.
                log(f"ElevenLabs failed ({e}) — falling back to edge")
                engine = "edge"
            else:
                chars_used = len(text)
                log_elevenlabs_usage(chars_used, api_key)
                mode = ("remote+local" if remote_target and play_local
                        else ("remote" if remote_target else "local"))
                log(f"TTS (elevenlabs/{lang}/{mode}): {time.time()-t0:.2f}s, {chars_used} chars")
                return

    if engine == "kokoro":
        source = _source_machine(cfg)
        voice_key = f"tts_voice_kokoro_{source}_{lang}"
        voice = cfg.get(
            voice_key,
            KOKORO_SOURCE_PERSONAS.get(source, cfg.get("tts_voice_kokoro_en", "af_heart")),
        )
        # Kokoro is local-only; if there's no audible local path but remote exists,
        # we still need an engine that can send to remote — fall through to edge.
        if play_local and speak_kokoro(text, voice):
            log(f"TTS (kokoro/{lang}): {time.time()-t0:.2f}s")
            return
        log("Kokoro failed or not local-audible, falling back to Edge")
        engine = "edge"

    if engine == "edge":
        source = _source_machine(cfg)
        voice_key = f"tts_voice_edge_{source}_{lang}"
        voice = cfg.get(
            voice_key,
            EDGE_SOURCE_PERSONAS.get(source, {}).get(
                lang, cfg.get(f"tts_voice_edge_{lang}", "en-GB-SoniaNeural")
            ),
        )
        asyncio.run(speak_edge(
            text, voice, speed, remote_target=remote_target, play_local=play_local,
            remote_requires_off_lan=remote_requires_off_lan,
            remote_fallback_target=remote_fallback_target,
        ))
        mode = ("remote+local" if remote_target and play_local
                else ("remote" if remote_target else "local"))
        log(f"TTS (edge/{lang}/{mode}): {time.time()-t0:.2f}s")


def main():
    # Install SIGTERM handler — exit silently when killed by timeout or voice-shutup
    signal.signal(signal.SIGTERM, handle_sigterm)

    # Read JSON input from stdin
    try:
        raw = sys.stdin.read()
    except Exception:
        return

    if not raw:
        return

    # Parse JSON
    response = ""
    try:
        data = json.loads(raw)
        response = data.get("last_assistant_message", "")
        # Capture session_id so voice-shutup can scope kills to the same session.
        global _SESSION_ID
        _SESSION_ID = str(data.get("session_id", "") or "")
    except (json.JSONDecodeError, TypeError):
        response = raw

    if not response:
        return

    # Skip TTS for automated sessions (defense-in-depth — shell wrapper also guards)
    session_type = os.environ.get("CLAUDE_SESSION_TYPE", "main")
    if session_type in ("cron", "spinoff", "headless"):
        log(f"Skipped TTS: session_type={session_type}")
        return

    # Extract voice block
    voice_text = extract_voice_block(response)
    if not voice_text:
        return

    # Sanitize
    clean = sanitize_for_speech(voice_text)
    if not clean:
        return

    # Load config
    cfg = load_config()

    # Honor tts_enabled config flag
    if not cfg.get("tts_enabled", True):
        log("TTS disabled via config")
        return

    # Acquire lock (prevents dual-session double-playback)
    lock = acquire_lock()
    if lock is None:
        log("Could not acquire lock, skipping")
        return

    try:
        speak(clean, cfg, lang_hint=extract_voice_lang(response))
    except Exception as e:
        log(f"TTS error: {e}")
    finally:
        release_lock(lock)


if __name__ == "__main__":
    main()
