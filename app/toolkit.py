"""
JARVIS Toolkit — executable tools the brain can invoke via [TOOL: args] syntax.
"""
import os
import re
import json
import random
import threading
import time
import subprocess
import urllib.request
import urllib.parse
import webbrowser
from pathlib import Path

import os_adapter

MEMORY_FILE = Path(__file__).parent.parent / "data" / "jarvis_memory.json"
MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

# Local JSON-backed notes/reminders — the cross-platform path NOTES/REMIND
# use outside macOS, since there's no Notes.app/Reminders.app to bind to.
_LOCAL_NOTES_FILE = Path(__file__).parent.parent / "data" / "local_notes.json"


def _load_local_store():
    try:
        return json.loads(_LOCAL_NOTES_FILE.read_text())
    except Exception:
        return {"notes": [], "reminders": []}


def _save_local_store(data):
    _LOCAL_NOTES_FILE.write_text(json.dumps(data, indent=2))

# Optional, user-provided config for PROJECT_STATUS/WEBSCOUT — gitignored, so
# a fresh clone has none of this until the user fills in their own
# config/projects.json (see config/projects.example.json for the shape).
_PROJECTS_CONFIG_FILE = Path(__file__).parent.parent / "config" / "projects.json"


def _load_projects_config():
    try:
        return json.loads(_PROJECTS_CONFIG_FILE.read_text())
    except Exception:
        return {}

# Commands allowed in SHELL tool (prefix whitelist)
_SAFE_PREFIXES = (
    "ls", "cat", "pwd", "date", "echo", "python3", "python",
    "df", "du", "uname", "whoami", "uptime", "ps", "top -l 1",
    "find", "grep", "head", "tail", "wc", "osascript",
    "open", "say", "afplay", "curl",
)

# The colon+arg is optional — several tools are documented in the system
# prompt as bare, argument-less brackets ([JOKE], [FACT], [QUOTE], [RANDOM],
# action tools like [LOCK]/[EMPTY_TRASH], etc). Requiring a colon meant any
# model output that correctly followed that no-arg format (e.g. a literal
# "[FACT]") silently failed to match at all — no tool executed, no
# synthesis ran, and the user got total silence with no error and no
# indication anything had gone wrong.
TOOL_PATTERN = re.compile(r'\[([A-Z_]+)(?::\s*(.*?))?\]', re.DOTALL)

# Fallback for weaker models (small local Ollama models, mainly) that
# sometimes drop the required [...] brackets around a tool call — writing
# "MEMORY_SET: key=value" or "NOTES: buy milk" as bare text on its own line
# instead of "[MEMORY_SET: key=value]", despite the prompt showing the
# bracket format explicitly. Observed repeatedly with NOTES/MEMORY_SET/
# TEXT_CASE on llama3.2; without this, the call silently leaks as prose and
# nothing happens. Only matches a real, known tool name at the start of a
# line, and only outside spans already matched by TOOL_PATTERN, so a normal
# bracketed call is never double-counted.
#
# Deliberately excludes anything consequential enough that an accidental
# false-positive match (a stray capitalized line that happens to equal a
# tool name) would be a real problem rather than a harmless no-op/query —
# destructive actions, real outbound messages/calls, and self-modification
# all require the real bracket syntax, no fallback.
_UNBRACKETED_DENYLIST = frozenset({
    "EMPTY_TRASH", "LOCK", "SLEEP", "SHELL",
    "SELF_PUSH", "SELF_HEAL", "SELF_UPGRADE",
    "MESSAGES", "EMAIL", "FACETIME", "WHATSAPP", "CALL_ME",
})
_UNBRACKETED_PATTERN = re.compile(r'^([A-Z][A-Z_]+)(?::[ \t]*(.*))?$', re.MULTILINE)
_KNOWN_TOOLS = frozenset({
    "AIRPLAY", "ALARM", "APP_LIST", "ARP_SCAN", "AXIS", "BASE64", "BATTERY_SAVER",
    "BLUETOOTH", "BRIGHTNESS", "BROWSER_JS", "BROWSER_NAV", "CALC", "CALENDAR",
    "CALL_ME", "CLEANUP_DELETE", "CLEANUP_SCAN", "CLIPBOARD", "CLIPBOARD_CLEAR",
    "CLIPBOARD_HIST", "COLOR", "CONTACTS", "CONVERT", "COUNTDOWN", "CRYPTO",
    "CURRENCY", "DARKMODE", "DEFINE", "DIFF", "DISCARD_DRAFT", "DRAFT_OUTREACH",
    "EMAIL", "EMPTY_TRASH", "ENCODE", "FACETIME", "FACT", "FILE", "FLIGHT",
    "FOCUS", "FOCUS_MODE", "GITHUB", "HASH", "HOROSCOPE", "HOTKEY", "IP", "JOKE",
    "JSON_FORMAT", "LIST_DRAFTS", "LOCK", "MAPS", "MATRIX", "MEMORY_GET",
    "MEMORY_LIST", "MEMORY_SET", "MESSAGES", "MOVIES", "MUSIC", "NEWS",
    "NEWS_CATEGORY", "NIGHTSHIFT", "NOTES", "NOTIFY", "OCR", "OPEN", "PARSE_DATE",
    "PING", "PODCAST", "POMODORO", "PROCESS", "PROJECT_STATUS", "QR", "QUIT",
    "QUOTE", "RADIO", "RANDOM", "RECIPE", "REMIND", "REMIND_AT", "SCREEN_RECORD",
    "SCREENSHOT", "SEARCH", "SELF_HEAL", "SELF_PUSH", "SELF_UPGRADE",
    "SEND_DRAFT", "SHELL", "SHORTCUT", "SLEEP", "SPEAK", "SPEAK_SLOW",
    "SPEEDTEST", "SPORTS", "SPOTIFY", "STOCKS", "STOPWATCH", "SUMMARIZE",
    "SYSINFO", "TEXT_CASE", "TIME", "TIMER", "TRANSLATE", "TRANSLATE_AUTO",
    "TRIVIA", "TYPE", "UNZIP", "URL_ENCODE", "UUID", "VISION_DO", "VOLUME",
    "WEATHER", "WEBSCOUT", "WHATSAPP", "WIFI", "WIKIPEDIA", "WORD_COUNT",
    "WORD_OF_DAY", "YOUTUBE", "ZIP",
}) - _UNBRACKETED_DENYLIST


def _load_memory():
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_memory(data):
    MEMORY_FILE.write_text(json.dumps(data, indent=2))


def run_tool(name: str, arg: str) -> str:
    name = name.upper()

    if name == "SEARCH":
        return _tool_search(arg.strip())

    elif name == "SHELL":
        return _tool_shell(arg.strip())

    elif name == "OPEN":
        return _tool_open(arg.strip())

    elif name == "NOTIFY":
        return _tool_notify(arg.strip())

    elif name == "MEMORY_SET":
        return _tool_memory_set(arg.strip())

    elif name == "MEMORY_GET":
        return _tool_memory_get(arg.strip())

    elif name == "MEMORY_LIST":
        mem = _load_memory()
        if not mem:
            return "Memory is empty."
        return "\n".join(f"{k}: {v}" for k, v in mem.items())

    elif name == "BROWSER_NAV":
        return _tool_browser_nav(arg.strip())

    elif name == "BROWSER_JS":
        return _tool_browser_js(arg.strip())

    elif name == "VISION_DO":
        # Ollama llava vision agent — replaces Claude-based auto.py for BROWSER_DO
        return _ollama_vision_agent(arg.strip(), focus_app=None)

    elif name == "SPOTIFY":
        return _tool_spotify(arg.strip())

    elif name == "MUSIC":
        return _tool_music(arg.strip())

    elif name == "CLIPBOARD":
        return _tool_clipboard(arg.strip())

    elif name == "SPEAK":
        return _tool_speak(arg.strip())

    elif name == "WEATHER":
        return _tool_weather(arg.strip())

    elif name == "CALC":
        return _tool_calc(arg.strip())

    elif name == "CALENDAR":
        return _tool_calendar(arg.strip())

    elif name == "REMIND":
        return _tool_remind(arg.strip())

    elif name == "NOTES":
        return _tool_notes(arg.strip())

    elif name == "FILE":
        return _tool_file(arg.strip())

    elif name == "VOLUME":
        return _tool_volume(arg.strip())

    elif name == "QUIT":
        return _tool_quit(arg.strip())

    elif name == "TRANSLATE":
        return _tool_translate(arg.strip())

    elif name == "TIMER":
        return _tool_timer(arg.strip())

    elif name == "NEWS":
        return _tool_news(arg.strip())

    elif name == "SCREENSHOT":
        return _tool_screenshot(arg.strip())

    elif name == "SYSINFO":
        return _tool_sysinfo(arg.strip())

    elif name == "BRIGHTNESS":
        return _tool_brightness(arg.strip())

    elif name == "TYPE":
        return _tool_type(arg)

    elif name == "HOTKEY":
        return _tool_hotkey(arg.strip())

    elif name == "EMAIL":
        return _tool_email(arg.strip())

    elif name == "CONTACTS":
        return _tool_contacts(arg.strip())

    elif name == "STOCKS":
        return _tool_stocks(arg.strip())

    elif name == "DEFINE":
        return _tool_define(arg.strip())

    elif name == "CONVERT":
        return _tool_convert(arg.strip())

    elif name == "TIME":
        return _tool_time(arg.strip())

    elif name == "YOUTUBE":
        return _tool_youtube(arg.strip())

    elif name == "MAPS":
        return _tool_maps(arg.strip())

    # ── 43 new tools ──────────────────────────────────────────────────────────
    elif name == "LOCK":          return _tool_lock()
    elif name == "SLEEP":         return _tool_sleep_mac()
    elif name == "DARKMODE":      return _tool_darkmode(arg.strip())
    elif name == "WIFI":          return _tool_wifi(arg.strip())
    elif name == "BLUETOOTH":     return _tool_bluetooth(arg.strip())
    elif name == "NIGHTSHIFT":    return _tool_nightshift(arg.strip())
    elif name == "FOCUS":         return _tool_focus(arg.strip())
    elif name == "EMPTY_TRASH":   return _tool_empty_trash()
    elif name == "PROCESS":       return _tool_process(arg.strip())
    elif name == "ZIP":           return _tool_zip(arg.strip())
    elif name == "UNZIP":         return _tool_unzip(arg.strip())
    elif name == "HASH":          return _tool_hash(arg.strip())
    elif name == "WORD_COUNT":    return _tool_word_count(arg)
    elif name == "QR":            return _tool_qr(arg.strip())
    elif name == "UUID":          return _tool_uuid()
    elif name == "MESSAGES":      return _tool_messages(arg.strip())
    elif name == "FACETIME":      return _tool_facetime(arg.strip())
    elif name == "CALL_ME":       return _tool_call_me()
    elif name == "WHATSAPP":      return _tool_whatsapp(arg.strip())
    elif name == "WIKIPEDIA":     return _tool_wikipedia(arg.strip())
    elif name == "SPORTS":        return _tool_sports(arg.strip())
    elif name == "MOVIES":        return _tool_movies(arg.strip())
    elif name == "FLIGHT":        return _tool_flight(arg.strip())
    elif name == "RECIPE":        return _tool_recipe(arg.strip())
    elif name == "JOKE":          return _tool_joke()
    elif name == "QUOTE":         return _tool_quote()
    elif name == "TRIVIA":        return _tool_trivia()
    elif name == "FACT":          return _tool_fact()
    elif name == "WORD_OF_DAY":   return _tool_word_of_day()
    elif name == "IP":            return _tool_ip(arg.strip())
    elif name == "PING":          return _tool_ping(arg.strip())
    elif name == "SPEEDTEST":     return _tool_speedtest()
    elif name == "BASE64":        return _tool_base64(arg.strip())
    elif name == "JSON_FORMAT":   return _tool_json_format(arg)
    elif name == "URL_ENCODE":    return _tool_url_encode(arg.strip())
    elif name == "PARSE_DATE":    return _tool_parse_date(arg.strip())
    elif name == "STOPWATCH":     return _tool_stopwatch(arg.strip())
    elif name == "POMODORO":      return _tool_pomodoro(arg.strip())
    elif name == "SUMMARIZE":     return _tool_summarize(arg)
    elif name == "REMIND_AT":     return _tool_remind_at(arg.strip())
    elif name == "RADIO":         return _tool_radio(arg.strip())
    elif name == "PODCAST":       return _tool_podcast(arg.strip())
    elif name == "COUNTDOWN":     return _tool_countdown(arg.strip())
    elif name == "RANDOM":        return _tool_random(arg.strip())

    # ── 20 additional tools ───────────────────────────────────────────────────
    elif name == "AIRPLAY":       return _tool_airplay(arg.strip())
    elif name == "FOCUS_MODE":    return _tool_focus_mode(arg.strip())
    elif name == "CLIPBOARD_HIST": return _tool_clipboard_hist()
    elif name == "TEXT_CASE":     return _tool_text_case(arg.strip())
    elif name == "DIFF":          return _tool_diff(arg)
    elif name == "ENCODE":        return _tool_encode(arg.strip())
    elif name == "COLOR":         return _tool_color(arg.strip())
    elif name == "CURRENCY":      return _tool_currency(arg.strip())
    elif name == "HOROSCOPE":     return _tool_horoscope(arg.strip())
    elif name == "GITHUB":        return _tool_github(arg.strip())
    elif name == "CRYPTO":        return _tool_crypto(arg.strip())
    elif name == "NEWS_CATEGORY": return _tool_news_category(arg.strip())
    elif name == "TRANSLATE_AUTO": return _tool_translate_auto(arg.strip())
    elif name == "SPEAK_SLOW":    return _tool_speak_slow(arg.strip())
    elif name == "ALARM":         return _tool_alarm(arg.strip())
    elif name == "BATTERY_SAVER": return _tool_battery_saver(arg.strip())
    elif name == "APP_LIST":      return _tool_app_list()
    elif name == "SHORTCUT":      return _tool_shortcut(arg.strip())
    elif name == "SCREEN_RECORD": return _tool_screen_record(arg.strip())
    elif name == "CLIPBOARD_CLEAR": return _tool_clipboard_clear()

    # ── self-evolution ────────────────────────────────────────────────────────
    elif name == "SELF_UPGRADE":  return _tool_self_upgrade(arg.strip())
    elif name == "SELF_HEAL":     return _tool_self_heal()
    elif name == "SELF_PUSH":     return _tool_self_push(arg.strip())

    elif name == "ARP_SCAN":    return _tool_arp_scan()
    elif name == "OCR":         return _tool_ocr(arg.strip())
    elif name == "MATRIX":      return _tool_matrix()

    elif name == "PROJECT_STATUS":    return _tool_project_status(arg.strip())
    elif name == "WEBSCOUT":          return _tool_webscout(arg.strip())
    elif name == "AXIS":              return _tool_axis(arg.strip())
    elif name == "CLEANUP_SCAN":      return _tool_cleanup_scan(arg.strip())
    elif name == "CLEANUP_DELETE":    return _tool_cleanup_delete(arg.strip())
    elif name == "DRAFT_OUTREACH":    return _tool_draft_outreach(arg.strip())
    elif name == "SEND_DRAFT":        return _tool_send_draft(arg.strip())
    elif name == "LIST_DRAFTS":       return _tool_list_drafts()
    elif name == "DISCARD_DRAFT":     return _tool_discard_draft(arg.strip())

    return f"Unknown tool: {name}"


def extract_tools(text: str):
    """Return list of (name, arg) tuples found in text."""
    # group(2) is None when the bracket had no colon (e.g. "[FACT]") since
    # that whole capture group is now optional — normalize to "" so callers
    # don't have to special-case None vs empty-string args.
    matches = list(TOOL_PATTERN.finditer(text))
    found = [(m.group(1), m.group(2) or "") for m in matches]

    # See _UNBRACKETED_PATTERN's comment — catches tool calls a weak model
    # wrote without the required brackets. Skip any line whose span already
    # overlaps a real bracketed match, so nothing is ever double-counted.
    covered = [(m.start(), m.end()) for m in matches]
    for m in _UNBRACKETED_PATTERN.finditer(text):
        name = m.group(1)
        if name not in _KNOWN_TOOLS:
            continue
        if any(m.start() < e and m.end() > s for s, e in covered):
            continue
        found.append((name, (m.group(2) or "").strip()))

    return found


def _tool_search(query):
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=2))
        if not results:
            return "No results found."
        lines = []
        for r in results:
            lines.append(f"• {r['title']}")
            lines.append(f"  {r['body'][:180]}")
            if r.get('href'):
                lines.append(f"  🔗 {r['href']}")
        return "\n".join(lines)
    except Exception as e:
        return f"Search failed: {e}"


def _tool_shell(cmd):
    # Safety check
    first_word = cmd.split()[0] if cmd.split() else ""
    if not any(cmd.startswith(p) for p in _SAFE_PREFIXES):
        return f"Command not permitted: '{first_word}'. Allowed prefixes: {', '.join(_SAFE_PREFIXES[:8])}..."
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=10
        )
        out = (result.stdout or "") + (result.stderr or "")
        return out.strip()[:1500] or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out after 10s."
    except Exception as e:
        return f"Shell error: {e}"


