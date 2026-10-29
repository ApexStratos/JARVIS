#!/usr/bin/env python3
"""
tts_watchdog.py — Sovereign Nexus TTS Daemon Watchdog

24/7 health monitor for the multi-engine TTS server (Kokoro, Chatterbox, Fish Audio).
Runs as a background process alongside the main server.

Responsibilities:
  • Ping /v1/health and /v1/engines every 30s
  • Detect hangs (response > 10s), crashes (connection refused), memory bloat
  • Restart the TTS daemon subprocess if any fault is detected
  • Purge stale audio cache files older than 10 minutes
  • Log all events to tts_watchdog.log with rotation

Usage:
  python3.13 tts_watchdog.py          # run standalone
  python3.13 tts_watchdog.py --once   # single health check (for cron/CI)
"""

import argparse, concurrent.futures, gc, json, logging, os, platform, psutil, shutil, signal
import subprocess, sys, tempfile, time, urllib.request
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
TTS_DAEMON  = BASE / "sovereign_nexus_tts.py"
AUDIO_CACHE = BASE / "tts_output"
LOG_FILE    = BASE / "tts_watchdog.log"
PID_FILE    = BASE / "tts_daemon.pid"

TTS_HOST        = "http://127.0.0.1:8765"   # TTS daemon port (not main server)
MAIN_SERVER_URL = "http://127.0.0.1:5001"
# Fish Speech is a separate child process server.py spawns on its own (see
# _start_fish_speech_server() in server.py) — it is NOT the TTS_HOST daemon
# above (that one is a vestigial standalone mode nothing runs in production;
# TTS is embedded in the main server process instead). Flask staying
# responsive to /api/status says nothing about whether Fish Speech itself
# has wedged — that's exactly what was happening: the main server answered
# fine while Fish Speech was hung mid-generation and unresponsive to every
# request including its own health check, and nothing was watching it.
FISH_HOST = "http://127.0.0.1:8080"
FISH_VENV = Path.home() / ".cache" / "fish-speech" / "venv"
FISH_REPO = Path.home() / ".cache" / "fish-speech" / "repo"
FISH_CKPT = Path.home() / ".cache" / "fish-speech" / "fish-speech-1.5"
HEALTH_INTERVAL = 30       # seconds between checks
MAX_RESPONSE_MS = 10_000   # 10s timeout → consider hung
CACHE_MAX_AGE_S = 600      # 10 minutes — purge old audio files
MEM_LIMIT_MB    = 4096     # restart if process exceeds 4GB RAM
MAX_LOG_MB      = 10       # rotate log at 10MB
STARTUP_GRACE_S = 90       # ignore failures for this long after the watchdog itself
                           # starts — server.py's own warmup (vision/Ollama/Kokoro/
                           # Fish Speech) can legitimately take longer than one 10s
                           # health-check window, and restarting mid-warmup is what
                           # causes duplicate-process pileups.
FAILURES_BEFORE_RESTART = 2  # debounce — one slow check shouldn't trigger a restart
PORT_FREE_TIMEOUT_S = 15   # how long to wait for the old process to release the port
                           # before giving up on restart_main_server (never spawn a
                           # duplicate onto a port that's still held)

# ── Logging ───────────────────────────────────────────────────────────────────
log = logging.getLogger("tts_watchdog")
log.setLevel(logging.DEBUG)
_fh = RotatingFileHandler(LOG_FILE, maxBytes=MAX_LOG_MB * 1048576, backupCount=3)
_fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_sh = logging.StreamHandler()
_sh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log.addHandler(_fh)
log.addHandler(_sh)


