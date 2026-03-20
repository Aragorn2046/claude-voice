#!/usr/bin/env python3
"""Claude Code Stop hook — multi-engine TTS for macOS.

Engines: edge (default/free), elevenlabs (premium/streaming), say (macOS built-in)
Features:
  - Lockfile prevents dual-session double-playback
  - Auto-detects Dutch content → switches to Dutch voice
  - Engine switchable via config or /tts command
  - Remote audio piping: auto-detects SSH sessions and sends audio
    directly to the SSH client's IP over Tailscale (no SSH tunnel needed)

Receives JSON on stdin with last_assistant_message. Extracts the <voice>...</voice>
block, sanitizes it for speech, and plays it.

Audio playback: afplay (macOS built-in) for local, TCP socket for remote
"""

import asyncio
import fcntl
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time

# Paths
CONFIG_PATH = os.path.expanduser("~/projects/claude-voice/scripts/config.json")
LOCKFILE_PATH = "/tmp/claude-tts.lock"
REMOTE_AUDIO_PORT = 12345  # Must match audio-listener.py on the remote machine

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
    "tts_voice_say_en": "Samantha",
    "tts_voice_say_nl": "Xander",
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


def write_wav(path: str, pcm_data: bytes, sample_rate: int, channels: int):
    """Write raw PCM data to a WAV file for afplay."""
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm_data)
    with open(path, 'wb') as f:
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))
        f.write(struct.pack('<H', 1))   # PCM format
        f.write(struct.pack('<H', channels))
        f.write(struct.pack('<I', sample_rate))
        f.write(struct.pack('<I', byte_rate))
        f.write(struct.pack('<H', block_align))
        f.write(struct.pack('<H', bits_per_sample))
        f.write(b'data')
        f.write(struct.pack('<I', data_size))
        f.write(pcm_data)


# --- Remote audio piping ---

def get_remote_ip() -> str | None:
    """Extract the SSH client's IP from SSH_CONNECTION.
    Returns the IP of the machine that SSHed in (e.g. Tailscale IP), or None if local."""
    ssh_conn = os.environ.get("SSH_CONNECTION", "")
    if ssh_conn:
        parts = ssh_conn.split()
        if parts:
            return parts[0]
    return None


def convert_to_wav(path: str) -> str:
    """Convert any audio file to WAV using ffmpeg. Returns WAV path."""
    wav_path = path.rsplit(".", 1)[0] + ".wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", path, "-ar", "44100", "-ac", "1", "-f", "wav", wav_path],
            capture_output=True, timeout=15,
        )
        if os.path.exists(wav_path) and os.path.getsize(wav_path) > 0:
            return wav_path
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return path


def send_audio_remote(path: str, host: str = "localhost") -> bool:
    """Send audio file to remote machine over Tailscale (or SSH tunnel fallback).
    Converts to WAV first for reliable playback on the remote end.
    Returns True if sent successfully."""
    wav_path = convert_to_wav(path) if not path.endswith(".wav") else path
    try:
        with open(wav_path, "rb") as f:
            audio_data = f.read()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, REMOTE_AUDIO_PORT))
        sock.sendall(audio_data)
        sock.close()
        sys.stderr.write(f"TTS: sent {len(audio_data)} bytes WAV to {host}:{REMOTE_AUDIO_PORT}\n")
        return True
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        sys.stderr.write(f"TTS: remote send to {host} failed ({e}), falling back to local\n")
        return False
    finally:
        if wav_path != path and os.path.exists(wav_path):
            os.unlink(wav_path)


def play_audio_file(path: str):
    """Play an audio file — remote via Tailscale if SSH, local via afplay otherwise."""
    remote_ip = get_remote_ip()
    if remote_ip:
        if send_audio_remote(path, host=remote_ip):
            return
        # Fallback: try localhost (SSH reverse tunnel if configured)
        if send_audio_remote(path, host="localhost"):
            return
    # Local playback
    subprocess.run(["afplay", path], capture_output=True, timeout=60)


def play_raw_pcm(pcm_data: bytes, srate: int, channels: int):
    """Play raw PCM data by writing a temp WAV and using afplay/remote."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = tmp.name
    tmp.close()
    try:
        write_wav(tmp_path, pcm_data, srate, channels)
        play_audio_file(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# --- TTS engines ---

async def speak_edge(text: str, voice: str, speed: str):
    """Edge TTS — free, cloud-based."""
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, voice, rate=speed)
        await communicate.save(tmp_path)
        play_audio_file(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def speak_elevenlabs_streaming(text: str, voice_id: str, model: str, api_key: str, speed: float = 1.0):
    """ElevenLabs with pre-buffered streaming."""
    import requests

    CHUNK_SIZE = 8192

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

    resp = requests.post(url, json=body, headers=headers, stream=True, timeout=(10, 60))
    resp.raise_for_status()

    pcm_data = bytearray()
    for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
        if chunk:
            pcm_data.extend(chunk)

    if not pcm_data:
        return

    play_raw_pcm(bytes(pcm_data), 24000, 1)


def speak_say(text: str, voice: str, rate: int = 220):
    """macOS built-in 'say' command — free, no network, instant."""
    subprocess.run(["say", "-v", voice, "-r", str(rate), text],
                   capture_output=True, timeout=30)


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
            sys.stderr.write(f"TTS (elevenlabs/{lang}): {time.time()-t0:.2f}s, {len(text)} chars\n")
            return

    if engine == "say":
        voice_key = f"tts_voice_say_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_say_en", "Samantha"))
        rate = cfg.get("say_rate", 220)
        speak_say(text, voice, rate)
        sys.stderr.write(f"TTS (say/{lang}): {time.time()-t0:.2f}s\n")
        return

    if engine == "edge":
        voice_key = f"tts_voice_edge_{lang}"
        voice = cfg.get(voice_key, cfg.get("tts_voice_edge_en", "en-GB-SoniaNeural"))
        asyncio.run(speak_edge(text, voice, speed))
        sys.stderr.write(f"TTS (edge/{lang}): {time.time()-t0:.2f}s\n")


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return

    if not raw:
        return

    response = ""
    try:
        data = json.loads(raw)
        response = data.get("last_assistant_message", "")
    except (json.JSONDecodeError, TypeError):
        response = raw

    if not response:
        return

    voice_text = extract_voice_block(response)
    if not voice_text:
        return

    clean = sanitize_for_speech(voice_text)
    if not clean:
        return

    # Check mute state
    if os.path.exists("/tmp/claude-tts-muted"):
        sys.stderr.write("TTS: muted, skipping\n")
        return

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
