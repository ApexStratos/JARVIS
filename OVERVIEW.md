# J.A.R.V.I.S. Nexus — Project Overview

A local-first autonomous AI assistant running on macOS. Combines a local LLM (Ollama), vision-based screen control, 70+ system tools, trading integration, and a PWA web interface — all accessible from your phone via Cloudflare tunnel.

---

## Directory Structure

```
jarvis-nexus/
├── server.py                  # Flask web server + all API routes
├── sovereign_nexus.py         # Ollama-based screen agent (PTAE loop)
├── auto.py                    # Claude vision-based screen agent
├── dispatch.py                # Low-level OS control (pyautogui + mss)
├── storage_manager.py         # Disk cleanup automation
├── telegram_notify.py         # Telegram push notifications
│
├── app/
│   ├── brain.py               # LLM reasoning engine (llama3.2 via Ollama)
│   ├── toolkit.py             # 70+ executable tools (~3300 lines)
│   ├── engine.py              # Alpaca paper trading client
│   ├── evolution.py           # Self-update protocols
│   ├── self_upgrade.py        # Self-healing + GitHub deployment
│   └── core/
│       ├── config.py          # System prompt configuration
│       ├── guardian.py        # Error logging / monitoring
│       ├── heartbeat.py       # System health monitoring
│       └── kernel.py          # Bot orchestration framework
│
├── templates/
│   ├── login.html             # PIN authentication page
│   ├── overview.html          # Main dashboard (voice, chat, nexus)
│   ├── dashboard.html         # Trading analytics
│   └── spotify_setup.html     # Spotify OAuth flow
│
├── static/
│   ├── sw.js                  # Service Worker (PWA / offline cache)
│   ├── manifest.json          # PWA manifest
│   ├── icons/                 # App icons (180, 192, 512px)
│   └── visuals/               # WebGL backgrounds (nebula, tesseract, etc.)
│
├── data/
│   └── jarvis_memory.json     # Persistent key-value store
│
├── identity.json              # User profile + trading params
├── safety_perimeter.json      # Agent safety constraints
├── nexus_audit.log            # Sovereign Nexus action audit trail
├── requirements.txt           # Python dependencies
└── runtime.txt                # Python 3.11.0
```

---

## Core Components

### `server.py` — Flask Web Server

The entry point. Serves the web UI and exposes all API routes. Runs on `localhost:5001`.

**Authentication:** PIN-based session (`JARVIS_PIN` env var, default `1704`). All `/api/` routes require `@requires_auth`.

**Startup:** Two background threads warm up Ollama — `llama3.2` for chat and `llava` for vision — so the first real request is instant.

| Route | Method | Purpose |
|-------|--------|---------|
| `/login` | GET / POST | PIN authentication |
| `/api/chat` | POST (SSE) | Conversational chat with tool execution |
| `/api/think` | POST (SSE) | Raw brain output stream |
| `/api/speak` | POST | TTS → returns audio bytes to browser |
| `/api/nexus/run` | POST (SSE) | Sovereign Nexus screen agent |
| `/api/auto` | POST (SSE) | Claude vision screen agent |
| `/api/dispatch` | POST | Single OS action (click, type, screenshot) |
| `/api/memory` | GET / POST / DELETE | Persistent key-value data |
| `/api/alpha` | GET | Alpaca trading account status |
| `/api/metrics` | GET | CPU / RAM / disk stats |
| `/api/storage` | GET / POST | Disk usage + purge trigger |
| `/api/screenshot` | GET | Capture and return current screen |
| `/api/notify` | POST | Send Telegram message |
| `/setup/spotify/*` | GET / POST | Spotify OAuth flow |
| `/sw.js` | GET | Service worker (no-cache headers) |

**TTS priority (fastest first):**
1. macOS `say -o` → AIFF bytes (instant, zero network)
2. Microsoft edge-tts → MP3 bytes (good quality, network)
3. ElevenLabs → MP3 bytes (best quality, paid API key required)

---

### `app/brain.py` — LLM Reasoning Engine

Drives all conversational AI using **Ollama llama3.2** running locally.

**Three-pass architecture:**
1. **Stream** — Model response streamed token by token; `[TOOL: arg]` markers extracted
2. **Execute** — Each tool called via `toolkit.run_tool()`; results collected
3. **Synthesize** — For info tools (SEARCH, WEATHER, etc.), results fed back to model for a concise 1-sentence reply (80 tokens, capped)