# ── Health check helpers ───────────────────────────────────────────────────────
def _get(url: str, timeout: float = 10.0) -> tuple[int, dict | None]:
    """Return (http_status, json_body) or (-1, None) on error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "watchdog/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
            return r.status, body
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return -1, None


def check_main_server() -> dict:
    """Ping the main JARVIS server health endpoint."""
    t0 = time.monotonic()
    status, body = _get(f"{MAIN_SERVER_URL}/api/status", timeout=10)
    ms = int((time.monotonic() - t0) * 1000)
    return {"ok": status == 200, "status": status, "ms": ms}


def check_tts_engines() -> dict:
    """Probe /v1/engines — confirms Kokoro/Fish are loaded."""
    t0 = time.monotonic()
    status, body = _get(f"{TTS_HOST}/v1/engines", timeout=MAX_RESPONSE_MS / 1000)
    ms = int((time.monotonic() - t0) * 1000)
    if status == -1:
        return {"ok": False, "err": "unreachable", "ms": ms}
    engines = body.get("engines", []) if body else []
    return {"ok": bool(engines), "engines": engines, "ms": ms, "status": status}


def check_fish_speech() -> dict:
    """POST /v1/health on the actual Fish Speech subprocess (port 8080) —
    the fish-speech server's real health-check method/path, distinct from
    the vestigial TTS_HOST checks above."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(f"{FISH_HOST}/v1/health", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=MAX_RESPONSE_MS / 1000) as r:
            ms = int((time.monotonic() - t0) * 1000)
            return {"ok": r.status == 200, "status": r.status, "ms": ms}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "err": str(e), "ms": ms}


def check_tts_synthesis() -> dict:
    """Send a minimal synthesis request — confirms full pipeline works."""
    t0 = time.monotonic()
    try:
        payload = json.dumps({"text": "ok", "voice_id": "af_heart", "speed": 1.0}).encode()
        req = urllib.request.Request(
            f"{TTS_HOST}/v1/tts",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=MAX_RESPONSE_MS / 1000) as r:
            body = json.loads(r.read())
            ms   = int((time.monotonic() - t0) * 1000)
            ok   = body.get("audio_path") or body.get("ok")
            return {"ok": bool(ok), "ms": ms, "body": body}
    except Exception as e:
        ms = int((time.monotonic() - t0) * 1000)
        return {"ok": False, "err": str(e), "ms": ms}


# ── Process management ─────────────────────────────────────────────────────────
def get_daemon_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if psutil.pid_exists(pid):
                return pid
        except Exception:
            pass
    # Fallback: search by cmdline
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "sovereign_nexus_tts.py" in cmd:
                return proc.info["pid"]
        except Exception:
            pass
    return None


def get_fish_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "tools.api_server" in cmd and "fish-speech" in cmd:
                return proc.info["pid"]
        except Exception:
            pass
    return None


def get_main_server_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "server.py" in cmd and "jarvis-nexus" in cmd:
                return proc.info["pid"]
        except Exception:
            pass
    return None


def get_process_mem_mb(pid: int) -> float:
    try:
        p = psutil.Process(pid)
        return p.memory_info().rss / 1048576
    except Exception:
        return 0.0


def restart_tts_daemon():
    """Kill old TTS daemon and start a fresh one."""
    old_pid = get_daemon_pid()
    if old_pid:
        log.warning(f"Killing stale TTS daemon PID {old_pid}")
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(2)
            if psutil.pid_exists(old_pid):
                os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    log.info("Starting fresh TTS daemon...")
    python = sys.executable
    out_log = open(BASE / "tts_daemon.out", "a")
    proc = subprocess.Popen(
        [python, str(TTS_DAEMON)],
        cwd=str(BASE),
        stdout=out_log,
        stderr=out_log,
        start_new_session=True,
    )
    PID_FILE.write_text(str(proc.pid))
    log.info(f"TTS daemon started PID {proc.pid}")
    time.sleep(5)  # allow model loading
    return proc.pid