def _tool_open(app):
    # App-name launching is inherently OS-specific (app names/paths don't
    # generalize) — macOS uses the proven `open -a`; Windows/Linux try the
    # name as a direct command, which works for anything already on PATH
    # but won't resolve a Start-Menu-only app name the way `open -a` does.
    try:
        if os_adapter.IS_MAC:
            subprocess.run(["open", "-a", app], capture_output=True, timeout=5)
        elif os_adapter.IS_WINDOWS:
            os.startfile(app)  # noqa: works for exe names on PATH / registered file types
        else:
            subprocess.Popen([app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Opened {app}."
    except Exception as e:
        return f"Could not open {app}: {e}"


def _tool_notify(msg):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return "Telegram not configured."
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
        return f"Notification sent: {msg}"
    except Exception as e:
        return f"Telegram error: {e}"


def _tool_memory_set(arg):
    # Format: key=value  or  key: value
    for sep in ("=", ":"):
        if sep in arg:
            key, _, val = arg.partition(sep)
            mem = _load_memory()
            mem[key.strip()] = val.strip()
            _save_memory(mem)
            return f"Saved: {key.strip()} = {val.strip()}"
    return "Format: [MEMORY_SET: key=value]"


def _tool_memory_get(key):
    mem = _load_memory()
    val = mem.get(key.strip())
    return f"{key.strip()}: {val}" if val else f"No memory for '{key.strip()}'"


# ── BROWSER TOOLS ──────────────────────────────────────────────────────────────

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

def _browser_see(prompt="What browser page is open? Answer in one short sentence: site name and what's on screen."):
    """Take a small screenshot and describe it with llava (fast at low resolution)."""
    import time, io, base64
    time.sleep(0.5)
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            img.thumbnail((480, 300))   # small = fast llava inference
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=65)
            img_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        return f"Screenshot failed: {e}"

    payload = json.dumps({
        "model": "llava:latest",
        "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
        "stream": False,
        "options": {"num_predict": 80, "temperature": 0},
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data["message"]["content"].strip()
    except Exception as e:
        return f"Vision error: {e}"

def _focus_browser():
    """Bring Chrome or Safari to front using pyautogui."""
    import pyautogui, time
    # Use Cmd+Tab to cycle to Chrome, or just open it
    subprocess.run(["open", "-a", "Google Chrome"], capture_output=True, timeout=5)
    time.sleep(0.5)


def _tool_browser_nav(arg):
    """
    Navigate the browser using open/pyautogui (no AppleScript automation perms needed).
    arg: URL, domain, search query, or command: back / forward / reload / new tab / scroll down / scroll up
    """
    import pyautogui, time
    a = arg.lower().strip()

    _focus_browser()
    time.sleep(0.4)

    # Scroll commands
    if "scroll down" in a:
        n = 5
        m = re.search(r'(\d+)', arg)
        if m: n = int(m.group(1))
        pyautogui.scroll(-n * 300)
        time.sleep(0.3)
        seen = _browser_see("What's visible after scrolling? 1 sentence.")
        return f"Scrolled down. Seeing: {seen}"

    if "scroll up" in a:
        pyautogui.scroll(300)
        time.sleep(0.3)
        seen = _browser_see("What's visible after scrolling? 1 sentence.")
        return f"Scrolled up. Seeing: {seen}"

    # Keyboard shortcuts for navigation
    if a == "back":
        pyautogui.hotkey('command', '[')
        time.sleep(0.8)
        seen = _browser_see()
        return f"Went back. Now seeing: {seen}"

    if a == "forward":
        pyautogui.hotkey('command', ']')
        time.sleep(0.8)
        seen = _browser_see()
        return f"Went forward. Now seeing: {seen}"

    if a == "reload":
        pyautogui.hotkey('command', 'r')
        time.sleep(1.5)
        seen = _browser_see()
        return f"Reloaded. Now seeing: {seen}"

    if a in ("new tab", "new tab"):
        pyautogui.hotkey('command', 't')
        time.sleep(0.4)
        return "Opened new tab."

    if a == "close tab":
        pyautogui.hotkey('command', 'w')
        return "Closed tab."

    # URL or search — use Cmd+L to open address bar
    url = arg.strip()
    if not re.match(r'https?://', url):
        if re.match(r'[\w-]+\.[\w.-]+(/|$)', url):
            url = "https://" + url
        else:
            import urllib.parse
            url = "https://www.google.com/search?q=" + urllib.parse.quote(url)

    webbrowser.open(url)
    time.sleep(2.5)  # let page load
    seen = _browser_see()
    return f"Navigated to {url}\nI see: {seen}"


def _tool_browser_js(code):
    """Type JS into Chrome's console via keyboard (no AppleScript perms needed)."""
    import pyautogui, time
    _focus_browser()
    time.sleep(0.3)
    # Open DevTools console
    pyautogui.hotkey('command', 'option', 'j')
    time.sleep(0.8)
    pyautogui.typewrite(code, interval=0.02)
    pyautogui.press('return')
    time.sleep(0.6)
    # Close DevTools
    pyautogui.hotkey('command', 'option', 'j')
    time.sleep(0.4)
    seen = _browser_see("What changed on screen? 1 sentence.")
    return f"JS executed. Screen: {seen}"


# ── SCREENSHOT ─────────────────────────────────────────────────────────────────

def _tool_screenshot(cmd):
    """Take a screenshot and either describe it with llava or save to Desktop."""
    import io, base64, time as _time
    a = cmd.lower().strip()
    try:
        import mss
        from PIL import Image
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception as e:
        return f"Screenshot failed: {e}"

    if "save" in a or "desktop" in a:
        path = os.path.expanduser(f"~/Desktop/screenshot_{int(_time.time())}.png")
        img.save(path)
        return f"Screenshot saved to {path}"

    # Default: describe with llava
    orig = img.copy()
    orig.thumbnail((800, 500))
    buf = io.BytesIO()
    orig.save(buf, format="JPEG", quality=75)
    img_b64 = base64.b64encode(buf.getvalue()).decode()
    prompt = cmd if cmd and a not in ("describe", "see", "look", "what", "") else \
        "Describe what is on this screen in 2-3 sentences. Be specific about apps and content visible."
    payload = json.dumps({
        "model": "llava:latest",
        "messages": [{"role": "user", "content": prompt, "images": [img_b64]}],
        "stream": False,
        "options": {"num_predict": 150, "temperature": 0},
    }).encode()
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["message"]["content"].strip()
    except Exception as e:
        return f"Vision error: {e}"


# ── SYSINFO ────────────────────────────────────────────────────────────────────

def _tool_sysinfo(cmd):
    """System info: battery, disk, cpu/ram, network, or all. Built on psutil
    (already a dependency) instead of pmset/top/ipconfig — cross-platform."""
    import psutil
    a = cmd.lower().strip() or "all"
    parts = []

    if any(x in a for x in ("battery", "batt", "all")):
        try:
            batt = psutil.sensors_battery()
            if batt:
                status = "charging" if batt.power_plugged else "on battery"
                parts.append(f"🔋 {batt.percent:.0f}% ({status})")
        except Exception:
            pass

    if any(x in a for x in ("disk", "storage", "all")):
        try:
            d = psutil.disk_usage(os.path.expanduser("~"))
            parts.append(f"💾 Disk: {d.total/1e9:.0f}GB total · {d.free/1e9:.0f}GB free · {d.percent:.0f}% used")
        except Exception:
            pass

    if any(x in a for x in ("cpu", "ram", "memory", "all")):
        try:
            cpu = psutil.cpu_percent(interval=0.3)
            mem = psutil.virtual_memory()
            parts.append(f"⚡ CPU: {cpu:.0f}%")
            parts.append(f"🧠 RAM: {mem.used/1e9:.1f}GB used of {mem.total/1e9:.1f}GB ({mem.percent:.0f}%)")
        except Exception:
            pass

    if any(x in a for x in ("network", "ip", "wifi", "all")):
        try:
            import socket
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # no data sent — just picks the outbound interface
            parts.append(f"🌐 Local IP: {s.getsockname()[0]}")
            s.close()
        except Exception:
            pass

    return "\n".join(parts) if parts else "Could not retrieve system info."


# ── BRIGHTNESS ─────────────────────────────────────────────────────────────────

def _tool_brightness(cmd):
    """Control screen brightness. Requires: brew install brightness"""
    import shutil, re as _re
    a = cmd.lower().strip()
    bin_path = shutil.which("brightness")

    if not bin_path:
        # Keyboard fallback for up/down
        try:
            import pyautogui
            if "up" in a:
                pyautogui.press("brightnessup")
                return "Brightness up."
            if "down" in a:
                pyautogui.press("brightnessdown")
                return "Brightness down."
        except Exception:
            pass
        return "Install brightness control: brew install brightness"

    if "get" in a or not a:
        r = subprocess.run([bin_path, "-l"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "Could not read brightness."

    if "up" in a:
        subprocess.run([bin_path, "+0.1"], capture_output=True, timeout=5)
        return "Brightness up."
    if "down" in a:
        subprocess.run([bin_path, "-0.1"], capture_output=True, timeout=5)
        return "Brightness down."

    m = _re.search(r'(\d+)', a)
    if m:
        val = max(0, min(100, int(m.group(1)))) / 100.0
        subprocess.run([bin_path, str(round(val, 2))], capture_output=True, timeout=5)
        return f"Brightness set to {int(val * 100)}%."

    return "Usage: brightness get | brightness 70 | brightness up | brightness down"


# ── TYPE / HOTKEY ──────────────────────────────────────────────────────────────

def _tool_type(text):
    """Type text at the current cursor by pasting from clipboard."""
    import time as _t
    import pyperclip
    old = pyperclip.paste()
    pyperclip.copy(text)
    _t.sleep(0.1)
    try:
        import pyautogui
        pyautogui.hotkey(os_adapter.modifier_key(), "v")
    except Exception:
        if os_adapter.IS_MAC:
            subprocess.run(["osascript", "-e",
                            'tell application "System Events" to keystroke "v" using command down'])
    _t.sleep(0.15)
    # Restore clipboard
    pyperclip.copy(old)
    return f"Typed: {text[:80]}{'...' if len(text) > 80 else ''}"


def _tool_hotkey(combo):
    """Press a keyboard shortcut: cmd+c, cmd+space, ctrl+tab, etc."""
    try:
        import pyautogui
        # "ctrl" is already pyautogui's real key name on every OS (the old
        # code rewrote it to "control", which pyautogui doesn't recognize —
        # silently broke any combo using ctrl). Only "cmd" needs translating,
        # and only on mac — it's "command" there, "ctrl" everywhere else.
        keys = [k.strip().lower().replace("cmd", os_adapter.modifier_key())
                for k in combo.split("+")]
        pyautogui.hotkey(*keys)
        return f"Pressed: {combo}"
    except Exception as e:
        return f"Hotkey error: {e}"


# ── EMAIL ──────────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _email_via_smtp(to, subject, body) -> str:
    """Cross-platform send path — used whenever SMTP_* env vars are set,
    regardless of OS. stdlib only (smtplib), no extra dependency."""
    import smtplib
    from email.mime.text import MIMEText
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER", "")
    pwd  = os.environ.get("SMTP_PASS", "")
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    try:
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return f"Email sent to {to} — {subject}"
    except Exception as e:
        return f"Email error (SMTP): {e}"


def _tool_email(cmd):
    """Send email: to@email.com | Subject | Body
    Uses SMTP (SMTP_HOST/PORT/USER/PASS in .env) if configured — works on
    any OS. Otherwise falls back to macOS Mail.app via AppleScript."""
    parts = [p.strip() for p in cmd.split("|")]
    if len(parts) < 2:
        return "Format: [EMAIL: recipient@email.com | Subject | Body]"
    if not _EMAIL_RE.match(parts[0]):
        # A malformed recipient still makes osascript's `send m` return exit
        # 0 (Mail silently drops it rather than erroring) — without this
        # check, a garbage/hallucinated recipient gets reported as "sent"
        # when nothing was ever delivered.
        return f"Not a valid email address: {parts[0]!r} — nothing sent."
    recipient = parts[0]
    subject_raw = parts[1] if len(parts) > 1 else "Message from JARVIS"
    body_raw = parts[2] if len(parts) > 2 else ""

    if os.environ.get("SMTP_HOST"):
        return _email_via_smtp(recipient, subject_raw, body_raw)

    if (err := os_adapter.require_macos("Sending email without SMTP configured")):
        return err

    to      = recipient.replace('"', '\\"')
    subject = subject_raw.replace('"', '\\"')
    body    = body_raw.replace('"', '\\"').replace("\n", "\\n")
    script = (
        'tell application "Mail"\n'
        f'  set m to make new outgoing message with properties {{subject:"{subject}", content:"{body}", visible:false}}\n'
        f'  tell m\n'
        f'    make new to recipient at end of to recipients with properties {{address:"{to}"}}\n'
        f'  end tell\n'
        f'  send m\n'
        'end tell'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
    if r.returncode == 0:
        return f"Email sent to {recipient} — {subject_raw}"
    return f"Email error: {r.stderr.strip() or 'Mail app not set up'}"


# ── CONTACTS ───────────────────────────────────────────────────────────────────

def _tool_contacts(name):
    """Look up a contact by name in macOS Contacts."""
    if (msg := os_adapter.require_macos("Contacts lookup")):
        return msg
    safe = name.replace('"', '\\"')
    script = (
        'tell application "Contacts"\n'
        f'  set matches to people whose name contains "{safe}"\n'
        '  set res to ""\n'
        '  repeat with p in matches\n'
        '    set res to res & (name of p) & ": "\n'
        '    if (count of phones of p) > 0 then\n'
        '      set res to res & (value of item 1 of phones of p)\n'
        '    end if\n'
        '    if (count of emails of p) > 0 then\n'
        '      set res to res & " | " & (value of item 1 of emails of p)\n'
        '    end if\n'
        '    set res to res & "\\n"\n'
        '  end repeat\n'
        '  return res\n'
        'end tell'
    )
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
    result = r.stdout.strip()
    if result:
        return result
    return f"No contacts found for '{name}'."


# ── CLIPBOARD ──────────────────────────────────────────────────────────────────

def _tool_clipboard(cmd):
    import pyperclip
    a = cmd.lower()
    if a in ("read", "get", "paste", ""):
        content = (pyperclip.paste() or "").strip()
        return f"Clipboard: {content[:800]}" if content else "Clipboard is empty."
    pyperclip.copy(cmd)
    return f"Copied to clipboard: {cmd[:100]}{'...' if len(cmd)>100 else ''}"


# ── SPEAK ──────────────────────────────────────────────────────────────────────

def _tts_play(text):
    """Play TTS on the Mac. ElevenLabs primary, macOS say fallback."""
    import tempfile
    api_key  = os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")
    if api_key:
        try:
            payload = json.dumps({
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            }).encode()
            req = urllib.request.Request(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                data=payload,
                headers={
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as r:
                audio = r.read()
            tmp = tempfile.mktemp(suffix=".mp3")
            with open(tmp, "wb") as f:
                f.write(audio)
            os_adapter.play_audio_file(tmp)
            try:
                os.unlink(tmp)
            except Exception:
                pass
            return
        except Exception:
            pass
    # Fallback
    os_adapter.speak_native(text)


def _tool_speak(text):
    import threading
    threading.Thread(target=_tts_play, args=(text,), daemon=True).start()
    return f"Speaking: {text[:100]}{'...' if len(text)>100 else ''}"


# ── WEATHER ────────────────────────────────────────────────────────────────────

_WMO = {
    0:"Clear sky",1:"Mainly clear",2:"Partly cloudy",3:"Overcast",
    45:"Foggy",48:"Icy fog",51:"Light drizzle",53:"Drizzle",55:"Heavy drizzle",
    61:"Light rain",63:"Rain",65:"Heavy rain",71:"Light snow",73:"Snow",75:"Heavy snow",
    80:"Rain showers",81:"Showers",82:"Heavy showers",85:"Snow showers",86:"Heavy snow showers",
    95:"Thunderstorm",96:"Thunderstorm+hail",99:"Heavy thunderstorm+hail",
}

def _tool_weather(location):
    """Rich weather: current conditions + 3-day forecast via Open-Meteo (free, no key)."""
    import urllib.parse
    loc = location.strip() or "auto"

    # Geocode the location
    try:
        if loc == "auto":
            # Get IP-based location
            req = urllib.request.Request("https://ipapi.co/json/",
                                         headers={"User-Agent": "JARVIS/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                geo = json.loads(r.read())
            lat, lon = geo["latitude"], geo["longitude"]
            city = geo.get("city", "Your Location")
        else:
            enc = urllib.parse.quote(loc)
            req = urllib.request.Request(
                f"https://geocoding-api.open-meteo.com/v1/search?name={enc}&count=1&language=en&format=json")
            with urllib.request.urlopen(req, timeout=6) as r:
                geo = json.loads(r.read())
            if not geo.get("results"):
                return f"Location not found: {loc}"
            r0 = geo["results"][0]
            lat, lon, city = r0["latitude"], r0["longitude"], r0.get("name", loc)
    except Exception as e:
        return f"Geocoding error: {e}"

    # Fetch weather
    try:
        params = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,wind_speed_10m,weather_code,uv_index",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,wind_speed_10m_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "forecast_days": 3,
            "timezone": "auto",
        })
        with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{params}", timeout=8) as r:
            data = json.loads(r.read())
        c = data["current"]
        d = data["daily"]

        cond    = _WMO.get(c.get("weather_code", 0), "Unknown")
        temp    = c.get("temperature_2m", "?")
        feels   = c.get("apparent_temperature", "?")
        humid   = c.get("relative_humidity_2m", "?")
        wind    = c.get("wind_speed_10m", "?")
        uv      = c.get("uv_index", "?")
        precip  = c.get("precipitation", 0)

        lines = [
            f"📍 {city}",
            f"🌡️  {temp}°F (feels {feels}°F) · {cond}",
            f"💧 Humidity {humid}% · 🌬️ Wind {wind} mph · ☀️ UV {uv}" +
            (f" · 🌧️ {precip}mm precip" if precip else ""),
            "",
            "3-Day Forecast:",
        ]
        days = ["Today", "Tomorrow", "Day 3"]
        for i in range(min(3, len(d.get("time", [])))):
            hi   = d["temperature_2m_max"][i]
            lo   = d["temperature_2m_min"][i]
            code = d["weather_code"][i]
            pop  = d["precipitation_probability_max"][i]
            desc = _WMO.get(code, "")
            lines.append(f"  {days[i]}: {hi}°/{lo}°F · {desc} · {pop}% rain chance")

        return "\n".join(lines)
    except Exception as e:
        return f"Weather fetch error: {e}"


# ── STOCKS / CRYPTO ────────────────────────────────────────────────────────────

def _tool_stocks(query):
    """Get real-time stock or crypto price via Yahoo Finance (no API key needed)."""
    symbol = query.strip().upper().replace(" ", "-")
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=1d"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        meta = data["chart"]["result"][0]["meta"]
        price  = meta.get("regularMarketPrice", "?")
        prev   = meta.get("chartPreviousClose", meta.get("previousClose", None))
        name   = meta.get("longName") or meta.get("shortName") or symbol
        curr   = meta.get("currency", "USD")
        change = f" ({'+' if price > prev else ''}{round(price-prev,2)} / {round((price-prev)/prev*100,2)}%)" if prev else ""
        return f"📈 {name} ({symbol}): {price} {curr}{change}"
    except Exception as e:
        return f"Could not fetch {symbol}: {e}"


# ── DEFINE ─────────────────────────────────────────────────────────────────────

def _tool_define(word):
    """Dictionary definition via dictionaryapi.dev (free, no key)."""
    w = word.strip().lower()
    try:
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(w)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        entry = data[0]
        phonetic = entry.get("phonetic", "")
        lines = [f"📖 {entry['word']}" + (f"  {phonetic}" if phonetic else "")]
        for meaning in entry.get("meanings", [])[:3]:
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])[:2]
            for d in defs:
                lines.append(f"  ({pos}) {d['definition']}")
                if d.get("example"):
                    lines.append(f'    e.g. "{d["example"]}"')
        return "\n".join(lines)
    except Exception as e:
        return f"Definition not found for '{word}'."


# ── CONVERT ────────────────────────────────────────────────────────────────────

_UNIT_MAP = {
    # length
    "miles": ("km", 1.60934), "mile": ("km", 1.60934),
    "km": ("miles", 0.621371), "kilometers": ("miles", 0.621371),
    "feet": ("meters", 0.3048), "ft": ("meters", 0.3048),
    "meters": ("feet", 3.28084), "m": ("feet", 3.28084),
    "inches": ("cm", 2.54), "in": ("cm", 2.54),
    "cm": ("inches", 0.393701),
    # weight
    "lbs": ("kg", 0.453592), "pounds": ("kg", 0.453592),
    "kg": ("lbs", 2.20462), "kilograms": ("lbs", 2.20462),
    "oz": ("grams", 28.3495), "ounces": ("grams", 28.3495),
    "grams": ("oz", 0.035274),
    # volume
    "gallons": ("liters", 3.78541), "gallon": ("liters", 3.78541),
    "liters": ("gallons", 0.264172), "litres": ("gallons", 0.264172),
    "cups": ("ml", 236.588),
    # speed
    "mph": ("kmh", 1.60934), "kmh": ("mph", 0.621371),
}
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₿": "BTC"}

def _tool_convert(cmd):
    """Convert units or currencies: '100 USD to EUR', '5 miles to km', '72°F to C'."""
    import re as _re
    a = cmd.strip()

    # Temperature — accept single letter or full word (celsius/fahrenheit/kelvin)
    _TEMP_ALIAS = {
        "f": "F", "fahrenheit": "F",
        "c": "C", "celsius": "C", "centigrade": "C",
        "k": "K", "kelvin": "K",
    }
    tf = _re.search(
        r'([\d.]+)\s*°?\s*(fahrenheit|celsius|centigrade|kelvin|[FCKfck])\s+to\s+°?\s*(fahrenheit|celsius|centigrade|kelvin|[FCKfck])',
        a, _re.IGNORECASE
    )
    if tf:
        val = float(tf.group(1))
        frm = _TEMP_ALIAS.get(tf.group(2).lower(), tf.group(2).upper())
        to  = _TEMP_ALIAS.get(tf.group(3).lower(), tf.group(3).upper())
        if frm == "F" and to == "C":
            return f"{val}°F = {round((val-32)*5/9, 2)}°C"
        if frm == "C" and to == "F":
            return f"{val}°C = {round(val*9/5+32, 2)}°F"
        if frm == "C" and to == "K":
            return f"{val}°C = {round(val + 273.15, 2)}K"
        if frm == "K" and to == "C":
            return f"{val}K = {round(val - 273.15, 2)}°C"
        if frm == "F" and to == "K":
            return f"{val}°F = {round((val-32)*5/9 + 273.15, 2)}K"
        if frm == "K" and to == "F":
            return f"{val}K = {round((val - 273.15)*9/5 + 32, 2)}°F"

    # Unit conversion
    um = _re.search(r'([\d.]+)\s+(\w+)\s+(?:to|in)\s+(\w+)', a, _re.IGNORECASE)
    if um:
        val, frm, to_unit = float(um.group(1)), um.group(2).lower(), um.group(3).lower()
        if frm in _UNIT_MAP:
            _, factor = _UNIT_MAP[frm]
            target_name, _ = _UNIT_MAP[frm]
            return f"{val} {frm} = {round(val * factor, 4)} {target_name}"

    # Currency conversion
    cm = _re.search(r'([\d.]+)\s*([A-Z$€£¥₿]{1,4})\s+(?:to|in)\s+([A-Z$€£¥₿]{1,4})', a, _re.IGNORECASE)
    if cm:
        val = float(cm.group(1))
        frm = _CURRENCY_SYMBOLS.get(cm.group(2), cm.group(2).upper())
        to  = _CURRENCY_SYMBOLS.get(cm.group(3), cm.group(3).upper())
        try:
            url = f"https://open.er-api.com/v6/latest/{frm}"
            with urllib.request.urlopen(url, timeout=6) as r:
                data = json.loads(r.read())
            rate = data["rates"].get(to)
            if rate:
                return f"{val} {frm} = {round(val * rate, 4)} {to}"
        except Exception:
            pass

    return f"Couldn't parse conversion: '{cmd}'. Try '100 USD to EUR' or '5 miles to km' or '72F to C'."


# ── TIME ───────────────────────────────────────────────────────────────────────

def _tool_time(query):
    """Get current time/date, optionally in a specific timezone or city."""
    from datetime import datetime
    import re as _re
    q = query.strip().lower()

    # Common timezone aliases
    tz_map = {
        "est": "America/New_York", "eastern": "America/New_York", "new york": "America/New_York",
        "cst": "America/Chicago", "central": "America/Chicago", "chicago": "America/Chicago",
        "mst": "America/Denver", "mountain": "America/Denver", "denver": "America/Denver",
        "pst": "America/Los_Angeles", "pacific": "America/Los_Angeles",
        "los angeles": "America/Los_Angeles", "la": "America/Los_Angeles",
        "gmt": "UTC", "utc": "UTC",
        "london": "Europe/London", "paris": "Europe/Paris", "berlin": "Europe/Berlin",
        "tokyo": "Asia/Tokyo", "japan": "Asia/Tokyo",
        "dubai": "Asia/Dubai", "india": "Asia/Kolkata", "ist": "Asia/Kolkata",
        "sydney": "Australia/Sydney", "australia": "Australia/Sydney",
        "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "china": "Asia/Shanghai",
        "moscow": "Europe/Moscow", "brazil": "America/Sao_Paulo",
        "miami": "America/New_York", "atlanta": "America/New_York",
    }
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    tz_name = None
    for alias, tz in tz_map.items():
        if alias in q:
            tz_name = tz
            break

    if not tz_name:
        # local time
        now = datetime.now()
        return now.strftime("🕐 %A, %B %d, %Y — %I:%M %p (local)")

    try:
        now = datetime.now(ZoneInfo(tz_name))
        label = query.strip().title() or tz_name
        return now.strftime(f"🕐 %A, %B %d, %Y — %I:%M %p ({label})")
    except Exception as e:
        return f"Timezone error: {e}"


# ── YOUTUBE ────────────────────────────────────────────────────────────────────

def _tool_youtube(query):
    """Search YouTube and open results in the browser."""
    import urllib.parse
    q = query.strip()
    url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(q)}"
    webbrowser.open(url)
    return f"Opened YouTube search for: {q}"


# ── MAPS ───────────────────────────────────────────────────────────────────────

def _tool_maps(query):
    """Open Apple Maps with directions or a search."""
    import urllib.parse, re as _re
    q = query.strip()
    # Detect "from X to Y" or "directions to X"
    m = _re.search(r'from (.+?) to (.+)', q, _re.IGNORECASE)
    if m:
        origin = urllib.parse.quote(m.group(1).strip())
        dest   = urllib.parse.quote(m.group(2).strip())
        url = f"https://maps.apple.com/?saddr={origin}&daddr={dest}&dirflg=d"
    else:
        dest_raw = _re.sub(r'^(directions? to|navigate to|take me to)\s+', '', q, flags=_re.IGNORECASE).strip()
        url = f"https://maps.apple.com/?daddr={urllib.parse.quote(dest_raw)}&dirflg=d"
    webbrowser.open(url)
    return f"Opening Maps: {q}"


# ── CALCULATOR ─────────────────────────────────────────────────────────────────

def _tool_calc(expr):
    import math, ast
    _safe = {k: getattr(math, k) for k in dir(math) if not k.startswith("_")}
    _safe.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow})
    try:
        tree = ast.parse(expr.strip(), mode="eval")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id not in _safe:
                    return f"Function not allowed: {node.func.id}"
        result = eval(compile(tree, "<calc>", "eval"), {"__builtins__": {}}, _safe)
        return f"{expr} = {result}"
    except Exception as e:
        return f"Calc error: {e}"


# ── CALENDAR ───────────────────────────────────────────────────────────────────

def _tool_calendar(cmd):
    if (msg := os_adapter.require_macos("Calendar")):
        return msg
    a = cmd.strip().lower()
    if not a or a in ("today", "list", "events", "show", "upcoming", "next"):
        script = '''tell application "Calendar"
  set evList to {}
  set nowD to current date
  set endD to nowD + (86400 * 7)
  repeat with cal in calendars
    try
      repeat with ev in (events of cal whose start date >= nowD and start date <= endD)
        set evList to evList & {(summary of ev) & " @ " & ((start date of ev) as text)}
      end repeat
    end try
  end repeat
  if (count of evList) = 0 then return "No events in the next 7 days."
  return evList as text
end tell'''
        return _applescript(script, timeout=20) or "Could not read calendar."
    if a.startswith(("create ", "add ", "new ", "schedule ")):
        title = cmd.split(" ", 1)[1].strip().replace('"', '\\"')
        script = f'''tell application "Calendar"
  tell calendar 1
    make new event at end with properties {{summary:"{title}", start date:(current date) + 3600, end date:(current date) + 7200}}
    return "Created: {title}"
  end tell
end tell'''
        return _applescript(script, timeout=10) or f"Created: {title}"
    return "Calendar: [CALENDAR: list] to see events, [CALENDAR: add <title>] to create."


# ── REMINDERS ──────────────────────────────────────────────────────────────────

def _tool_remind(cmd):
    if not cmd.strip():
        return "Format: [REMIND: task description]"
    title = cmd.strip()
    if not os_adapter.IS_MAC:
        store = _load_local_store()
        store["reminders"].append(title)
        _save_local_store(store)
        return f"Added reminder: {title}"
    title_e = title.replace('"', '\\"')
    script = f'''tell application "Reminders"
  make new reminder at end of default list with properties {{name:"{title_e}"}}
  return "Reminder added: {title_e}"
end tell'''
    return _applescript(script, timeout=10) or f"Added reminder: {title}"


# ── NOTES ──────────────────────────────────────────────────────────────────────

def _tool_notes(cmd):
    a = cmd.strip()
    body = a
    for prefix in ("create ", "add ", "new ", "write "):
        if a.lower().startswith(prefix):
            body = a[len(prefix):].strip()
            break
    title = body[:60].split("\n")[0]
    # Tolerant list-check — the model doesn't always send a bare "list",
    # sometimes "list my notes" or "show notes" etc.
    is_list = a.lower().startswith(("list", "show", "read", "all"))

    if not os_adapter.IS_MAC:
        store = _load_local_store()
        if is_list:
            notes = store["notes"][-10:]
            return "\n".join(f"- {n}" for n in notes) if notes else "No notes found."
        store["notes"].append(body)
        _save_local_store(store)
        return f"Created note: {title}"

    if is_list:
        script = '''tell application "Notes"
  set nList to {}
  repeat with n in (notes of default account)
    set nList to nList & {name of n}
    if (count of nList) >= 10 then exit repeat
  end repeat
  if (count of nList) = 0 then return "No notes found."
  return nList as text
end tell'''
        return _applescript(script, timeout=10) or "No notes found."
    title_e = title.replace('"', '\\"')
    body_e = body.replace('"', '\\"').replace("\n", "\\n")
    script = f'''tell application "Notes"
  make new note at folder "Notes" of default account with properties {{name:"{title_e}", body:"{body_e}"}}
  return "Note created: {title_e}"
end tell'''
    return _applescript(script, timeout=10) or f"Created note: {title}"


# ── FILE ───────────────────────────────────────────────────────────────────────

def _tool_file(cmd):
    a = cmd.strip()
    if a.lower().startswith("read "):
        path_str = a[5:].strip()
        try:
            p = Path(path_str).expanduser()
            if not p.exists():
                return f"File not found: {path_str}"
            content = p.read_text(errors="replace")
            return (content[:2500] + "\n...[truncated]") if len(content) > 2500 else content
        except Exception as e:
            return f"Read error: {e}"
    if a.lower().startswith("write "):
        rest = a[6:].strip()
        if ":" in rest:
            path_str, _, content = rest.partition(":")
            try:
                p = Path(path_str.strip()).expanduser()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content.strip())
                return f"Written {len(content)} chars to {p}."
            except Exception as e:
                return f"Write error: {e}"
        return "Format: [FILE: write ~/Desktop/note.txt: content here]"
    if a.lower().startswith(("list", "ls")):
        parts = a.split(None, 1)
        path_str = parts[1].strip() if len(parts) > 1 else "~"
        try:
            p = Path(path_str).expanduser()
            items = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name))
            lines = [f"{'DIR' if i.is_dir() else '   '} {i.name}" for i in items[:30]]
            return f"{p}:\n" + "\n".join(lines)
        except Exception as e:
            return f"List error: {e}"
    if a.lower().startswith("append "):
        rest = a[7:].strip()
        if ":" in rest:
            path_str, _, content = rest.partition(":")
            try:
                p = Path(path_str.strip()).expanduser()
                with p.open("a") as f:
                    f.write("\n" + content.strip())
                return f"Appended to {p}."
            except Exception as e:
                return f"Append error: {e}"
    return "FILE: read <path> | write <path>: <content> | list <dir> | append <path>: <content>"


