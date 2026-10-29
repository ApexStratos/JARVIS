"""
Storage manager — monitors disk usage and purges safe targets automatically.
Sends Telegram alerts at 80% (warning) and 90% (critical) thresholds.
Protected project paths are NEVER touched regardless of threshold.
"""
import os
import shutil
import subprocess
import threading
import time
import glob
import telegram_notify

def _hud_alert(text: str) -> None:
    """Forward a storage alert to connected G1 glasses (no-op if module absent)."""
    try:
        from even_glasses import proactive_push
        proactive_push(text)
    except Exception:
        pass

# --- Thresholds ---
CHECK_INTERVAL = 600          # seconds between scans
WARN_THRESHOLD = 80.0         # % → warning alert + light purge
CRITICAL_THRESHOLD = 90.0     # % → critical alert + aggressive purge

HOME = os.path.expanduser("~")

# --- Protected paths — never entered, never deleted ---
# Deliberately generic (no project/business-specific folder names) — this
# ships to anyone, so it can't assume what someone's project folders are
# called. Add your own via extra_protected_paths in config/projects.json
# if you want more than this general safety net.
PROTECTED_PATHS = [
    os.path.join(HOME, ".claude"),
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Downloads"),
    os.path.join(HOME, "Desktop"),
    os.path.join(HOME, "Pictures"),
    os.path.join(HOME, "Movies"),
    os.path.join(HOME, "Music"),
]

try:
    import json as _json
    from pathlib import Path as _Path
    _cfg_file = _Path(__file__).parent / "config" / "projects.json"
    _extra = _json.loads(_cfg_file.read_text()).get("extra_protected_paths", [])
    PROTECTED_PATHS.extend(_extra)
except Exception:
    pass

# Normalize for prefix matching
_PROTECTED_NORM = [os.path.realpath(p) + os.sep for p in PROTECTED_PATHS] + \
                  [os.path.realpath(p) for p in PROTECTED_PATHS]


def _is_protected(path: str) -> bool:
    """Return True if path is inside (or equal to) any protected directory."""
    real = os.path.realpath(path)
    for p in _PROTECTED_NORM:
        if real == p.rstrip(os.sep) or real.startswith(p if p.endswith(os.sep) else p + os.sep):
            return True
    return False


# --- Safe purge targets (explicit, bounded) ---
_SAFE_DIRS = [
    os.path.join(HOME, ".Trash"),
    os.path.join(HOME, "Library", "Caches"),
    os.path.join(HOME, "Library", "Developer", "Xcode", "DerivedData"),
    os.path.join(HOME, "Library", "Developer", "CoreSimulator", "Caches"),
    os.path.join(HOME, "Library", "Application Support", "pip", "cache"),
    "/Library/Caches",
]

_SAFE_GLOB_PATTERNS = [
    os.path.join(HOME, "**", "__pycache__"),
    os.path.join(HOME, "**", "*.pyc"),
    os.path.join(HOME, "**", ".DS_Store"),
    "/private/tmp/claude-*",
]

INSTALLER_EXTS = {".dmg", ".pkg", ".iso", ".exe", ".msi"}
LOG_MAX_AGE_DAYS = 30
LARGE_FILE_MIN_MB = 200
LARGE_FILE_AGE_DAYS = 90

_lock = threading.Lock()
_thread = None
_last_report: dict = {}
_pending_unused: list = []   # populated by scan; consumed by purge_pending()


# --- Disk stats ---
def disk_stats(path="/") -> dict:
    usage = shutil.disk_usage(path)
    pct = (usage.used / usage.total) * 100
    return {
        "total_gb": round(usage.total / 1e9, 2),
        "used_gb": round(usage.used / 1e9, 2),
        "free_gb": round(usage.free / 1e9, 2),
        "percent": round(pct, 1),
    }


# --- Purge primitives ---
def _purge_dir(path: str) -> int:
    if not os.path.isdir(path) or _is_protected(path):
        return 0
    freed = 0
    for entry in os.scandir(path):
        if _is_protected(entry.path):
            continue
        try:
            size = shutil.disk_usage(entry.path).used if os.path.isdir(entry.path) else os.path.getsize(entry.path)
            if os.path.isdir(entry.path):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.remove(entry.path)
            freed += size
        except Exception:
            pass
    return freed


