#!/usr/bin/env python3
"""Generate 15 short voice acknowledgment PCM clips using Edge TTS.

Run once to pre-generate. Clips are stored as raw PCM s16le 24000Hz mono
for instant playback via paplay (no decoding overhead).
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Short acknowledgment phrases — natural, varied
ACK_PHRASES = [
    "Mm-hmm.",
    "Got it.",
    "Okay.",
    "Right.",
    "Sure.",
    "On it.",
    "Yep.",
    "Alright.",
    "Understood.",
    "Hmm.",
    "Of course.",
    "Noted.",
    "Ah yes.",
    "I see.",
    "Let's see.",
]

OUTPUT_DIR = Path(__file__).parent / "ack_clips"
VOICE = "en-GB-SoniaNeural"
RATE = "+20%"  # slightly faster for snappy acks


async def generate_clip(phrase: str, index: int):
    """Generate a single ack clip as raw PCM."""
    import edge_tts

    # Generate MP3 first
    mp3_path = tempfile.mktemp(suffix=".mp3")
    communicate = edge_tts.Communicate(phrase, VOICE, rate=RATE)
    await communicate.save(mp3_path)

    # Convert to raw PCM s16le 24000Hz mono via ffmpeg
    pcm_path = OUTPUT_DIR / f"ack_{index:02d}.pcm"
    subprocess.run(
        ["ffmpeg", "-y", "-i", mp3_path, "-f", "s16le", "-acodec", "pcm_s16le",
         "-ar", "24000", "-ac", "1", str(pcm_path)],
        capture_output=True, check=True,
    )
    os.unlink(mp3_path)
    print(f"  [{index+1:2d}/15] {phrase:20s} → {pcm_path.name} ({pcm_path.stat().st_size} bytes)")


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating {len(ACK_PHRASES)} ack clips to {OUTPUT_DIR}/")
    print(f"Voice: {VOICE}, Rate: {RATE}")
    print()

    for i, phrase in enumerate(ACK_PHRASES):
        await generate_clip(phrase, i)

    print(f"\nDone! {len(ACK_PHRASES)} clips ready.")
    print("Test: paplay --raw --rate=24000 --channels=1 --format=s16le ack_clips/ack_00.pcm")


if __name__ == "__main__":
    asyncio.run(main())
