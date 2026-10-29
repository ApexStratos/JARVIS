# J.A.R.V.I.S. Nexus

Personal AI assistant with multi-engine voice synthesis, multilingual support, a real-time web UI, and ~115 tools (web search, system control, calendar, email, Spotify, trading-bot/project status, and more). Runs entirely on your own machine — your API keys, your data.

**Platforms:** macOS (full feature set), Windows & Linux (core assistant + most tools — see [Supported Platforms](#supported-platforms) below for what's macOS-only).

---

## Features

- **Multi-engine TTS** — Kokoro ONNX (54 voices, 9 languages), Chatterbox voice cloning, Fish Audio cloud API, local OS voices as a no-config fallback
- **Auto language detection** — detects language from typed text or speech and switches voice automatically
- **Voice selector panel** — browse, preview, and assign voices per language
- **Fish Audio voice cloning** — 27 reference voices cloned via Chatterbox locally (no API key required)
- **Streaming AI responses** — Ollama (free, local) or Claude, with SSE streaming and sentence-by-sentence TTS queue
- **Wake word + voice mode** — hands-free operation with VAD silence detection
- **24/7 daemon** — background service on macOS (launchd), Linux (systemd), or Windows (Task Scheduler) — see [deploy/](deploy/)
- **Persona system** — 40+ selectable personas
- **~115 tools** — web search, email, calendar, Spotify, system controls, disk cleanup, project/trading-bot status monitoring, approval-gated business outreach, live phone calling (Twilio), and more

---

## Quick Install

One command. It clones the repo, installs Ollama if you don't have it,
pulls a free local model, installs everything else, starts JARVIS, and
opens your browser to it — no follow-up steps.

**macOS / Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/ApexStratos/JARVIS/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/ApexStratos/JARVIS/main/install.ps1 | iex
```

Or download the [latest release](https://github.com/ApexStratos/JARVIS/releases/latest), extract it, and run `bash install.sh` (mac/Linux) or `install.ps1` (Windows) from inside.

---

## Manual Setup

### Requirements

- Python 3.11–3.13
- [Ollama](https://ollama.com) with a model pulled (`ollama pull llama3.2`) — or an Anthropic API key, see Configuration below
- ffmpeg (optional — some audio paths use it; everything else works without it)

### Steps

```bash
# 1. Clone
git clone https://github.com/ApexStratos/JARVIS.git
cd JARVIS

# 2. Install Python dependencies
pip install -r requirements.txt     # Windows: use `pip`, elsewhere `pip3` also works

# 3. Download TTS voice models
python provision_all_voices.py      # Windows: `python`, elsewhere `python3`

# 4. Start the server
python server.py                    # or: ./run.sh (mac/Linux) / run.bat (Windows)
```

Then open **http://localhost:5001** in your browser.

---

## Configuration

Copy `.env.example` to `.env` and fill in any optional keys — every single
one is optional, JARVIS runs with zero configuration on free/local Ollama:

```env
# Optional: Fish Audio cloud API (for cloud synthesis instead of local cloning)
FISH_AUDIO_API_KEY=your_key_here

# Optional: Telegram notifications
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional: Claude instead of local Ollama (a few dollars/month at normal use)
BRAIN_PROVIDER=ollama
ANTHROPIC_API_KEY=

# Optional: send email via any SMTP provider (Gmail, Outlook, etc) instead
# of macOS Mail.app — required on Windows/Linux if you want the EMAIL tool
SMTP_HOST=
SMTP_USER=
SMTP_PASS=

# Ollama model (default: llama3.2)
OLLAMA_MODEL=llama3.2
```

See `.env.example` for the full list (Twilio calling, Spotify, trading
bot integration, etc).

**Project/trading-bot status monitoring** (the `PROJECT_STATUS`/`WEBSCOUT`/disk-cleanup
tools) is driven by `config/projects.json`, which is also optional and gitignored —
copy `config/projects.example.json` to get started, or skip it entirely.

---

## Voice Engines

| Engine | Voices | Languages | Requires | Platforms |
|---|---|---|---|---|
| Kokoro ONNX | 54 | en, ja, zh, fr, de, es, pt, hi, it | `pip install kokoro-onnx` | All |
| Chatterbox (cloning) | 27 Fish Audio reference voices | en | `pip install chatterbox-tts` | All |
| Fish Audio API | 27 reference voices | en + multilingual | `FISH_AUDIO_API_KEY` | All |
| ElevenLabs API | Your ElevenLabs voices | multilingual | `ELEVENLABS_API_KEY` | All |
| Local OS voice | System voices | en (+ more via `say` on macOS) | Built-in (fallback) | All — macOS uses `say`, Windows/Linux use `pyttsx3` |

---

## Supported Platforms

The core assistant (chat, all info/search/utility tools, system basics like
volume/clipboard/notifications/dark-mode/lock/sleep, Spotify playback,
SMTP email, local notes/reminders) works identically on **macOS, Windows,
and Linux**. A smaller set of tools are macOS-only because they bind to a
specific Apple app or framework with no real cross-platform equivalent —
these return a clear "needs macOS" message instead of failing silently on
other OSes:

iMessage, FaceTime, Contacts.app, Apple Calendar/Notes/Reminders (native —
Windows/Linux use a local JSON-backed fallback instead), Apple Music
app-level control (Spotify's Web API control still works everywhere),
AirPlay, Focus modes, Shortcuts, Night Shift, Empty Trash, screen
recording, and the Spotify playlist-by-name/search-and-play commands
(which use keystroke automation rather than the Web API).

Windows/Linux support was built and tested against documented OS
APIs/CLIs but hasn't been run on real Windows/Linux hardware yet — if
something's off, please open an issue.

---

## 24/7 Daemon

**macOS:**
```bash
bash manage_nexus.sh install-agents
bash manage_nexus.sh start   # stop / restart / logs also available
```

**Linux / Windows:** see [`deploy/`](deploy/) — systemd unit template for
Linux, Task Scheduler instructions for Windows.

---

## Architecture

```
browser  ──────────►  Flask server (port 5001)
                           │
                   ┌───────┼───────────┐
                   │       │           │
              Ollama LLM   │      sovereign_nexus_tts.py
            (llama3.2)     │           │
                           │    ┌──────┴──────────┐
                        /api/   │                 │
                        speak  Kokoro ONNX   Chatterbox
                               (54 voices)  (voice clone)
```

---

## License

MIT
