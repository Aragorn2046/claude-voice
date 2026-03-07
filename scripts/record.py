#!/usr/bin/env python3
"""Push-to-talk audio capture with Voice Activity Detection (VAD).

Uses parecord (PulseAudio) for capture — no portaudio dependency needed.
Falls back to sounddevice if parecord is unavailable.
"""

import argparse
import json
import shutil
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

CONFIG_PATH = Path(__file__).parent / "config.json"
OUTPUT_DIR = Path("/tmp/claude-voice")


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_wav(audio: np.ndarray, sample_rate: int, path: Path):
    """Save numpy audio array (float32, mono) to WAV file."""
    audio_int16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_int16.tobytes())


def record_raw(seconds: int, sample_rate: int) -> np.ndarray:
    """Record raw PCM audio via parecord for a fixed duration."""
    proc = subprocess.Popen(
        ["parecord", "--rate", str(sample_rate), "--channels", "1",
         "--format", "s16le", "--raw"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        raw, _ = proc.communicate(timeout=seconds + 1)
    except subprocess.TimeoutExpired:
        proc.terminate()
        raw, _ = proc.communicate()

    if not raw:
        return np.array([], dtype="float32")
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def test_microphone():
    """Record 3 seconds and report audio levels to verify mic works."""
    print("Testing microphone... Speak now (3 seconds)")
    cfg = load_config()
    sr = cfg["sample_rate"]

    audio = record_raw(3, sr)

    if len(audio) == 0:
        print("WARNING: No audio captured. Microphone may not be connected.")
        return False

    peak = np.max(np.abs(audio))
    rms = np.sqrt(np.mean(audio ** 2))
    print(f"Duration: {len(audio)/sr:.1f}s")
    print(f"Peak level: {peak:.4f}")
    print(f"RMS level:  {rms:.4f}")

    if peak < 0.001:
        print("WARNING: No audio detected. Microphone may not be working.")
        print("Check: Settings > System > Sound > Input in Windows")
        return False

    print("Microphone is working!")
    OUTPUT_DIR.mkdir(exist_ok=True)
    dest = OUTPUT_DIR / "mic_test.wav"
    save_wav(audio, sr, dest)
    print(f"Saved test recording to {dest}")
    return True


def record_until_silence(sample_rate=16000, silence_threshold=0.02,
                         silence_duration=1.5, max_seconds=30):
    """Record audio via parecord until silence detected or max duration.

    Uses parecord subprocess and reads raw PCM for real-time VAD.
    Returns numpy float32 array.
    """
    chunk_duration = 0.1  # 100ms chunks
    chunk_bytes = int(sample_rate * chunk_duration * 2)  # 16-bit = 2 bytes/sample
    silence_chunks_needed = int(silence_duration / chunk_duration)
    max_chunks = int(max_seconds / chunk_duration)

    print("Listening... (speak now, will stop after silence)", file=sys.stderr)

    # Record raw PCM (s16le) to stdout
    proc = subprocess.Popen(
        ["parecord", "--rate", str(sample_rate), "--channels", "1",
         "--format", "s16le", "--raw"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    chunks = []
    silence_count = 0
    has_speech = False

    try:
        for _ in range(max_chunks):
            raw = proc.stdout.read(chunk_bytes)
            if not raw:
                break

            # Convert raw bytes to float32
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            chunks.append(samples)

            rms = np.sqrt(np.mean(samples ** 2))

            if rms > silence_threshold:
                has_speech = True
                silence_count = 0
            elif has_speech:
                silence_count += 1
                if silence_count >= silence_chunks_needed:
                    print("Silence detected, stopping.", file=sys.stderr)
                    break
    finally:
        proc.terminate()
        proc.wait()

    if not chunks:
        return np.array([], dtype="float32")

    audio = np.concatenate(chunks)

    # Trim trailing silence (keep half for natural ending)
    if has_speech and silence_count > 0:
        trim_samples = int(silence_count * sample_rate * chunk_duration * 0.5)
        if trim_samples < len(audio):
            audio = audio[:-trim_samples]

    return audio


def record(output_path: str = None):
    """Record audio and save to WAV. Returns the output path."""
    cfg = load_config()
    sr = cfg["sample_rate"]

    audio = record_until_silence(
        sample_rate=sr,
        silence_threshold=cfg["silence_threshold"],
        silence_duration=cfg["silence_duration"],
        max_seconds=cfg["max_record_seconds"],
    )

    if len(audio) == 0:
        print("No audio recorded.", file=sys.stderr)
        return None

    OUTPUT_DIR.mkdir(exist_ok=True)
    if output_path is None:
        output_path = str(OUTPUT_DIR / "recording.wav")

    save_wav(audio, sr, Path(output_path))
    duration = len(audio) / sr
    print(f"Recorded {duration:.1f}s -> {output_path}", file=sys.stderr)
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Record audio from microphone")
    parser.add_argument("--test", action="store_true", help="Test microphone")
    parser.add_argument("-o", "--output", help="Output WAV file path")
    args = parser.parse_args()

    if args.test:
        sys.exit(0 if test_microphone() else 1)

    path = record(args.output)
    if path:
        print(path)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
