# claude-voice

Give Claude Code a voice. Multi-engine TTS that speaks every response, with remote audio piping for headless servers.

## What it does

Claude Code responds in text. This project adds a voice layer:

1. Claude includes a `<voice>` block in every response (via CLAUDE.md instruction)
2. A **Stop hook** extracts the voice block, sanitizes it, and speaks it through TTS
3. A **UserPromptSubmit hook** kills playback when you start typing (so it doesn't talk over you)

Works on **macOS** (local or headless Mac Mini) and **Windows 11 via WSL2**.

## TTS Engines

| Engine | Cost | Latency | Quality | Requires |
|--------|------|---------|---------|----------|
| **Edge TTS** (default) | Free | ~1-2s | Good | Internet |
| **ElevenLabs** | Paid | ~0.5s streaming | Excellent | API key |
| **Kokoro** | Free | ~2s CPU, <1s GPU | Good | Local install |
| **say** (macOS only) | Free | Instant | Basic | Nothing |

Switch engines via `config.json` or a `/tts` slash command.

## Remote Audio Piping

The killer feature: if you SSH/Mosh into a headless Mac from a laptop, TTS audio plays **on your laptop**, not on the remote Mac.

How it works:
- The macOS TTS hook reads `SSH_CONNECTION` to get the client's IP
- Sends WAV audio directly to that IP over TCP (port 12345)
- A persistent listener on the laptop receives and plays it
- Requires [Tailscale](https://tailscale.com/) (or any VPN that makes the IPs routable)

No SSH tunnels needed. No extra terminals. Just works.

```
┌─────────────┐    SSH/Mosh    ┌──────────────┐
│   Laptop    │ ─────────────→ │  Mac (remote) │
│  (WSL2)     │                │              │
│             │   WAV audio    │  Claude Code │
│  audio-     │ ←───────────── │  + TTS hook  │
│  listener   │  TCP :12345    │              │
│  → paplay   │  (Tailscale)   │  → afplay    │
└─────────────┘                └──────────────┘
```

## Quick Start

### macOS (local)

```bash
# Install dependencies
pip install edge-tts

# Copy the hook and config
cp platforms/macos/voice-stop-hook.py ~/projects/claude-voice/scripts/
cp scripts/config.example.json ~/projects/claude-voice/scripts/config.json

# Add to Claude Code settings.json
# See "Hook Configuration" below
```

### WSL2 (Windows 11)

```bash
# Install dependencies
pip install edge-tts soundfile numpy

# Copy the hook
cp platforms/wsl2/voice-stop-hook.py ~/projects/claude-voice/scripts/

# Verify audio works
paplay /usr/share/sounds/freedesktop/stereo/bell.oga
```

### Remote Audio Piping (Mac → Laptop)

**On the Mac (remote):**
```bash
# Use the macOS hook (it auto-detects SSH and pipes audio)
cp platforms/macos/voice-stop-hook.py ~/projects/claude-voice/scripts/

# Install ffmpeg for audio format conversion
brew install ffmpeg mosh
```

**On the laptop (WSL2):**
```bash
# 1. Enable mirrored networking so Tailscale traffic reaches WSL2
echo -e '[wsl2]\nnetworkingMode=mirrored' > /mnt/c/Users/YOUR_USER/.wslconfig

# 2. Install the audio listener
cp platforms/wsl2/audio-listener.py ~/scripts/
chmod +x ~/scripts/audio-listener.py

# 3. Install mosh client
sudo apt-get install -y mosh

# 4. Auto-start listener (add to /etc/wsl.conf):
#    [boot]
#    command=su -c 'nohup python3 /home/YOUR_USER/scripts/audio-listener.py > /tmp/audio-listener.log 2>&1 &' YOUR_USER

# 5. Restart WSL: wsl --shutdown (from PowerShell), then reopen

# 6. Connect to your Mac
mosh user@your-mac-tailscale-ip
```

## Hook Configuration

Add to your Claude Code `settings.json` (or `~/.claude/settings.json`):

```json
{
  "hooks": [
    {
      "event": "Stop",
      "command": "python3 ~/projects/claude-voice/scripts/voice-stop-hook.py"
    },
    {
      "event": "UserPromptSubmit",
      "command": "bash ~/projects/claude-voice/scripts/shutup.sh"
    }
  ]
}
```

## CLAUDE.md Voice Instruction

Add this to your project's CLAUDE.md (or global instructions):

```markdown
## Voice Mode

Append a `<voice>` block at the end of every response:

    <voice>
    (concise spoken summary — 1-3 sentences, pure spoken language, no formatting)
    </voice>

The voice block must be PURE SPOKEN LANGUAGE:
- No file paths, code, URLs, markdown formatting
- Casual and conversational — like talking to a colleague
- 1-2 sentences for simple answers, max 3-4 for complex ones
```

## Config

Copy `scripts/config.example.json` to `scripts/config.json` and customize:

```json
{
  "tts_engine": "edge",
  "tts_speed": "+30%",
  "tts_voice_edge_en": "en-GB-SoniaNeural",
  "tts_voice_edge_nl": "nl-NL-FennaNeural",
  "elevenlabs_api_key_env": "ELEVENLABS_API_KEY",
  "tts_enabled": true
}
```

**API keys go in environment variables, not config.json.** Set `ELEVENLABS_API_KEY` in your shell profile if using ElevenLabs.

## Mute/Unmute

```bash
# Mute
touch /tmp/claude-tts-muted

# Unmute
rm -f /tmp/claude-tts-muted
```

Or add these as Claude Code slash commands — see `commands/mute.md` and `commands/unmute.md`.

## Language Detection

Auto-detects Dutch vs English using word frequency heuristics. Switches to the configured Dutch voice when Dutch content is detected (>15% Dutch marker words). Extend `detect_language()` for other languages.

## Features

- **Multi-engine**: Edge (free), ElevenLabs (premium), Kokoro (local), say (macOS built-in)
- **Remote audio piping**: SSH into a headless Mac, hear TTS on your laptop
- **Language detection**: Auto-switches voice for Dutch content
- **Dual-session lockfile**: Prevents double-playback when multiple Claude sessions run
- **Mute toggle**: Quick mute for meetings/public spaces
- **Interrupt on type**: Kills TTS when you start your next prompt

## Requirements

- Python 3.10+
- `edge-tts` (pip install)
- For WSL2: `soundfile`, `numpy` (pip install)
- For ElevenLabs: `requests` (pip install), API key
- For Kokoro: `kokoro` (pip install), GPU recommended
- For remote piping: Tailscale, ffmpeg, mosh

## License

MIT