# ── VOLUME ─────────────────────────────────────────────────────────────────────

def _tool_volume(cmd):
    a = cmd.strip().lower()
    if not os_adapter.IS_MAC:
        if a in ("get", ""):
            return os_adapter.get_volume()
        try:
            return os_adapter.set_volume(max(0, min(100, int(a))))
        except ValueError:
            return "Volume mute/unmute/up/down aren't supported outside macOS yet — use a 0-100 number or 'get'."
    if a in ("get", ""):
        v = _applescript("output volume of (get volume settings)")
        muted = _applescript("output muted of (get volume settings)")
        return f"Volume: {v}%{' (muted)' if muted == 'true' else ''}"
    if a == "mute":
        _applescript("set volume with output muted")
        return "Muted."
    if a in ("unmute",):
        _applescript("set volume without output muted")
        return "Unmuted."
    if a in ("up", "+"):
        _applescript("set volume output volume ((output volume of (get volume settings)) + 10)")
        return "Volume up."
    if a in ("down", "-"):
        _applescript("set volume output volume ((output volume of (get volume settings)) - 10)")
        return "Volume down."
    try:
        v = max(0, min(100, int(a)))
        _applescript(f"set volume output volume {v}")
        return f"Volume set to {v}%."
    except ValueError:
        return "VOLUME: 0-100 | mute | unmute | up | down | get"


