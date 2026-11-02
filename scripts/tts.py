#!/usr/bin/env python3
"""Text-to-speech engine abstraction for Edge, ElevenLabs, Kokoro and routed speech lanes."""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = str(Path(__file__).resolve().parent)
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from shelby_speech_policy import (
    DUTCH_RESOLVER_TIMEOUT_S,
    SHELBY_DUTCH_PERSONAS,
    SHELBY_SPEECH_PERSONA,
    bounded_dutch_timeout,
    is_content_voice,
    normalize_dutch_persona,
)
from omnivoice_http import post_json as _raw_omnivoice_post_json

CONFIG_PATH = Path(__file__).parent / "config.json"

DUTCH_ENGINE_ENDPOINTS = {
    "omnivoice:tts": "http://100.77.19.108:8934",
}
OMNIVOICE_RESPONSE_CHUNK_SIZE = 16 * 1024
MAX_OMNIVOICE_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OMNIVOICE_RESPONSE_HEADER_BYTES = 64 * 1024


def _omnivoice_post_json(base_url: str, payload: bytes, timeout: float) -> bytes:
    return _raw_omnivoice_post_json(
        base_url, payload, timeout, MAX_OMNIVOICE_RESPONSE_BYTES,
        OMNIVOICE_RESPONSE_CHUNK_SIZE, MAX_OMNIVOICE_RESPONSE_HEADER_BYTES,
    )


def _model_route_resolver_candidates():
    return (
        os.path.expanduser("~/scripts/shelby-model-route.py"),
        "/Users/aragorn/scripts/shelby-model-route.py",
        "/home/arago/scripts/shelby-model-route.py",
    )


def _find_model_route_resolver():
    return next((path for path in _model_route_resolver_candidates()
                 if os.path.isfile(path)), None)


def resolve_dutch_speech_engine():
    """Return the live Dutch speech catalog ID, or None when resolution fails."""
    def failed(reason):
        print(f"Dutch speech Model Routing resolution failed: {reason}", file=sys.stderr)
        return None

    resolver = _find_model_route_resolver()
    if not resolver:
        return failed("resolver not found")
    try:
        result = subprocess.run(
            [sys.executable, resolver, "--consumer", "shelby-audio",
             "--modality", "speech-nl", "--target", "catalog",
             "--field", "catalog_id"],
            capture_output=True, text=True, timeout=DUTCH_RESOLVER_TIMEOUT_S, shell=False,
        )
    except subprocess.TimeoutExpired:
        return failed("resolver timed out after 3 seconds")
    except OSError as exc:
        return failed(f"resolver could not run: {exc}")

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:160]
        suffix = f": {detail}" if detail else ""
        return failed(f"resolver exited {result.returncode}{suffix}")

    catalog_id = (result.stdout or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", catalog_id):
        return failed("resolver returned an unparseable catalog ID")
    return catalog_id

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


def _resolve_shelby_pocket_voice(cfg: dict, voice: str = None) -> str:
    """Resolve every non-content Pocket voice to one Shelby persona id.

    The requested voice, or `tts_voice_pocket_en` when the caller omitted it,
    is normalized with `normalize_dutch_persona` (strip, casefold, and
    `[a-z0-9-]+` only). It is kept only when that id is in
    `SHELBY_DUTCH_PERSONAS` and is not a content voice. Otherwise this returns
    `SHELBY_SPEECH_PERSONA` and writes one stderr line. `content=True` does
    not call this function.
    """
    candidate = voice if voice else cfg.get("tts_voice_pocket_en", SHELBY_SPEECH_PERSONA)
    content_voice = cfg.get("tts_voice_pocket_content", "aragorn")
    normalized = normalize_dutch_persona(candidate)
    if (
        normalized in SHELBY_DUTCH_PERSONAS
        and not is_content_voice(candidate, content_voice)
        and not is_content_voice(normalized, content_voice)
    ):
        return normalized
    print(
        f"Shelby speech refused persona {candidate!r}; using {SHELBY_SPEECH_PERSONA}",
        file=sys.stderr,
    )
    return SHELBY_SPEECH_PERSONA


def _pocket_server_answered(exc: BaseException) -> bool:
    """Return whether Pocket answered with an HTTP 4xx/5xx status.

    Connection refused, connection reset, DNS errors, and timeouts
    (``socket.timeout``, ``TimeoutError``, or a urllib ``URLError`` wrapping
    them) are transport failures. They are not answers. A false result must
    not open a second request for the jarvis retry.
    """
    import urllib.error

    current = exc
    seen = set()
    while isinstance(current, BaseException) and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, urllib.error.HTTPError):
            code = getattr(current, "code", None)
            if code is None:
                return True
            try:
                status = int(code)
            except (TypeError, ValueError):
                return True
            return 400 <= status <= 599
        reason = getattr(current, "reason", None)
        if isinstance(reason, BaseException) and reason is not current:
            current = reason
            continue
        break
    return False