Action tools (OPEN, SPEAK, SPOTIFY, NOTIFY, etc.) skip synthesis — no extra LLM call.

Rolling 6-message conversation memory per session.

---

### `app/toolkit.py` — Tool Library (70+ tools)

All tools follow the `[TOOL: arg]` syntax embedded in model responses. `run_tool(name, arg)` dispatches to the right handler.

| Category | Tools |
|----------|-------|
| Web | SEARCH, NEWS, NEWS_CATEGORY, WIKIPEDIA |
| System | SHELL, OPEN, QUIT, LOCK, SLEEP, DARKMODE, WIFI, BLUETOOTH |
| Audio / UI | SPEAK, VOLUME, BRIGHTNESS, TIMER, NOTIFY |
| Input | TYPE, HOTKEY, CLIPBOARD |
| Lookup | WEATHER, STOCKS, CRYPTO, CURRENCY, DEFINE, TIME, TRANSLATE |
| Navigation | BROWSER_NAV, BROWSER_JS, YOUTUBE, MAPS, FLIGHT |
| Comms | EMAIL, CONTACTS, MESSAGES, FACETIME, WHATSAPP |
| Files | FILE, ZIP, UNZIP, HASH, QR, BASE64, URL_ENCODE, JSON_FORMAT |
| Music | SPOTIFY, MUSIC, RADIO, PODCAST |
| Calendar | CALENDAR, REMIND, ALARM, POMODORO, COUNTDOWN |
| Memory | MEMORY_SET, MEMORY_GET, MEMORY_LIST |
| Self-Evolution | SELF_UPGRADE, SELF_HEAL, SELF_PUSH |

**Notable implementations:**
- `SEARCH` → DuckDuckGo (no API key, rate-tolerant)
- `SPOTIFY` → Direct HTTP to Spotify Web API (refresh token auth)
- `SHELL` → Safe whitelist of allowed command prefixes only
- `SELF_UPGRADE` → Generates new tool code via Ollama, validates Python syntax, appends to `toolkit.py`, pushes to GitHub
- `SELF_HEAL` → Scans broken tools, auto-patches, commits fix
- `MEMORY_*` → Reads/writes `data/jarvis_memory.json`

---

### `sovereign_nexus.py` — Local Screen Agent (PTAE Loop)

Vision-based screen automation using **Ollama only** (no cloud). Targets 4–8 seconds per step.

**Per-step pipeline:**

```
PERCEIVE → take screenshot + overlay 64px coordinate grid (1024px wide)
DESCRIBE → llava streams typed-element JSON: {"app":"...","elements":["button:X at (x,y)",...]}
THINK    → llama3.2 streams action JSON: {"action":{"tool_name":"left_click","parameters":{"x":0,"y":0}},"confidence":8,...}
ACT      → pyautogui executes the action
EVALUATE → background thread captures next frame; sub-goal index advances on success
```

**Tools (Claude Computer Use aligned):**
`left_click`, `right_click`, `double_click`, `middle_click`, `mouse_move`, `drag`, `key`, `type`, `scroll`, `screenshot`, `wait`, `log_event`

**Streaming optimization:** Both models stop generating as soon as the closing `}` is seen — no wasted tokens waiting for model to finish.

**Safety:** Constraints loaded from `safety_perimeter.json` — max steps, blocked key combos, file deletion off by default.

---

### `auto.py` — Claude Vision Agent

Cloud-based screen automation using **Claude Sonnet** (Anthropic API). Higher accuracy than local, slower and costs tokens.

**Flow:** Screenshot → base64 JPEG → Claude with goal → action JSON → `dispatch.execute()` → repeat (max 40 steps). Stops when `done: true` or 4+ consecutive waits (stall detection).

---

### `dispatch.py` — OS Control Layer

Thin wrapper over `pyautogui` and `mss`. Provides: `screenshot()`, `click()`, `rightclick()`, `doubleclick()`, `key()`, `type()`, `scroll()`, `drag()`.

Used by both screen agents and directly from toolkit.

---

### `app/engine.py` — Alpaca Trading

Connects to Alpaca paper trading account. Provides account summary (equity, buying power, positions) and Kelly position sizing.

**Kelly formula:** `f = (b·p − q) / b` applied at 0.25× (quarter-Kelly) capped at 5% max position size.

---

### `storage_manager.py` — Disk Cleanup

Monitors disk usage every cycle. Auto-purges safe targets when thresholds are exceeded.

| Threshold | Action |
|-----------|--------|
| 80% | Warning Telegram alert + light purge |
| 90% | Critical Telegram alert + aggressive purge |

