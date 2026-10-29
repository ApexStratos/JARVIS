"""
safety_perimeter — one safety gate shared by all of JARVIS's screen/automation
agents (sovereign_nexus.py, auto.py, app/toolkit.py's _ollama_vision_agent).

Before this, each agent had its own gate (or none): sovereign_nexus.py had a
blocked-key list and a destructive-text-pattern check; auto.py and toolkit's
vision agent had neither. "Deeper autonomy" only makes sense once the floor
is the same everywhere — this module is that floor.

allow_destructive/allow_file_delete in safety_perimeter.json used to be read
by nothing; they're real switches now.
"""
import json
from pathlib import Path

PERIMETER_FILE = Path(__file__).parent / "safety_perimeter.json"

_DEFAULT_PERIMETER = {
    "blocked_keys": ["cmd+q", "cmd+shift+q", "ctrl+alt+delete",
                      "cmd+option+eject", "cmd+shift+delete"],
    "allow_destructive": False,
    "allow_file_delete": False,
    "max_steps": 60,
    "max_roadblocks": 5,
}

_DESTRUCTIVE_PATTERNS = ["rm -rf", "sudo rm", "mkfs", "dd if=", ":(){:|:&};:"]
_FILE_DELETE_PATTERNS = ["rm ", "del ", "move to trash", "empty trash", "unlink("]


def load_perimeter() -> dict:
    if PERIMETER_FILE.exists():
        try:
            return {**_DEFAULT_PERIMETER, **json.loads(PERIMETER_FILE.read_text())}
        except Exception:
            pass
    return _DEFAULT_PERIMETER.copy()


def check_perimeter(key: str = "", text: str = "", perimeter: dict | None = None) -> tuple[bool, str]:
    """key: a key-combo string (e.g. 'cmd+q'). text: typed text or a shell/command
    string to scan for destructive patterns. Returns (allowed, reason)."""
    perimeter = perimeter if perimeter is not None else load_perimeter()

    key = (key or "").lower().strip()
    if key and key in [k.lower() for k in perimeter.get("blocked_keys", [])]:
        return False, f"Key combo '{key}' blocked"

    text_l = (text or "").lower()
    if not perimeter.get("allow_destructive", False):
        for bad in _DESTRUCTIVE_PATTERNS:
            if bad in text_l:
                return False, f"Destructive pattern: {bad}"
    if not perimeter.get("allow_file_delete", False):
        for bad in _FILE_DELETE_PATTERNS:
            if bad in text_l:
                return False, f"File-delete pattern: {bad.strip()}"

    return True, "SAFE"
