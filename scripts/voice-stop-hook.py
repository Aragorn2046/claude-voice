#!/usr/bin/env python3
"""Claude Code Stop hook — multi-engine TTS with language detection.

Engines: edge (default/free), elevenlabs (premium/streaming), kokoro (local/free)
Features:
  - Lockfile prevents dual-session double-playback
  - Auto-detects Dutch content → switches to Dutch voice
  - Engine switchable via config or /tts command

Receives JSON on stdin with last_assistant_message. Extracts the <voice>...</voice>
block, sanitizes it for speech, and plays it.
"""

import asyncio
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time

# Ensure WSLg PulseAudio is available
if not os.environ.get("PULSE_SERVER") and os.path.exists("/mnt/wslg/PulseServer"):
    os.environ["PULSE_SERVER"] = "unix:/mnt/wslg/PulseServer"

# Paths
CONFIG_PATH = os.path.expanduser("~/projects/claude-voice/scripts/config.json")
LOCKFILE_PATH = "/tmp/sonia-tts.lock"

# Defaults (overridden by config.json)
DEFAULTS = {
    "tts_engine": "edge",
    "tts_speed": "+30%",
    "tts_voice_edge_en": "en-GB-SoniaNeural",
    "tts_voice_edge_nl": "nl-NL-FennaNeural",
    "tts_voice_elevenlabs_en": "pNInz6obpgDQGcFmaJgB",  # Adam
    "tts_voice_elevenlabs_nl": "pNInz6obpgDQGcFmaJgB",  # Adam (multilingual)
    "elevenlabs_model": "eleven_turbo_v2_5",
    "elevenlabs_api_key_env": "ELEVENLABS_API_KEY",
    "elevenlabs_api_key": "",
    "tts_voice_kokoro_en": "af_heart",
    "tts_voice_kokoro_nl": "af_heart",
}


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
    try:
        lock_fd = open(LOCKFILE_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
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
            return lock_fd
        except (IOError, OSError):
            return None


def release_lock(lock_fd):
    """Release lockfile."""
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.unlink(LOCKFILE_PATH)
        except (IOError, OSError):
            pass


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


def play_raw_pcm(pcm_data: bytes, srate: int, channels: int):
    """Play raw PCM data via paplay."""
    subprocess.run(
        ["paplay", "--raw", f"--rate={srate}", f"--channels={channels}", "--format=s16le"],
        input=pcm_data, capture_output=True, timeout=30
    )


async def speak_edge(text: str, voice: str, speed: str):
    """Edge TTS — free, cloud-based."""
    import edge_tts
    import soundfile as sf
    import numpy as np

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=speed)
        await communicate.save(tmp_path)

        data, srate = sf.read(tmp_path)
        pcm = (data * 32767).astype(np.int16).tobytes()
        channels = 1 if data.ndim == 1 else data.shape[1]
        play_raw_pcm(pcm, srate, channels)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str, speed: float = 1.0):
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

    # Stream directly to paplay
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
        sys.stderr.write("Kokoro not installed, falling back to Edge TTS\n")
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
        sys.stderr.write(f"Kokoro error: {e}\n")
        return False


ELEVENLABS_LOG = os.path.expanduser("~/claude-voice-venv/elevenlabs-usage.log")


def log_elevenlabs_usage(chars_this_call: int, api_key: str):
    """Log character usage and fetch remaining quota from API."""
    try:
        import requests
        resp = requests.get("https://api.elevenlabs.io/v1/user/subscription",
                            headers={"xi-api-key": api_key}, timeout=5)
        data = resp.json()
        used = data.get("character_count", "?")
        limit = data.get("character_limit", "?")
        pct = f"{used/limit*100:.1f}%" if isinstance(used, int) and isinstance(limit, int) else "?"
        reset = data.get("next_character_count_reset_unix", 0)
        from datetime import datetime
        reset_date = datetime.fromtimestamp(reset).strftime("%Y-%m-%d") if reset else "?"
    except Exception:
        used, limit, pct, reset_date = "?", "?", "?", "?"

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} | +{chars_this_call} chars | {used}/{limit} ({pct}) | resets {reset_date}\n"
    try:
        with open(ELEVENLABS_LOG, "a") as f:
            f.write(line)
    except Exception:
        pass
    sys.stderr.write(f"ElevenLabs: {used}/{limit} ({pct}), resets {reset_date}\n")


def speak(text: str, cfg: dict):
    """Route to the configured TTS engine with language detection."""
    engine = cfg.get("tts_engine", "edge")
    lang = detect_language(text)
    speed = cfg.get("tts_speed", "+30%")

    t0 = time.time()

    if engine == "elevenlabs":
        api_key = cfg.get("elevenlabs_api_key") or os.environ.get(
            cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY"), ""
        )
        if not api_key:
            sys.stderr.write("No ElevenLabs API key, falling back to Edge\n")
            engine = "edge"
        else:
            voice_key = f"tts_voice_elevenlabs_{lang}"
            voice_id = cfg.get(voice_key, cfg.get("tts_voice_elevenlabs_en"))
            model = cfg.get("elevenlabs_model", "eleven_turbo_v2_5")
            el_speed = cfg.get("elevenlabs_speed", 1.0)
            speak_elevenlabs_streaming(text, voice_id, model, api_key, speed=el_speed)
            chars_used = len(text)
            log_elevenlabs_usage(chars_used, api_key)
            sys.stderr.write(f"TTS (elevenlabs/{lang}): {time.time()-t0:.2f}s, {chars_used} chars\n")
            return

    if engine == "kokoro":
        voice_key = f"tts_voice_kokoro_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_kokoro_en", "af_heart"))
        if speak_kokoro(text, voice):
            sys.stderr.write(f"TTS (kokoro/{lang}): {time.time()-t0:.2f}s\n")
            return
        # Fallback to Edge
        sys.stderr.write("Kokoro failed, falling back to Edge\n")
        engine = "edge"

    if engine == "edge":
        voice_key = f"tts_voice_edge_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_edge_en", "en-GB-SoniaNeural"))
        asyncio.run(speak_edge(text, voice, speed))
        sys.stderr.write(f"TTS (edge/{lang}): {time.time()-t0:.2f}s\n")


def main():
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

    # Extract voice block
    voice_text = extract_voice_block(response)
    if not voice_text:
        return

    # Sanitize
    clean = sanitize_for_speech(voice_text)
    if not clean:
        return

    # Acquire lock (prevents dual-session double-playback)
    lock = acquire_lock()
    if lock is None:
        sys.stderr.write("TTS: could not acquire lock, skipping\n")
        return

    try:
        cfg = load_config()
        speak(clean, cfg)
    finally:
        release_lock(lock)


if __name__ == "__main__":
    main()
