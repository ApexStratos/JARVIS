"""
project_monitor — proactive half of PROJECT_STATUS (app/toolkit.py). That
tool is pull-only: dumps fresh git/PM2 state only when asked, with no
comparison to prior state. This runs the same scan on a background thread
and pushes only on a real change (new commit, a PM2 service flipping
status) — using the same dedup-on-content approach project_health.py
already uses, so a standing state doesn't get re-pushed every cycle, and a
restart doesn't spam "everything changed" for state that was already there.

Reuses toolkit.py's _load_projects_config() (config/projects.json) rather
than keeping a second copy of the project list — one source of truth for
what counts as "a project."
"""
import json
import os
import subprocess
import threading
import time

CHECK_INTERVAL_S = 60

_last_commit: dict[str, str] = {}       # project name -> last-seen commit hash
_last_pm2_status: dict[str, str] = {}   # "project:service" -> last-seen status
_first_cycle = True                     # first scan establishes baseline, never pushes


def _get_commit(path: str):
    """Returns (hash, subject) or None."""
    try:
        r = subprocess.run(
            ["git", "-C", path, "log", "-1", "--format=%H|%s"],
            capture_output=True, text=True, timeout=6,
        )
        if r.returncode == 0 and r.stdout.strip():
            h, _, msg = r.stdout.strip().partition("|")
            return h, msg
    except Exception:
        pass
    return None


def _get_pm2_map() -> dict:
    try:
        r = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=8)
        data = json.loads(r.stdout) if r.returncode == 0 else []
        return {p["name"]: p["pm2_env"]["status"] for p in data}
    except Exception:
        return {}


def _push(message: str) -> None:
    try:
        from even_glasses import proactive_push
        proactive_push(message)
    except Exception:
        pass  # best-effort — never let a push failure break the monitor loop


def run_check_cycle() -> None:
    global _first_cycle
    from toolkit import _load_projects_config
    cfg = _load_projects_config()
    projects = cfg.get("projects", {})
    pm2_services = cfg.get("pm2_services", {})
    if not projects:
        return

    pm2_map = _get_pm2_map()

    for name, path in projects.items():
        if not os.path.isdir(path):
            continue

        commit = _get_commit(path)
        if commit is not None:
            h, msg = commit
            prev = _last_commit.get(name)
            if prev is not None and prev != h and not _first_cycle:
                _push(f"{name}: new commit — {msg[:80]}")
            _last_commit[name] = h

        for svc in pm2_services.get(name, []):
            status = pm2_map.get(svc, "unknown")
            key = f"{name}:{svc}"
            prev_status = _last_pm2_status.get(key)
            if prev_status is not None and prev_status != status and not _first_cycle:
                _push(f"{name}: {svc} is now {status} (was {prev_status})")
            _last_pm2_status[key] = status

    _first_cycle = False


def _loop(interval_s: float):
    while True:
        try:
            run_check_cycle()
        except Exception as e:
            print(f"[project_monitor] check cycle error: {e}")
        time.sleep(interval_s)


def start_background_loop(interval_s: float = CHECK_INTERVAL_S):
    threading.Thread(target=_loop, args=(interval_s,), daemon=True).start()
