"""
JARVIS Nexus — Open WebUI Tools Integration

Paste this file's `Tools` class into Open WebUI as a custom tool set.
Every method returns a plain string so the LLM execution loop never
receives raw JSON objects or unserializable payloads.
"""
import os
import json
import subprocess
import urllib.request
import urllib.parse
from pathlib import Path


# ── shared memory path (mirrors toolkit.py) ────────────────────────────────
_MEMORY_FILE = Path(__file__).parent.parent / "data" / "jarvis_memory.json"
_MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

_SAFE_PREFIXES = (
    "ls", "cat", "pwd", "date", "echo", "python3",
    "df", "du", "uname", "whoami", "uptime", "ps",
    "find", "grep", "head", "tail", "wc",
)


def _load_memory() -> dict:
    try:
        return json.loads(_MEMORY_FILE.read_text()) if _MEMORY_FILE.exists() else {}
    except Exception:
        return {}


def _save_memory(data: dict) -> None:
    _MEMORY_FILE.write_text(json.dumps(data, indent=2))


class Tools:
    """JARVIS Nexus tool suite for Open WebUI."""

    # ── Web search ─────────────────────────────────────────────────────────

    def search_web(self, query: str) -> str:
        """
        Search the web with DuckDuckGo and return top results.

        :param query: The search query.
        :return: Bullet-list of titles and summaries.
        """
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=5))
            if not results:
                return "No results found."
            return "\n".join(f"• {r['title']}: {r['body'][:220]}" for r in results)
        except Exception as e:
            return f"Search failed: {e}"

    # ── Wikipedia ──────────────────────────────────────────────────────────

    def search_wikipedia(self, topic: str) -> str:
        """
        Fetch a Wikipedia summary for a topic.

        :param topic: Topic or entity to look up.
        :return: Wikipedia extract (first ~500 chars) or an error message.
        """
        try:
            url = (
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + urllib.parse.quote(topic.replace(" ", "_"))
            )
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            return data.get("extract", "No summary available.")[:600]
        except Exception as e:
            return f"Wikipedia error: {e}"

    # ── Weather ────────────────────────────────────────────────────────────

    def get_weather(self, location: str = "") -> str:
        """
        Get current weather for a location using wttr.in.

        :param location: City name or empty string for auto-detect by IP.
        :return: Weather summary as plain text.
        """
        try:
            loc = urllib.parse.quote(location) if location else ""
            url = f"https://wttr.in/{loc}?format=3"
            req = urllib.request.Request(url, headers={"User-Agent": "curl/7"})
            with urllib.request.urlopen(req, timeout=8) as r:
                return r.read().decode().strip()
        except Exception as e:
            return f"Weather error: {e}"

    # ── Time ───────────────────────────────────────────────────────────────

    def get_current_time(self, timezone: str = "") -> str:
        """
        Return the current local date and time.

        :param timezone: Optional IANA timezone name (e.g. 'America/New_York').
                         Defaults to local system time.
        :return: Formatted date/time string.
        """
        try:
            from datetime import datetime
            if timezone:
                import zoneinfo
                tz = zoneinfo.ZoneInfo(timezone)
                now = datetime.now(tz)
            else:
                now = datetime.now()
            return now.strftime("%A, %B %d %Y — %I:%M:%S %p %Z").strip()
        except Exception as e:
            return f"Time error: {e}"

    # ── News ───────────────────────────────────────────────────────────────

    def get_news(self, topic: str = "top") -> str:
        """
        Fetch recent news headlines via DuckDuckGo news search.

        :param topic: Topic to search, e.g. 'AI', 'crypto', 'sports'. Use 'top' for general headlines.
        :return: Bullet-list of recent headlines.
        """
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            q = "" if topic.lower() == "top" else topic
            with DDGS() as ddgs:
                results = list(ddgs.news(q or "breaking news today", max_results=6))
            if not results:
                return "No news found."
            return "\n".join(f"• {r['title']} — {r.get('source','')}" for r in results)
        except Exception as e:
            return f"News error: {e}"

    # ── Stocks ─────────────────────────────────────────────────────────────

    def get_stock_price(self, symbol: str) -> str:
        """
        Look up a stock quote using yfinance (must be pip-installed).

        :param symbol: Ticker symbol, e.g. AAPL, TSLA, SPY.
        :return: Current price and daily change as a string.
        """
        try:
            import yfinance as yf
            t = yf.Ticker(symbol.upper())
            info = t.fast_info
            price = info.last_price
            prev  = info.previous_close
            chg   = price - prev
            pct   = chg / prev * 100
            sign  = "+" if chg >= 0 else ""
            return f"{symbol.upper()}: ${price:.2f} ({sign}{chg:.2f} / {sign}{pct:.2f}%)"
        except ImportError:
            return "yfinance not installed. Run: pip install yfinance"
        except Exception as e:
            return f"Stock error for {symbol}: {e}"

    # ── Crypto ─────────────────────────────────────────────────────────────

    def get_crypto_price(self, coin: str = "bitcoin") -> str:
        """
        Fetch current crypto price from CoinGecko (no API key needed).

        :param coin: CoinGecko coin ID, e.g. bitcoin, ethereum, solana.
        :return: Current price in USD.
        """
        try:
            url = (
                f"https://api.coingecko.com/api/v3/simple/price"
                f"?ids={urllib.parse.quote(coin.lower())}&vs_currencies=usd&include_24hr_change=true"
            )
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            if not data:
                return f"Coin '{coin}' not found on CoinGecko."
            key = list(data.keys())[0]
            price = data[key]["usd"]
            chg   = data[key].get("usd_24h_change", 0)
            sign  = "+" if chg >= 0 else ""
            return f"{key.capitalize()}: ${price:,.2f} ({sign}{chg:.2f}% 24h)"
        except Exception as e:
            return f"Crypto error: {e}"

    # ── System info ────────────────────────────────────────────────────────

    def get_system_info(self) -> str:
        """
        Return macOS system stats: CPU, memory, disk, and uptime.

        :return: Multi-line system summary.
        """
        try:
            lines = []
            r = subprocess.run(["uname", "-a"], capture_output=True, text=True, timeout=5)
            lines.append(r.stdout.strip())
            r = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5)
            lines.append(r.stdout.strip())
            r = subprocess.run(["df", "-h", "/"], capture_output=True, text=True, timeout=5)
            lines.append(r.stdout.strip())
            try:
                import psutil
                mem = psutil.virtual_memory()
                lines.append(f"RAM: {mem.used/1e9:.1f} GB used / {mem.total/1e9:.1f} GB total ({mem.percent}%)")
            except ImportError:
                pass
            return "\n".join(lines)
        except Exception as e:
            return f"Sysinfo error: {e}"

    # ── Safe shell ─────────────────────────────────────────────────────────

    def run_safe_command(self, command: str) -> str:
        """
        Execute a read-only shell command from a safe allowlist.

        Allowed prefixes: ls, cat, pwd, date, echo, python3, df, du,
        uname, whoami, uptime, ps, find, grep, head, tail, wc.

        :param command: Shell command string.
        :return: stdout/stderr output (max 1500 chars) or an error.
        """
        if not any(command.strip().startswith(p) for p in _SAFE_PREFIXES):
            allowed = ", ".join(_SAFE_PREFIXES[:8]) + "…"
            return f"Command not permitted. Allowed prefixes: {allowed}"
        try:
            result = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=10
            )
            out = (result.stdout or "") + (result.stderr or "")
            return out.strip()[:1500] or "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 10 seconds."
        except Exception as e:
            return f"Shell error: {e}"

    # ── Telegram notification ──────────────────────────────────────────────

    def send_notification(self, message: str) -> str:
        """
        Send a message to the configured Telegram chat.

        Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars.

        :param message: Text to send (Markdown supported).
        :return: Confirmation or error string.
        """
        token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return "Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID)."
        try:
            url     = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps({"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}).encode()
            req     = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8)
            return f"Notification sent: {message[:100]}"
        except Exception as e:
            return f"Telegram error: {e}"

    # ── Memory ─────────────────────────────────────────────────────────────

    def memory_set(self, key: str, value: str) -> str:
        """
        Store a key-value pair in JARVIS persistent memory.

        :param key: Memory key (alphanumeric, underscores ok).
        :param value: Value to store.
        :return: Confirmation string.
        """
        try:
            mem = _load_memory()
            mem[key.strip()] = value.strip()
            _save_memory(mem)
            return f"Remembered: {key.strip()} = {value.strip()}"
        except Exception as e:
            return f"Memory write error: {e}"

    def memory_get(self, key: str) -> str:
        """
        Retrieve a value from JARVIS persistent memory.

        :param key: Memory key to look up.
        :return: Stored value or a not-found message.
        """
        try:
            mem = _load_memory()
            val = mem.get(key.strip())
            return f"{key.strip()}: {val}" if val is not None else f"No memory for '{key.strip()}'."
        except Exception as e:
            return f"Memory read error: {e}"

    def memory_list(self) -> str:
        """
        List all stored JARVIS memory entries.

        :return: All key-value pairs as a newline-separated string.
        """
        try:
            mem = _load_memory()
            if not mem:
                return "Memory is empty."
            return "\n".join(f"{k}: {v}" for k, v in mem.items())
        except Exception as e:
            return f"Memory list error: {e}"

    # ── Calculator ─────────────────────────────────────────────────────────

    def calculate(self, expression: str) -> str:
        """
        Evaluate a safe mathematical expression.

        :param expression: Math expression, e.g. '(12 * 3.5) / 2 + 100'.
        :return: Result as a string or an error message.
        """
        try:
            allowed = set("0123456789 +-*/().,%^eE")
            if not all(c in allowed for c in expression):
                return "Expression contains unsafe characters."
            result = eval(expression, {"__builtins__": {}})  # noqa: S307
            return str(result)
        except Exception as e:
            return f"Calculation error: {e}"

    # ── IP lookup ──────────────────────────────────────────────────────────

    def get_ip_info(self, ip: str = "") -> str:
        """
        Look up geolocation and ISP info for an IP address.

        :param ip: IPv4 address to look up, or empty for your own public IP.
        :return: Location, ISP, and org details as a string.
        """
        try:
            target = ip.strip() if ip.strip() else ""
            url = f"https://ipinfo.io/{target}/json"
            req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            parts = [
                f"IP: {data.get('ip','?')}",
                f"Location: {data.get('city','?')}, {data.get('region','?')}, {data.get('country','?')}",
                f"Org: {data.get('org','?')}",
                f"Timezone: {data.get('timezone','?')}",
            ]
            return "\n".join(parts)
        except Exception as e:
            return f"IP lookup error: {e}"

    # ── Clipboard ──────────────────────────────────────────────────────────

    def read_clipboard(self) -> str:
        """
        Read the current macOS clipboard contents.

        :return: Clipboard text (max 800 chars) or empty message.
        """
        try:
            r = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=5)
            content = r.stdout.strip()
            return f"Clipboard: {content[:800]}" if content else "Clipboard is empty."
        except Exception as e:
            return f"Clipboard error: {e}"

    def write_clipboard(self, text: str) -> str:
        """
        Write text to the macOS clipboard.

        :param text: Text to copy to clipboard.
        :return: Confirmation string.
        """
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=5)
            return f"Copied to clipboard: {text[:100]}{'…' if len(text) > 100 else ''}"
        except Exception as e:
            return f"Clipboard write error: {e}"

    # ── Open application ───────────────────────────────────────────────────

    def open_application(self, app_name: str) -> str:
        """
        Open a macOS application by name.

        :param app_name: Application name as it appears in /Applications, e.g. 'Safari', 'Finder'.
        :return: Confirmation or error message.
        """
        try:
            subprocess.run(["open", "-a", app_name], capture_output=True, timeout=5)
            return f"Opened {app_name}."
        except Exception as e:
            return f"Could not open {app_name}: {e}"
