import os
import re
import sys
import json
import time
import uuid
import socket
import atexit
import platform
import threading
import subprocess
from pathlib import Path
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, redirect
from collections import OrderedDict
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "app"))

import dispatch
import auto
import sovereign_nexus
import telegram_notify
import storage_manager
import project_health
import trading_context
import project_monitor
from brain import JarvisBrain
from jarvis_core_personas import JARVIS_CORE_PERSONAS
from jarvis_regional_personas import JARVIS_REGIONAL_PERSONAS
from even_glasses import register_glasses_routes, proactive_push as _glasses_push
from glasses_mode_router import register_mode_route, start_mode_watcher

ALL_PERSONAS = {**JARVIS_CORE_PERSONAS, **JARVIS_REGIONAL_PERSONAS}

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "jarvis-nexus-secret")
app.jinja_env.auto_reload = True   # always re-read templates from disk (no stale bytecode cache)
CORS(app)

def requires_auth(f):
    return f

# requires_auth above is a deliberate no-op — it's already applied to ~50
# routes (chat/speak/memory/etc.) that voice activation and the glasses
# daemon hit directly with no browser login step; flipping it on site-wide
# would silently break those live flows. requires_pin_auth is a separate,
# narrower gate for the handful of routes that can actually control the
# screen/keyboard or run shell commands (/api/nexus/run, /api/auto,
# /api/browser, /api/dispatch, /api/terminal) — those had zero auth of any
# kind, which matters more once the away-mode Cloudflare tunnel can expose
# them publicly.
import functools

def requires_pin_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            return jsonify({"ok": False, "msg": "Authentication required — visit /login"}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route("/login", methods=["GET"])
def login_page():
    if session.get("authed"):
        return redirect("/")
    return render_template("login.html", error=None)


@app.route("/login", methods=["POST"])
def login_submit():
    pin = request.form.get("pin", "")
    expected = os.environ.get("JARVIS_PIN", "")
    if expected and pin == expected:
        session["authed"] = True
        return redirect("/")
    return render_template("login.html", error="Incorrect PIN"), 401


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("authed", None)
    return redirect("/login")

# One JarvisBrain per browser/device, not one shared global — verified live
# that a single shared instance lets concurrent callers' conversation turns
# bleed into each other (request B's reply included request A's answer,
# because A's turn was already sitting in the one shared `self.memory` by
# the time B built its prompt; this is a shared-state bug, not a race a lock
# would fix — two callers need two histories, not serialized access to one).
# Keyed off a signed session cookie so the same browser/tab keeps its
# conversation across requests; capped so an unbounded string of new
# devices/cookies can't grow this forever.
_current_model = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
_brains: "OrderedDict[str, JarvisBrain]" = OrderedDict()
_brains_lock = threading.Lock()
_MAX_BRAINS = 50

def _get_brain() -> JarvisBrain:
    if "sid" not in session:
        session["sid"] = uuid.uuid4().hex
    sid = session["sid"]
    with _brains_lock:
        brain = _brains.get(sid)
        if brain is None:
            brain = JarvisBrain()
            _brains[sid] = brain
            if len(_brains) > _MAX_BRAINS:
                _brains.popitem(last=False)  # evict the oldest session
        else:
            _brains.move_to_end(sid)
        brain.model = _current_model
    return brain

# The glasses are a single physical device, not several concurrent browser
# tabs, so they don't hit the cross-talk bug above — a dedicated instance
# (kept out of the `_brains` session pool) is correct here, not a shortcut.
_glasses_brain = JarvisBrain()
register_glasses_routes(app, _glasses_brain)
register_mode_route(app)
start_mode_watcher()

try:
    from twilio_voice import register_twilio_routes
    register_twilio_routes(app)
except ImportError:
    print("[twilio] 'twilio'/'flask-sock' not installed — CALL_ME disabled "
          "until `pip install twilio flask-sock`", flush=True)

def _warmup_ollama():
    """Pre-load the model into memory so the first real request is instant."""
    try:
        import urllib.request as _ur
        payload = json.dumps({
            "model": os.environ.get("OLLAMA_MODEL", "llama3.2:latest"),
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False,
            "options": {"num_predict": 1, "temperature": 0, "num_ctx": 3072},
        }).encode()
        req = _ur.Request("http://localhost:11434/api/chat", data=payload,
                          headers={"Content-Type": "application/json"})
        _ur.urlopen(req, timeout=60).read()
    except Exception:
        pass

threading.Thread(target=_warmup_ollama, daemon=True).start()
project_health.start_background_loop()
trading_context.start_background_loop()
project_monitor.start_background_loop()

# --- Alpha (Alpaca) connection ---
def get_alpha_status():
    try:
        from engine import TradeEngine
        engine = TradeEngine()
        account = engine.client.get_account()
        positions = engine.client.get_all_positions()
        return {
            "connected": True,
            "equity": str(account.equity),
            "buying_power": str(account.buying_power),
            "cash": str(account.cash),
            "positions": len(positions),
            "status": account.status,
            "paper": True,
        }
    except Exception as e:
        return {"connected": False, "error": str(e)}


# --- Spotify OAuth helpers ---
_SPOTIFY_SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "user-read-playback-state user-modify-playback-state "
    "user-library-read user-library-modify"
)
_SPOTIFY_REDIRECT = "http://localhost:5001/setup/spotify/callback"

def _write_env_key(key, value):
    """Persist a key=value into .env without touching other lines."""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    with open(env_path, "r") as f:
        lines = f.readlines()
    new_lines = []
    found = False
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(new_lines)
    os.environ[key] = value

# --- Routes ---


@app.route("/setup/spotify")
@requires_auth
def spotify_setup():
    client_id     = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    refresh_token = os.environ.get("SPOTIFY_REFRESH_TOKEN", "")
    connected = bool(refresh_token)
    return render_template("spotify_setup.html",
        client_id=client_id, client_secret=client_secret,
        connected=connected, redirect_uri=_SPOTIFY_REDIRECT)

@app.route("/setup/spotify/save", methods=["POST"])
@requires_auth
def spotify_save_creds():
    cid = request.form.get("client_id", "").strip()
    sec = request.form.get("client_secret", "").strip()
    if cid: _write_env_key("SPOTIFY_CLIENT_ID", cid)
    if sec: _write_env_key("SPOTIFY_CLIENT_SECRET", sec)
    import urllib.parse
    params = urllib.parse.urlencode({
        "client_id": cid or os.environ.get("SPOTIFY_CLIENT_ID", ""),
        "response_type": "code",
        "redirect_uri": _SPOTIFY_REDIRECT,
        "scope": _SPOTIFY_SCOPES,
    })
    return redirect(f"https://accounts.spotify.com/authorize?{params}")