# ── QUIT ───────────────────────────────────────────────────────────────────────

def _tool_quit(app):
    name = app.strip()
    if os_adapter.IS_MAC:
        try:
            _applescript(f'tell application "{name}" to quit', timeout=8)
            return f"Quit {name}."
        except Exception:
            pass
    try:
        if os_adapter.IS_WINDOWS:
            subprocess.run(["taskkill", "/IM", f"{name}.exe", "/F"], capture_output=True)
        else:
            subprocess.run(["pkill", "-f", name], capture_output=True)
        return f"Force-quit {name}."
    except Exception as e:
        return f"Couldn't quit {name}: {e}"


# ── TRANSLATE ──────────────────────────────────────────────────────────────────

def _tool_translate(cmd):
    import urllib.parse
    parts = cmd.split("|", 1)
    text = parts[0].strip()
    target = "en"
    if len(parts) > 1:
        hint = parts[1].strip().lower()
        _langs = {
            "english": "en", "spanish": "es", "french": "fr", "german": "de",
            "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
            "japanese": "ja", "korean": "ko", "chinese": "zh", "arabic": "ar",
            "hindi": "hi", "turkish": "tr", "polish": "pl", "swedish": "sv",
        }
        for lang_name, code in _langs.items():
            if lang_name in hint:
                target = code
                break
        else:
            target = hint.replace(" ", "")[:5]
    if not text:
        return "Format: [TRANSLATE: Hello world | Spanish]"
    try:
        # MyMemory raises the free-tier rate limit if a contact email is
        # passed — optional, so a fresh install works without configuring one.
        contact = os.environ.get("TRANSLATE_CONTACT_EMAIL", "").strip()
        url = (f"https://api.mymemory.translated.net/get"
               f"?q={urllib.parse.quote(text)}&langpair=autodetect|{target}")
        if contact:
            url += f"&de={urllib.parse.quote(contact)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return f"[{target.upper()}] {data['responseData']['translatedText']}"
    except Exception as e:
        return f"Translate error: {e}"


# ── TIMER ──────────────────────────────────────────────────────────────────────

def _tool_timer(cmd):
    import threading, time as _time, re
    a = cmd.strip().lower()
    seconds = 0
    for pat, mul in [(r'(\d+)\s*h', 3600), (r'(\d+)\s*m', 60), (r'(\d+)\s*s', 1)]:
        m = re.search(pat, a)
        if m:
            seconds += int(m.group(1)) * mul
    if seconds == 0:
        m = re.search(r'(\d+)', a)
        if m:
            seconds = int(m.group(1)) * 60
    if seconds == 0:
        return "Format: [TIMER: 5 minutes] or [TIMER: 1h30m]"
    label = cmd.strip().replace('"', '\\"')
    def _run():
        _time.sleep(seconds)
        os_adapter.notify("J.A.R.V.I.S.", f"Timer done: {label}", sound=True)
        os_adapter.speak_native(f"Timer complete. {label}")
    threading.Thread(target=_run, daemon=True).start()
    mins, secs = divmod(seconds, 60)
    hrs, mins = divmod(mins, 60)
    readable = (f"{hrs}h {mins}m {secs}s" if hrs else (f"{mins}m {secs}s" if mins else f"{secs}s"))
    return f"Timer set for {readable}. I'll notify you when done."


# ── NEWS ───────────────────────────────────────────────────────────────────────

def _tool_news(query):
    topic = query.strip() if query.strip() else "top headlines today"
    return _tool_search(f"latest news {topic}")


# ── MUSIC TOOLS ────────────────────────────────────────────────────────────────

def _applescript(script, timeout=15):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return r.stdout.strip()


# ── Spotify Web API ───────────────────────────────────────────────────────────

_spotify_access_token = None
_spotify_token_expiry = 0

def _spotify_token():
    """Return a valid Spotify access token, refreshing if needed."""
    global _spotify_access_token, _spotify_token_expiry
    import time, base64, json as _json, urllib.request as _ur, urllib.parse
    if _spotify_access_token and time.time() < _spotify_token_expiry - 60:
        return _spotify_access_token
    cid     = os.environ.get("SPOTIFY_CLIENT_ID", "")
    sec     = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    refresh = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
    if not (cid and sec and refresh):
        return None
    creds = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    body  = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
    req   = _ur.Request("https://accounts.spotify.com/api/token", data=body,
                        headers={"Authorization": f"Basic {creds}",
                                 "Content-Type": "application/x-www-form-urlencoded"})
    resp = _json.loads(_ur.urlopen(req, timeout=10).read())
    _spotify_access_token = resp.get("access_token")
    _spotify_token_expiry = time.time() + resp.get("expires_in", 3600)
    return _spotify_access_token

def _spotify_api(method, path, data=None):
    """Make an authenticated Spotify Web API call. Returns parsed JSON or None."""
    import json as _json, urllib.request as _ur
    token = _spotify_token()
    if not token:
        return None
    url = f"https://api.spotify.com/v1{path}"
    body = _json.dumps(data).encode() if data else None
    req  = _ur.Request(url, data=body, method=method,
                       headers={"Authorization": f"Bearer {token}",
                                "Content-Type": "application/json"})
    try:
        resp = _ur.urlopen(req, timeout=10)
        raw  = resp.read()
        return _json.loads(raw) if raw else {}
    except Exception:
        return None

def _spotify_get_playlists():
    """Fetch all of the user's playlists (handles pagination)."""
    playlists = []
    path = "/me/playlists?limit=50"
    while path:
        result = _spotify_api("GET", path)
        if not result:
            break
        playlists.extend(result.get("items", []))
        next_url = result.get("next")
        if next_url:
            path = next_url.replace("https://api.spotify.com/v1", "")
        else:
            path = None
    return [p for p in playlists if p]

def _spotify_find_playlist(name):
    """Fuzzy-find a playlist by name. Returns playlist dict or None."""
    playlists = _spotify_get_playlists()
    if not playlists:
        return None
    name_lower = name.lower().strip()
    # Exact match first
    for p in playlists:
        if p.get("name", "").lower() == name_lower:
            return p
    # Starts-with match
    for p in playlists:
        if p.get("name", "").lower().startswith(name_lower):
            return p
    # Contains match
    for p in playlists:
        if name_lower in p.get("name", "").lower():
            return p
    # Word-by-word: all query words present
    words = name_lower.split()
    for p in playlists:
        pn = p.get("name", "").lower()
        if all(w in pn for w in words):
            return p
    return None

def _spotify_play_context(uri, shuffle=False):
    """Start playback of a context URI (playlist/album). Returns status string."""
    import time
    # Make sure Spotify desktop app is running so a device is available
    subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=5)
    time.sleep(0.8)

    # Get active device — retry once if none found (app just launched)
    for attempt in range(2):
        devices = _spotify_api("GET", "/me/player/devices")
        device_id = None
        if devices and devices.get("devices"):
            active = [d for d in devices["devices"] if d.get("is_active")]
            device_id = (active or devices["devices"])[0]["id"]
        if device_id:
            break
        time.sleep(1.0)  # wait for Spotify to register as a device

    # Enable shuffle before playing if requested
    if shuffle and device_id:
        _spotify_api("PUT", f"/me/player/shuffle?state=true&device_id={device_id}")
        time.sleep(0.2)

    payload = {"context_uri": uri}
    if device_id:
        payload["device_id"] = device_id

    result = _spotify_api("PUT", "/me/player/play", payload)
    return result  # None on success (204), dict on error


# ── Spotify AppleScript ───────────────────────────────────────────────────────

def _spotify_as(script, timeout=8):
    """Run an AppleScript command against Spotify, return stdout or None."""
    r = subprocess.run(
        ["osascript", "-e", f'tell application "Spotify" to {script}'],
        capture_output=True, text=True, timeout=timeout
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _spotify_now_playing():
    """Return 'Song - Artist' using a single AppleScript call (faster than two calls)."""
    try:
        script = (
            'tell application "Spotify"\n'
            '  set t to name of current track\n'
            '  set a to artist of current track\n'
            '  return t & " — " & a\n'
            'end tell'
        )
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=4)
        result = r.stdout.strip()
        if result:
            return f"Now playing: {result}"
        return "Nothing playing."
    except Exception:
        return "Could not read now-playing info."


def _ollama_vision_agent(goal, focus_app=None, max_steps=20):
    """
    Claude Computer Use-style vision agent — Ollama only, no Anthropic API.

    Two-model pipeline per step (mirrors how Claude Computer Use works):
      1. llava  → PERCEIVE: describe screen contents and locate elements
      2. llama3 → THINK: reason from description + full step history → pick action
      3. Execute the action (CLICK/TYPE/KEY/SCROLL/DRAG/RIGHTCLICK/DOUBLECLICK/WAIT)
      4. VERIFY: compare screen hash — detect stalls and self-correct

    Maintains full action history so llama3 never repeats a failed move.
    """
    import pyautogui, time, io, base64, re as _re, hashlib
    from safety_perimeter import check_perimeter as _check_perimeter

    LLAVA_MODEL  = "llava:latest"
    BRAIN_MODEL  = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
    OLLAMA_URL_L = os.environ.get("OLLAMA_URL", "http://localhost:11434")

    if focus_app:
        subprocess.run(["open", "-a", focus_app], capture_output=True, timeout=5)
        time.sleep(1.5)

    app_ctx = (f"You are controlling the {focus_app} app on macOS."
               if focus_app else "You are controlling a macOS desktop.")

    orig_w = orig_h = thumb_w = thumb_h = 1
    last_hash = None
    consecutive_stalls = 0
    step_history: list[str] = []  # running log for llama3

    # ── helpers ──────────────────────────────────────────────────────────────

    def _capture():
        nonlocal orig_w, orig_h, thumb_w, thumb_h
        import mss
        from PIL import Image
        with mss.mss() as sct:
            raw = sct.grab(sct.monitors[1])
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            orig_w, orig_h = img.size
            img.thumbnail((900, 560))
            thumb_w, thumb_h = img.size
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=80)
            data = buf.getvalue()
            return base64.b64encode(data).decode(), hashlib.md5(data).hexdigest()

    def _ollama(model, messages, tokens=300):
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": tokens, "temperature": 0, "num_ctx": 2048},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL_L}/api/chat", data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read())["message"]["content"].strip()

    def _perceive(img_b64, step_n):
        """Phase 1 — llava describes what it sees."""
        prompt = (
            f"Step {step_n}. Goal: {goal}\n"
            "Describe the screen precisely:\n"
            "1. What application/window is open?\n"
            "2. What relevant UI elements are visible (buttons, text fields, lists, menus)?\n"
            "3. Where is the element needed for the goal? Give approximate x,y pixel coords on this "
            f"{thumb_w}x{thumb_h} image.\n"
            "4. Is there a loading spinner, dialog, or pop-up blocking interaction?\n"
            "Be concise — 4-6 sentences max."
        )
        return _ollama(LLAVA_MODEL,
                       [{"role": "user", "content": prompt, "images": [img_b64]}],
                       tokens=200)

    def _think(description, step_n, stalled):
        """Phase 2 — llama3 reasons from description + history → action."""
        history_block = (
            "\n\nHistory of steps taken so far:\n" + "\n".join(step_history[-8:])
            if step_history else ""
        )
        stall_block = (
            "\n\nWARNING: Screen has not changed. The last action had no effect. "
            "Try a completely different approach: dismiss dialogs (KEY escape), "
            "click a different element, use a keyboard shortcut, or scroll to find the target."
            if stalled else ""
        )
        prompt = (
            f"{app_ctx}\n"
            f"Goal: {goal}\n"
            f"Step {step_n}/{max_steps}. Screen ({thumb_w}x{thumb_h} px).\n\n"
            f"What llava sees:\n{description}"
            f"{history_block}{stall_block}\n\n"
            "Based on the screen description and history, choose exactly ONE action to get closer to the goal.\n"
            "Reply with ONLY this format (one line, nothing else):\n"
            "ACTION: CLICK <x> <y>\n"
            "ACTION: DOUBLECLICK <x> <y>\n"
            "ACTION: RIGHTCLICK <x> <y>\n"
            "ACTION: TYPE <text>\n"
            "ACTION: KEY <key>              (return/escape/tab/space/cmd+a/cmd+f/cmd+l/cmd+v)\n"
            "ACTION: SCROLL <x> <y> <dy>   (dy positive = scroll down, negative = scroll up)\n"
            "ACTION: DRAG <x1> <y1> <x2> <y2>   (click-drag from point A to point B)\n"
            "ACTION: WAIT <seconds>         (wait for app to load — max 5s)\n"
            "ACTION: DONE <reason>          (goal accomplished)\n"
            "ACTION: FAIL <reason>          (truly impossible)\n"
            "One line. No explanation."
        )
        return _ollama(BRAIN_MODEL,
                       [{"role": "user", "content": prompt}],
                       tokens=60)

    def _execute(action_type, action_arg):
        if action_type in ("CLICK", "DOUBLECLICK", "RIGHTCLICK"):
            nums = _re.findall(r'\d+', action_arg)
            if len(nums) >= 2:
                tx, ty = int(nums[0]), int(nums[1])
                ax = int(tx * orig_w / thumb_w)
                ay = int(ty * orig_h / thumb_h)
                if action_type == "DOUBLECLICK":
                    pyautogui.doubleClick(ax, ay)
                elif action_type == "RIGHTCLICK":
                    pyautogui.rightClick(ax, ay)
                else:
                    pyautogui.click(ax, ay)
                time.sleep(0.7)
                return True

        elif action_type == "TYPE":
            allowed, reason = _check_perimeter(text=action_arg)
            if not allowed:
                step_history.append(f"Step blocked: {reason}")
                return False
            old = subprocess.run(["pbpaste"], capture_output=True, text=True).stdout
            subprocess.run(["pbcopy"], input=action_arg, text=True)
            time.sleep(0.05)
            pyautogui.hotkey("command", "v")
            time.sleep(0.3)
            subprocess.run(["pbcopy"], input=old, text=True)
            return True

        elif action_type == "KEY":
            allowed, reason = _check_perimeter(key=action_arg)
            if not allowed:
                step_history.append(f"Step blocked: {reason}")
                return False
            key = action_arg.lower().strip()
            if "cmd+" in key or "command+" in key:
                k = key.split("+")[-1]
                pyautogui.hotkey("command", k)
            elif "ctrl+" in key or "control+" in key:
                k = key.split("+")[-1]
                pyautogui.hotkey("ctrl", k)
            elif "shift+" in key:
                k = key.split("+")[-1]
                pyautogui.hotkey("shift", k)
            else:
                pyautogui.press(key)
            time.sleep(0.5)
            return True

        elif action_type == "SCROLL":
            nums = _re.findall(r'-?\d+', action_arg)
            if len(nums) >= 3:
                tx, ty, dy = int(nums[0]), int(nums[1]), int(nums[2])
                ax = int(tx * orig_w / thumb_w)
                ay = int(ty * orig_h / thumb_h)
                pyautogui.scroll(dy, x=ax, y=ay)
                time.sleep(0.4)
                return True

        elif action_type == "DRAG":
            nums = _re.findall(r'\d+', action_arg)
            if len(nums) >= 4:
                x1, y1, x2, y2 = [int(n) for n in nums[:4]]
                ax1 = int(x1 * orig_w / thumb_w); ay1 = int(y1 * orig_h / thumb_h)
                ax2 = int(x2 * orig_w / thumb_w); ay2 = int(y2 * orig_h / thumb_h)
                pyautogui.drag(ax2 - ax1, ay2 - ay1, duration=0.4, button='left')
                time.sleep(0.5)
                return True

        elif action_type == "WAIT":
            m = _re.search(r'[\d.]+', action_arg)
            secs = float(m.group()) if m else 1.5
            time.sleep(min(secs, 5.0))
            return True

        return False

    # ── main loop ─────────────────────────────────────────────────────────────

    for step in range(1, max_steps + 1):
        try:
            img_b64, screen_hash = _capture()
        except Exception as e:
            return f"Screenshot failed: {e}"

        stalled = (screen_hash == last_hash) and step > 1
        if stalled:
            consecutive_stalls += 1
            # Auto-recovery on first stall: press Escape to dismiss any blocking dialog
            if consecutive_stalls == 1:
                pyautogui.press("escape")
                time.sleep(0.4)
        else:
            consecutive_stalls = 0
        last_hash = screen_hash

        if consecutive_stalls >= 4:
            return (f"Vision agent stuck — screen unchanged for 4 steps. "
                    f"History: {'; '.join(step_history[-4:])}. Goal: {goal}")

        # ── PERCEIVE ─────────────────────────────────────────────────────────
        try:
            description = _perceive(img_b64, step)
        except Exception as e:
            description = f"(vision error: {e})"

        # ── THINK ────────────────────────────────────────────────────────────
        try:
            action_line = _think(description, step, stalled)
        except Exception as e:
            return f"Brain error at step {step}: {e}"

        m = _re.search(r'ACTION:\s+(\w+)\s*(.*)', action_line, _re.IGNORECASE)
        if not m:
            step_history.append(f"Step {step}: (unparseable response — retrying)")
            continue

        action_type = m.group(1).upper()
        action_arg  = m.group(2).strip()

        step_history.append(f"Step {step}: {action_type} {action_arg} | saw: {description[:80]}")

        if action_type == "DONE":
            return f"Done ({step} steps): {action_arg}"
        if action_type == "FAIL":
            return f"Cannot complete: {action_arg}"

        # ── EXECUTE ──────────────────────────────────────────────────────────
        _execute(action_type, action_arg)

        # Brief settle before next capture
        time.sleep(0.3)

    return f"Reached max steps ({max_steps}). Goal: {goal}. Last steps: {'; '.join(step_history[-3:])}"