**Safe purge targets:** Trash, Caches, Xcode DerivedData, pip cache, `__pycache__`, `.DS_Store`, old logs (>30 days), large unused files (>200 MB, >90 days old), `.dmg`/`.pkg` installers.

**Protected (never touched):** `~/jarvis-nexus`, `~/Documents`, `~/Downloads`, `~/Desktop`, `~/.claude`.

---

### `telegram_notify.py`

Sends real-time alerts to your Telegram via raw HTTP POST. Requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`.

---

## Web Interface (`templates/overview.html`)

Single-page app (~3800 lines). Key sections:

- **Command bar** — Always-visible text input at bottom; auto-enters voice mode on first tap
- **Voice mode** — Full-screen overlay; continuous mic listening with 2.2s silence timeout → sends to `/api/chat`; auto-opened on first user interaction
- **Wake word** — Background listener for "JARVIS" / "hey jar" re-activates voice mode after exit
- **Sovereign Nexus panel** — Launches local screen agent; shows live PTAE feed + screenshot
- **Visual gallery** — WebGL backgrounds (nebula, tesseract, cube, orb, robot, etc.)
- **Side drawers** — Karpathy self-upgrade proposals, integrity checker, visual gallery picker
- **PWA** — Installable to iOS home screen; service worker caches shell for offline use
- **Mobile** — Full responsive layout: safe-area insets, 44px touch targets, 16px inputs (no iOS zoom), stacked Nexus panel

---

## Data Flow: Message → Response

```
User speaks / types
        ↓
/api/think  (SSE POST)
        ↓
brain.stream_think()
        ↓
  [Pass 1] llama3.2 streams response
        ↓ extract [TOOL: arg]
  [Pass 2] toolkit.run_tool() for each tool
        ↓ collect results
  [Pass 3] llama3.2 synthesizes (info tools only, 80 tokens)
        ↓
SSE events → browser
  {"chunk": "..."} → display text
  {"tool": name, "result": ...} → show tool echo
        ↓
playVoice(reply) → POST /api/speak → macOS say → AIFF → browser audio
```

---

## External Services

| Service | Purpose | Env Vars |
|---------|---------|----------|
| Ollama (localhost:11434) | Chat + vision inference | `OLLAMA_URL`, `OLLAMA_MODEL` |
| Anthropic (Claude) | Cloud vision agent | `ANTHROPIC_API_KEY` |
| Spotify Web API | Music control | `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `SPOTIFY_REFRESH_TOKEN` |
| Alpaca | Paper trading | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` |
| Telegram Bot | Push notifications | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| ElevenLabs | Premium TTS (optional) | `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID` |
| Microsoft Edge TTS | Neural TTS fallback | *(none — free)* |
| DuckDuckGo | Web search | *(none — free)* |
| Cloudflare Tunnel | Remote phone access | PM2: `jarvis-tunnel` |

---

## Environment Variables (`.env`)

```env
ANTHROPIC_API_KEY=
FLASK_SECRET=
JARVIS_PIN=1704
OLLAMA_MODEL=llama3.2:latest
OLLAMA_URL=http://localhost:11434
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
SPOTIFY_REFRESH_TOKEN=
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
EDGE_TTS_VOICE=en-US-AriaNeural
EDGE_TTS_RATE=+0%
EDGE_TTS_VOLUME=+50%
SAY_VOICE=Samantha
SAY_RATE=185
```

---

## Running the Server

```bash
cd ~/jarvis-nexus
/Library/Frameworks/Python.framework/Versions/3.13/bin/python3 server.py
```

Via PM2 (persistent):
```bash
pm2 start run_nexus.sh --name jarvis
pm2 start /opt/homebrew/bin/cloudflared --name jarvis-tunnel -- tunnel --url http://localhost:5001
pm2 save
```

---

## Key Design Decisions

- **Local-first**: Ollama runs all inference on-device. No reasoning logs sent to cloud.
- **Streaming everywhere**: SSE for chat, agent steps, and tool results — UI updates in real time.
- **TTS local-first**: macOS `say -o` runs in ~0.2s vs. 1–2s for network TTS.
- **Always-on mic**: Voice mode auto-starts on first user interaction — no wake word required to begin.
- **Self-evolving**: JARVIS can write, test, and install new tools into itself, then push to GitHub.
- **Mobile-ready**: Full PWA with iOS safe-area support; accessible anywhere via Cloudflare tunnel.
