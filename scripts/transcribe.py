#!/usr/bin/env python3
"""Speech-to-text using faster-whisper with GPU acceleration."""

import argparse
import json
import sys
import time
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"

# Lazy-loaded model (stays in GPU memory between calls via voice_server)
_model = None


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def get_model():
    """Load faster-whisper model (cached after first call)."""
    global _model
    if _model is not None:
        return _model

    from faster_whisper import WhisperModel

    cfg = load_config()
    model_size = cfg.get("stt_model", "large-v3")
    device = cfg.get("stt_device", "cuda")
    compute_type = cfg.get("stt_compute_type", "float16")

    print(f"Loading whisper model '{model_size}' on {device} ({compute_type})...",
          file=sys.stderr)
    t0 = time.time()

    try:
        _model = WhisperModel(model_size, device=device, compute_type=compute_type)
    except Exception as e:
        print(f"GPU loading failed ({e}), falling back to CPU...", file=sys.stderr)
        _model = WhisperModel(model_size, device="cpu", compute_type="int8")

    print(f"Model loaded in {time.time() - t0:.1f}s", file=sys.stderr)
    return _model


def transcribe(audio_path: str, language: str = None) -> str:
    """Transcribe audio file to text. Returns transcribed text."""
    model = get_model()

    t0 = time.time()
    segments, info = model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(
            min_silence_duration_ms=500,
            speech_pad_ms=200,
        ),
    )

    text_parts = []
    for segment in segments:
        text_parts.append(segment.text.strip())

    text = " ".join(text_parts)
    elapsed = time.time() - t0

    print(f"Transcribed in {elapsed:.2f}s | Language: {info.language} "
          f"({info.language_probability:.0%})", file=sys.stderr)

    return text


def main():
    parser = argparse.ArgumentParser(description="Transcribe audio to text")
    parser.add_argument("audio_file", help="Path to audio file (WAV, MP3, etc.)")
    parser.add_argument("-l", "--language", help="Language code (e.g., en, nl)")
    args = parser.parse_args()

    if not Path(args.audio_file).exists():
        print(f"Error: File not found: {args.audio_file}", file=sys.stderr)
        sys.exit(1)

    text = transcribe(args.audio_file, language=args.language)
    print(text)


if __name__ == "__main__":
    main()