def _spotify_vision_navigate(goal, max_steps=15):
    """Navigate Spotify using Ollama llava vision — no Claude API."""
    return _ollama_vision_agent(goal, focus_app="Spotify", max_steps=max_steps)


def _spotify_search_and_play(query, shuffle=False):
    """
    Search for a song/artist and play the top result using keyboard only.
    No Spotify API key needed.
    """
    import time

    # Open Spotify with the search already populated
    encoded = urllib.parse.quote(query)
    subprocess.run(["open", "-a", "Spotify", f"spotify:search:{encoded}"],
                   capture_output=True, timeout=5)
    time.sleep(2.5)  # wait for search results to render

    # Strategy 1: cmd+Enter plays the Top Result in most Spotify versions
    script1 = """
tell application "Spotify" to activate
delay 0.5
tell application "System Events"
    keystroke return using {command down}
end tell
"""
    subprocess.run(["osascript", "-e", script1], capture_output=True, timeout=8)
    time.sleep(1.0)

    now = _spotify_now_playing()
    if now and "playing" not in now.lower() or True:
        # Strategy 2: arrow down to first result, then Enter
        script2 = """
tell application "Spotify" to activate
delay 0.3
tell application "System Events"
    tell process "Spotify"
        key code 125
        delay 0.2
        key code 36
    end tell
end tell
"""
        subprocess.run(["osascript", "-e", script2], capture_output=True, timeout=8)
        time.sleep(0.8)
        now = _spotify_now_playing()

    if shuffle:
        _spotify_as("set shuffling to true")

    return now or f"Searching Spotify for '{query}'…"


def _spotify_playlist_vision(name, shuffle=False):
    """
    Find and play a playlist without Web API.
    Searches Spotify for 'playlist:<name>' and plays the top result.
    """
    import time
    encoded = urllib.parse.quote(f"playlist:{name}")
    subprocess.run(["open", "-a", "Spotify", f"spotify:search:{encoded}"],
                   capture_output=True, timeout=5)
    time.sleep(2.5)

    script = f"""
tell application "Spotify" to activate
delay 0.6
tell application "System Events"
    tell process "Spotify"
        key code 48
        delay 0.15
        key code 48
        delay 0.15
        key code 36
    end tell
end tell
"""
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    if shuffle:
        time.sleep(0.5)
        _spotify_as("set shuffling to true")
    time.sleep(0.8)
    return _spotify_now_playing() or f"Opened Spotify search for playlist '{name}'"


def _spotify_web_api_control(cmd: str) -> str:
    """Cross-platform Spotify control via the Web API — used on Windows/Linux
    (and anywhere Web API creds are set) instead of the macOS-only AppleScript
    path below. Covers the common playback commands; anything more elaborate
    (search-and-play, playlist-by-name, vision navigation) stays macOS-only
    for now — those lean on AppleScript/keystroke automation that doesn't
    have a clean Web API equivalent without a larger rewrite."""
    if not _spotify_token():
        return ("Spotify control needs either macOS, or Spotify Web API "
                "credentials (SPOTIFY_CLIENT_ID/SECRET/REFRESH_TOKEN in .env).")
    a = cmd.lower().strip()
    if a in ("play", "resume"):
        _spotify_api("PUT", "/me/player/play")
        return "Spotify playing."
    if a in ("pause", "stop"):
        _spotify_api("PUT", "/me/player/pause")
        return "Spotify paused."
    if a in ("playpause", "toggle"):
        state = _spotify_api("GET", "/me/player")
        if state and state.get("is_playing"):
            _spotify_api("PUT", "/me/player/pause")
            return "Spotify paused."
        _spotify_api("PUT", "/me/player/play")
        return "Spotify playing."
    if a in ("next", "next track", "skip"):
        _spotify_api("POST", "/me/player/next")
        return _spotify_now_playing_web()
    if a in ("previous", "prev", "previous track", "back"):
        _spotify_api("POST", "/me/player/previous")
        return _spotify_now_playing_web()
    if a in ("current", "now playing", "what's playing", "whats playing"):
        return _spotify_now_playing_web()
    if a == "shuffle":
        state = _spotify_api("GET", "/me/player")
        new_val = not (state or {}).get("shuffle_state", False)
        _spotify_api("PUT", f"/me/player/shuffle?state={'true' if new_val else 'false'}")
        return f"Shuffle {'on' if new_val else 'off'}."
    if a == "repeat":
        state = _spotify_api("GET", "/me/player")
        new_val = "off" if (state or {}).get("repeat_state") == "context" else "context"
        _spotify_api("PUT", f"/me/player/repeat?state={new_val}")
        return f"Repeat {'on' if new_val == 'context' else 'off'}."
    vol_match = re.match(r'volume\s+(\d+)', a)
    if vol_match:
        v = max(0, min(100, int(vol_match.group(1))))
        _spotify_api("PUT", f"/me/player/volume?volume_percent={v}")
        return f"Spotify volume set to {v}%."
    return ("That Spotify command needs macOS right now — search-and-play, "
            "playlists-by-name, and vision navigation aren't ported to the "
            "Web API path yet. Play/pause/next/previous/current/shuffle/"
            "repeat/volume all work here.")


def _spotify_now_playing_web() -> str:
    data = _spotify_api("GET", "/me/player/currently-playing")
    if not data or not data.get("item"):
        return "Nothing is currently playing."
    item = data["item"]
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    return f"Now playing: {item.get('name')} — {artists}"


def _tool_spotify(cmd):
    """
    Control Spotify via AppleScript.
    Commands: play, pause, next, previous, current, volume N, shuffle, repeat,
              play/search/find <query>, like, open, URI
    """
    import time, urllib.parse
    if not os_adapter.IS_MAC:
        return _spotify_web_api_control(cmd)
    a = cmd.lower().strip()

    if a in ("play", "resume"):
        _spotify_as("play")
        return "Spotify playing."

    if a in ("pause", "stop"):
        _spotify_as("pause")
        return "Spotify paused."

    if a in ("playpause", "toggle"):
        _spotify_as("playpause")
        state = _spotify_as("player state") or "unknown"
        return f"Spotify {state}."

    if a in ("next", "next track", "skip"):
        _spotify_as("next track")
        time.sleep(0.5)
        return _spotify_now_playing()

    if a in ("previous", "prev", "previous track", "back"):
        _spotify_as("previous track")
        time.sleep(0.5)
        return _spotify_now_playing()

    if a in ("current", "now playing", "what's playing", "whats playing"):
        return _spotify_now_playing()

    if a == "shuffle":
        cur = _spotify_as("shuffling")
        new_val = "false" if cur == "true" else "true"
        _spotify_as(f"set shuffling to {new_val}")
        return f"Shuffle {'on' if new_val == 'true' else 'off'}."

    if a == "repeat":
        cur = _spotify_as("repeating")
        new_val = "false" if cur == "true" else "true"
        _spotify_as(f"set repeating to {new_val}")
        return f"Repeat {'on' if new_val == 'true' else 'off'}."

    if a in ("like", "save", "heart"):
        import pyautogui
        subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=5)
        time.sleep(0.5)
        pyautogui.hotkey("command", "shift", "b")
        return "Liked current track."

    # List playlists — open Your Library in Spotify
    if a in ("list playlists", "list", "playlists", "my playlists", "show playlists"):
        subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=5)
        time.sleep(0.5)
        # cmd+shift+n opens the library / left sidebar in Spotify
        script = """
tell application "Spotify" to activate
delay 0.5
tell application "System Events"
    keystroke "n" using {command down, shift down}
end tell
"""
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=8)
        return "Opened your Spotify library — your playlists are listed on the left."

    # "my <name>" — search for that playlist name
    if a.startswith("my "):
        name = cmd[3:].strip()
        clean = re.sub(r'\s*playlist\s*$', '', name, flags=re.IGNORECASE).strip()
        return _spotify_playlist_vision(clean, shuffle=False)

    # Play playlist: "play playlist <name>" or "playlist <name>"
    for prefix in ("play playlist ", "playlist ", "open playlist ", "start playlist "):
        if a.startswith(prefix):
            name = cmd[len(prefix):].strip()
            return _spotify_playlist_vision(name, shuffle=False)

    # Shuffle playlist: "shuffle playlist <name>" or "shuffle <name>"
    for prefix in ("shuffle playlist ", "shuffle "):
        if a.startswith(prefix):
            name = cmd[len(prefix):].strip()
            if not name:
                _spotify_as("set shuffling to true")
                return "Shuffle enabled."
            return _spotify_playlist_vision(name, shuffle=True)

    # Volume
    vol_match = re.match(r'volume\s+(\d+)', a)
    if vol_match:
        v = max(0, min(100, int(vol_match.group(1))))
        _spotify_as(f"set sound volume to {v}")
        return f"Spotify volume set to {v}%."
    if "volume up" in a:
        cur = int(_spotify_as("sound volume") or "50")
        _spotify_as(f"set sound volume to {min(100, cur + 10)}")
        return f"Volume up → {min(100, cur + 10)}%."
    if "volume down" in a:
        cur = int(_spotify_as("sound volume") or "50")
        _spotify_as(f"set sound volume to {max(0, cur - 10)}")
        return f"Volume down → {max(0, cur - 10)}%."

    # Search and play — open Spotify search then vision-click the first track result
    for prefix in ("play ", "search for ", "search ", "find ", "queue ", "look up "):
        if a.startswith(prefix):
            query = cmd[len(prefix):].strip()
            query = re.sub(r'\s+on\s+spotify\s*$', '', query, flags=re.IGNORECASE).strip()
            return _spotify_search_and_play(query)

    # Direct Spotify URI
    if cmd.strip().startswith("spotify:"):
        subprocess.run(["open", "-a", "Spotify", cmd.strip()], capture_output=True, timeout=5)
        time.sleep(1.5)
        _spotify_as("play")
        time.sleep(0.5)
        return _spotify_now_playing()

    # Open Spotify home
    if a in ("open", "home", "browse"):
        subprocess.run(["open", "-a", "Spotify", "spotify:"], capture_output=True, timeout=5)
        return "Opened Spotify."

    # Ollama vision navigation: "navigate <goal>", "vision <goal>", "look for <x>"
    for prefix in ("navigate ", "vision navigate ", "vision ", "look for ", "find visually "):
        if a.startswith(prefix):
            nav_goal = cmd[len(prefix):].strip()
            return _spotify_vision_navigate(nav_goal)

    # Last resort: treat the whole command as a song/artist search
    if len(cmd.strip()) > 1 and not a.startswith(("navigate", "vision")):
        return _spotify_search_and_play(cmd.strip())

    # Fallback: open Spotify
    subprocess.run(["open", "-a", "Spotify"], capture_output=True, timeout=5)
    return f"Opened Spotify. Unrecognized command: '{cmd}'"


def _tool_music(cmd):
    """
    Control Apple Music via AppleScript.
    Commands: play, pause, next, previous, current, volume N, shuffle, repeat,
              play <song/artist/album/playlist>, search <query>, like, library
    """
    if (msg := os_adapter.require_macos("Apple Music control")):
        return msg
    import time
    a = cmd.lower().strip()

    _applescript('if not application "Music" is running then tell application "Music" to activate')
    time.sleep(0.3)

    if a in ("play", "resume"):
        _applescript('tell application "Music" to play')
        return _music_now_playing()

    if a in ("pause", "stop"):
        _applescript('tell application "Music" to pause')
        return "Apple Music paused."

    if a in ("next", "next track", "skip"):
        _applescript('tell application "Music" to next track')
        time.sleep(0.5)
        return _music_now_playing()

    if a in ("previous", "prev", "previous track", "back"):
        _applescript('tell application "Music" to previous track')
        time.sleep(0.5)
        return _music_now_playing()

    if a in ("current", "now playing", "what's playing", "whats playing"):
        return _music_now_playing()

    if a == "shuffle":
        state = _applescript('tell application "Music" to get shuffle enabled')
        new = "false" if state == "true" else "true"
        _applescript(f'tell application "Music" to set shuffle enabled to {new}')
        return f"Shuffle {'off' if new == 'false' else 'on'}."

    if a in ("like", "love", "heart"):
        _applescript('tell application "Music" to set loved of current track to true')
        track = _applescript('tell application "Music" to get name of current track')
        return f"Loved '{track}'."

    # Volume
    vol_match = re.match(r'volume\s+(\d+)', a)
    if vol_match:
        v = max(0, min(100, int(vol_match.group(1))))
        _applescript(f'tell application "Music" to set sound volume to {v}')
        return f"Apple Music volume set to {v}%."
    if "volume up" in a:
        cur = int(_applescript('tell application "Music" to get sound volume') or "50")
        _applescript(f'tell application "Music" to set sound volume to {min(100, cur+10)}')
        return f"Volume up → {min(100, cur+10)}%."
    if "volume down" in a:
        cur = int(_applescript('tell application "Music" to get sound volume') or "50")
        _applescript(f'tell application "Music" to set sound volume to {max(0, cur-10)}')
        return f"Volume down → {max(0, cur-10)}%."

    # Search library and play
    for prefix in ("play ", "search ", "find "):
        if a.startswith(prefix):
            query = cmd[len(prefix):].strip()
            script = f'''tell application "Music"
  set results to (search playlist "Library" for "{query}")
  if (count of results) > 0 then
    play (item 1 of results)
    return "Playing: " & (name of item 1 of results) & " by " & (artist of item 1 of results)
  else
    return "Not found in library: {query}"
  end if
end tell'''
            result = _applescript(script, timeout=20)
            return result or f"Searched for '{query}' in Apple Music."

    # Open playlist by name
    if a.startswith("playlist "):
        pl = cmd[9:].strip()
        script = f'''tell application "Music"
  try
    play playlist "{pl}"
    return "Playing playlist: {pl}"
  on error
    return "Playlist not found: {pl}"
  end try
end tell'''
        return _applescript(script, timeout=10)

    # Show library / open the app
    if a in ("library", "open", "show"):
        subprocess.run(["open", "-a", "Music"], capture_output=True, timeout=5)
        time.sleep(0.5)
        return "Opened Apple Music."

    subprocess.run(["open", "-a", "Music"], capture_output=True, timeout=5)
    return f"Opened Apple Music. (Unknown command: '{cmd}')"


def _music_now_playing():
    script = '''tell application "Music"
  if player state is playing then
    return (name of current track) & " by " & (artist of current track) & " [" & (album of current track) & "]"
  else if player state is paused then
    return "Paused: " & (name of current track) & " by " & (artist of current track)
  else
    return "Not playing."
  end if
end tell'''
    try:
        return _applescript(script) or "Nothing playing."
    except Exception:
        return "Apple Music not responding."


# ═══════════════════════════════════════════════════════════════════════════════
# 43 NEW TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

# ── SYSTEM ────────────────────────────────────────────────────────────────────

def _tool_lock():
    """Lock the screen."""
    return os_adapter.lock_screen()


def _tool_sleep_mac():
    """Put the system to sleep."""
    return os_adapter.sleep_system()


def _tool_darkmode(cmd):
    """Toggle or set dark/light mode. cmd: on|off|toggle"""
    return os_adapter.set_dark_mode(cmd)


def _tool_wifi(cmd):
    """Turn Wi-Fi on/off or get status. cmd: on|off|status"""
    cmd = cmd.lower().strip()
    if cmd in ("on", "off"):
        if (err := os_adapter.require_macos("Turning Wi-Fi on/off")):
            return err
        subprocess.run(["/usr/sbin/networksetup", "-setairportpower", "en0", cmd], timeout=5)
        return f"Wi-Fi turned {cmd}."
    return os_adapter.wifi_status()


