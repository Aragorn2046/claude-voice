#!/usr/bin/env python3
"""Pre-generate acknowledgment audio clips as raw PCM for instant playback."""

import asyncio
import os
import tempfile
import soundfile as sf
import numpy as np

ACKS_DIR = os.path.expanduser("~/claude-voice-venv/acks")
VOICE = "en-GB-SoniaNeural"
SPEED = "+40%"  # slightly faster than normal TTS for snappiness

PHRASES = [
    "On it.",
    "Got it.",
    "Working on it.",
    "Let me check.",
    "One moment.",
    "Right.",
    "Understood.",
    "Looking into it.",
    "Give me a sec.",
    "I'm on it.",
    "Let's see.",
    "Checking.",
    "Alright.",
    "Copy that.",
    "Processing.",
]


async def generate_clip(text: str, index: int):
    """Generate a single clip as raw PCM."""
    import edge_tts

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        communicate = edge_tts.Communicate(text, VOICE, rate=SPEED)
        await communicate.save(tmp_path)

        data, srate = sf.read(tmp_path)
        pcm = (data * 32767).astype(np.int16).tobytes()
        channels = 1 if data.ndim == 1 else data.shape[1]

        out_path = os.path.join(ACKS_DIR, f"ack_{index:02d}.raw")
        meta_path = os.path.join(ACKS_DIR, f"ack_{index:02d}.meta")

        with open(out_path, "wb") as f:
            f.write(pcm)
        with open(meta_path, "w") as f:
            f.write(f"{srate}\n{channels}\n")

        print(f"  [{index:02d}] \"{text}\" → {len(pcm)} bytes, {srate}Hz, {channels}ch")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


async def main():
    print(f"Generating {len(PHRASES)} acknowledgment clips...")
    for i, phrase in enumerate(PHRASES):
        await generate_clip(phrase, i)
    print(f"\nDone! Clips saved to {ACKS_DIR}/")


if __name__ == "__main__":
    asyncio.run(main())
