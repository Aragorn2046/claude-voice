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
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        _lock_fd = lock_fd
        return lock_fd
    except (IOError, OSError):
        # Another instance holds the lock — kill it and take over
        try:
            with open(LOCKFILE_PATH) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 9)
        except (ValueError, OSError, FileNotFoundError):
            pass
        # Try again
        try:
            lock_fd = open(LOCKFILE_PATH, "w")
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_fd.write(str(os.getpid()))
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


def extract_voice_block(text: str) -> str:
    """Extract content from <voice>...</voice> tags."""
    match = re.search(r'<voice>(.*?)</voice>', text, re.DOTALL)
    return match.group(1).strip() if match else ""


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


async def speak_edge(text: str, voice: str, speed: str, remote_target: str = None):
    """Edge TTS — free, cloud-based."""
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=speed)
        await communicate.save(tmp_path)

        if remote_target:
            # Convert to WAV for remote receiver
            import soundfile as sf
            import numpy as np
            data, srate = sf.read(tmp_path)
            pcm = (data * 32767).astype(np.int16).tobytes()
            channels = 1 if data.ndim == 1 else data.shape[1]
            wav_data = make_wav(pcm, srate, channels)
            if send_audio_remote(wav_data, remote_target):
                return
            # Fallback to local on failure

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


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str, speed: float = 1.0, remote_target: str = None):
    """ElevenLabs with streaming via raw HTTP — supports speed parameter."""
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
        # Collect all PCM, convert to WAV, send to remote receiver
        pcm_data = b""
        for chunk in resp.iter_content(chunk_size=4096):
            if chunk:
                pcm_data += chunk
        wav_data = make_wav(pcm_data, 24000, 1)
        if send_audio_remote(wav_data, remote_target):
            return
        # Fallback to local on failure

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
            proc.wait(timeout=30)
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
    If remote_audio is enabled, sends audio to the discovered receiver."""
    engine = cfg.get("tts_engine", "edge")
    lang = detect_language(text)
    speed = cfg.get("tts_speed", "+30%")
    remote_target = get_remote_audio_target(cfg)

    if remote_target:
        log(f"Remote mode: sending to {remote_target}")

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
            speak_elevenlabs_streaming(text, voice_id, model, api_key, speed=el_speed, remote_target=remote_target)
            chars_used = len(text)
            log_elevenlabs_usage(chars_used, api_key)
            mode = "remote" if remote_target else "local"
            log(f"TTS (elevenlabs/{lang}/{mode}): {time.time()-t0:.2f}s, {chars_used} chars")
            return

    if engine == "kokoro":
        voice_key = f"tts_voice_kokoro_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_kokoro_en", "af_heart"))
        if speak_kokoro(text, voice):
            log(f"TTS (kokoro/{lang}): {time.time()-t0:.2f}s")
            return
        log("Kokoro failed, falling back to Edge")
        engine = "edge"

    if engine == "edge":
        voice_key = f"tts_voice_edge_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_edge_en", "en-GB-SoniaNeural"))
        asyncio.run(speak_edge(text, voice, speed, remote_target=remote_target))
        mode = "remote" if remote_target else "local"
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
