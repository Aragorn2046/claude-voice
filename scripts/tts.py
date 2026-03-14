#!/usr/bin/env python3
"""Text-to-speech engine abstraction: Edge TTS, ElevenLabs (REST streaming), Kokoro (local GPU)."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

# Dutch detection word list (common Dutch words unlikely in English)
DUTCH_MARKERS = {
    "de", "het", "een", "is", "van", "en", "in", "dat", "die", "niet",
    "op", "te", "zijn", "voor", "met", "ook", "maar", "bij", "nog",
    "wel", "dit", "wat", "naar", "kan", "als", "uit", "dan", "er",
    "zo", "heb", "om", "aan", "geen", "meer", "moet", "mijn", "al",
    "wordt", "heeft", "over", "hun", "door", "werd", "zou", "veel",
    "gaan", "haar", "deze", "wie", "tot", "ons", "waar", "heel",
    "dus", "wij", "zij", "hier", "omdat", "alleen", "toen", "alle",
    "jaar", "goed", "zeer", "hebben", "mensen", "nieuwe", "twee",
    "onder", "eerste", "andere", "groot", "tussen", "eigen", "zonder",
    "je", "jij", "jouw", "ik", "wij", "uw", "jullie", "we",
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def detect_dutch(text: str, threshold: float = 0.15) -> bool:
    """Detect if text is Dutch based on word frequency heuristic."""
    words = text.lower().split()
    if len(words) < 3:
        return False
    dutch_count = sum(1 for w in words if w.strip(".,!?;:'\"()") in DUTCH_MARKERS)
    return (dutch_count / len(words)) > threshold


def play_pcm(data: bytes, rate: int = 24000, channels: int = 1, fmt: str = "s16le"):
    """Play raw PCM data through paplay."""
    proc = subprocess.Popen(
        ["paplay", "--raw", f"--rate={rate}", f"--channels={channels}", f"--format={fmt}"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.stdin.write(data)
    proc.stdin.close()
    proc.wait()


def play_audio(path: str):
    """Play audio file through speakers."""
    # Try external players first (better latency), then Python fallback
    for player in ["mpv", "ffplay"]:
        try:
            if player == "mpv":
                subprocess.run(
                    ["mpv", "--no-video", "--really-quiet", path],
                    check=True, capture_output=True,
                )
            elif player == "ffplay":
                subprocess.run(
                    ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                    check=True, capture_output=True,
                )
            return
        except (FileNotFoundError, subprocess.CalledProcessError, PermissionError):
            continue

    # Optimized: decode with soundfile, pipe raw PCM to paplay (skip WAV disk write)
    try:
        import numpy as np
        import soundfile as sf
        data, srate = sf.read(path, dtype='int16')
        if len(data.shape) > 1:
            data = data[:, 0]  # mono
        play_pcm(data.tobytes(), rate=srate)
        return
    except (ImportError, FileNotFoundError, subprocess.CalledProcessError):
        pass

    # Last resort: aplay (WAV only)
    try:
        subprocess.run(["aplay", "-q", path], check=True, capture_output=True)
        return
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    print("Error: No audio player found (install mpv or ffmpeg)", file=sys.stderr)


async def tts_edge(text: str, voice: str = None, output_path: str = None) -> str:
    """Generate speech using Edge TTS (free, cloud-based)."""
    import edge_tts

    cfg = load_config()

    # Auto-detect Dutch and switch voice
    if voice is None:
        threshold = cfg.get("dutch_word_threshold", 0.15)
        if detect_dutch(text, threshold):
            voice = cfg.get("tts_voice_edge_nl", "nl-NL-FennaNeural")
        else:
            voice = cfg.get("tts_voice_edge", "en-GB-SoniaNeural")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = tmp.name
        tmp.close()

    speed = cfg.get("tts_speed", "+0%")
    communicate = edge_tts.Communicate(text, voice, rate=speed)
    await communicate.save(output_path)
    return output_path


def tts_elevenlabs(text: str, voice: str = None, output_path: str = None) -> str:
    """Generate speech using ElevenLabs REST API with streaming (supports speed param).

    Uses direct REST API instead of Python SDK because the SDK doesn't support
    the speed parameter for text-to-speech.
    """
    import urllib.request
    import urllib.error

    cfg = load_config()

    # Get API key from env var or config
    api_key = os.environ.get(cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY"))
    if not api_key:
        api_key = cfg.get("elevenlabs_api_key", "")
    if not api_key:
        print("Error: ELEVENLABS_API_KEY not set (env var or config)", file=sys.stderr)
        return None

    voice_id = voice or cfg.get("tts_voice_elevenlabs", "fATgBRI8wg5KkDFg8vBd")
    model_id = cfg.get("elevenlabs_model", "eleven_turbo_v2_5")
    speed = cfg.get("elevenlabs_speed", 1.4)

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"

    payload = json.dumps({
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True
        },
        "speed": speed,
    }).encode()

    headers = {
        "Content-Type": "application/json",
        "xi-api-key": api_key,
        "Accept": "audio/mpeg",
    }

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = tmp.name
        tmp.close()

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as resp:
            with open(output_path, "wb") as f:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"ElevenLabs API error {e.code}: {body}", file=sys.stderr)
        return None
    except urllib.error.URLError as e:
        print(f"ElevenLabs connection error: {e.reason}", file=sys.stderr)
        return None

    return output_path


def tts_kokoro(text: str, voice: str = None, output_path: str = None) -> str:
    """Generate speech using Kokoro local TTS (GPU-accelerated)."""
    import warnings
    warnings.filterwarnings('ignore')

    cfg = load_config()
    voice = voice or cfg.get("tts_voice_kokoro", "af_heart")
    speed = cfg.get("kokoro_speed", 1.0)
    lang_code = cfg.get("kokoro_lang_code", "a")
    repo_id = cfg.get("kokoro_repo_id", "hexgrad/Kokoro-82M")

    try:
        from kokoro import KPipeline
        import soundfile as sf
        import numpy as np
    except ImportError as e:
        print(f"Kokoro not installed: {e}", file=sys.stderr)
        return None

    # Cache pipeline in module-level variable for reuse
    global _kokoro_pipeline
    if '_kokoro_pipeline' not in globals() or _kokoro_pipeline is None:
        try:
            _kokoro_pipeline = KPipeline(lang_code=lang_code, repo_id=repo_id)
        except Exception as e:
            print(f"Kokoro init failed: {e}", file=sys.stderr)
            return None

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        output_path = tmp.name
        tmp.close()

    try:
        gen = _kokoro_pipeline(text, voice=voice, speed=speed)
        # Concatenate all segments
        all_audio = []
        for gs, ps, audio in gen:
            all_audio.append(audio)

        if not all_audio:
            print("Kokoro generated no audio", file=sys.stderr)
            return None

        combined = np.concatenate(all_audio)
        sf.write(output_path, combined, 24000)
    except Exception as e:
        print(f"Kokoro generation failed: {e}", file=sys.stderr)
        return None

    return output_path


_kokoro_pipeline = None


def speak(text: str, engine: str = None, voice: str = None, play: bool = True,
          output_path: str = None) -> str:
    """Speak text using the configured TTS engine.

    Returns path to generated audio file.
    Falls back to configured fallback engine on failure.
    """
    cfg = load_config()
    engine = engine or cfg.get("tts_engine", "edge")
    fallbacks = cfg.get("engine_fallback", {})

    t0 = time.time()
    path = None

    if engine == "edge":
        path = asyncio.run(tts_edge(text, voice, output_path))
    elif engine == "elevenlabs":
        path = tts_elevenlabs(text, voice, output_path)
    elif engine == "kokoro":
        path = tts_kokoro(text, voice, output_path)
    else:
        print(f"Unknown TTS engine: {engine}", file=sys.stderr)

    # Fallback on failure
    if path is None and engine in fallbacks:
        fb = fallbacks[engine]
        print(f"TTS ({engine}) failed, falling back to {fb}", file=sys.stderr)
        if fb == "edge":
            path = asyncio.run(tts_edge(text, voice=None, output_path=output_path))
        elif fb == "elevenlabs":
            path = tts_elevenlabs(text, voice=None, output_path=output_path)
        elif fb == "kokoro":
            path = tts_kokoro(text, voice=None, output_path=output_path)
        engine = f"{engine}->{fb}"

    elapsed = time.time() - t0
    if path:
        print(f"TTS ({engine}): generated in {elapsed:.2f}s", file=sys.stderr)

    if play and path:
        play_audio(path)

    return path


async def list_edge_voices(language: str = None):
    """List available Edge TTS voices."""
    import edge_tts
    voices = await edge_tts.list_voices()
    for v in voices:
        if language and not v["Locale"].startswith(language):
            continue
        print(f"{v['ShortName']:40s} {v['Locale']:10s} {v['Gender']}")


def main():
    parser = argparse.ArgumentParser(description="Text-to-speech")
    parser.add_argument("text", nargs="?", help="Text to speak")
    parser.add_argument("-e", "--engine", choices=["edge", "elevenlabs", "kokoro"],
                        help="TTS engine")
    parser.add_argument("-v", "--voice", help="Voice name/ID")
    parser.add_argument("-o", "--output", help="Save audio to file instead of playing")
    parser.add_argument("--no-play", action="store_true", help="Don't play audio")
    parser.add_argument("--list-voices", action="store_true",
                        help="List available Edge TTS voices")
    parser.add_argument("--language", help="Filter voices by language (e.g., en, nl)")
    parser.add_argument("--stdin", action="store_true", help="Read text from stdin")
    args = parser.parse_args()

    if args.list_voices:
        asyncio.run(list_edge_voices(args.language))
        return

    text = args.text
    if args.stdin or (not text and not sys.stdin.isatty()):
        text = sys.stdin.read().strip()

    if not text:
        parser.print_help()
        sys.exit(1)

    speak(text, engine=args.engine, voice=args.voice,
          play=not args.no_play, output_path=args.output)


if __name__ == "__main__":
    main()