def _purge_glob(pattern: str) -> int:
    freed = 0
    for path in glob.glob(pattern, recursive=True):
        if _is_protected(path):
            continue
        try:
            size = os.path.getsize(path) if os.path.isfile(path) else 0
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path):
                os.remove(path)
            freed += size
        except Exception:
            pass
    return freed


def _purge_brew() -> None:
    try:
        subprocess.run(["brew", "cleanup", "--prune=all", "-q"],
                       capture_output=True, timeout=120)
    except Exception:
        pass


# --- Public purge entry point ---
def run_purge(aggressive: bool = False) -> dict:
    freed_bytes = 0
    actions = []

    for d in _SAFE_DIRS:
        b = _purge_dir(d)
        if b > 0:
            freed_bytes += b
            actions.append(f"cleared {os.path.basename(d)} ({b/1e6:.1f} MB)")

    for pat in _SAFE_GLOB_PATTERNS:
        b = _purge_glob(pat)
        if b > 0:
            freed_bytes += b
            actions.append(f"glob pattern ({b/1e6:.1f} MB)")

    if aggressive:
        _purge_brew()
        actions.append("brew cleanup --prune=all")

    return {
        "freed_mb": round(freed_bytes / 1e6, 1),
        "actions": actions,
        "post_stats": disk_stats(),
    }


# --- Background monitor ---
def _monitor_loop():
    global _last_report
    alerted_warn = False
    alerted_crit = False

    while True:
        stats = disk_stats()
        _last_report = stats
        pct = stats["percent"]

        if pct >= CRITICAL_THRESHOLD and not alerted_crit:
            purge = run_purge(aggressive=True)
            post = purge["post_stats"]
            msg = (
                f"*JARVIS STORAGE — CRITICAL*\n"
                f"Disk at *{pct}%* ({stats['free_gb']} GB free)\n"
                f"Auto-purge freed *{purge['freed_mb']} MB*\n"
                f"Now at *{post['percent']}%* ({post['free_gb']} GB free)"
            )
            telegram_notify.send(msg)
            _hud_alert(f"STORAGE CRITICAL {pct}% — purged {purge['freed_mb']}MB → now {post['percent']}%")
            alerted_crit = True
            alerted_warn = True

        elif pct >= WARN_THRESHOLD and not alerted_warn:
            purge = run_purge(aggressive=False)
            post = purge["post_stats"]
            msg = (
                f"*JARVIS STORAGE — WARNING*\n"
                f"Disk at *{pct}%* ({stats['free_gb']} GB free)\n"
                f"Auto-purge freed *{purge['freed_mb']} MB*\n"
                f"Now at *{post['percent']}%* ({post['free_gb']} GB free)"
            )
            telegram_notify.send(msg)
            _hud_alert(f"STORAGE WARNING {pct}% — purged {purge['freed_mb']}MB → now {post['percent']}%")
            alerted_warn = True

        else:
            if pct < WARN_THRESHOLD - 5:
                alerted_warn = False
                alerted_crit = False

        time.sleep(CHECK_INTERVAL)


def start():
    # Off by default — this silently empties ~/.Trash and clears caches on a
    # timer with no per-run confirmation, which is a reasonable thing to opt
    # into for your own machine but not something a downloaded assistant
    # should do to a stranger's computer without being asked. Same pattern
    # as JARVIS_ALLOW_SELF_PUSH.
    if os.environ.get("JARVIS_AUTO_STORAGE_CLEANUP", "0") != "1":
        return
    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return
        _thread = threading.Thread(target=_monitor_loop, daemon=True, name="storage-monitor")
        _thread.start()


def get_status() -> dict:
    return {
        "monitoring": bool(_thread and _thread.is_alive()),
        "stats": _last_report or disk_stats(),
        "protected_paths": PROTECTED_PATHS,
    }