def _pocket_http_wav(base: str, text: str, voice: str, timeout: int) -> tuple[bytes | None, bool]:
    """One Pocket HTTP synth.

    Returns ``(wav, server_answered)``. ``wav`` is None when this persona on
    this server is unusable. ``server_answered`` is True for an HTTP 4xx/5xx
    or a body under 1000 bytes, and False for a transport failure.
    """
    import urllib.request as _ur

    body = json.dumps({"text": text, "voice": voice}).encode()
    req = _ur.Request(f"{base}/tts", data=body, headers={"Content-Type": "application/json"})
    try:
        with _ur.urlopen(req, timeout=timeout) as response:
            wav = response.read()
    except Exception as exc:
        print(f"pocket-tts request failed: {exc}", file=sys.stderr)
        return None, _pocket_server_answered(exc)
    if len(wav) < 1000:
        print(f"pocket-tts returned {len(wav)} bytes", file=sys.stderr)
        return None, True
    return wav, True


def tts_pocket(text: str, voice: str = None, output_path: str = None,
               content: bool = False) -> str:
    """Local Kyutai pocket-tts over HTTP (daemon on :8933, $0, offline).

    Voice resolution is deliberately asymmetric (CARSO 2026-09-10):
    - content=False (default, including an omitted voice) -> a Shelby persona.
      `_resolve_shelby_pocket_voice` accepts only `SHELBY_DUTCH_PERSONAS` and
      refuses Aragorn's content clone. Anything else is CASE-O.
    - content=True (CLI `--content`) -> `tts_voice_pocket_content` (aragorn):
      voiceovers, explainers, generated video, per his 2026-09-10 directive.
      An explicit `voice` still wins on this path only.

    If that Shelby persona fails because the server answered (HTTP 4xx/5xx,
    or a body under 1000 bytes) and the voice is not `jarvis`, retry the same
    server once with `jarvis` before the caller falls back to Edge. Timeouts,
    connection refused/reset, and DNS errors are not answers: they skip that
    retry so a dead server costs one timeout. Same per-request timeout.
    Content mode never uses that fallback.
    """
    cfg = load_config()
    base = cfg.get("pocket_tts_url", "http://127.0.0.1:8933")
    if content:
        content_voice = str(cfg.get("tts_voice_pocket_content", "aragorn")).strip().lower()
        if not voice:
            voice = content_voice
    else:
        voice = _resolve_shelby_pocket_voice(cfg, voice)
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
    timeout = int(os.environ.get("SHELBY_POCKET_TIMEOUT", "60"))
    wav, server_answered = _pocket_http_wav(base, text, voice, timeout)
    content_name = str(cfg.get("tts_voice_pocket_content", "aragorn"))
    normalized = normalize_dutch_persona(voice)
    # One extra attempt, same per-request timeout, before the Edge fallback.
    # Only when this server answered. The retry voice is jarvis. A content
    # voice is never the fallback. A transport failure goes straight to Edge.
    if (
        wav is None
        and server_answered
        and not content
        and normalized not in {"", "jarvis"}
        and not is_content_voice(voice, content_name)
        and not is_content_voice(normalized, content_name)
    ):
        print(
            f"pocket: persona {voice} unavailable on {base}, using jarvis",
            file=sys.stderr,
        )
        wav, _jarvis_answered = _pocket_http_wav(base, text, "jarvis", timeout)
    if wav is None:
        return None
    with open(output_path, "wb") as fh:
        fh.write(wav)
    return output_path


def _omnivoice_content_request(cfg: dict, voice: str = None,
                               content: bool = False) -> bool:
    """The explicit content mode is refused; other voices resolve to Shelby."""
    return bool(content)


def _resolve_omnivoice_voice(cfg: dict, voice: str = None) -> str:
    """Select only a Shelby persona for the Dutch OmniVoice lane."""
    return _resolve_shelby_pocket_voice(cfg, voice)


