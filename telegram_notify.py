"""Lightweight Telegram notification module — uses raw HTTP, no third-party bot SDK."""
import os
import requests

_BASE = "https://api.telegram.org/bot{token}/sendMessage"

def send(text: str, parse_mode: str = "Markdown") -> dict:
    """Send a message to the configured Telegram chat. Returns the API response dict."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set"}
    try:
        resp = requests.post(
            _BASE.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