def _port_is_free(port: int) -> bool:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def restart_main_server():
    """Restart the main JARVIS Flask server via run_nexus.sh.

    Never spawns a replacement unless the old process is confirmed gone and
    port 5001 is confirmed free — spawning blind onto a still-held port just
    produces a duplicate process that fails to bind and piles up (this is
    exactly what used to happen: a slow-but-alive main server would get
    SIGTERM'd, a fixed 3s sleep would elapse, and a new process would spawn
    anyway while the old one — still shutting down or simply not dead yet —
    kept the port, leaving both running).
    """
    log.warning("Restarting main JARVIS server...")
    old_pid = get_main_server_pid()
    if old_pid:
        try:
            os.kill(old_pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + PORT_FREE_TIMEOUT_S
        killed_hard = False
        while time.monotonic() < deadline:
            if not psutil.pid_exists(old_pid) and _port_is_free(5001):
                break
            if not killed_hard and time.monotonic() > deadline - PORT_FREE_TIMEOUT_S / 2:
                try:
                    os.kill(old_pid, signal.SIGKILL)
                    killed_hard = True
                except ProcessLookupError:
                    pass
            time.sleep(1)

    if not _port_is_free(5001):
        log.error(
            "Port 5001 still held after waiting — aborting restart "
            "(refusing to spawn a duplicate onto a busy port)"
        )
        return

    python  = "/opt/homebrew/bin/python3.13"
    out_log = open(BASE / "jarvis-nexus.out", "a")
    proc = subprocess.Popen(
        [python, str(BASE / "server.py")],
        cwd=str(BASE),
        stdout=out_log,
        stderr=open(BASE / "jarvis-nexus.err", "a"),
        start_new_session=True,
    )
    log.info(f"Main server restarted PID {proc.pid}")


def restart_fish_speech():
    """Kill a hung/dead Fish Speech child process and relaunch it. Mirrors the
    launch command in server.py's _start_fish_speech_server() — Fish Speech is
    JARVIS's own child, so a wedge in it doesn't need a full main-server
    restart, just replacing this one process."""
    old_pid = get_fish_pid()
    if old_pid:
        log.warning(f"Fish Speech hung/unresponsive — restarting PID {old_pid}")
        try:
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(2)
            if psutil.pid_exists(old_pid):
                os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not _port_is_free(8080):
            time.sleep(0.5)

    python  = FISH_VENV / "bin" / "python"
    decoder = FISH_CKPT / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
    if not python.exists() or not decoder.exists():
        log.error("Fish Speech venv/checkpoint missing — cannot restart")
        return

    log_path = Path("/tmp/sovereign_tts/fish_server.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a")
    proc = subprocess.Popen(
        [str(python), "-m", "tools.api_server",
         "--llama-checkpoint-path", str(FISH_CKPT),
         "--decoder-checkpoint-path", str(decoder),
         "--listen", "127.0.0.1:8080",
         "--half"],
        cwd=str(FISH_REPO),
        stdout=log_fh, stderr=log_fh,
        start_new_session=True,
    )
    log.info(f"Fish Speech restarted PID {proc.pid}")


# ── Cache purge ───────────────────────────────────────────────────────────────
def purge_audio_cache() -> int:
    """Delete WAV/MP3 temp files older than CACHE_MAX_AGE_S. Returns count removed."""
    count  = 0
    now    = time.time()
    target = AUDIO_CACHE if AUDIO_CACHE.exists() else BASE / "tts_output"
    for ext in ("*.wav", "*.mp3", "*.aiff"):
        for f in target.glob(ext):
            try:
                age = now - f.stat().st_mtime
                if age > CACHE_MAX_AGE_S:
                    f.unlink()
                    count += 1
            except Exception:
                pass
    # Also purge /tmp tts_ files left by the daemon
    for f in Path(tempfile.gettempdir()).glob("tts_*.wav"):
        try:
            age = now - f.stat().st_mtime
            if age > CACHE_MAX_AGE_S:
                f.unlink()
                count += 1
        except Exception:
            pass
    return count


# ── Resource monitor ──────────────────────────────────────────────────────────
def log_system_resources(restart_on_fail: bool = True):
    vm   = psutil.virtual_memory()
    disk = psutil.disk_usage(str(BASE))
    cpu  = psutil.cpu_percent(interval=0.5)
    log.debug(
        f"System: CPU {cpu:.1f}%  RAM {vm.used//1048576}MB/{vm.total//1048576}MB "
        f"({vm.percent:.0f}%)  Disk {disk.used//1073741824}GB/{disk.total//1073741824}GB"
    )

    # Check main server memory
    pid = get_main_server_pid()
    if pid:
        mem = get_process_mem_mb(pid)
        log.debug(f"JARVIS server (PID {pid}): {mem:.0f}MB RAM")
        if mem > MEM_LIMIT_MB:
            if restart_on_fail:
                log.warning(f"JARVIS server memory {mem:.0f}MB > {MEM_LIMIT_MB}MB — restarting")
                restart_main_server()
            else:
                log.warning(f"JARVIS server memory {mem:.0f}MB > {MEM_LIMIT_MB}MB — in startup grace, not restarting")


# ── Single health-check cycle ─────────────────────────────────────────────────
def run_health_check(restart_on_fail: bool = True) -> dict:
    """Runs one full check. `restart_on_fail` gates ALL restart side-effects here
    (memory-limit breach) — but a single failed main-server ping does NOT restart
    on its own anymore; that decision needs consecutive failures and belongs to
    the caller (run_loop), which has the failure-count context this function
    doesn't. Restarting on the very first slow/failed ping is what caused
    duplicate-process pileups when the server was just busy, not actually down.
    """
    results = {}

    # 1. Main JARVIS server
    r = check_main_server()
    results["main_server"] = r
    if r["ok"]:
        log.info(f"Main server OK ({r['ms']}ms)")
    else:
        log.warning(f"Main server unhealthy: status={r['status']} ms={r['ms']}")

    # 2. TTS engines endpoint
    r = check_tts_engines()
    results["tts_engines"] = r
    if r["ok"]:
        log.info(f"TTS engines OK: {r.get('engines',[])} ({r['ms']}ms)")
    elif r.get("err") == "unreachable":
        log.info("TTS daemon not running (embedded in main server — OK)")
    else:
        log.warning(f"TTS engines check failed: {r}")

    # 2b. Fish Speech child process — the actual TTS engine, separate from
    # both the main server and the vestigial TTS_HOST daemon above.
    r = check_fish_speech()
    results["fish_speech"] = r
    if r["ok"]:
        log.info(f"Fish Speech OK ({r['ms']}ms)")
    elif get_fish_pid() is None:
        log.info("Fish Speech has no process (not installed, still starting, or just died)")
    else:
        log.warning(f"Fish Speech unhealthy (process present but unresponsive): {r}")

    # 3. Cache purge
    purged = purge_audio_cache()
    if purged:
        log.info(f"Purged {purged} stale audio cache files")
    results["cache_purged"] = purged

    # 4. System resources
    log_system_resources(restart_on_fail=restart_on_fail)

    return results


# ── Main loop ─────────────────────────────────────────────────────────────────
def run_loop(interval: int = HEALTH_INTERVAL):
    log.info(f"TTS Watchdog started — interval={interval}s")
    log.info(f"Monitoring: {MAIN_SERVER_URL}")
    log.info(f"Cache dir:  {AUDIO_CACHE}")
    log.info(f"Log:        {LOG_FILE}")

    consecutive_failures = 0
    restart_attempts     = 0   # counts restarts that did NOT bring the server back healthy
    fish_consecutive_failures = 0
    fish_restart_attempts     = 0
    fish_restart_time         = 0.0   # monotonic time of last restart_fish_speech() call
    FISH_STARTUP_GRACE_S      = 60    # model load + MPS session init observed at ~10-20s;
                                       # 60s gives headroom without the 2x30s check cadence
                                       # racing ahead of boot and restarting an instance that
                                       # was already coming up fine, which just wastes the
                                       # MAX_RESTART_ATTEMPTS budget on a false alarm
    check_num            = 0
    watchdog_start       = time.monotonic()
    MAX_RESTART_ATTEMPTS = 3   # stop auto-restarting after this many failed attempts in a
                               # row — a crash loop is a problem restarting harder can't fix,
                               # and churning forever is what caused today's process pileup

    # run_health_check() is executed in a worker thread so the loop can enforce a
    # hard wall-clock timeout on it. Incident 2026-08-06: it hung forever inside
    # a local psutil/disk syscall (the external volume stalled) — every *network*
    # call in this file has a timeout, but that one didn't, so the process froze
    # solid for 10 days without ever exiting. launchd's KeepAlive only restarts a
    # job that exits, so a hang-but-alive process is invisible to it; forcing an
    # exit on timeout is what makes this self-healing again.
    CHECK_TIMEOUT_S = 60
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="healthcheck")

    while True:
        check_num += 1
        log.info(f"── Health check #{check_num} ─────────────────────────")
        in_grace = (time.monotonic() - watchdog_start) < STARTUP_GRACE_S
        try:
            # During the grace window, observe but never act — server.py's own
            # warmup chain (vision/Ollama/Kokoro/Fish Speech) can legitimately
            # take longer than one check interval.
            future = executor.submit(run_health_check, restart_on_fail=not in_grace)
            results = future.result(timeout=CHECK_TIMEOUT_S)
            all_ok = results.get("main_server", {}).get("ok", False)
            if all_ok:
                consecutive_failures = 0
                restart_attempts = 0
            else:
                consecutive_failures += 1
                if in_grace:
                    log.info("Still in startup grace period — not restarting yet")
                elif restart_attempts >= MAX_RESTART_ATTEMPTS:
                    log.error(
                        f"Gave up after {restart_attempts} restart attempts — "
                        "server still unhealthy. Not restarting again automatically; "
                        "check server manually (jarvis-nexus.err / fish_server.log)."
                    )
                elif consecutive_failures >= FAILURES_BEFORE_RESTART:
                    log.warning(
                        f"{consecutive_failures} consecutive failures — restarting main server "
                        f"(attempt {restart_attempts + 1}/{MAX_RESTART_ATTEMPTS})"
                    )
                    restart_main_server()
                    restart_attempts += 1
                    consecutive_failures = 0
                    watchdog_start = time.monotonic()  # re-arm grace period for the new process

            # Fish Speech: independent of the main-server outcome above — Flask
            # can be perfectly healthy while its Fish Speech child is wedged.
            # Gate on whether Fish Speech is actually *installed* (venv +
            # checkpoint present on disk), not on whether a PID currently
            # exists — a hard-killed/crashed process leaves no PID at all,
            # which looks identical to "never installed" if you only check
            # get_fish_pid(), and would silently stop auto-recovering after
            # exactly the kind of hard failure this is meant to catch.
            fish_installed = (
                (FISH_VENV / "bin" / "python").exists()
                and (FISH_CKPT / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth").exists()
            )
            fish_in_grace = (time.monotonic() - fish_restart_time) < FISH_STARTUP_GRACE_S
            fish_r = results.get("fish_speech", {})
            if fish_r.get("ok"):
                fish_consecutive_failures = 0
                fish_restart_attempts = 0
            elif not in_grace and fish_installed:
                if fish_in_grace:
                    log.info("Fish Speech still in its own post-restart grace period — not acting yet")
                else:
                    fish_consecutive_failures += 1
                    if fish_restart_attempts >= MAX_RESTART_ATTEMPTS:
                        log.error(
                            f"Gave up on Fish Speech after {fish_restart_attempts} restart "
                            "attempts — still unhealthy. Not restarting again automatically; "
                            "check fish_server.log manually."
                        )
                    elif fish_consecutive_failures >= FAILURES_BEFORE_RESTART:
                        log.warning(
                            f"Fish Speech: {fish_consecutive_failures} consecutive failures — "
                            f"restarting (attempt {fish_restart_attempts + 1}/{MAX_RESTART_ATTEMPTS})"
                        )
                        restart_fish_speech()
                        fish_restart_attempts += 1
                        fish_consecutive_failures = 0
                        fish_restart_time = time.monotonic()
        except concurrent.futures.TimeoutError:
            log.critical(
                f"Health check #{check_num} hung > {CHECK_TIMEOUT_S}s (stuck syscall — e.g. "
                "external volume stall). Force-exiting so launchd relaunches a clean process "
                "rather than sitting frozen indefinitely."
            )
            os._exit(1)
        except Exception as e:
            log.exception(f"Watchdog check error: {e}")

        gc.collect()
        time.sleep(interval)


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sovereign Nexus TTS Watchdog")
    parser.add_argument("--once",     action="store_true", help="Single check then exit")
    parser.add_argument("--interval", type=int, default=30,
                        help="Check interval in seconds (default 30)")
    args = parser.parse_args()
    interval = args.interval

    if args.once:
        results = run_health_check(restart_on_fail=False)
        print(json.dumps(results, indent=2))
        sys.exit(0 if results.get("main_server", {}).get("ok") else 1)

    # Handle SIGTERM gracefully
    def _sig(sig, frame):
        log.info("Watchdog shutting down on signal")
        sys.exit(0)
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT,  _sig)

    run_loop(interval)


if __name__ == "__main__":
    main()