@app.route("/setup/spotify/callback")
@requires_auth
def spotify_callback():
    code  = request.args.get("code")
    error = request.args.get("error")
    if error or not code:
        return f"<h2>Spotify auth failed: {error}</h2><a href='/setup/spotify'>Try again</a>"
    import urllib.request as _ur, base64
    cid = os.environ.get("SPOTIFY_CLIENT_ID", "")
    sec = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    creds = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    import urllib.parse
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _SPOTIFY_REDIRECT,
    }).encode()
    req = _ur.Request("https://accounts.spotify.com/api/token", data=body,
        headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
    try:
        resp = json.loads(_ur.urlopen(req, timeout=10).read())
        refresh_token = resp.get("refresh_token", "")
        if refresh_token:
            _write_env_key("SPOTIFY_REFRESH_TOKEN", refresh_token)
            return redirect("/setup/spotify?connected=1")
        return f"<h2>No refresh token in response</h2><pre>{resp}</pre>"
    except Exception as e:
        return f"<h2>Token exchange failed: {e}</h2><a href='/setup/spotify'>Try again</a>"

@app.route("/sw.js")
def service_worker():
    """Serve SW from root so its scope covers the whole app."""
    import pathlib
    sw_path = pathlib.Path(__file__).parent / "static" / "sw.js"
    resp = app.make_response(sw_path.read_text())
    resp.headers["Content-Type"]  = "application/javascript"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

@app.route("/")
@requires_auth
def overview():
    resp = app.make_response(render_template("overview.html"))
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp

@app.route("/dashboard")
@requires_auth
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/alpha")
@requires_auth
def alpha():
    return jsonify(get_alpha_status())


@app.route("/api/dispatch", methods=["POST"])
@requires_pin_auth
def dispatch_cmd():
    data = request.get_json(force=True)
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"ok": False, "msg": "No command provided"}), 400
    result = dispatch.execute(cmd)
    return jsonify(result)


@app.route("/api/screenshot")
@requires_auth
def live_screenshot():
    result = dispatch.execute("screenshot")
    return jsonify(result)


@app.route("/api/project-health")
@requires_auth
def project_health_status():
    return jsonify({"issues": project_health.get_issues()})


@app.route("/api/trading-context")
@requires_auth
def trading_context_status():
    return jsonify({"summary": trading_context.get_summary()})


@app.route("/api/status")
@requires_auth
def status():
    import platform, datetime
    w, h = __import__("pyautogui").size()
    return jsonify({
        "system": platform.system(),
        "node": platform.node(),
        "screen": f"{w}x{h}",
        "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": platform.python_version(),
    })


@app.route("/api/auto", methods=["POST"])
@requires_pin_auth
def auto_run():
    data = request.get_json(force=True)
    goal = data.get("goal", "").strip()
    max_steps = int(data.get("max_steps", 60))
    if not goal:
        return jsonify({"ok": False, "msg": "No goal provided"}), 400
    if auto.is_running():
        return jsonify({"ok": False, "msg": "Agent already running"}), 409

    def generate():
        events = auto.run(goal, max_steps=max_steps)
        for ev in events:
            yield f"data: {json.dumps(ev)}\n\n"
        yield "data: {\"type\":\"end\"}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/auto/stop", methods=["POST"])
@requires_auth
def auto_stop():
    auto.stop()
    return jsonify({"ok": True, "msg": "Stop signal sent"})


@app.route("/api/auto/status")
@requires_auth
def auto_status():
    return jsonify({"running": auto.is_running()})


# ── Sovereign Nexus PTAE Agent ────────────────────────────────────────────────
@app.route("/api/nexus/run", methods=["POST"])
@requires_pin_auth
def nexus_run():
    data      = request.get_json(force=True)
    goal      = data.get("goal", "").strip()
    max_steps = int(data.get("max_steps", 60))
    if not goal:
        return jsonify({"ok": False, "msg": "No goal provided"}), 400
    if sovereign_nexus.is_running():
        return jsonify({"ok": False, "msg": "Nexus agent already running"}), 409

    def generate():
        last_t = [time.time()]
        for ev in sovereign_nexus.run_stream(goal, max_steps=max_steps):
            ka = _sse_keepalive(last_t, interval=15.0)
            if ka: yield ka
            last_t[0] = time.time()
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers=_sse_headers())


@app.route("/api/nexus/stop", methods=["POST"])
@requires_auth
def nexus_stop():
    sovereign_nexus.stop()
    return jsonify({"ok": True, "msg": "Stop signal sent"})


@app.route("/api/nexus/status")
@requires_auth
def nexus_status():
    return jsonify(sovereign_nexus.get_status())


@app.route("/api/nexus/audit")
@requires_auth
def nexus_audit():
    n = int(request.args.get("n", 80))
    return jsonify({"lines": sovereign_nexus.get_audit_tail(n)})


def _sse_headers():
    """Headers that keep SSE alive through Cloudflare / proxies."""
    return {
        "Cache-Control":     "no-cache, no-transform",
        "X-Accel-Buffering": "no",          # nginx
        "Connection":        "keep-alive",
    }

def _sse_keepalive(last_t: list, interval: float = 20.0):
    """Yield an SSE comment if more than `interval` seconds have passed."""
    now = time.time()
    if now - last_t[0] >= interval:
        last_t[0] = now
        return ": keep-alive\n\n"
    return None


@app.route("/api/logs/stream")
@requires_pin_auth
def logs_stream():
    """Live-tail jarvis-nexus.err for the UI's terminal drawer. Raw and
    unfiltered by design (that's what was asked for) — gated behind real
    PIN auth rather than the site-wide no-op, since a raw log can contain
    tracebacks with values a casual read-only route shouldn't expose."""
    log_path = Path(__file__).parent / "jarvis-nexus.err"

    def generate():
        last_ka = [time.time()]
        try:
            with open(log_path, "r", errors="replace") as f:
                f.seek(0, os.SEEK_END)
                while True:
                    line = f.readline()
                    if line:
                        yield f"data: {json.dumps({'line': line.rstrip()})}\n\n"
                    else:
                        ka = _sse_keepalive(last_ka)
                        if ka:
                            yield ka
                        time.sleep(0.5)
        except GeneratorExit:
            return

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                     headers=_sse_headers())


