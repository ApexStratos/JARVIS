"""
project_health — auto-discovers this Mac's personal launchd jobs and PM2
processes (no hardcoded project list — anything new under ~/Library/
LaunchAgents or `pm2 jlist` that isn't a known vendor/system job is picked
up automatically) and tracks process health only: is it running, and is it
crash-looping. Not git activity, not log scraping — see PROJECT_STATUS in
toolkit.py for that (a separate, on-demand, git/log-focused tool).

Runs on its own daemon thread (this file's `start_background_loop`), same
pattern as _warmup_ollama in server.py. brain.py reads get_issues() to
proactively surface problems in chat — see its module docstring.
"""

import json
import os
import plistlib
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"

# Vendor/system jobs that happen to live under $HOME but aren't "my projects".
_VENDOR_LABEL_PREFIXES = (
    "com.google.",
    "com.grammarly.",
    "org.freedownloadmanager.",
    "com.winzip.",
    "com.PM2",  # the pm2-resurrect meta job, not a project itself
    "com.apple.",
)

# Known HTTP health endpoints for services we can actively ping, beyond
# plain PID-liveness. Best-effort and small on purpose — most jobs only get
# a liveness/crash-loop check.
_HTTP_HEALTH = {
    "com.local.jarvis-nexus": "http://localhost:5001/api/status",
    "com.local.jarvis-remote-control": "http://localhost:7787/",
}
_HTTP_TIMEOUT_S = 3

# Crash-loop heuristic: N respawns (PID change while the job is present)
# within a rolling window counts as crash-looping. Mirrors what a human
# would notice from watching `launchctl list` a few times in a row.
_CRASH_LOOP_RESTARTS = 3
_CRASH_LOOP_WINDOW_S = 300

CHECK_INTERVAL_S = 60

_state_lock = threading.Lock()
_job_state: dict[str, dict] = {}  # label -> {"last_pid": str|None, "restarts": [timestamps]}
_current_issues: list[str] = []


def _discover_launchd_jobs() -> dict[str, dict]:
    """label -> {"path": best-guess project path, "persistent": KeepAlive is
    truthy}. Only persistent (KeepAlive) jobs are expected to always be up —
    a StartInterval/StartCalendarInterval job sitting idle between scheduled
    runs is normal, not a problem, so it's exempted from the "not running"
    check and only judged by the crash-loop counter."""
    jobs = {}
    if not LAUNCH_AGENTS_DIR.is_dir():
        return jobs
    for plist_path in LAUNCH_AGENTS_DIR.glob("*.plist"):
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        label = data.get("Label", "")
        if not label or label.startswith(_VENDOR_LABEL_PREFIXES):
            continue
        project_path = data.get("WorkingDirectory", "")
        if not project_path:
            for arg in data.get("ProgramArguments", []):
                if isinstance(arg, str) and arg.startswith("/") and Path(arg).exists():
                    project_path = str(Path(arg).parent)
                    break
        jobs[label] = {"path": project_path, "persistent": bool(data.get("KeepAlive"))}
    return jobs


def _launchctl_pid(label: str) -> str | None:
    """Returns the PID string if loaded, "-" if loaded-but-not-running, None if not loaded."""
    try:
        out = subprocess.run(
            ["launchctl", "list", label], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith('"PID"'):
            return line.split("=", 1)[1].strip().rstrip(";").strip()
    return "-"


def _check_launchd_job(label: str, project_path: str, persistent: bool) -> str | None:
    pid = _launchctl_pid(label)
    now = time.monotonic()

    with _state_lock:
        state = _job_state.setdefault(label, {"last_pid": None, "restarts": []})
        if pid not in (None, "-") and state["last_pid"] not in (None, pid, "-"):
            state["restarts"].append(now)
        state["restarts"] = [t for t in state["restarts"] if now - t < _CRASH_LOOP_WINDOW_S]
        state["last_pid"] = pid
        crash_looping = len(state["restarts"]) >= _CRASH_LOOP_RESTARTS

    name = Path(project_path).name if project_path else label
    if pid is None:
        return None  # not loaded at all — nothing to report (intentionally stopped/disabled)
    if crash_looping:
        return f"{name} ({label}) is crash-looping — restarted {len(state['restarts'])}+ times in the last {_CRASH_LOOP_WINDOW_S // 60} min"
    if pid == "-":
        if not persistent:
            return None  # scheduled job idle between runs — expected, not an issue
        return f"{name} ({label}) is loaded but not running"

    url = _HTTP_HEALTH.get(label)
    if url:
        try:
            urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT_S)
        except Exception:
            return f"{name} ({label}) process is up but not responding to HTTP checks"
    return None


def _check_pm2() -> list[str]:
    issues = []
    try:
        out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=8)
        procs = json.loads(out.stdout) if out.returncode == 0 else []
    except Exception:
        return issues
    for p in procs:
        name = p.get("name", "?")
        env = p.get("pm2_env", {})
        status = env.get("status", "unknown")
        label = f"pm2:{name}"
        restart_time = env.get("restart_time", 0)

        with _state_lock:
            state = _job_state.setdefault(label, {"last_restart_time": restart_time, "restarts": []})
            now = time.monotonic()
            if restart_time > state["last_restart_time"]:
                state["restarts"].extend([now] * (restart_time - state["last_restart_time"]))
            state["last_restart_time"] = restart_time
            state["restarts"] = [t for t in state["restarts"] if now - t < _CRASH_LOOP_WINDOW_S]
            crash_looping = len(state["restarts"]) >= _CRASH_LOOP_RESTARTS

        if crash_looping:
            issues.append(f"{name} (pm2) is crash-looping — {len(state['restarts'])}+ restarts in the last {_CRASH_LOOP_WINDOW_S // 60} min")
        elif status != "online":
            issues.append(f"{name} (pm2) is {status}, not online")
    return issues


_pushed_issues: set[str] = set()  # dedup for the proactive HUD push below —
                                   # same issue shouldn't re-push every 60s


def run_check_cycle() -> list[str]:
    issues = []
    for label, info in _discover_launchd_jobs().items():
        issue = _check_launchd_job(label, info["path"], info["persistent"])
        if issue:
            issues.append(issue)
    issues.extend(_check_pm2())

    with _state_lock:
        _current_issues[:] = issues

    global _pushed_issues
    _pushed_issues &= set(issues)  # resolved issues drop out, so a recurrence re-pushes
    new_issues = [i for i in issues if i not in _pushed_issues]
    if new_issues:
        _pushed_issues.update(new_issues)
        try:
            from even_glasses import proactive_push
            for issue in new_issues:
                proactive_push(issue)
        except Exception:
            pass  # HUD push is best-effort — never let it break the health check itself

    return issues


def get_issues() -> list[str]:
    with _state_lock:
        return list(_current_issues)


def _loop(interval_s: float):
    while True:
        try:
            run_check_cycle()
        except Exception as e:
            print(f"[project_health] check cycle error: {e}")
        time.sleep(interval_s)


def start_background_loop(interval_s: float = CHECK_INTERVAL_S):
    threading.Thread(target=_loop, args=(interval_s,), daemon=True).start()