def _tool_bluetooth(cmd):
    """Toggle Bluetooth on/off. cmd: on|off|status"""
    cmd = cmd.lower().strip()

    if not os_adapter.IS_MAC:
        if cmd in ("on", "off"):
            return "Turning Bluetooth on/off isn't supported outside macOS yet."
        if os_adapter.IS_LINUX and os_adapter._which("bluetoothctl"):
            r = os_adapter._run(["bluetoothctl", "show"])
            return "Bluetooth is on." if "Powered: yes" in r.stdout else "Bluetooth is off."
        return "Bluetooth status isn't available on this OS/setup."

    def _blueutil_available():
        import shutil
        return shutil.which("blueutil") is not None

    if cmd == "on":
        if _blueutil_available():
            subprocess.run(["blueutil", "--power", "1"], timeout=5)
        else:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to tell process "ControlCenter" to click menu bar item "Bluetooth" of menu bar 1'],
                timeout=5, capture_output=True
            )
        return "Bluetooth on."
    elif cmd == "off":
        if _blueutil_available():
            subprocess.run(["blueutil", "--power", "0"], timeout=5)
        else:
            subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to tell process "ControlCenter" to click menu bar item "Bluetooth" of menu bar 1'],
                timeout=5, capture_output=True
            )
        return "Bluetooth off."
    else:
        if _blueutil_available():
            r = subprocess.run(["blueutil", "--power"], capture_output=True, text=True, timeout=5)
            state = "on" if r.stdout.strip() == "1" else "off"
            return f"Bluetooth is {state}."
        # Fallback: system_profiler
        try:
            r = subprocess.run(
                ["system_profiler", "SPBluetoothDataType", "-detailLevel", "mini"],
                capture_output=True, text=True, timeout=10
            )
            if "State: On" in r.stdout or "Bluetooth Power: On" in r.stdout:
                return "Bluetooth is on."
            elif "State: Off" in r.stdout or "Bluetooth Power: Off" in r.stdout:
                return "Bluetooth is off."
            return "Bluetooth status: " + (r.stdout[:200] or "unknown")
        except Exception as e:
            return f"Bluetooth status unavailable: {e}"


def _tool_nightshift(cmd):
    """Toggle Night Shift on/off via AppleScript UI."""
    if (msg := os_adapter.require_macos("Night Shift")):
        return msg
    cmd = cmd.lower().strip()
    # Use CoreBrightness via Python (needs Objective-C bridge) or system pref toggle
    action = "enable" if cmd in ("on", "enable") else "disable"
    # Fallback: shell nightlight utility if installed
    try:
        subprocess.run(["nightlight", action], timeout=5)
        return f"Night Shift {action}d."
    except FileNotFoundError:
        return f"Night Shift toggle: install 'nightlight' via npm (npm install -g @johnlindquist/nightlight) or use System Settings manually."


def _tool_focus(cmd):
    """Enable/disable Do Not Disturb focus. cmd: on|off"""
    if (msg := os_adapter.require_macos("Focus mode")):
        return msg
    cmd = cmd.lower().strip()
    # macOS 12+ uses Focus via shortcuts
    if cmd == "on":
        script = 'tell application "System Events" to tell process "ControlCenter" to click menu bar item "Control Center" of menu bar 1'
        subprocess.run(["osascript", "-e", script], timeout=5)
        return "Opened Control Center — toggle Focus manually, or ask me to use a Shortcut."
    else:
        return "Focus mode: use Control Center or System Settings > Focus to toggle."


def _tool_empty_trash():
    """Empty the macOS Trash."""
    if (msg := os_adapter.require_macos("Empty Trash")):
        return msg
    script = 'tell application "Finder" to empty the trash'
    try:
        subprocess.run(["osascript", "-e", script], timeout=15)
        return "Trash emptied."
    except Exception as e:
        return f"Empty trash failed: {e}"


def _tool_process(cmd):
    """List or kill a process. cmd: list | kill <name|pid>"""
    cmd = cmd.strip()
    if cmd.lower().startswith("kill "):
        target = cmd[5:].strip()
        if target.isdigit():
            os.kill(int(target), 9)
            return f"Killed PID {target}."
        else:
            r = subprocess.run(["pkill", "-f", target], capture_output=True, text=True, timeout=5)
            return f"Killed processes matching '{target}'." if r.returncode == 0 else f"No process matching '{target}'."
    else:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
        lines = r.stdout.strip().split("\n")
        # Filter by keyword if provided
        kw = cmd.lower().replace("list", "").strip()
        if kw:
            lines = [l for l in lines if kw in l.lower()]
        return "\n".join(lines[:20]) if lines else "No matching processes."


# ── FILES ─────────────────────────────────────────────────────────────────────

def _tool_zip(cmd):
    """Zip a file or folder. cmd: /path/to/source -> /path/to/dest.zip"""
    import shutil
    parts = [p.strip() for p in cmd.split("->")]
    src = os.path.expanduser(parts[0])
    if len(parts) > 1:
        dst = os.path.expanduser(parts[1].replace(".zip", ""))
    else:
        dst = src + "_archive"
    try:
        out = shutil.make_archive(dst, "zip", os.path.dirname(src), os.path.basename(src))
        return f"Zipped to: {out}"
    except Exception as e:
        return f"Zip failed: {e}"


def _tool_unzip(cmd):
    """Unzip a file. cmd: /path/to/file.zip [-> /destination/]"""
    import zipfile
    parts = [p.strip() for p in cmd.split("->")]
    src = os.path.expanduser(parts[0])
    dst = os.path.expanduser(parts[1]) if len(parts) > 1 else os.path.dirname(src)
    try:
        with zipfile.ZipFile(src, "r") as z:
            z.extractall(dst)
        return f"Unzipped to: {dst}"
    except Exception as e:
        return f"Unzip failed: {e}"


def _tool_hash(cmd):
    """Hash a file or string. cmd: [md5|sha256|sha1] <text or /path>"""
    import hashlib
    parts = cmd.split(None, 1)
    alg = "sha256"
    data_str = cmd
    if len(parts) == 2 and parts[0].lower() in ("md5", "sha256", "sha1", "sha512"):
        alg, data_str = parts[0].lower(), parts[1]
    h = hashlib.new(alg)
    path = os.path.expanduser(data_str.strip())
    if os.path.isfile(path):
        with open(path, "rb") as f:
            h.update(f.read())
        return f"{alg.upper()} of file: {h.hexdigest()}"
    else:
        h.update(data_str.encode())
        return f"{alg.upper()}: {h.hexdigest()}"


def _tool_word_count(text):
    """Count words, characters, and lines in text."""
    lines = text.splitlines()
    words = text.split()
    return f"Words: {len(words)} | Characters: {len(text)} | Lines: {len(lines)}"


def _tool_qr(text):
    """Generate a QR code and save to Desktop."""
    try:
        import qrcode
        img = qrcode.make(text)
        path = os.path.expanduser(f"~/Desktop/qr_{int(time.time())}.png")
        img.save(path)
        os_adapter.open_path(path)
        return f"QR code saved to: {path}"
    except ImportError:
        return "Install qrcode: pip install qrcode[pil]"
    except Exception as e:
        return f"QR failed: {e}"


def _tool_uuid():
    """Generate a random UUID."""
    import uuid
    return str(uuid.uuid4())


# ── COMMUNICATION ─────────────────────────────────────────────────────────────

def _tool_messages(cmd):
    """Send an iMessage. cmd: <contact> | <message>"""
    if (err := os_adapter.require_macos("iMessage")):
        return err
    parts = [p.strip() for p in cmd.split("|", 1)]
    if len(parts) < 2:
        return "Usage: MESSAGES: contact | message text"
    contact, msg = parts
    script = f'''tell application "Messages"
  set targetService to 1st service whose service type = iMessage
  set targetBuddy to buddy "{contact}" of targetService
  send "{msg}" to targetBuddy
end tell'''
    try:
        subprocess.run(["osascript", "-e", script], timeout=10)
        return f"iMessage sent to {contact}."
    except Exception as e:
        return f"Messages failed: {e}"


def _tool_facetime(contact):
    """Start a FaceTime call with a contact or phone number."""
    if (err := os_adapter.require_macos("FaceTime")):
        return err
    clean = contact.replace(" ", "")
    subprocess.run(["open", f"facetime://{clean}"], timeout=5)
    return f"Starting FaceTime call with {contact}..."


def _tool_call_me():
    """Places a real phone call via Twilio to MY_PHONE_NUMBER (.env) and
    opens a live two-way conversation — see app/twilio_voice.py."""
    import twilio_voice
    return twilio_voice.initiate_call()


def _tool_whatsapp(cmd):
    """Open WhatsApp with a contact or open to a number. cmd: <phone number or name>"""
    clean = cmd.replace("+", "").replace(" ", "").replace("-", "")
    if clean.isdigit():
        # whatsapp:// is a registered URL scheme on Mac/Windows WhatsApp
        # Desktop installs — webbrowser.open hands it to the OS either way.
        webbrowser.open(f"whatsapp://send?phone={clean}")
        return f"Opening WhatsApp to {cmd}..."
    if (err := os_adapter.require_macos("Opening WhatsApp without a number")):
        return err
    subprocess.run(["open", "-a", "WhatsApp"], timeout=5)
    return "Opened WhatsApp — search for the contact manually."


# ── INFORMATION ───────────────────────────────────────────────────────────────

def _tool_wikipedia(query):
    """Search Wikipedia and return the intro paragraph."""
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        title = data.get("title", query)
        extract = data.get("extract", "No summary available.")
        return f"**{title}**\n{extract}"
    except Exception as e:
        return f"Wikipedia error: {e}"


def _tool_sports(query):
    """Get sports scores/standings via web search."""
    try:
        url = f"https://api.duckduckgo.com/?q={urllib.parse.quote(query + ' score today')}&format=json&no_redirect=1"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        answer = data.get("Answer", "") or data.get("AbstractText", "")
        if answer:
            return answer
        # Fallback: open sports search
        webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
        return f"Opened browser for: {query}"
    except Exception as e:
        return f"Sports lookup failed: {e}"


def _tool_movies(query):
    """Look up a movie — rating, year, plot — via OMDb."""
    try:
        url = f"https://www.omdbapi.com/?t={urllib.parse.quote(query)}&apikey=trilogy"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if data.get("Response") == "True":
            return (f"**{data['Title']}** ({data['Year']}) — {data['Rated']}\n"
                    f"IMDb: {data.get('imdbRating','N/A')} | Runtime: {data.get('Runtime','?')}\n"
                    f"{data.get('Plot','')}")
        # OMDb fallback with no key — open Google
        webbrowser.open(f"https://www.google.com/search?q={urllib.parse.quote(query + ' movie')}")
        return f"Searched Google for movie: {query}"
    except Exception as e:
        return f"Movie lookup failed: {e}"


def _tool_flight(query):
    """Look up a flight status — opens Google Flights."""
    url = f"https://www.google.com/search?q={urllib.parse.quote('flight ' + query)}"
    webbrowser.open(url)
    return f"Opened Google search for flight: {query}"


def _tool_recipe(query):
    """Find a recipe — opens search."""
    url = f"https://www.google.com/search?q={urllib.parse.quote(query + ' recipe')}"
    webbrowser.open(url)
    return f"Opened recipe search for: {query}"


def _tool_joke():
    """Get a random joke from JokeAPI."""
    try:
        url = "https://v2.jokeapi.dev/joke/Any?blacklistFlags=nsfw,racist,sexist&type=single"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if data.get("type") == "single":
            return data["joke"]
        return f"{data['setup']}\n...{data['delivery']}"
    except Exception as e:
        return f"Joke API error: {e}"


def _tool_quote():
    """Get an inspirational quote."""
    try:
        url = "https://zenquotes.io/api/random"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        q = data[0]
        return f'"{q["q"]}" — {q["a"]}'
    except Exception as e:
        return f"Quote error: {e}"


def _tool_trivia():
    """Get a random trivia question."""
    try:
        url = "https://opentdb.com/api.php?amount=1&type=multiple"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        item = data["results"][0]
        import html
        q = html.unescape(item["question"])
        correct = html.unescape(item["correct_answer"])
        wrong = [html.unescape(a) for a in item["incorrect_answers"]]
        import random
        answers = wrong + [correct]
        random.shuffle(answers)
        opts = "\n".join(f"  {chr(65+i)}) {a}" for i, a in enumerate(answers))
        return f"**Trivia** ({item['category']} — {item['difficulty']})\n{q}\n{opts}\n\nAnswer: {correct}"
    except Exception as e:
        return f"Trivia error: {e}"


def _tool_fact():
    """Get a random interesting fact."""
    try:
        url = "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        return data.get("text", "No fact found.")
    except Exception as e:
        return f"Fact error: {e}"


def _tool_word_of_day():
    """Get the word of the day via Wordnik free API."""
    try:
        # Primary: Wordnik word of the day (no auth needed for this endpoint)
        url = "https://api.wordnik.com/v4/words.json/wordOfTheDay?api_key=a2a73e7b947cedad81991624"
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        word = data.get("word", "")
        definitions = data.get("definitions", [])
        defn = definitions[0].get("text", "") if definitions else ""
        note = data.get("note", "")
        parts = [f"**Word of the Day: {word.title()}**"]
        if defn:
            parts.append(defn)
        if note:
            parts.append(f"Note: {note[:200]}")
        return "\n".join(parts)
    except Exception:
        pass
    # Fallback: random word from random-word-api
    try:
        url = "https://random-word-api.herokuapp.com/word?number=1"
        req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            words = json.loads(r.read())
        word = words[0] if words else "serendipity"
    except Exception as e:
        return f"Word of the day error: {e}"
    # Define it — best-effort; a dead/slow dictionary API shouldn't throw away
    # a perfectly good word we already have.
    try:
        def_url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
        req2 = urllib.request.Request(def_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=6) as r2:
            ddata = json.loads(r2.read())
        defn = ddata[0]["meanings"][0]["definitions"][0]["definition"]
        return f"**Word of the Day: {word.title()}**\n{defn}"
    except Exception:
        return f"**Word of the Day: {word.title()}**"


# ── NETWORK / DEV ─────────────────────────────────────────────────────────────

def _tool_ip(cmd):
    """Get public or local IP address. cmd: public|local|blank"""
    cmd = cmd.lower().strip()
    try:
        if cmd in ("public", "external", ""):
            with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=5) as r:
                data = json.loads(r.read())
            return f"Public IP: {data['ip']}"
        else:
            import socket
            hostname = socket.gethostname()
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))  # no data sent — just picks the outbound interface
            local_ip = s.getsockname()[0]
            s.close()
            return f"Local IP: {local_ip} | Hostname: {hostname}"
    except Exception as e:
        return f"IP lookup error: {e}"


def _tool_ping(host):
    """Ping a host and return latency. cmd: hostname or IP"""
    host = host.strip() or "8.8.8.8"
    lines = os_adapter.ping(host).split("\n")
    return "\n".join(lines[-3:]) if lines else "Ping failed."


def _tool_speedtest():
    """Run a speed test via fast.com or open speedtest.net."""
    try:
        # Try using speedtest-cli if installed
        r = subprocess.run(["speedtest", "--simple"], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return r.stdout.strip()
    except FileNotFoundError:
        pass
    # Fallback: open fast.com
    webbrowser.open("https://fast.com")
    return "Opened fast.com for speed test — install speedtest-cli for terminal results: pip install speedtest-cli"


def _tool_base64(cmd):
    """Encode or decode Base64. cmd: encode <text> | decode <text>"""
    import base64
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return "Usage: BASE64: encode <text> | decode <text>"
    action, data = parts[0].lower(), parts[1]
    try:
        if action == "encode":
            return base64.b64encode(data.encode()).decode()
        elif action == "decode":
            return base64.b64decode(data.encode()).decode()
        else:
            return "Usage: encode <text> | decode <text>"
    except Exception as e:
        return f"Base64 error: {e}"


def _tool_json_format(text):
    """Pretty-print or validate JSON."""
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, indent=2)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"


def _tool_url_encode(cmd):
    """URL-encode or decode text. cmd: encode <text> | decode <text>"""
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return urllib.parse.quote(cmd)
    action, data = parts[0].lower(), parts[1]
    if action == "decode":
        return urllib.parse.unquote(data)
    return urllib.parse.quote(data)