@app.route("/api/chat", methods=["POST"])
@requires_auth
def chat():
    """Compatibility shim: old jarvis_dust.html SSE event format → current brain."""
    data = request.get_json(force=True)
    text = data.get("message", "").strip()
    lang = data.get("lang", "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "No input"}), 400

    # Prepend a light lang hint so the LLM doesn't drift to English
    _LANG_NAMES_SHORT = {
        "ja": "Japanese", "zh": "Chinese", "fr": "French", "de": "German",
        "es": "Spanish", "pt": "Portuguese", "ko": "Korean", "ru": "Russian",
        "ar": "Arabic", "hi": "Hindi", "it": "Italian",
    }
    if lang and lang not in ("en", "en-gb"):
        lang_name = _LANG_NAMES_SHORT.get(lang, lang.upper())
        text = f"[Respond in {lang_name}] {text}"

    # Must resolve (and possibly create) the session's brain here, before the
    # streaming Response exists — session mutations made *inside* generate()
    # are invisible to Flask's cookie-saving step, which runs against the
    # response Flask finalizes right after this view function returns, well
    # before the generator body (and any session writes in it) ever executes.
    brain = _get_brain()

    def _strip_tool_syntax(s: str) -> str:
        clean = re.sub(r'\[[A-Z_]+:[^\]]*\]', '', s)
        return re.sub(r'\[[A-Z_]+\]', '', clean)

    def generate():
        t0     = time.time()
        last_t = [t0]
        reply_parts = []
        # Ollama streams token-by-token, so a tool-call bracket like
        # [WEATHER: NYC] routinely arrives split across many small chunks
        # (e.g. "[", "WEA", "THER", ":", " NYC", "]"). Stripping each chunk
        # independently — the old behavior — can never match a pattern split
        # across chunks, so raw tool-call syntax leaked straight into the
        # visible/spoken reply on essentially any tool call. Instead, hold
        # back everything from the last unmatched '[' onward until it either
        # closes (then strip+emit) or the stream ends (then flush as-is).
        pending = ""

        def _advance(buf: str) -> str:
            """Split buf into (safe-to-emit, still-pending) at the last unmatched '['."""
            last_open, last_close = buf.rfind('['), buf.rfind(']')
            if last_open > last_close:
                return buf[:last_open], buf[last_open:]
            return buf, ""

        def _emit_text(s: str):
            nonlocal pending
            clean = _strip_tool_syntax(s)
            if clean:
                reply_parts.append(clean)
                last_t[0] = time.time()
            return clean

        try:
            yield f"event: meta\ndata: {json.dumps({'model': os.environ.get('OLLAMA_MODEL','llama3.2'), 'mode': 'chat'})}\n\n"
            for event in brain.stream_think(text, financial_context=(trading_context.get_summary() if trading_context.is_relevant(text) else "")):
                ka = _sse_keepalive(last_t)
                if ka: yield ka
                if "chunk" in event:
                    pending += event["chunk"]
                    to_emit, pending = _advance(pending)
                    if to_emit:
                        clean = _emit_text(to_emit)
                        if clean:
                            yield f"event: text\ndata: {json.dumps({'text': clean})}\n\n"
                elif "tool" in event:
                    if pending:
                        clean = _emit_text(pending)
                        pending = ""
                        if clean:
                            yield f"event: text\ndata: {json.dumps({'text': clean})}\n\n"
                    last_t[0] = time.time()
                    yield f"event: tool_call\ndata: {json.dumps({'tool': event['tool'], 'input': {'command': event.get('arg', '')}})}\n\n"
                    yield f"event: tool_result\ndata: {json.dumps({'tool': event['tool'], 'result': str(event.get('result', ''))[:300]})}\n\n"
            if pending:
                clean = _emit_text(pending)
                pending = ""
                if clean:
                    yield f"event: text\ndata: {json.dumps({'text': clean})}\n\n"
        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'msg': str(e)})}\n\n"
            return
        full_reply = "".join(reply_parts)
        # Two failure shapes end up here: (1) full_reply is genuinely empty —
        # every chunk was valid tool-call syntax and got stripped, and no
        # tool produced synthesizable output; (2) full_reply is non-empty but
        # is PURELY leftover bracket junk, e.g. the model hallucinates
        # "[COIN Flip]" — mixed case, contains a space — which doesn't match
        # the strict [A-Z_]+ tool-call format so the display-stripping regex
        # above correctly leaves it alone (it can't tell hallucinated
        # tool-call syntax from a literal bracketed aside in normal prose),
        # and it leaks through as raw garbage instead. Stripping ANY
        # bracket-wrapped span (regardless of case) and checking what's left
        # catches both: real prose that happens to contain "[brackets]"
        # still leaves non-bracket text behind and is left untouched: only
        # a reply that is nothing *but* bracket junk trips the fallback.
        residual = re.sub(r'\[[^\]]*\]', '', full_reply).strip()
        if not residual:
            # Without this, the user gets total silence: no reply text, no
            # audio, no error, nothing visibly wrong to explain why. A short,
            # honest fallback beats a response that looks like it just didn't
            # happen, or worse, literally reads "[COIN Flip]" aloud.
            full_reply = "Sorry, I didn't get a clear answer for that — could you try rephrasing?"
            yield f"event: text\ndata: {json.dumps({'text': full_reply})}\n\n"
        yield f"event: action\ndata: {json.dumps({'reply': full_reply, 'action': None})}\n\n"
        yield f"event: done\ndata: {json.dumps({'elapsed': round(time.time()-t0, 2)})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers=_sse_headers())


