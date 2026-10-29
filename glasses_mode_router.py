"""
glasses_mode_router.py — Home / Away mode detection and URL routing.

At-Home  → Mac BLE daemon drives the glasses directly; no phone involved.
On-the-Go → Phone Even Hub app routes through Cloudflare tunnel to home Mac.

Mode is detected by checking which WiFi network the Mac is on.
The server also exposes GET /api/glasses/home-check so the phone's Even Hub
app can self-discover which endpoint to use when it boots.
"""

from __future__ import annotations
import os
import subprocess
import threading
import time
from enum import Enum

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

HOME_SSID          = os.environ.get("HOME_SSID", "")
LOCAL_JARVIS_URL   = os.environ.get("LOCAL_JARVIS_URL", "http://127.0.0.1:5001")
TUNNEL_JARVIS_URL  = os.environ.get("CLOUDFLARE_TUNNEL_URL", "")
POLL_INTERVAL      = 30          # seconds between mode re-checks
NETWORK_IFACE      = os.environ.get("WIFI_IFACE", "en0")


class Mode(str, Enum):
    HOME   = "home"    # Mac BLE daemon active; local Ollama; no phone needed
    AWAY   = "away"    # Phone Even Hub → Cloudflare tunnel → home Mac


# ── network helpers ───────────────────────────────────────────────────────────

def current_ssid() -> str:
    """Return the SSID of the currently connected WiFi network, or '' if none."""
    try:
        result = subprocess.run(
            ["networksetup", "-getairportnetwork", NETWORK_IFACE],
            capture_output=True, text=True, timeout=5,
        )
        # Output: "Current Wi-Fi Network: MySSID"
        line = result.stdout.strip()
        if ": " in line:
            return line.split(": ", 1)[1]
    except Exception:
        pass
    return ""


def is_home() -> bool:
    """True when the Mac is on the configured home WiFi network."""
    if not HOME_SSID:
        # No home SSID configured — assume home (daemon always active)
        return True
    return current_ssid().strip() == HOME_SSID.strip()


def jarvis_url() -> str:
    """Return the correct JARVIS endpoint for the current mode."""
    return LOCAL_JARVIS_URL if is_home() else TUNNEL_JARVIS_URL


def current_mode() -> Mode:
    return Mode.HOME if is_home() else Mode.AWAY


# ── background poller ─────────────────────────────────────────────────────────

_mode: Mode = Mode.HOME
_mode_lock   = threading.Lock()
_callbacks: list = []


def get_mode() -> Mode:
    with _mode_lock:
        return _mode


def on_mode_change(callback) -> None:
    """Register a function(old_mode, new_mode) called on every mode transition."""
    _callbacks.append(callback)


def _poll():
    global _mode
    while True:
        detected = current_mode()
        with _mode_lock:
            if detected != _mode:
                old = _mode
                _mode = detected
                for cb in _callbacks:
                    try:
                        cb(old, detected)
                    except Exception:
                        pass
        time.sleep(POLL_INTERVAL)


def start_mode_watcher() -> None:
    """Start the background thread that polls WiFi mode every POLL_INTERVAL s."""
    global _mode
    _mode = current_mode()
    t = threading.Thread(target=_poll, daemon=True, name="mode-router")
    t.start()


# ── Flask route helper (imported by server.py) ────────────────────────────────

def register_mode_route(app) -> None:
    """
    GET /api/glasses/home-check

    The phone's EvenHub app calls this on boot (via local IP or tunnel).
    If it gets a 200 back on the local IP it knows it's at home and can
    switch its chatEndpoint to the local URL for lower latency.

    Response:
      { "reachable": true, "mode": "home", "local_url": "http://...:5001",
        "tunnel_url": "https://xxx.trycloudflare.com" }
    """
    from flask import jsonify

    @app.route("/api/glasses/home-check")
    def glasses_home_check():
        return jsonify({
            "reachable":   True,
            "mode":        get_mode().value,
            "local_url":   LOCAL_JARVIS_URL,
            "tunnel_url":  TUNNEL_JARVIS_URL,
            "ssid":        current_ssid(),
        })
