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


def acquire_lock():
    """Acquire lockfile to prevent dual-session double-playback.
    Returns lock file handle or None if another instance is speaking."""
    global _lock_fd
    try:
        lock_fd = open(LOCKFILE_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(f"{os.getpid()}\n{_SESSION_ID}\n")
        lock_fd.flush()
        _lock_fd = lock_fd
        return lock_fd
    except (IOError, OSError):
        # Another instance holds the lock — kill it and take over
        try:
            with open(LOCKFILE_PATH) as f:
                old_pid = int(f.readline().strip())
            os.kill(old_pid, 9)
        except (ValueError, OSError, FileNotFoundError):
            pass
        # Try again
        try:
            lock_fd = open(LOCKFILE_PATH, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.write(f"{os.getpid()}\n{_SESSION_ID}\n")
            lock_fd.flush()
            _lock_fd = lock_fd
            return lock_fd
        except (IOError, OSError):
            return None


def release_lock(lock_fd):
    """Release lockfile."""
    global _lock_fd
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.unlink(LOCKFILE_PATH)
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
SUMMARY_TARGET_CHARS = 280
SUMMARY_MAX_SENTENCES = 4
SUMMARY_SOFT_LONG_SENTENCE = 220  # a single sentence longer than this isn't headsup-shaped

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
    # Treat it as too-long if it exceeds soft-long, fall back rather than cut.
    if len(sentences) == 1 and not sentences[0].endswith(('.', '!', '?')):
        if len(sentences[0]) > SUMMARY_SOFT_LONG_SENTENCE:
            return "Update ready."
        return sentences[0] + "."

    # Build from the END backward (conclusion-first), stopping when adding the
    # next earlier sentence would exceed targets.
    picked = []
    char_count = 0
    for s in reversed(sentences):
        if len(picked) >= SUMMARY_MAX_SENTENCES:
            break
        # Skip individual sentences that are themselves too long to be headsup.
        if len(s) > SUMMARY_SOFT_LONG_SENTENCE:
            if picked:
                break
            continue
        prospective = char_count + len(s) + (1 if picked else 0)
        if picked and prospective > SUMMARY_TARGET_CHARS:
            break
        picked.insert(0, s)
        char_count = prospective
        # Prefer terse: stop after 1 sentence unless it's very short.
        if len(picked) == 1 and len(s) >= 50:
            break
        if len(picked) == 2 and char_count >= 100:
            break

    if not picked:
        # All sentences were over the soft-long threshold — no clean headsup
        # exists. Don't truncate mid-sentence; emit a generic neutral message.
        return "Update ready."

    return " ".join(picked)


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


async def speak_edge(text: str, voice: str, speed: str, remote_target: str = None, play_local: bool = True):
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
            if send_audio_remote(wav_data, remote_target):
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


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str, speed: float = 1.0, remote_target: str = None, play_local: bool = True):
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

    resp = requests.post(url, json=body, headers=headers, stream=True, timeout=30)
    resp.raise_for_status()

    if remote_target:
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        wav_data = make_wav(pcm_data, 24000, 1)
        if send_audio_remote(wav_data, remote_target):
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
        cfg_target = (cfg.get("remote_audio_target") or "").strip()
        if cfg_target:
            health_url = cfg_target.replace("/tts", "/health")
            if _probe_url(health_url, timeout=0.5):
                remote_url = cfg_target
                log(f"Explicit remote_audio_target {cfg_target} healthy")
            else:
                log(f"Explicit remote_audio_target {cfg_target} DOWN — falling through to receivers list")

        if not remote_url:
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


def send_audio_remote(wav_data: bytes, target_url: str) -> bool:
    """POST WAV audio data to a remote audio receiver. Returns True on success."""
    try:
        import requests
        resp = requests.post(target_url, data=wav_data,
                             headers={"Content-Type": "audio/wav"}, timeout=10)
        resp.raise_for_status()
        log(f"Sent {len(wav_data)} bytes to {target_url}")
        return True
    except Exception as e:
        log(f"Remote send failed ({target_url}): {e} — falling back to local")
        return False


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


def speak(text: str, cfg: dict):
    """Route to the configured TTS engine with language detection.

    Audibility pre-check: if neither a healthy remote receiver NOR an audible
    local output exists, suppress the entire TTS call. This is the load-bearing
    cost-saver — it prevents ElevenLabs character spend on audio that wouldn't
    be heard anywhere (default output is BlackHole with no forwarding alive,
    all remote receivers down, etc.).
    """
    engine = cfg.get("tts_engine", "edge")
    lang = detect_language(text)
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

    if engine == "elevenlabs":
        api_key = cfg.get("elevenlabs_api_key") or os.environ.get(
            cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY"), ""
        )
        if not api_key:
            log("No ElevenLabs API key, falling back to Edge")
            engine = "edge"
        else:
            voice_key = f"tts_voice_elevenlabs_{lang}"
            voice_id = cfg.get(voice_key, cfg.get("tts_voice_elevenlabs_en"))
            model = cfg.get("elevenlabs_model", "eleven_turbo_v2_5")
            el_speed = cfg.get("elevenlabs_speed", 1.0)
            speak_elevenlabs_streaming(text, voice_id, model, api_key, speed=el_speed,
                                       remote_target=remote_target, play_local=play_local)
            chars_used = len(text)
            log_elevenlabs_usage(chars_used, api_key)
            mode = ("remote+local" if remote_target and play_local
                    else ("remote" if remote_target else "local"))
            log(f"TTS (elevenlabs/{lang}/{mode}): {time.time()-t0:.2f}s, {chars_used} chars")
            return

    if engine == "kokoro":
        voice_key = f"tts_voice_kokoro_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_kokoro_en", "af_heart"))
        # Kokoro is local-only; if there's no audible local path but remote exists,
        # we still need an engine that can send to remote — fall through to edge.
        if play_local and speak_kokoro(text, voice):
            log(f"TTS (kokoro/{lang}): {time.time()-t0:.2f}s")
            return
        log("Kokoro failed or not local-audible, falling back to Edge")
        engine = "edge"

    if engine == "edge":
        voice_key = f"tts_voice_edge_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_edge_en", "en-GB-SoniaNeural"))
        asyncio.run(speak_edge(text, voice, speed, remote_target=remote_target, play_local=play_local))
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
        speak(clean, cfg)
    except Exception as e:
        log(f"TTS error: {e}")
    finally:
        release_lock(lock)


if __name__ == "__main__":
    main()