def tts_omnivoice(text: str, voice: str = None, output_path: str = None,
                  content: bool = False) -> str:
    """Generate private Dutch speech through the Model-Routing-selected lane."""
    cfg = load_config()
    if _omnivoice_content_request(cfg, voice, content):
        print("OmniVoice weights are CC-BY-NC: never for content", file=sys.stderr)
        return None

    voice = _resolve_omnivoice_voice(cfg, voice)

    catalog_id = resolve_dutch_speech_engine()
    base_url = DUTCH_ENGINE_ENDPOINTS.get(catalog_id)
    if not base_url:
        if catalog_id:
            print(f"Dutch speech lane off: Model Routing selected {catalog_id!r}; "
                  "no Dutch endpoint is mapped", file=sys.stderr)
        else:
            print("Dutch speech lane off: Model Routing returned no catalog ID", file=sys.stderr)
        return None

    dutch_config = cfg.get("dutch_speech")
    dutch_config = dutch_config if isinstance(dutch_config, dict) else {}
    timeout = bounded_dutch_timeout(dutch_config, reserve_fallback=False)
    if timeout is None:
        print("Dutch speech lane skipped: synthesis budget is below the minimum",
              file=sys.stderr)
        return None
    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)

    payload = json.dumps({"text": text, "voice": voice}).encode()
    try:
        wav = _omnivoice_post_json(base_url, payload, timeout)
    except Exception as exc:
        print(f"omnivoice request failed: {exc}", file=sys.stderr)
        return None
    if len(wav) < 1000:
        print(f"omnivoice returned {len(wav)} bytes", file=sys.stderr)
        return None
    with open(output_path, "wb") as fh:
        fh.write(wav)
    return output_path


def speak(text: str, engine: str = None, voice: str = None, play: bool = True,
          output_path: str = None, content: bool = False) -> str:
    """Speak text using the configured TTS engine.

    Returns path to generated audio file.
    Falls back to configured fallback engine on failure.
    """
    cfg = load_config()
    engine = engine or cfg.get("tts_engine", "edge")
    fallbacks = cfg.get("engine_fallback", {})

    t0 = time.time()
    path = None
    content_refused = False

    if engine == "edge":
        path = asyncio.run(tts_edge(text, voice, output_path))
    elif engine == "elevenlabs":
        path = tts_elevenlabs(text, voice, output_path)
    elif engine == "kokoro":
        path = tts_kokoro(text, voice, output_path)
    elif engine == "pocket":
        path = tts_pocket(text, voice, output_path, content=content)
    elif engine == "omnivoice":
        content_refused = _omnivoice_content_request(cfg, voice, content)
        path = tts_omnivoice(text, voice, output_path, content=content)
    else:
        print(f"Unknown TTS engine: {engine}", file=sys.stderr)

    # Fallback on failure
    if (path is None and engine in fallbacks and not content_refused
            and engine != "omnivoice"):
        fb = fallbacks[engine]
        print(f"TTS ({engine}) failed, falling back to {fb}", file=sys.stderr)
        if fb == "edge":
            path = asyncio.run(tts_edge(text, voice=None, output_path=output_path))
        elif fb == "elevenlabs":
            path = tts_elevenlabs(text, voice=None, output_path=output_path)
        elif fb == "kokoro":
            path = tts_kokoro(text, voice=None, output_path=output_path)
        elif fb == "pocket":
            path = tts_pocket(text, voice=None, output_path=output_path)
        elif fb == "omnivoice":
            path = tts_omnivoice(
                text, voice=None, output_path=output_path, content=content
            )
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
    parser.add_argument("-e", "--engine", choices=["edge", "elevenlabs", "kokoro", "pocket", "omnivoice"],
                        help="TTS engine")
    parser.add_argument("-v", "--voice", help="Voice name/ID")
    parser.add_argument("-o", "--output", help="Save audio to file instead of playing")
    parser.add_argument("--no-play", action="store_true", help="Don't play audio")
    parser.add_argument("--content", action="store_true",
                        help="Content voice (Aragorn's own clone) instead of the Shelby persona; OmniVoice refuses content use")
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

    selected_engine = args.engine or load_config().get("tts_engine", "edge")
    path = speak(text, engine=args.engine, voice=args.voice, content=args.content,
                 play=not args.no_play, output_path=args.output)
    if selected_engine == "omnivoice" and path is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