@app.route("/api/think", methods=["POST"])
@requires_auth
def think():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "No input"}), 400

    brain = _get_brain()  # resolved before the streaming Response — see /api/chat for why

    def generate():
        last_t = [time.time()]
        for event in brain.stream_think(text, financial_context=(trading_context.get_summary() if trading_context.is_relevant(text) else "")):
            ka = _sse_keepalive(last_t)
            if ka: yield ka
            last_t[0] = time.time()
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: {\"done\": true}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers=_sse_headers())


@app.route("/api/memory", methods=["GET"])
@requires_auth
def memory_get():
    from toolkit import _load_memory
    return jsonify(_load_memory())


@app.route("/api/memory", methods=["POST"])
@requires_auth
def memory_set():
    from toolkit import _load_memory, _save_memory
    data = request.get_json(force=True)
    mem = _load_memory()
    mem.update(data)
    _save_memory(mem)
    return jsonify({"ok": True})


@app.route("/api/memory/<key>", methods=["DELETE"])
@requires_auth
def memory_delete(key):
    from toolkit import _load_memory, _save_memory
    mem = _load_memory()
    mem.pop(key, None)
    _save_memory(mem)
    return jsonify({"ok": True})


@app.route("/api/notify", methods=["POST"])
@requires_auth
def notify():
    data = request.get_json(force=True)
    text = data.get("text", "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "No text provided"}), 400
    result = telegram_notify.send(text)
    return jsonify(result)


@app.route("/api/metrics")
@requires_auth
def metrics():
    import psutil, datetime
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    net = psutil.net_io_counters()
    return jsonify({
        "cpu": cpu,
        "mem_used": mem.used,
        "mem_total": mem.total,
        "mem_percent": mem.percent,
        "disk_used": disk.used,
        "disk_total": disk.total,
        "disk_percent": disk.percent,
        "net_sent": net.bytes_sent,
        "net_recv": net.bytes_recv,
        "uptime": int(datetime.datetime.now().timestamp() - psutil.boot_time()),
    })


@app.route("/api/storage")
@requires_auth
def storage_status():
    return jsonify(storage_manager.get_status())


@app.route("/api/storage/purge", methods=["POST"])
@requires_auth
def storage_purge():
    data = request.get_json(force=True) or {}
    aggressive = bool(data.get("aggressive", False))
    result = storage_manager.run_purge(aggressive=aggressive)
    telegram_notify.send(
        f"*JARVIS STORAGE — Manual Purge*\nFreed *{result['freed_mb']} MB*\n"
        f"Disk now at *{result['post_stats']['percent']}%* "
        f"({result['post_stats']['free_gb']} GB free)"
    )
    return jsonify(result)


# ── Stub routes for legacy UI endpoints ──────────────────────────────────────

# ── Language detection helpers ────────────────────────────────────────────────
# ISO 639-1 → Kokoro lang code mapping
_LANG_MAP = {
    "en": "en", "en-gb": "en-gb",
    "ja": "ja", "zh-cn": "zh", "zh-tw": "zh", "zh": "zh",
    "fr": "fr", "de": "de", "es": "es", "pt": "pt",
    "ko": "ko", "ar": "ar", "ru": "ru", "hi": "hi",
    "it": "it", "nl": "nl", "tr": "tr", "pl": "pl",
    "sv": "sv", "da": "da", "fi": "fi", "no": "no",
    "sw": "sw", "he": "he", "el": "el", "ms": "ms",
}

_SUPPORTED_LANGS = {
    "en", "en-gb", "ja", "zh", "fr", "de", "es", "pt",
    "ko", "ar", "ru", "hi", "it", "sw",
}

def _detect_lang_text(text: str) -> str:
    """Detect language of text. Returns a supported lang code, defaults to 'en'."""
    if not text or len(text.strip()) < 4:
        return "en"
    try:
        from langdetect import detect, detect_langs, DetectorFactory
        DetectorFactory.seed = 0
        # Try the fast single-result first
        raw = detect(text)
        mapped = _LANG_MAP.get(raw, raw.split("-")[0])
        if mapped in _SUPPORTED_LANGS:
            return mapped
        # Top result was an unsupported language (e.g. "so"=Somali for English text).
        # Scan the full candidate list for the first supported language.
        for c in detect_langs(text):
            m = _LANG_MAP.get(str(c.lang), str(c.lang).split("-")[0])
            if m in _SUPPORTED_LANGS:
                return m
        return "en"
    except Exception:
        return "en"

# Whisper model singleton (lazy-loaded)
_whisper_model = None
_whisper_lock  = __import__("threading").Lock()

def _get_whisper():
    global _whisper_model
    if _whisper_model:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
            print("[STT] faster-whisper base model loaded", flush=True)
        except Exception as e:
            print(f"[STT] whisper load failed: {e}", flush=True)
    return _whisper_model

def _transcribe_whisper(wav_path: str) -> tuple[str, str]:
    """Transcribe wav and detect language. Returns (text, lang_code)."""
    model = _get_whisper()
    if not model:
        return "", "en"
    try:
        segments, info = model.transcribe(wav_path, beam_size=3, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        lang = _LANG_MAP.get(info.language, info.language)
        return text, lang
    except Exception as e:
        print(f"[STT] whisper transcribe error: {e}", flush=True)
        return "", "en"


@app.route("/api/speak", methods=["POST"])
@requires_auth
def speak():
    """
    Return audio bytes so the browser controls playback volume.
    Detects language from text → routes to best matching voice via sovereign TTS.
    Fallback chain: Kokoro → macOS say → edge-tts
    """
    import re as _re, traceback as _tb

    data      = request.get_json(force=True) or {}
    raw       = data.get("text", "").strip()
    persona   = data.get("persona", "default")
    lang_in   = data.get("lang", "")          # explicit lang override from client
    voice_id  = data.get("voice_id") or os.environ.get("JARVIS_VOICE_ID") or None
    req_engine = data.get("engine", "").strip()  # engine hint from client (kokoro/fish)
    if not raw:
        return Response(b"", status=204)

    text = _re.sub(r'\[[^\]]*\]', '', raw)
    text = _re.sub(r'[*_`#>~|]', '', text)
    text = _re.sub(r'\s+', ' ', text).strip()[:600]
    if not text:
        return Response(b"", status=204)

    # Detect language from text
    lang = lang_in or _detect_lang_text(text)

    active_engine = req_engine or os.environ.get("JARVIS_VOICE_ENG", "macos_say")

    # Languages Kokoro supports natively — everything else skips straight to macOS say
    # unless Fish Audio API key is set (enabling cloud synthesis for any language)
    # zh omitted: kokoro-onnx espeak backend lacks Chinese phoneme support
    _KOKORO_NATIVE_LANGS = {"en","en-gb","ja","fr","es","hi","it","pt"}

    # ── 1. Sovereign TTS — local fish server (free) / Kokoro (native) ───────────
    # fish → LocalFishSpeechEngine (localhost:8080, voice cloning, zero API cost)
    # kokoro-native → Kokoro via synthesize_direct
    # non-native, no fish → macOS say directly
    _wants_fish_engine = active_engine in ("fish", "fish_audio_api")
    _skip_kokoro = (
        lang not in _KOKORO_NATIVE_LANGS and not _wants_fish_engine
    )
    if not _skip_kokoro:
        try:
            import sovereign_nexus_tts as _tts
            # Forward ANY explicit client-provided engine hint (req_engine, before
            # the "macos_say" env-var default gets mixed into active_engine) — this
            # used to only forward fish variants, silently discarding e.g. "kokoro"
            # from the frontend's own per-language voice choice and falling through
            # to the auto-select cascade (which currently prefers Fish Speech),
            # completely overriding what the client actually asked for.
            _eng_hint = req_engine or (active_engine if _wants_fish_engine else None)
            result = _tts.synthesize_direct(text, persona=persona,
                                            emotion=data.get("emotion", "neutral"),
                                            voice_id=voice_id,
                                            engine_name=_eng_hint,
                                            lang=lang)
            audio_path = result["audio_path"]
            if os.path.exists(audio_path):
                with open(audio_path, "rb") as f:
                    audio = f.read()
                try: os.unlink(audio_path)
                except: pass
                if len(audio) > 100:
                    return Response(audio, mimetype="audio/wav",
                                    headers={
                                        "Content-Disposition": "inline",
                                        "X-Lang": lang,
                                        "X-TTS-Engine": result["engine"],
                                        "X-TTS-Voice": result["voice_id"],
                                    })
        except Exception as _e:
            # Falling back to macOS say silently here is exactly what made today's
            # voice-quality regression invisible for hours — always log why the
            # high-quality engine was skipped so a recurrence is diagnosable from
            # the server log alone instead of requiring a fresh investigation.
            print(f"[speak] sovereign TTS failed (engine_hint={_eng_hint!r}, "
                  f"lang={lang!r}, persona={persona!r}): {_e} — falling back to macOS say",
                  flush=True)

    # ── 3. macOS say (multilingual fallback, direct WAV — ~0.4-0.6s) ────────────
    _MACOS_VOICES = {
        "en":    "Reed (English (US))",
        "en-gb": "Reed (English (UK))",
        "fr":    "Thomas",
        "de":    "Reed (German (Germany))",
        "es":    "Paulina",
        "it":    "Alice",
        "pt":    "Luciana",
        "ja":    "Reed (Japanese (Japan))",
        "zh":    "Tingting",
        "zh-tw": "Meijia",
        "zh-hk": "Sinji",
        "ko":    "Yuna",
        "ar":    "Majed",
        "hi":    "Lekha",
        "ru":    "Milena",
        "nl":    "Xander",
        "tr":    "Yelda",
        "pl":    "Zosia",
        "sw":    "Reed (English (US))",
    }
    say_voice = _MACOS_VOICES.get(lang, "Reed (English (US))")
    say_rate  = "175"
    try:
        import tempfile as _tf
        wav = _tf.mktemp(suffix=".wav")
        ret = subprocess.run(
            ["/usr/bin/say", "-v", say_voice, "-r", say_rate,
             "-o", wav, "--data-format=LEI16@22050", "--", text],
            timeout=20, capture_output=True,
        )
        if ret.returncode != 0 or not os.path.exists(wav):
            # Fallback: two-step AIFF conversion
            aiff = wav.replace(".wav", ".aiff")
            subprocess.run(["/usr/bin/say", "-v", say_voice, "-r", say_rate, "-o", aiff, "--", text],
                           check=True, timeout=20, capture_output=True)
            subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@22050", aiff, wav],
                           check=True, timeout=10, capture_output=True)
            try: os.unlink(aiff)
            except: pass
        with open(wav, "rb") as f:
            audio = f.read()
        try: os.unlink(wav)
        except: pass
        if len(audio) > 100:
            return Response(audio, mimetype="audio/wav",
                            headers={"Content-Disposition": "inline",
                                     "X-Lang": lang,
                                     "X-TTS-Engine": f"macos_say:{say_voice}"})
    except Exception:
        pass

    # Last-resort fallback for Windows/Linux (the macOS `say` step above only
    # ever fires on Darwin — this covers the case where Kokoro isn't
    # provisioned yet and no cloud TTS key is set).
    if platform.system() != "Darwin":
        try:
            import pyttsx3, tempfile as _tf
            wav = _tf.mktemp(suffix=".wav")
            engine = pyttsx3.init()
            engine.save_to_file(text, wav)
            engine.runAndWait()
            if os.path.exists(wav):
                with open(wav, "rb") as f:
                    audio = f.read()
                try: os.unlink(wav)
                except: pass
                if len(audio) > 100:
                    return Response(audio, mimetype="audio/wav",
                                    headers={"Content-Disposition": "inline",
                                             "X-Lang": lang, "X-TTS-Engine": "pyttsx3"})
        except Exception:
            pass

    print(f"[speak] all TTS methods failed for lang={lang}", flush=True)
    return Response(b"", status=204)


# ── Fish streaming: prepare + stream endpoints ─────────────────────────────────
# POST /api/speak/prepare  → stores params, returns {stream_url, session_id}
# GET  /api/speak/stream/<id> → streams WAV from local fish server in real-time
#
# This lets the frontend do:
#   audio.src = "/api/speak/stream/" + session_id
# so the browser starts playing when the first WAV chunks arrive (~3s into gen).
_STREAM_SESSIONS: dict = {}
_STREAM_SESSIONS_LOCK = threading.Lock()

def _expire_stream_sessions():
    """Remove sessions older than 120 seconds."""
    cutoff = time.time() - 120
    with _STREAM_SESSIONS_LOCK:
        stale = [k for k, v in _STREAM_SESSIONS.items() if v.get("ts", 0) < cutoff]
        for k in stale:
            del _STREAM_SESSIONS[k]

@app.route("/api/speak/prepare", methods=["POST"])
def speak_prepare():
    """
    Stores fish TTS params and returns a one-time streaming URL.
    Returns immediately (<10ms); the actual synthesis starts when the browser
    GETs /api/speak/stream/<session_id>.
    """
    data     = request.json or {}
    text     = (data.get("text") or "").strip()
    voice_id = data.get("voice_id") or ""
    lang     = data.get("lang") or "en"
    if not text or not voice_id:
        return {"error": "text and voice_id required"}, 400

    session_id = uuid.uuid4().hex
    _expire_stream_sessions()
    with _STREAM_SESSIONS_LOCK:
        _STREAM_SESSIONS[session_id] = {
            "text": text, "voice_id": voice_id, "lang": lang,
            "ts": time.time(),
        }
    return {"session_id": session_id,
            "stream_url": f"/api/speak/stream/{session_id}"}


@app.route("/api/speak/stream/<session_id>", methods=["GET"])
def speak_stream(session_id):
    """
    Streams WAV audio from the local fish server for the given session.
    Proxies chunks as they arrive so the browser can start playing in ~3s.
    Falls back to macOS say (complete WAV) if fish server is unavailable.
    """
    with _STREAM_SESSIONS_LOCK:
        params = _STREAM_SESSIONS.pop(session_id, None)
    if not params:
        return Response(b"", status=404)

    text     = params["text"]
    voice_id = params["voice_id"]
    lang     = params["lang"]

    # Try streaming from local fish server
    try:
        import sovereign_nexus_tts as _tts
        gen = _tts.stream_local_fish_speech(text, voice_id, lang=lang)
        # Peek at the first chunk to detect errors early
        first = next(gen, None)
        if first and len(first) >= 4:
            def _stream():
                yield first
                yield from gen
            return Response(
                stream_with_context(_stream()),
                mimetype="audio/wav",
                # Transfer-Encoding is intentionally NOT set here — Werkzeug already
                # applies real chunked encoding automatically for a generator response
                # with no Content-Length. Setting it manually duplicated the header;
                # any client that respects both (curl, and likely some browser fetch
                # paths) tried to de-chunk twice, choked on the raw WAV bytes on the
                # second pass, and the stream silently failed.
                headers={"X-Accel-Buffering": "no",
                         "Cache-Control": "no-cache",
                         "X-TTS-Engine": "local_fish_speech",
                         "X-TTS-Voice": voice_id},
            )
    except Exception as e:
        print(f"[speak_stream] fish failed, falling back to macOS say: {e}", flush=True)

    # Fallback: macOS say (complete WAV, fast)
    _MACOS_VOICES_FB = {
        "en": "Reed (English (US))", "en-gb": "Reed (English (UK))",
        "fr": "Thomas", "de": "Reed (German (Germany))", "es": "Paulina",
        "it": "Alice",  "pt": "Luciana", "ja": "Reed (Japanese (Japan))",
        "zh": "Tingting", "zh-tw": "Meijia", "zh-hk": "Sinji",
        "ko": "Yuna", "ar": "Majed", "hi": "Lekha", "ru": "Milena",
    }
    say_voice = _MACOS_VOICES_FB.get(lang, "Reed (English (US))")
    import tempfile as _tf
    wav = _tf.mktemp(suffix=".wav")
    if platform.system() == "Darwin":
        try:
            ret = subprocess.run(
                ["/usr/bin/say", "-v", say_voice, "-r", "175",
                 "-o", wav, "--data-format=LEI16@22050", "--", text],
                timeout=20, capture_output=True,
            )
            if ret.returncode != 0 or not os.path.exists(wav):
                aiff = wav.replace(".wav", ".aiff")
                subprocess.run(["/usr/bin/say", "-v", say_voice, "-r", "175", "-o", aiff, "--", text],
                               check=True, timeout=20, capture_output=True)
                subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@22050", aiff, wav],
                               check=True, timeout=10, capture_output=True)
                try: os.unlink(aiff)
                except: pass
            with open(wav, "rb") as f:
                audio = f.read()
            try: os.unlink(wav)
            except: pass
            return Response(audio, mimetype="audio/wav",
                            headers={"X-TTS-Engine": f"macos_say:{say_voice}"})
        except Exception:
            try: os.unlink(wav)
            except: pass
    else:
        # Windows/Linux last resort — same pyttsx3 fallback as /api/speak.
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.save_to_file(text, wav)
            engine.runAndWait()
            if os.path.exists(wav):
                with open(wav, "rb") as f:
                    audio = f.read()
                try: os.unlink(wav)
                except: pass
                if len(audio) > 100:
                    return Response(audio, mimetype="audio/wav",
                                    headers={"X-TTS-Engine": "pyttsx3"})
        except Exception:
            pass
    return Response(b"", status=204)


@app.route("/api/tts", methods=["POST"])
@requires_auth
def tts_sovereign():
    """
    Sovereign Nexus TTS — hardware-routed local synthesis.
    Engines: Fish Audio S2 → Chatterbox → Kokoro → macOS say
    Returns audio/wav bytes for browser playback.
    """
    import traceback as _tb
    data    = request.get_json(force=True) or {}
    text    = data.get("text", "").strip()
    persona = data.get("persona", "default")
    emotion = data.get("emotion", "neutral")
    engine  = data.get("engine", None)
    if not text:
        return Response(b"", status=204)

    # Strip markdown/tool brackets
    import re as _re
    text = _re.sub(r'\[[^\]]*\]', '', text)
    text = _re.sub(r'[*_`#>~|]', '', text)
    text = _re.sub(r'\s+', ' ', text).strip()[:600]
    if not text:
        return Response(b"", status=204)

    try:
        import sovereign_nexus_tts as _tts
        result = _tts.synthesize_direct(text, persona=persona,
                                        emotion=emotion, engine_name=engine)
        audio_path = result["audio_path"]
        if os.path.exists(audio_path):
            with open(audio_path, "rb") as f:
                audio = f.read()
            try:
                os.unlink(audio_path)
            except Exception:
                pass
            return Response(audio, mimetype="audio/wav",
                            headers={"Content-Disposition": "inline",
                                     "X-TTS-Engine": result["engine"],
                                     "X-TTS-Accelerator": result["accelerator"]})
    except Exception as e:
        print(f"[tts] sovereign engine failed: {e}\n{_tb.format_exc()}", flush=True)
        # Fall back to /api/speak behavior
        return speak()

    return Response(b"", status=204)


@app.route("/api/tts/engines")
@requires_auth
def tts_engines():
    """Return hardware profile and available TTS engine status."""
    try:
        import sovereign_nexus_tts as _tts
        hw = _tts._HW_PROFILE
        return jsonify({
            "hardware": {
                "system":      hw.system,
                "accelerator": hw.accelerator,
                "memory_gb":   round(hw.memory_gb, 1),
                "device":      hw.device,
                "tier":        hw.tier(),
            },
            "engines":   _tts.engine_status(),
            "install":   _tts.install_guide(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mode", methods=["GET", "POST"])
@requires_auth
def mode():
    if request.method == "POST":
        return jsonify({"ok": True, "mode": "chat"})
    return jsonify({"mode": "chat"})

@app.route("/api/engine/state")
@requires_auth
def engine_state():
    return jsonify({"state": "idle", "running": False})

@app.route("/api/integrity")
@requires_auth
def integrity():
    return jsonify({"files": [], "ok": True})

_LANG_NAMES = {
    "en": "English", "en-gb": "English (British)", "ja": "Japanese",
    "zh": "Chinese", "fr": "French", "de": "German", "es": "Spanish",
    "pt": "Portuguese", "ko": "Korean", "ar": "Arabic", "ru": "Russian",
    "hi": "Hindi", "it": "Italian", "nl": "Dutch", "tr": "Turkish",
    "pl": "Polish", "sv": "Swedish", "da": "Danish", "fi": "Finnish",
    "no": "Norwegian", "sw": "Swahili", "he": "Hebrew", "el": "Greek",
    "ms": "Malay",
}

@app.route("/api/language", methods=["GET", "POST"])
@requires_auth
def language():
    """Detect language from text or audio. Returns {language, name, ok}."""
    if request.method == "GET":
        lang = os.environ.get("JARVIS_LANG", "en")
        return jsonify({
            "language":     lang,
            "current":      lang,
            "name":         _LANG_NAMES.get(lang, lang),
            "current_name": _LANG_NAMES.get(lang, lang),
            "languages":    _LANG_NAMES,
        })

    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()

    # Manual language selection from the picker
    code = (data.get("code") or "").strip()
    if code:
        os.environ["JARVIS_LANG"] = code
        return jsonify({"ok": True, "language": code, "name": _LANG_NAMES.get(code, code)})

    # Text-based detection
    if text:
        lang = _detect_lang_text(text)
        return jsonify({"ok": True, "language": lang, "name": _LANG_NAMES.get(lang, lang)})

    # Audio-based detection (same as transcribe but returns lang only)
    import tempfile as _tf
    audio = request.files.get("audio")
    if audio:
        suffix = ".webm"
        ct = audio.content_type or ""
        if "wav" in ct: suffix = ".wav"
        elif "mp4" in ct or "m4a" in ct: suffix = ".m4a"
        elif "ogg" in ct: suffix = ".ogg"
        src = wav = None
        try:
            with _tf.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                audio.save(tmp.name)
                src = tmp.name
            wav = src.rsplit(".", 1)[0] + ".wav"
            subprocess.run(
                ["/opt/homebrew/bin/ffmpeg", "-y", "-i", src,
                 "-ar", "16000", "-ac", "1", "-f", "wav", wav],
                check=True, timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _, lang = _transcribe_whisper(wav)
            return jsonify({"ok": True, "language": lang, "name": _LANG_NAMES.get(lang, lang)})
        except Exception as e:
            return jsonify({"ok": False, "language": "en", "name": "English", "msg": str(e)})
        finally:
            for p in [src, wav]:
                if p:
                    try: os.unlink(p)
                    except: pass

    return jsonify({"ok": True, "language": "en", "name": "English"})

@app.route("/api/bots")
@requires_auth
def bots():
    return jsonify([])

@app.route("/api/search/suggest")
@requires_auth
def search_suggest():
    return jsonify([])

@app.route("/api/search")
@requires_auth
def search():
    q = request.args.get("q", "")
    from toolkit import run_tool
    result = run_tool("SEARCH", q)
    return jsonify({"results": [{"title": "Search result", "snippet": result, "url": ""}]})

@app.route("/api/search/mac")
@requires_auth
def search_mac():
    return jsonify({"results": []})

@app.route("/api/search/open", methods=["POST"])
@requires_auth
def search_open():
    return jsonify({"ok": True})

@app.route("/api/research", methods=["POST"])
@requires_auth
def research():
    data = request.get_json(force=True)
    query = data.get("query", "")
    from toolkit import run_tool
    result = run_tool("SEARCH", query)
    return jsonify({"ok": True, "summary": result})

@app.route("/api/browser", methods=["POST"])
@requires_pin_auth
def browser():
    data = request.get_json(force=True)
    intent = data.get("intent", data.get("goal", ""))
    from toolkit import run_tool
    result = run_tool("BROWSER_DO", intent)
    return jsonify({"ok": True, "result": result})

@app.route("/api/vision", methods=["POST"])
@requires_auth
def vision():
    result = dispatch.execute("screenshot")
    return jsonify(result)

@app.route("/api/karpathy/proposals")
@requires_auth
def karpathy_proposals():
    return jsonify([])

@app.route("/api/karpathy/scan", methods=["POST"])
@requires_auth
def karpathy_scan():
    return jsonify({"ok": True})

@app.route("/api/karpathy/stats")
@requires_auth
def karpathy_stats():
    return jsonify({"total": 0, "pending": 0, "applied": 0})

@app.route("/api/karpathy/approve/<pid>", methods=["POST"])
@requires_auth
def karpathy_approve(pid):
    return jsonify({"ok": True})

@app.route("/api/karpathy/reject/<pid>", methods=["POST"])
@requires_auth
def karpathy_reject(pid):
    return jsonify({"ok": True})

@app.route("/api/karpathy/apply/<pid>", methods=["POST"])
@requires_auth
def karpathy_apply(pid):
    return jsonify({"ok": True})

@app.route("/api/ollama/models")
@requires_auth
def ollama_models():
    try:
        import urllib.request as _ur
        d = json.loads(_ur.urlopen("http://localhost:11434/api/tags", timeout=3).read())
        return jsonify({"models": [m["name"] for m in d.get("models", [])]})
    except Exception:
        return jsonify({"models": [os.environ.get("OLLAMA_MODEL", "llama3.2:latest")]})

@app.route("/api/ollama/model", methods=["POST"])
@requires_auth
def ollama_model():
    global _current_model
    data = request.get_json(force=True)
    model = data.get("model", "").strip()
    if model:
        os.environ["OLLAMA_MODEL"] = model
        _current_model = model  # applies to existing sessions' next call and all new ones
    return jsonify({"ok": True, "model": _current_model})

# ─────────────────────────────────────────────────────────────────────────────

def _warmup_vision():
    """Pre-load llava into memory so first browser navigation isn't slow."""
    import threading, urllib.request as _ur, json as _json
    def _warm():
        try:
            tiny = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
            payload = _json.dumps({
                "model": "llava:latest",
                "messages": [{"role": "user", "content": "ok", "images": [tiny]}],
                "stream": False, "options": {"num_predict": 1},
            }).encode()
            req = _ur.Request("http://localhost:11434/api/chat", data=payload,
                              headers={"Content-Type": "application/json"})
            _ur.urlopen(req, timeout=30).read()
            print("  [JARVIS] Vision model warm.")
        except Exception:
            pass
    threading.Thread(target=_warm, daemon=True).start()


@app.route("/api/transcribe", methods=["POST"])
@requires_auth
def transcribe():
    """
    Server-side STT — accepts audio blob from browser MediaRecorder,
    converts via ffmpeg, transcribes + detects language via faster-whisper.
    Returns {ok, text, lang} — lang is ISO 639-1 code of detected language.
    """
    import tempfile as _tf, traceback as _tb
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"ok": False, "msg": "No audio file"}), 400

    suffix = ".webm"
    ct = audio.content_type or ""
    if "wav" in ct:   suffix = ".wav"
    elif "mp4" in ct or "m4a" in ct: suffix = ".m4a"
    elif "ogg" in ct: suffix = ".ogg"

    src = wav = None
    try:
        with _tf.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            audio.save(tmp.name)
            src = tmp.name
        wav = src.rsplit(".", 1)[0] + ".wav"
        subprocess.run(
            ["/opt/homebrew/bin/ffmpeg", "-y", "-i", src,
             "-ar", "16000", "-ac", "1", "-f", "wav", wav],
            check=True, timeout=20,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        text, lang = _transcribe_whisper(wav)
        return jsonify({"ok": True, "text": text, "lang": lang})
    except Exception as e:
        print(f"[transcribe] {_tb.format_exc()}", flush=True)
        return jsonify({"ok": False, "msg": str(e)}), 500
    finally:
        for p in [src, wav]:
            if p:
                try: os.unlink(p)
                except: pass


@app.route("/api/terminal", methods=["POST"])
@requires_pin_auth
def terminal_run():
    data = request.get_json(force=True)
    cmd = data.get("command", "").strip()
    if not cmd:
        return jsonify({"ok": False, "output": "No command provided"}), 400
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=30, cwd=os.path.expanduser("~"),
            env={**os.environ, "TERM": "xterm-256color"},
        )
        output = result.stdout + (result.stderr if result.stderr else "")
        return jsonify({"ok": True, "output": output or "(no output)", "code": result.returncode})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "output": "Command timed out (30s)", "code": -1})
    except Exception as e:
        return jsonify({"ok": False, "output": str(e), "code": -1})


@app.route("/api/persona", methods=["GET", "POST"])
@requires_auth
def persona_route():
    if request.method == "GET":
        current = os.environ.get("JARVIS_PERSONA", "")
        return jsonify({"current": current, "available": list(ALL_PERSONAS.keys())})
    data = request.get_json(force=True)
    key = data.get("persona", "").strip()
    if key and key not in ALL_PERSONAS:
        return jsonify({"ok": False, "error": f"Unknown persona: {key}"}), 400
    if key:
        os.environ["JARVIS_PERSONA"] = key
    else:
        os.environ.pop("JARVIS_PERSONA", None)
    return jsonify({"ok": True, "persona": key or None})


@app.route("/api/voices", methods=["GET"])
@requires_auth
def voices_list():
    """Return all available voices from voices_registry.json."""
    import pathlib
    reg_path = pathlib.Path(__file__).parent / "voices_registry.json"
    try:
        reg = json.loads(reg_path.read_text())
    except Exception:
        reg = {}

    kokoro = reg.get("engines", {}).get("kokoro", {}).get("voices", {})
    fish   = reg.get("engines", {}).get("fish",   {}).get("voices", {})

    active_voice  = os.environ.get("JARVIS_VOICE_ID",  "af_heart")
    active_engine = os.environ.get("JARVIS_VOICE_ENG", "kokoro")

    lang_defaults = reg.get("lang_defaults", {})

    return jsonify({
        "active_voice":  active_voice,
        "active_engine": active_engine,
        "kokoro":        kokoro,
        "fish":          fish,
        "lang_defaults": lang_defaults,
    })


@app.route("/api/voice/set", methods=["POST"])
@requires_auth
def voice_set():
    """Persist the chosen voice across all TTS calls."""
    data   = request.get_json(force=True) or {}
    voice  = data.get("voice_id", "").strip()
    engine = data.get("engine",   "kokoro").strip()
    if not voice:
        return jsonify({"ok": False, "error": "voice_id required"}), 400
    os.environ["JARVIS_VOICE_ID"]  = voice
    os.environ["JARVIS_VOICE_ENG"] = engine
    return jsonify({"ok": True, "voice_id": voice, "engine": engine})


@app.route("/api/voice/preview", methods=["POST"])
@requires_auth
def voice_preview():
    """Synthesize a short test phrase with a specific voice and return audio."""
    import re as _re, pathlib as _pl
    data     = request.get_json(force=True) or {}
    voice_id = data.get("voice_id", "af_heart")
    engine   = data.get("engine",   "kokoro")
    lang     = data.get("lang",     "en")
    text     = data.get("text",     "Hello. I am ready to assist you.")

    text = _re.sub(r'\s+', ' ', text).strip()[:300]

    # ── Fish Audio: serve the reference MP3 sample directly (instant, no API needed)
    if engine in ("fish", "fish_audio_api", "fish_audio_s2"):
        try:
            import json as _json
            reg_path = _pl.Path(__file__).parent / "voices_registry.json"
            reg = _json.loads(reg_path.read_text())
            fish_voices = reg.get("engines", {}).get("fish", {}).get("voices", {})
            voice_entry = fish_voices.get(voice_id, {})
            sample_path = voice_entry.get("sample_path", "")
            if sample_path and os.path.exists(sample_path):
                with open(sample_path, "rb") as f:
                    audio = f.read()
                if len(audio) > 100:
                    mime = "audio/mpeg" if sample_path.endswith(".mp3") else "audio/wav"
                    return Response(audio, mimetype=mime,
                                    headers={"Content-Disposition": "inline",
                                             "X-TTS-Voice": voice_id,
                                             "X-TTS-Engine": "fish_sample"})
        except Exception as e:
            print(f"[voice_preview fish sample] {e}", flush=True)

        # If no local sample, try Fish Audio API
        api_key = os.environ.get("FISH_AUDIO_API_KEY", "").strip()
        if api_key:
            try:
                import urllib.request as _ur
                payload = json.dumps({
                    "text": text, "reference_id": voice_id,
                    "format": "mp3", "latency": "normal"
                }).encode()
                req = _ur.Request(
                    "https://api.fish.audio/v1/tts",
                    data=payload,
                    headers={"Authorization": f"Bearer {api_key}",
                             "Content-Type": "application/json",
                             "model": "speech-1.6"},
                    method="POST",
                )
                with _ur.urlopen(req, timeout=30) as resp:
                    audio = resp.read()
                if len(audio) > 100:
                    return Response(audio, mimetype="audio/mpeg",
                                    headers={"Content-Disposition": "inline",
                                             "X-TTS-Voice": voice_id,
                                             "X-TTS-Engine": "fish_audio_api"})
            except Exception as e:
                print(f"[voice_preview fish api] {e}", flush=True)

        return Response(b"", status=204)

    # ── Kokoro / Chatterbox: synthesize with the specific voice
    try:
        import sovereign_nexus_tts as _tts
        result = _tts.synthesize_direct(
            text, persona="default", emotion="neutral",
            voice_id=voice_id, lang=lang
        )
        audio_path = result.get("audio_path", "")
        if audio_path and os.path.exists(audio_path):
            with open(audio_path, "rb") as f:
                audio = f.read()
            try: os.unlink(audio_path)
            except: pass
            if len(audio) > 100:
                return Response(audio, mimetype="audio/wav",
                                headers={"Content-Disposition": "inline",
                                         "X-TTS-Voice": voice_id,
                                         "X-TTS-Engine": engine})
    except Exception as e:
        print(f"[voice_preview] {e}", flush=True)

    return Response(b"", status=204)


def _start_cache_janitor():
    """Background thread: purge stale TTS audio files every 10 minutes."""
    import threading, glob, time as _time
    TTS_OUT_DIR = os.path.join(os.path.dirname(__file__), "tts_output")
    MAX_AGE_S   = 600  # 10 minutes

    def _janitor():
        while True:
            _time.sleep(MAX_AGE_S)
            try:
                now   = _time.time()
                count = 0
                for pat in [os.path.join(TTS_OUT_DIR, "tts_*.wav"),
                             "/tmp/tts_*.wav", "/tmp/jarvis_*.wav"]:
                    for f in glob.glob(pat):
                        try:
                            if now - os.path.getmtime(f) > MAX_AGE_S:
                                os.unlink(f)
                                count += 1
                        except OSError:
                            pass
                if count:
                    print(f"[janitor] purged {count} stale audio files", flush=True)
                # GC cycle to prevent memory bloat over long runtimes
                import gc
                gc.collect()
            except Exception as e:
                print(f"[janitor] error: {e}", flush=True)

    t = threading.Thread(target=_janitor, daemon=True, name="cache-janitor")
    t.start()


@app.route("/api/health", methods=["GET"])
def health():
    """Machine-readable health endpoint for watchdog + load balancers."""
    import gc
    import psutil
    proc = psutil.Process()
    mem_mb = proc.memory_info().rss / 1048576
    return jsonify({
        "ok":      True,
        "uptime":  int(__import__("time").time() - _SERVER_START),
        "mem_mb":  round(mem_mb, 1),
        "version": "nexus-2.0",
    })

_SERVER_START = __import__("time").time()


# ── Local Fish Speech server (co-process) ─────────────────────────────────────
_fish_proc: "subprocess.Popen | None" = None

def _start_fish_speech_server():
    """
    Launch the Fish Speech API server as a child process of JARVIS.
    Runs inside its own venv (~/.cache/fish-speech/venv) so its dependencies
    are fully isolated from JARVIS's packages — no version conflicts.
    Skips silently if the checkpoint, repo, or venv are not yet present.
    Killed automatically when JARVIS exits via atexit.
    Logs to /tmp/sovereign_tts/fish_server.log.
    """
    global _fish_proc

    venv    = Path.home() / ".cache" / "fish-speech" / "venv"
    repo    = Path.home() / ".cache" / "fish-speech" / "repo"
    ckpt    = Path.home() / ".cache" / "fish-speech" / "fish-speech-1.5"
    decoder = ckpt / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
    script  = repo / "tools" / "api_server.py"
    python  = venv / "bin" / "python"

    if not python.exists() or not script.exists() or not decoder.exists():
        return  # not installed yet — probe() returns False; JARVIS uses Kokoro/macOS say

    port = int(os.environ.get("FISH_LOCAL_PORT", "8080"))
    host = os.environ.get("FISH_LOCAL_HOST", "127.0.0.1")

    # Don't spawn a second instance if something already owns the port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _s:
        if _s.connect_ex((host, port)) == 0:
            print(f"[JARVIS] Fish Speech already running at {host}:{port}", flush=True)
            return

    log_path = Path(os.environ.get("TTS_OUT_DIR", "/tmp/sovereign_tts")) / "fish_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(log_path, "a")

    try:
        _fish_proc = subprocess.Popen(
            [str(python), "-m", "tools.api_server",
             "--llama-checkpoint-path", str(ckpt),
             "--decoder-checkpoint-path", str(decoder),
             "--listen", f"{host}:{port}",
             # --compile intentionally OMITTED: measured in production, "Compilation
             # time" and "generation time" were identical on almost every request —
             # organic text has varying token counts -> varying tensor shapes ->
             # torch.compile treats each as a new graph and recompiles from scratch,
             # so no steady-state speedup was ever actually being realized here, only
             # the recompilation tax (and it grew erratic under repeated load).
             "--half",      # float16 instead of bfloat16 — free speed gain on Apple Silicon
            ],
            cwd=str(repo),
            stdout=log_fh,
            stderr=log_fh,
        )
        print(f"[JARVIS] Fish Speech server starting (PID {_fish_proc.pid})"
              f" → log: {log_path}", flush=True)
    except Exception as _e:
        print(f"[JARVIS] Fish Speech server failed to start: {_e}", flush=True)
        return

    def _watch_ready():
        import urllib.request
        # fish-speech 1.5 health check is POST /v1/health
        url = f"http://{host}:{port}/v1/health"
        for _ in range(90):          # poll every 2s for up to 3 minutes (compile warmup)
            time.sleep(2)
            if _fish_proc.poll() is not None:
                print(f"[JARVIS] Fish Speech server exited early — check {log_path}",
                      flush=True)
                return
            try:
                req = urllib.request.Request(url, data=b"", method="POST")
                with urllib.request.urlopen(req, timeout=1) as _r:
                    if _r.status == 200:
                        print(f"[JARVIS] Fish Speech server ready at http://{host}:{port}",
                              flush=True)
                        return
            except Exception:
                pass
        print("[JARVIS] Fish Speech server did not become ready within 120s", flush=True)

    threading.Thread(target=_watch_ready, daemon=True, name="fish-ready-watcher").start()


@atexit.register
def _stop_fish_speech_server():
    global _fish_proc
    if _fish_proc and _fish_proc.poll() is None:
        _fish_proc.terminate()
        try:
            _fish_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _fish_proc.kill()


def _prewarm_fish_refs():
    """Pre-transcribe and pre-convert all fish reference audio, then pre-populate server cache."""
    try:
        import sovereign_nexus_tts as _tts
        # Phase 1: convert WAV sidecars + transcripts (fast, no server needed)
        threading.Thread(
            target=_tts.prewarm_fish_references,
            daemon=True, name="fish-ref-prewarm",
        ).start()
        # Phase 2: load Silero ru/de models into memory (models already cached on disk)
        threading.Thread(
            target=_tts.prewarm_silero_models,
            daemon=True, name="silero-model-prewarm",
        ).start()
        # Phase 3: synthesize one phrase per non-Kokoro/non-Silero voice so fish server caches tokens
        host = os.environ.get("FISH_LOCAL_HOST", "127.0.0.1")
        port = int(os.environ.get("FISH_LOCAL_PORT", "8080"))
        threading.Thread(
            target=_tts.prewarm_fish_server_cache,
            args=(f"http://{host}:{port}",),
            daemon=True, name="fish-server-cache-prewarm",
        ).start()
    except Exception:
        pass


def _prewarm_kokoro():
    """Load Kokoro ONNX + warm espeak phonemizer so first real TTS call is fast."""
    def _warm():
        try:
            import sovereign_nexus_tts as _tts
            from pathlib import Path
            import tempfile
            engine = _tts._ENGINE_MAP.get("kokoro")
            if engine and _tts._probe(engine):
                engine._load()
                # Synthesize a dummy phrase to warm the phonemizer pipeline
                dummy = Path(tempfile.mktemp(suffix=".wav"))
                try:
                    engine.generate("Jarvis online.", "af_heart", 1.0, 0.5, 0.5, dummy, lang="en")
                    dummy.unlink(missing_ok=True)
                except Exception:
                    pass
                print("  [JARVIS] Kokoro TTS warm.", flush=True)
        except Exception as e:
            print(f"  [JARVIS] Kokoro pre-warm failed: {e}", flush=True)
    threading.Thread(target=_warm, daemon=True, name="kokoro-prewarm").start()


if __name__ == "__main__":
    storage_manager.start()
    # _warmup_vision() intentionally NOT called at startup — it eagerly loads
    # Ollama's llava vision model (~5GB RSS), which competes with Fish Speech
    # for memory on every restart and was measurably degrading TTS generation
    # speed (tokens/sec dropped by more than half under this contention).
    # Ollama lazy-loads the model on its own on first real /api/vision call,
    # so vision still works — it just no longer pre-reserves that memory before
    # anyone has actually asked for a vision feature.
    _start_cache_janitor()
    _start_fish_speech_server()
    _prewarm_fish_refs()
    _prewarm_kokoro()
    print("\n  J.A.R.V.I.S. NEXUS OVERVIEW")
    print("  http://localhost:5001\n")
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
