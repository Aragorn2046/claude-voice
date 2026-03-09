#!/usr/bin/env python3
"""Text-to-speech engine abstraction: Edge TTS, ElevenLabs, Kokoro."""

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


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


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
        proc = subprocess.Popen(
            ["paplay", "--raw", f"--rate={srate}", "--channels=1", "--format=s16le"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.stdin.write(data.tobytes())
        proc.stdin.close()
        proc.wait()
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
    voice = voice or cfg.get("tts_voice_edge", "en-US-GuyNeural")

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = tmp.name
        tmp.close()

    speed = cfg.get("tts_speed", "+0%")
    communicate = edge_tts.Communicate(text, voice, rate=speed)
    await communicate.save(output_path)
    return output_path


def tts_elevenlabs(text: str, voice: str = None, output_path: str = None) -> str:
    """Generate speech using ElevenLabs API (premium quality)."""
    from elevenlabs import ElevenLabs

    cfg = load_config()
    api_key = os.environ.get(cfg.get("elevenlabs_api_key_env", "ELEVENLABS_API_KEY"))
    if not api_key:
        print("Error: ELEVENLABS_API_KEY not set", file=sys.stderr)
        return None

    voice = voice or cfg.get("tts_voice_elevenlabs", "Adam")

    client = ElevenLabs(api_key=api_key)

    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        output_path = tmp.name
        tmp.close()

    audio_generator = client.text_to_speech.convert(
        text=text,
        voice_id=voice,
        model_id="eleven_turbo_v2_5",
        output_format="mp3_44100_128",
    )

    with open(output_path, "wb") as f:
        for chunk in audio_generator:
            f.write(chunk)

    return output_path


def speak(text: str, engine: str = None, voice: str = None, play: bool = True,
          output_path: str = None) -> str:
    """Speak text using the configured TTS engine.

    Returns path to generated audio file.
    """
    cfg = load_config()
    engine = engine or cfg.get("tts_engine", "edge")

    t0 = time.time()

    if engine == "edge":
        path = asyncio.run(tts_edge(text, voice, output_path))
    elif engine == "elevenlabs":
        path = tts_elevenlabs(text, voice, output_path)
    elif engine == "kokoro":
        print("Kokoro TTS not yet implemented", file=sys.stderr)
        return None
    else:
        print(f"Unknown TTS engine: {engine}", file=sys.stderr)
        return None

    elapsed = time.time() - t0
    print(f"TTS ({engine}): generated in {elapsed:.2f}s → {path}", file=sys.stderr)

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