def _tool_parse_date(text):
    """Parse and describe a date string in various formats."""
    from datetime import datetime
    formats = [
        "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y",
        "%b %d, %Y", "%Y%m%d", "%d-%m-%Y", "%m-%d-%Y",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(text.strip(), fmt)
            return (f"{dt.strftime('%A, %B %d, %Y')} | "
                    f"Day {dt.timetuple().tm_yday}/365 | "
                    f"Week {dt.isocalendar()[1]}")
        except ValueError:
            continue
    # Try natural English
    try:
        from datetime import date
        today = date.today()
        low = text.lower().strip()
        if low == "today":
            dt = datetime.combine(today, datetime.min.time())
        elif low == "tomorrow":
            from datetime import timedelta
            dt = datetime.combine(today + timedelta(1), datetime.min.time())
        elif low == "yesterday":
            from datetime import timedelta
            dt = datetime.combine(today - timedelta(1), datetime.min.time())
        else:
            return f"Couldn't parse date: '{text}'"
        return f"{dt.strftime('%A, %B %d, %Y')} | Day {dt.timetuple().tm_yday}/365"
    except Exception as e:
        return f"Date parse error: {e}"


# ── PRODUCTIVITY ──────────────────────────────────────────────────────────────

_stopwatch_start: float = 0.0


def _tool_stopwatch(cmd):
    """Start, stop, or read a stopwatch. cmd: start|stop|read"""
    global _stopwatch_start
    cmd = cmd.lower().strip()
    if cmd == "start":
        _stopwatch_start = time.time()
        return "Stopwatch started."
    elif cmd in ("stop", "read", "lap"):
        if _stopwatch_start == 0:
            return "Stopwatch hasn't been started."
        elapsed = time.time() - _stopwatch_start
        mins, secs = divmod(int(elapsed), 60)
        hrs, mins = divmod(mins, 60)
        if cmd == "stop":
            _stopwatch_start = 0.0
        return f"Elapsed: {hrs:02d}:{mins:02d}:{secs:02d}"
    else:
        return "Stopwatch: start | stop | read"


def _tool_pomodoro(cmd):
    """Start a Pomodoro timer. cmd: <minutes> (default 25) or 'break' (5 min)"""
    try:
        if "break" in cmd.lower():
            mins = 5
            label = "Break"
        else:
            mins = int(cmd) if cmd.strip().isdigit() else 25
            label = "Pomodoro"

        def _pom():
            time.sleep(mins * 60)
            msg = f"{label} complete! {mins} minutes done."
            os_adapter.notify("JARVIS Pomodoro", msg)
            try:
                _tts_play(msg)
            except Exception:
                pass

        threading.Thread(target=_pom, daemon=True).start()
        return f"{label} timer started: {mins} minutes."
    except Exception as e:
        return f"Pomodoro error: {e}"


def _tool_summarize(text):
    """Summarize a block of text using Ollama llama3."""
    try:
        payload = json.dumps({
            "model": os.environ.get("OLLAMA_MODEL", "llama3.2:latest"),
            "messages": [
                {"role": "system", "content": "Summarize the following text concisely in 3-5 sentences."},
                {"role": "user", "content": text}
            ],
            "stream": False,
            "options": {"num_predict": 200},
        }).encode()
        req = urllib.request.Request(
            f"{os.environ.get('OLLAMA_URL','http://localhost:11434')}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data["message"]["content"]
    except Exception as e:
        return f"Summarize error: {e}"


def _tool_remind_at(cmd):
    """Set a reminder at a specific time. cmd: HH:MM <message> or <N> minutes <message>"""
    import re
    from datetime import datetime, timedelta

    # Try "X minutes message" pattern
    m = re.match(r"(\d+)\s*min(?:utes?)?\s+(.+)", cmd, re.I)
    if m:
        delay_secs = int(m.group(1)) * 60
        message = m.group(2)
    else:
        # Try "HH:MM message"
        m2 = re.match(r"(\d{1,2}):(\d{2})\s+(.+)", cmd)
        if m2:
            now = datetime.now()
            target = now.replace(hour=int(m2.group(1)), minute=int(m2.group(2)), second=0)
            if target < now:
                target += timedelta(days=1)
            delay_secs = (target - now).total_seconds()
            message = m2.group(3)
        else:
            return "Usage: REMIND_AT: 10 minutes take a break | or: 14:30 meeting starts"

    def _fire(msg, secs):
        time.sleep(secs)
        os_adapter.notify("JARVIS Reminder", msg)
        try:
            _tts_play(f"Reminder: {msg}")
        except Exception:
            pass

    threading.Thread(target=_fire, args=(message, delay_secs), daemon=True).start()
    mins = int(delay_secs // 60)
    return f"Reminder set in {mins} minute{'s' if mins != 1 else ''}: '{message}'"


# ── MEDIA ─────────────────────────────────────────────────────────────────────

_radio_process: "subprocess.Popen | None" = None
_RADIO_STATIONS = {
    "lofi":    "https://streams.ilovemusic.de/iloveradio17.mp3",
    "jazz":    "https://streaming.radio.co/s774887f7b/listen",
    "chill":   "https://streams.ilovemusic.de/iloveradio2.mp3",
    "hiphop":  "https://stream.zeno.fm/f3wvbbqmdg8uv",
    "rock":    "https://stream.zeno.fm/201a0b8ovh8uv",
    "pop":     "https://stream.zeno.fm/r0zy9mf0etquv",
    "news":    "https://npr-ice.streamguys1.com/live.mp3",
    "classical": "https://stream.radio.co/s6f24ea3f1/listen",
    "country": "https://stream.zeno.fm/yn65m159254uv",
}


def _tool_radio(cmd):
    """Play or stop internet radio. cmd: <genre> | stop | list"""
    global _radio_process
    cmd = cmd.lower().strip()

    if cmd in ("stop", "off", "quit"):
        if _radio_process and _radio_process.poll() is None:
            _radio_process.terminate()
            _radio_process = None
        return "Radio stopped."

    if cmd in ("list", "stations"):
        return "Stations: " + ", ".join(_RADIO_STATIONS.keys())

    station = _RADIO_STATIONS.get(cmd)
    if not station:
        # Fuzzy match
        for k in _RADIO_STATIONS:
            if cmd in k or k in cmd:
                station = _RADIO_STATIONS[k]
                cmd = k
                break
    if not station:
        return f"Unknown station '{cmd}'. Available: {', '.join(_RADIO_STATIONS)}"

    # Stop existing
    if _radio_process and _radio_process.poll() is None:
        _radio_process.terminate()

    try:
        _radio_process = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", station],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return f"Playing {cmd} radio. Say 'stop radio' to stop."
    except FileNotFoundError:
        # Fallback: open the stream URL in the browser
        webbrowser.open(station)
        return f"Opened {cmd} radio stream in browser (install ffmpeg for background playback)."


def _tool_podcast(query):
    """Search and open a podcast in the Podcasts app (macOS) or browser."""
    if os_adapter.IS_MAC:
        subprocess.run(["open", "-a", "Podcasts"], timeout=5)
    url = f"https://podcasts.apple.com/search?term={urllib.parse.quote(query)}"
    webbrowser.open(url)
    return f"Opened a podcast search for: {query}"


# ── UTILS ─────────────────────────────────────────────────────────────────────

def _tool_countdown(cmd):
    """Count down from N seconds/minutes and notify. cmd: <N> seconds|minutes [message]"""
    import re
    m = re.match(r"(\d+)\s*(second|minute|sec|min)s?\s*(.*)", cmd, re.I)
    if not m:
        return "Usage: COUNTDOWN: 30 seconds | 5 minutes wake up"
    n, unit, label = int(m.group(1)), m.group(2).lower(), m.group(3).strip()
    secs = n * 60 if "min" in unit else n
    label = label or f"{n} {unit}{'s' if n != 1 else ''}"

    def _fire(msg, delay):
        time.sleep(delay)
        os_adapter.notify("JARVIS Countdown", msg)
        try:
            _tts_play(f"Time's up! {msg}")
        except Exception:
            pass

    threading.Thread(target=_fire, args=(label, secs), daemon=True).start()
    return f"Countdown started: {n} {'minute' if 'min' in unit else 'second'}{'s' if n != 1 else ''} — '{label}'"


def _tool_random(cmd):
    """Random number, pick from list, or coin flip. cmd: <N> | <N>-<M> | pick <a,b,c> | coin"""
    import random
    cmd = cmd.strip().lower()

    if cmd == "coin" or cmd == "flip":
        return random.choice(["Heads", "Tails"])

    if cmd.startswith("pick "):
        items = [x.strip() for x in cmd[5:].split(",")]
        return f"I pick: **{random.choice(items)}**"

    if "-" in cmd:
        parts = cmd.split("-")
        try:
            lo, hi = int(parts[0]), int(parts[1])
            return str(random.randint(lo, hi))
        except ValueError:
            pass

    try:
        n = int(cmd) if cmd else 100
        return str(random.randint(1, n))
    except ValueError:
        return f"Random number between 1 and 100: {random.randint(1,100)}"


# ═══════════════════════════════════════════════════════════════════════════════
# 20 ADDITIONAL TOOLS
# ═══════════════════════════════════════════════════════════════════════════════

def _tool_airplay():
    """Open AirPlay / display preferences."""
    if (err := os_adapter.require_macos("AirPlay settings")):
        return err
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.displays"], timeout=5)
    return "Opened Display & AirPlay settings."


def _tool_focus_mode(cmd):
    """List or activate a macOS Focus mode via Shortcuts. cmd: list | <mode name>"""
    if cmd.lower() == "list":
        modes = ["Do Not Disturb", "Sleep", "Work", "Personal", "Driving", "Reading"]
        return "Available Focus modes: " + ", ".join(modes)
    if (err := os_adapter.require_macos("Focus modes")):
        return err
    try:
        subprocess.run(["shortcuts", "run", f"Enable {cmd} Focus"], timeout=10)
        return f"Activated '{cmd}' Focus mode."
    except Exception:
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.notifications"], timeout=5)
        return f"Opened Focus settings — enable '{cmd}' manually."


def _tool_clipboard_hist():
    """Show clipboard history (JARVIS itself only ever sees the current item —
    full history needs an OS-level clipboard manager)."""
    try:
        import pyperclip
        current = (pyperclip.paste() or "")[:500] or "(empty)"
        hint = ("a clipboard manager like Maccy or Paste" if os_adapter.IS_MAC else
                "a clipboard manager extension for your OS")
        return f"Current clipboard:\n{current}\n\n(For full history, use {hint}.)"
    except Exception as e:
        return f"Clipboard error: {e}"


def _tool_text_case(cmd):
    """Convert text case. cmd: upper/lower/title/camel/snake/kebab <text>"""
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return "Usage: TEXT_CASE: upper Hello World"
    mode, text = parts[0].lower(), parts[1]
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    if mode == "title":
        return text.title()
    if mode == "camel":
        words = re.split(r'[\s_\-]+', text)
        return words[0].lower() + "".join(w.title() for w in words[1:])
    if mode == "snake":
        return re.sub(r'[\s\-]+', '_', text).lower()
    if mode == "kebab":
        return re.sub(r'[\s_]+', '-', text).lower()
    return text


def _tool_diff(cmd):
    """Show diff between two texts separated by '|||'. cmd: text1 ||| text2"""
    import difflib
    parts = cmd.split("|||", 1)
    if len(parts) < 2:
        return "Usage: DIFF: old text ||| new text"
    a, b = parts[0].strip().splitlines(), parts[1].strip().splitlines()
    diff = difflib.unified_diff(a, b, fromfile="original", tofile="modified", lineterm="")
    result = "\n".join(list(diff))
    return result if result else "No differences found."


def _tool_encode(cmd):
    """Encode/decode text. cmd: morse <text> | rot13 <text> | hex encode/decode <text>"""
    parts = cmd.split(None, 1)
    if len(parts) < 2:
        return "Usage: ENCODE: rot13 hello | morse hello | hex encode hello"
    mode, text = parts[0].lower(), parts[1]

    if mode == "rot13":
        import codecs
        return codecs.encode(text, "rot_13")

    if mode == "hex":
        subparts = text.split(None, 1)
        action = subparts[0].lower() if len(subparts) > 1 else "encode"
        data = subparts[1] if len(subparts) > 1 else text
        if action == "decode":
            return bytes.fromhex(data.replace(" ", "")).decode("utf-8", errors="replace")
        return data.encode().hex()

    if mode == "morse":
        MORSE = {
            'A': '.-', 'B': '-...', 'C': '-.-.', 'D': '-..',
            'E': '.', 'F': '..-.', 'G': '--.', 'H': '....',
            'I': '..', 'J': '.---', 'K': '-.-', 'L': '.-..',
            'M': '--', 'N': '-.', 'O': '---', 'P': '.--.',
            'Q': '--.-', 'R': '.-.', 'S': '...', 'T': '-',
            'U': '..-', 'V': '...-', 'W': '.--', 'X': '-..-',
            'Y': '-.--', 'Z': '--..',
            '0': '-----', '1': '.----', '2': '..---', '3': '...--',
            '4': '....-', '5': '.....', '6': '-....', '7': '--...',
            '8': '---..', '9': '----.',
        }
        return " ".join(MORSE.get(c.upper(), "?") for c in text if c.strip())

    return f"Unknown encoding: {mode}"


def _tool_color(cmd):
    """Convert a color. cmd: #hex | rgb(r,g,b) | hsl(h,s,l)"""
    import colorsys
    cmd = cmd.strip()

    # Hex → RGB + HSL
    if cmd.startswith("#"):
        h = cmd.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        hue, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        return (f"HEX: #{h.upper()}\n"
                f"RGB: rgb({r}, {g}, {b})\n"
                f"HSL: hsl({int(hue*360)}, {int(s*100)}%, {int(l*100)}%)")

    # RGB → HEX + HSL
    m = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", cmd, re.I)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hue, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        return (f"HEX: #{r:02X}{g:02X}{b:02X}\n"
                f"RGB: rgb({r}, {g}, {b})\n"
                f"HSL: hsl({int(hue*360)}, {int(s*100)}%, {int(l*100)}%)")

    return f"Usage: COLOR: #FF5733 | rgb(255, 87, 51)"


def _tool_currency(cmd):
    """Convert currency in real time. cmd: 100 USD to EUR"""
    import re
    m = re.match(r"([\d.,]+)\s+([A-Za-z]{3})\s+(?:to\s+)?([A-Za-z]{3})", cmd.strip(), re.IGNORECASE)
    if not m:
        return "Usage: CURRENCY: 100 USD to EUR"
    amount, frm, to = float(m.group(1).replace(",", "")), m.group(2).upper(), m.group(3).upper()
    try:
        url = f"https://open.er-api.com/v6/latest/{frm}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        rate = data["rates"].get(to)
        if rate is None:
            return f"Unknown currency: {to}"
        converted = amount * rate
        return f"{amount:,.2f} {frm} = **{converted:,.2f} {to}** (rate: {rate:.4f})"
    except Exception as e:
        return f"Currency error: {e}"


def _tool_horoscope(sign):
    """Get today's horoscope for a zodiac sign."""
    sign = sign.lower().strip()
    valid = ["aries","taurus","gemini","cancer","leo","virgo",
             "libra","scorpio","sagittarius","capricorn","aquarius","pisces"]
    if sign not in valid:
        return f"Invalid sign. Choose from: {', '.join(valid)}"

    # Primary: aztro horoscope API
    try:
        import urllib.parse
        url = f"https://aztro.sameerkumar.website/?sign={sign}&day=today"
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"User-Agent": "JARVIS/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        desc = data.get("description", "")
        mood = data.get("mood", "")
        lucky = data.get("lucky_number", "")
        color = data.get("color", "")
        compat = data.get("compatibility", "")
        if desc:
            parts = [f"**{sign.title()} — Today**", desc]
            if mood:   parts.append(f"Mood: {mood}")
            if lucky:  parts.append(f"Lucky #: {lucky}")
            if color:  parts.append(f"Color: {color}")
            if compat: parts.append(f"Compatibility: {compat}")
            return "\n".join(parts)
    except Exception:
        pass

    # Fallback: horoscope-app-api vercel
    try:
        url = f"https://horoscope-app-api.vercel.app/api/v1/get-horoscope/daily?sign={sign}&day=TODAY"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        horoscope = data.get("data", {}).get("horoscope_data", "")
        if horoscope:
            return f"**{sign.title()} — Today**\n{horoscope}"
    except Exception:
        pass

    # Last resort: DuckDuckGo search
    try:
        result = _tool_search(f"{sign} horoscope today")
        return f"**{sign.title()} — Today**\n{result[:400]}"
    except Exception as e:
        return f"Horoscope unavailable for {sign}: {e}"


def _tool_github(cmd):
    """Look up a GitHub repo or user. cmd: user/repo | user:<username>"""
    cmd = cmd.strip()
    try:
        if cmd.startswith("user:"):
            username = cmd[5:].strip()
            url = f"https://api.github.com/users/{urllib.parse.quote(username)}"
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0", "Accept": "application/vnd.github.v3+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            return (f"**{d.get('name', d['login'])}** (@{d['login']})\n"
                    f"Repos: {d.get('public_repos',0)} | Followers: {d.get('followers',0)} | Following: {d.get('following',0)}\n"
                    f"Bio: {d.get('bio','')}")
        else:
            url = f"https://api.github.com/repos/{urllib.parse.quote(cmd)}"
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0", "Accept": "application/vnd.github.v3+json"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read())
            return (f"**{d['full_name']}** — ⭐ {d.get('stargazers_count',0)} | 🍴 {d.get('forks_count',0)}\n"
                    f"Language: {d.get('language','?')} | Issues: {d.get('open_issues_count',0)}\n"
                    f"{d.get('description','')}")
    except Exception as e:
        return f"GitHub lookup error: {e}"


def _tool_crypto(query):
    """Get real-time crypto price. cmd: BTC | ETH | bitcoin"""
    ids_map = {
        "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
        "doge": "dogecoin", "ada": "cardano", "xrp": "ripple",
        "bnb": "binancecoin", "dot": "polkadot", "avax": "avalanche-2",
        "matic": "matic-network", "link": "chainlink", "ltc": "litecoin",
    }
    coin_id = ids_map.get(query.lower().strip(), query.lower().strip())
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd&include_24hr_change=true"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        if coin_id not in data:
            return f"Coin not found: {query}. Try the full name (e.g. bitcoin, ethereum)."
        price = data[coin_id]["usd"]
        change = data[coin_id].get("usd_24h_change", 0)
        arrow = "▲" if change >= 0 else "▼"
        return f"**{query.upper()}**: ${price:,.2f} {arrow} {abs(change):.2f}% (24h)"
    except Exception as e:
        return f"Crypto error: {e}"


def _tool_news_category(category):
    """Get top news by category via DuckDuckGo search. cmd: sports|tech|business|health|science|entertainment"""
    cat = category.lower().strip() if category else "general"
    try:
        results = _tool_search(f"latest {cat} news today 2026")
        if results and len(results) > 20:
            return f"**{cat.title()} News:**\n{results}"
    except Exception:
        pass
    # Fallback: open Google News
    webbrowser.open(f"https://news.google.com/search?q={urllib.parse.quote(cat)}")
    return f"Opened Google News for: {cat}"


def _tool_translate_auto(cmd):
    """Auto-detect language and translate. cmd: <text> | <target language>"""
    parts = [p.strip() for p in cmd.split("|")]
    if len(parts) < 2:
        return "Usage: TRANSLATE_AUTO: Hola, ¿cómo estás? | English"
    text, target = parts[0], parts[1]
    try:
        url = (f"https://translate.googleapis.com/translate_a/single"
               f"?client=gtx&sl=auto&tl={urllib.parse.quote(target[:2].lower())}"
               f"&dt=t&q={urllib.parse.quote(text)}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        translated = "".join(part[0] for part in data[0] if part[0])
        src_lang = data[2] if len(data) > 2 else "?"
        return f"[{src_lang} → {target}] {translated}"
    except Exception as e:
        return f"Translation error: {e}"


def _tool_speak_slow(text):
    """Speak text aloud at a slower pace (for clarity)."""
    try:
        threading.Thread(target=os_adapter.speak_native, args=(text, 140), daemon=True).start()
        return f"Speaking slowly: {text}"
    except Exception as e:
        return f"Slow speak error: {e}"


def _tool_alarm(cmd):
    """Set an alarm at a specific time (24h or 12h). cmd: 07:30 | 9:00 AM wake up"""
    from datetime import datetime, timedelta
    import re as _re
    m = _re.match(r"(\d{1,2}):(\d{2})\s*(AM|PM)?\s*(.*)", cmd, _re.I)
    if not m:
        return "Usage: ALARM: 07:30 | 9:00 AM wake up"
    h, mi = int(m.group(1)), int(m.group(2))
    ampm = m.group(3)
    label = m.group(4).strip() or "Alarm"
    if ampm:
        if ampm.upper() == "PM" and h != 12:
            h += 12
        elif ampm.upper() == "AM" and h == 12:
            h = 0
    now = datetime.now()
    target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delay = (target - now).total_seconds()

    def _ring(msg, secs):
        time.sleep(secs)
        os_adapter.notify("JARVIS Alarm", msg, sound=True)
        try:
            _tts_play(f"Alarm! {msg}")
        except Exception:
            pass

    threading.Thread(target=_ring, args=(label, delay), daemon=True).start()
    return f"Alarm set for {target.strftime('%I:%M %p')} — '{label}'"


def _tool_battery_saver(cmd):
    """Toggle Low Power Mode. cmd: on | off | status"""
    cmd = cmd.lower().strip()
    if cmd == "on":
        subprocess.run(["sudo", "pmset", "-a", "lowpowermode", "1"], timeout=5)
        return "Low Power Mode enabled."
    elif cmd == "off":
        subprocess.run(["sudo", "pmset", "-a", "lowpowermode", "0"], timeout=5)
        return "Low Power Mode disabled."
    else:
        r = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()


def _tool_app_list():
    """List installed apps in /Applications."""
    try:
        r = subprocess.run(["ls", "/Applications"], capture_output=True, text=True, timeout=5)
        apps = [a.replace(".app", "") for a in r.stdout.strip().split("\n") if a.endswith(".app")]
        return "Installed Apps:\n" + "\n".join(f"• {a}" for a in sorted(apps))
    except Exception as e:
        return f"App list error: {e}"


def _tool_shortcut(name):
    """Run a macOS Shortcut by name."""
    if (err := os_adapter.require_macos("Shortcuts")):
        return err
    try:
        subprocess.run(["shortcuts", "run", name], timeout=30)
        return f"Ran Shortcut: '{name}'"
    except FileNotFoundError:
        return "Shortcuts CLI not available on this macOS version."
    except Exception as e:
        return f"Shortcut error: {e}"


def _tool_screen_record(cmd):
    """Start or stop a screen recording. cmd: start [filename] | stop"""
    if (err := os_adapter.require_macos("Screen recording")):
        return err
    cmd = cmd.lower().strip()
    if cmd == "stop":
        subprocess.run(["pkill", "-f", "screencapture"], timeout=5)
        return "Screen recording stopped."
    # Start
    filename = os.path.expanduser(f"~/Desktop/recording_{int(time.time())}.mov")
    subprocess.Popen(["screencapture", "-v", filename])
    return f"Screen recording started → {filename} (say 'stop screen recording' to stop)"


def _tool_clipboard_clear():
    """Clear the clipboard."""
    try:
        import pyperclip
        pyperclip.copy("")
        return "Clipboard cleared."
    except Exception as e:
        return f"Clipboard clear error: {e}"


# ── self-evolution ─────────────────────────────────────────────────────────────

def _tool_self_upgrade(description):
    """Generate and install a new tool, push to GitHub."""
    try:
        from self_upgrade import self_upgrade
        return self_upgrade(description)
    except Exception as e:
        return f"Self-upgrade error: {e}"


def _tool_self_heal():
    """Detect broken tools, auto-fix them, push to GitHub."""
    try:
        from self_upgrade import self_heal
        return self_heal()
    except Exception as e:
        return f"Self-heal error: {e}"


def _tool_self_push(message):
    """Commit current JARVIS changes and push to GitHub."""
    try:
        from self_upgrade import self_push
        return self_push(message or "JARVIS manual push")
    except Exception as e:
        return f"Self-push error: {e}"


# ── ARP SCAN ───────────────────────────────────────────────────────────────────

def _tool_arp_scan():
    """Run arp -a and return all devices found on the local network."""
    try:
        result = subprocess.run(["arp", "-a"], capture_output=True, text=True, timeout=10)
        lines = [l for l in result.stdout.strip().splitlines() if l.strip()]
        if not lines:
            return "No ARP entries found."
        return f"Devices on network ({len(lines)} found):\n" + "\n".join(lines)
    except Exception as e:
        return f"ARP scan error: {e}"


# ── OCR ────────────────────────────────────────────────────────────────────────

def _tool_ocr(path=""):
    """Extract text from a screenshot or image file using pytesseract."""
    try:
        import pytesseract
        from PIL import Image as _Img
        import mss as _mss, io as _io

        if path and os.path.exists(path):
            img = _Img.open(path)
        else:
            # Capture the current screen
            with _mss.mss() as sct:
                raw = sct.grab(sct.monitors[1])
                img = _Img.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        text = pytesseract.image_to_string(img).strip()
        return text if text else "No readable text found in the image."
    except ImportError:
        return "pytesseract not installed. Run: pip install pytesseract pillow"
    except Exception as e:
        return f"OCR error: {e}"


# ── MATRIX MODE ────────────────────────────────────────────────────────────────

def _tool_matrix():
    """Open a Terminal window running cmatrix (macOS). Falls back to a Python rain effect."""
    import shutil
    try:
        if shutil.which("cmatrix"):
            subprocess.Popen([
                "osascript", "-e",
                'tell application "Terminal" to do script "cmatrix"',
                "-e", 'tell application "Terminal" to activate',
            ])
            return "Matrix mode activated, sir."
        else:
            # Fallback: brief rain in current terminal via ANSI
            import random, time as _t
            cols = 60
            chars = "01アイウエオカキクケコ"
            lines = []
            for _ in range(12):
                lines.append("\033[32m" + "".join(random.choice(chars) for _ in range(cols)) + "\033[0m")
            return "\n".join(lines) + "\n\n(Install cmatrix for the full effect: brew install cmatrix)"
    except Exception as e:
        return f"Matrix error: {e}"


# PROJECT_STATUS — monitors whatever projects the user has listed in their
# own config/projects.json (see _load_projects_config above). Empty/missing
# by default — a fresh clone has no projects configured until the user adds
# their own, so this never leaks anyone's private project layout.
def _tool_project_status(arg: str) -> str:
    cfg = _load_projects_config()
    projects = cfg.get("projects", {})
    project_logs = cfg.get("project_logs", {})
    pm2_services = cfg.get("pm2_services", {})

    if not projects:
        return ("No projects configured. Add them to config/projects.json "
                "(see config/projects.example.json for the format) to use this tool.")

    lines = []

    # ── PM2 status snapshot ───────────────────────────────────────────────────
    try:
        pm2_raw = subprocess.run(
            ["pm2", "jlist"], capture_output=True, text=True, timeout=8
        )
        pm2_data = json.loads(pm2_raw.stdout) if pm2_raw.returncode == 0 else []
        pm2_map = {p["name"]: p["pm2_env"]["status"] for p in pm2_data}
    except Exception:
        pm2_map = {}

    # ── Per-project scan ──────────────────────────────────────────────────────
    for name, path in projects.items():
        if not os.path.isdir(path):
            lines.append(f"[{name}] ✗ directory not found")
            continue

        row = f"[{name}]"

        # Last commit
        try:
            git_log = subprocess.run(
                ["git", "-C", path, "log", "-1", "--format=%ar|%s"],
                capture_output=True, text=True, timeout=6
            )
            if git_log.returncode == 0 and git_log.stdout.strip():
                when, msg = git_log.stdout.strip().split("|", 1)
                row += f"  last commit {when} — {msg[:55]}"
            else:
                row += "  (no git)"
        except Exception:
            row += "  (git error)"

        # Uncommitted changes
        try:
            git_st = subprocess.run(
                ["git", "-C", path, "status", "--porcelain"],
                capture_output=True, text=True, timeout=6
            )
            changed = [l for l in git_st.stdout.splitlines() if l.strip()]
            if changed:
                row += f"  ⚠ {len(changed)} uncommitted file(s)"
        except Exception:
            pass

        # PM2 services
        if name in pm2_services:
            svc_status = []
            for svc in pm2_services[name]:
                status = pm2_map.get(svc, "unknown")
                icon = "✓" if status == "online" else "✗"
                svc_status.append(f"{icon}{svc}")
            row += "  PM2:" + " ".join(svc_status)

        # Recent errors
        for log_path in project_logs.get(name, []):
            try:
                with open(log_path) as f:
                    tail = f.readlines()[-5:]
                errors = [l.strip() for l in tail if any(
                    kw in l.lower() for kw in ("error", "exception", "traceback", "fatal")
                )]
                if errors:
                    row += f"  ⚠ recent error: {errors[-1][:80]}"
            except Exception:
                pass

        lines.append(row)

    return "\n".join(lines) if lines else "No projects found."


# AXIS — real trading performance (equity, daily/weekly P&L, win rate,
# per-module degradation vs validated baseline). Same data trading_context.py
# already keeps warm on a background thread for the passive financial-context
# injection — this just exposes it as an explicit, callable tool too, since
# the model reaches for a named tool here on its own regardless.
def _tool_axis(arg: str) -> str:
    import trading_context
    summary = trading_context.get_summary()
    return summary if summary else "AXIS backend isn't reachable right now (localhost:8000)."


# CLEANUP_SCAN / CLEANUP_DELETE — on-demand disk cleanup. Two tools by
# design: SCAN is read-only and just reports candidates with IDs; DELETE
# only ever removes IDs that came from the most recent SCAN, so this can't
# be used to delete an arbitrary path the model names on its own — see
# disk_cleanup.py's module docstring for the full rationale (built after a
# live incident earlier this session where a hung/degenerate reply
# triggered a real, unintended tool call).
def _tool_cleanup_scan(arg: str) -> str:
    import disk_cleanup
    candidates = disk_cleanup.scan()
    return disk_cleanup.summarize(candidates)


def _tool_cleanup_delete(arg: str) -> str:
    import disk_cleanup
    if not arg.strip():
        return "Usage: CLEANUP_DELETE 3,7,12 — IDs must come from a CLEANUP_SCAN result, nothing else is accepted."
    try:
        ids = [int(x.strip()) for x in arg.split(",") if x.strip()]
    except ValueError:
        return "IDs must be comma-separated numbers from a CLEANUP_SCAN result, e.g. CLEANUP_DELETE 3,7,12"
    result = disk_cleanup.delete(ids)
    if not result["ok"]:
        return result["error"]
    lines = [f"Freed {result['freed_human']} — removed {len(result['removed'])} item(s)."]
    for r in result["removed"]:
        lines.append(f"  deleted: {r['path']}")
    for s in result["skipped"]:
        lines.append(f"  skipped id {s['id']}: {s['reason']}")
    return "\n".join(lines)


# DRAFT_OUTREACH / SEND_DRAFT — same two-step shape as CLEANUP_SCAN/DELETE
# above: draft() only ever stages, never sends; SEND_DRAFT can only act on
# an id that's actually pending, so a hallucinated/guessed number does
# nothing. This is specifically for JARVIS initiating contact with someone
# on its own (business outreach) — the user's own direct instruction to
# send something right now still goes through EMAIL/MESSAGES, unchanged.
def _tool_draft_outreach(cmd: str) -> str:
    parts = [p.strip() for p in cmd.split("|", 3)]
    if len(parts) < 4:
        return "Format: [DRAFT_OUTREACH: email or imessage | recipient | subject or topic | body]"
    channel, recipient, subject, body = parts

    import outreach
    result = outreach.draft(channel, recipient, subject, body)
    if not result["ok"]:
        return result["error"]

    d = result["draft"]
    summary = (f"Drafted outreach #{d['id']} ({d['channel']}) to {d['recipient']}: "
               f"\"{d['subject']}\" — not sent. Say \"send draft {d['id']}\" to approve.")

    # Best-effort — surface the pending draft even if the user isn't
    # actively watching the chat, same bridge built for project_health/
    # trading_context alerts earlier today.
    try:
        from even_glasses import proactive_push
        proactive_push(summary)
    except Exception:
        pass
    try:
        import telegram_notify
        telegram_notify.send(summary)
    except Exception:
        pass

    return summary


def _tool_send_draft(arg: str) -> str:
    try:
        draft_id = int(arg.strip())
    except ValueError:
        return f"SEND_DRAFT needs a numeric draft id, got {arg!r} — only use an id from a DRAFT_OUTREACH result."

    import outreach
    return outreach.send(draft_id)


def _tool_list_drafts() -> str:
    import outreach
    pending = outreach.list_pending()
    if not pending:
        return "No pending outreach drafts."
    lines = [f"{len(pending)} pending draft(s):"]
    for d in pending:
        lines.append(f"  #{d['id']} ({d['channel']}) to {d['recipient']} — \"{d['subject']}\": {d['body']}")
    return "\n".join(lines)


def _tool_discard_draft(arg: str) -> str:
    try:
        draft_id = int(arg.strip())
    except ValueError:
        return f"DISCARD_DRAFT needs a numeric draft id, got {arg!r}."

    import outreach
    if outreach.discard(draft_id):
        return f"Discarded draft #{draft_id} — it will not be sent."
    return f"No pending draft #{draft_id} to discard."


# WEBSCOUT — scout business leads via a WebScout server at localhost:5055.
# Its install directory is user-specific, so it comes from config/projects.json
# (webscout_dir key) rather than being hardcoded — unset by default.
_WEBSCOUT_URL = "http://localhost:5055"


def _webscout_ensure_running():
    """Start WebScout server if not already up."""
    try:
        urllib.request.urlopen(f"{_WEBSCOUT_URL}/", timeout=2)
        return True
    except Exception:
        pass
    webscout_dir = _load_projects_config().get("webscout_dir")
    if not webscout_dir:
        return False
    try:
        import sys
        subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=webscout_dir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(2.5)
        urllib.request.urlopen(f"{_WEBSCOUT_URL}/", timeout=3)
        return True
    except Exception:
        return False


def _tool_webscout(arg: str) -> str:
    if not arg:
        return "Usage: WEBSCOUT: plumber Miami  — scouts business leads for a niche + city"

    if not _webscout_ensure_running():
        webscout_dir = _load_projects_config().get("webscout_dir")
        if not webscout_dir:
            return "WebScout isn't configured — set webscout_dir in config/projects.json."
        return f"WebScout server unavailable — check {webscout_dir}"

    n = 6
    leads = []
    errors = []

    try:
        url = f"{_WEBSCOUT_URL}/scout?q={urllib.parse.quote(arg)}&n={n}"
        req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                t = ev.get("type", "")
                if t == "result":
                    rating  = ev.get("rating", "?")
                    name    = ev.get("name", "?")[:40]
                    domain  = ev.get("domain", "")
                    score   = ev.get("score", "")
                    issues  = ev.get("issues", [])
                    issue_s = (", ".join(issues[:2])) if issues else "looks decent"
                    leads.append(f"  [{rating}] {name} ({domain}) score:{score} — {issue_s}")
                elif t == "error":
                    errors.append(ev.get("msg", "unknown error"))
    except Exception as e:
        return f"WebScout error: {e}"

    if not leads and not errors:
        return f"No leads found for '{arg}'. Try a broader search."

    lines = [f"WebScout results for '{arg}' ({len(leads)} leads):"]
    lines += leads
    if errors:
        lines.append("Errors: " + "; ".join(errors))
    lines.append(f"\nFull UI → {_WEBSCOUT_URL}")
    return "\n".join(lines)

