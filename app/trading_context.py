"""
trading_context — pulls AXIS bot performance (equity, daily/weekly P&L, win
rate, circuit-breaker state) and per-strategy-module health (degraded vs its
own validated backtest baseline — see AXIS's workers/strategy_monitor.py)
from the AXIS backend's own already-built /health and /backtest/health-check
endpoints, on a background thread. Read-only, never touches AXIS's engine.

Feeds brain.py's existing (previously unused) _inject_financial hook — the
built formatted summary is plain facts appended to the system prompt, no
"mention this" instruction, matching how e.g. today's date is already
injected: the model decides relevance itself rather than being told when to
bring it up. Deliberately not using the same explicit-instruction pattern as
project_health's _health_note — see that module's incident notes for why
instructing a small local model to proactively act on injected context is
risky; plain unprompted facts are the safer default.
"""

import json
import threading
import time
import urllib.request

AXIS_BASE = "http://localhost:8000"
CHECK_INTERVAL_S = 60
_TIMEOUT_S = 6

_lock = threading.Lock()
_summary = ""

# Proactive-push state. Deliberately NOT routed through the LLM at all —
# see this module's own docstring on why instructing the model to act on
# injected context is risky. This is a plain Python threshold check calling
# proactive_push() directly, the same mechanism project_health.py uses, so
# it can't repeat that failure mode (there's no model in this path to
# hallucinate a reaction).
_LOSS_ALERT_THRESHOLD_USD = -50.0
_circuit_was_broken = False
_loss_alert_sent = False


def _fetch(path: str):
    try:
        with urllib.request.urlopen(f"{AXIS_BASE}{path}", timeout=_TIMEOUT_S) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _check_proactive_alerts(health: dict) -> None:
    global _circuit_was_broken, _loss_alert_sent

    circuit_broken = bool(health.get("circuit_broken"))
    daily_pnl = health.get("daily_pnl_usd", 0)

    if circuit_broken and not _circuit_was_broken:
        _push(f"AXIS: circuit breaker tripped — trading halted (daily P&L ${daily_pnl:+.2f})")
    _circuit_was_broken = circuit_broken

    if daily_pnl < _LOSS_ALERT_THRESHOLD_USD and not _loss_alert_sent:
        _loss_alert_sent = True
        _push(f"AXIS: daily P&L down to ${daily_pnl:+.2f}")
    elif daily_pnl >= 0:
        _loss_alert_sent = False  # new day / recovered — can alert again if it dips


def _push(message: str) -> None:
    try:
        from even_glasses import proactive_push
        proactive_push(message)
    except Exception:
        pass  # best-effort — never let a push failure break the health poll


def _build_summary() -> str:
    health = _fetch("/health")
    if health is None:
        return ""  # backend unreachable — no context beats stale/fake context

    _check_proactive_alerts(health)

    state = "PAUSED" if not health.get("system_active") else "ACTIVE"
    if health.get("circuit_broken"):
        state += ", CIRCUIT BROKEN (trading halted)"

    lines = [
        f"AXIS trading bot status: {state}",
        f"Today: {health.get('trades_today', 0)} trades "
        f"({health.get('wins_today', 0)}W/{health.get('losses_today', 0)}L), "
        f"P&L ${health.get('daily_pnl_usd', 0):+.2f} ({health.get('daily_pnl_pct', 0) * 100:+.2f}%)",
    ]

    weekly = health.get("weekly_goal") or {}
    if weekly:
        lines.append(
            f"This week: P&L ${weekly.get('weekly_pnl_usd', 0):+.2f} "
            f"toward ${weekly.get('weekly_goal_usd', 0):.0f} goal "
            f"({weekly.get('weekly_pct', 0):+.1f}%)"
        )

    check = _fetch("/backtest/health-check")
    if check:
        degraded = [m["module"] for m in check.get("modules", []) if m.get("degraded")]
        if degraded:
            lines.append(f"Degraded vs their validated baseline: {', '.join(degraded)}")

    account = _fetch("/trades/account")
    if account:
        lines.append(f"Account equity: ${account.get('equity', 0):,.2f} (paper account)")

    return "\n".join(lines)


def get_summary() -> str:
    with _lock:
        return _summary


_RELEVANCE_KEYWORDS = (
    "axis", "trading", "trade", "money", "p&l", "pnl", "profit", "loss",
    "equity", "portfolio", "win rate", "degraded", "baseline",
)


def is_relevant(text: str) -> bool:
    """Cheap keyword gate — callers should only pay the system-prompt token
    cost of get_summary() when a message is plausibly about it. Injecting it
    on every single message (including "hi") was pushing baseline prompt
    size past 1000 tokens against this model's 2048-token context window,
    which measurably hurt both latency and reliability (see git history
    around the AXIS feature's first verify pass — a stacked, oversized
    prompt is what let one request degenerate into garbage output that
    triggered a hallucinated, real EMAIL tool call). The explicit AXIS tool
    in toolkit.py still works for anything this misses."""
    low = text.lower()
    return any(k in low for k in _RELEVANCE_KEYWORDS)


def _loop(interval_s: float):
    global _summary
    while True:
        try:
            new_summary = _build_summary()
            with _lock:
                _summary = new_summary
        except Exception as e:
            print(f"[trading_context] check cycle error: {e}")
        time.sleep(interval_s)


def start_background_loop(interval_s: float = CHECK_INTERVAL_S):
    threading.Thread(target=_loop, args=(interval_s,), daemon=True).start()
