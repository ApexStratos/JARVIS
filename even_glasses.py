"""
even_glasses.py — Even Realities G1/G2 HUD integration for J.A.R.V.I.S. Nexus.

Exposes two groups of routes (registered via register_glasses_routes):

  OpenAI-compatible shim (for EvenHub-LocalLLM companion app):
    POST /api/glasses/v1/chat/completions   — streaming chat backed by Ollama
    POST /api/glasses/v1/audio/transcriptions — Whisper STT (WAV → text)
    POST /api/glasses/v1/chats              — save chat history locally
    GET  /api/glasses/v1/models             — list available Ollama models
    GET  /api/glasses/v1/test               — connection health check

  Native JARVIS routes:
    POST /api/glasses/input        — voice transcript → brain → SSE + fan-out
    GET  /api/glasses/stream       — persistent SSE channel for HUD display
    POST /api/glasses/quick-action — one-tap shortcuts (STOCKS / WEATHER / …)

Configuration (.env):
    GLASSES_API_KEY   — Bearer token for all /api/glasses/* routes (empty = open)
    OLLAMA_URL        — defaults to http://localhost:11434
    OLLAMA_MODEL      — defaults to llama3.2:latest
"""

import os
import re
import json
import time
import queue
import shutil
import threading
import textwrap
import tempfile
import subprocess
import urllib.request
from functools import wraps
from pathlib import Path
from flask import Blueprint, request, jsonify, Response, stream_with_context

# ── config ─────────────────────────────────────────────────────────────────────
GLASSES_API_KEY  = os.environ.get("GLASSES_API_KEY", "")
OLLAMA_URL       = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
HUD_MAX_CHARS    = 120
HUD_MAX_LINES    = 2
SSE_KA_INTERVAL  = 20
MAX_CONTENT_CHARS = 1900  # matches EvenHub textContainerUpgrade limit

CHAT_HISTORY_FILE = Path(__file__).parent / "data" / "glasses_chats.jsonl"

# HOME-mode bridge to the physical HUD: proactive_push() below only reaches
# the AWAY-mode SSE stream (a connected phone client) — the home BLE daemon
# is a separate process that never touches Flask, so it can't receive a
# direct function call. It polls this file instead (see glasses_ble_daemon.py
# _hud_queue_loop). A plain append-only file, not a socket/port, so there's
# nothing new to secure and no new dependency.
HUD_QUEUE_FILE = Path(__file__).parent / "data" / "hud_push_queue.jsonl"

# ── SSE client queue pool ───────────────────────────────────────────────────────
_clients: list[queue.Queue] = []
_clients_lock = threading.Lock()


def _add_client() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=64)
    with _clients_lock:
        _clients.append(q)
    return q


def _remove_client(q: queue.Queue) -> None:
    with _clients_lock:
        try:
            _clients.remove(q)
        except ValueError:
            pass


def proactive_push(message: str) -> None:
    """Push a storage/trading alert to the HUD — AWAY mode via SSE to any
    connected phone client, HOME mode via the queue file the BLE daemon polls."""
    line  = _hud_format(message)

    try:
        from glasses_mode_router import is_home
        at_home = is_home()
    except Exception:
        at_home = False

    if at_home:
        try:
            HUD_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(HUD_QUEUE_FILE, "a") as f:
                f.write(json.dumps({"text": line, "ts": time.time()}) + "\n")
        except Exception:
            pass
        return

    event = f"data: {json.dumps({'hud': line, 'type': 'alert'})}\n\n"
    with _clients_lock:
        for q in list(_clients):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# ── display sanitizer (ported from EvenHub-LocalLLM sanitizeForDisplay) ────────
# The G2 monochrome firmware has a limited character set. Unsupported codepoints
# cause textContainerUpgrade to silently fail, leaving stale content on screen.
def sanitize_for_display(text: str) -> str:
    """Make text safe for the Even Realities G1/G2 HUD firmware."""
    # curly / low quotes → ASCII
    text = re.sub(r'[‘’‚‛]', "'", text)
    text = re.sub(r'[“”„‟]', '"', text)
    # hyphens and dashes → ASCII hyphen
    text = re.sub(r'[‐‑‒–—―]', '-', text)
    # ellipsis
    text = text.replace('…', '...')
    # fractions
    text = (text.replace('½', '1/2').replace('¼', '1/4')
                .replace('¾', '3/4').replace('⅓', '1/3')
                .replace('⅔', '2/3'))
    text = re.sub(r'[⅐-⅟↉]', '', text)
    # math / currency
    text = (text.replace('×', 'x').replace('÷', '/')
                .replace('≈', '~').replace('≅', '~')
                .replace('≠', '!=').replace('≤', '<=')
                .replace('≥', '>=').replace('°', ' deg')
                .replace('€', 'EUR').replace('£', 'GBP')
                .replace('¥', 'JPY').replace('™', '(TM)')
                .replace('®', '(R)').replace('©', '(C)'))
    # non-breaking / figure spaces → regular space
    text = re.sub(r'[   ]', ' ', text)
    # bullets → dash
    text = re.sub(r'[•·∙]', '-', text)
    # common accented Latin → base ASCII
    for src, dst in [
        (r'[À-Å]', 'A'), (r'[à-å]', 'a'),
        (r'[È-Ë]', 'E'), (r'[è-ë]', 'e'),
        (r'[Ì-Ï]', 'I'), (r'[ì-ï]', 'i'),
        (r'[Ò-Ö]', 'O'), (r'[ò-ö]', 'o'),
        (r'[Ù-Ü]', 'U'), (r'[ù-ü]', 'u'),
        (r'Ñ', 'N'), (r'ñ', 'n'),
        (r'Ç', 'C'), (r'ç', 'c'),
        (r'Ý', 'Y'), (r'[ýÿ]', 'y'),
    ]:
        text = re.sub(src, dst, text)
    text = text.replace('ß', 'ss')
    # emoji / pictographs / dingbats (keep geometric shapes U+2500–U+25FF)
    text = re.sub(r'[\U0001F000-\U0001FFFF]', '', text)
    text = re.sub(r'[☀-⛿✀-➿]', '', text)
    # control chars (keep tab \x09, newline \x0A)
    text = re.sub(r'[\x00-\x08\x0B-\x1F\x7F]', '', text)
    # final allowlist: tab, newline, printable ASCII, geometric shapes
    text = re.sub(r'[^\x09\x0A\x20-\x7E─-◿]', '', text)
    return text


def _hud_format(text: str) -> str:
    """Strip tool syntax, sanitize, collapse to HUD_MAX_LINES × HUD_MAX_CHARS."""
    text = re.sub(r'\[[A-Z_]+:[^\]]*\]', '', text)
    text = re.sub(r'\[[A-Z_]+\]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = sanitize_for_display(text)
    lines = textwrap.wrap(text, width=HUD_MAX_CHARS) or ['']
    return ' / '.join(lines[:HUD_MAX_LINES])


def _content_format(text: str) -> str:
    """Sanitize and truncate for main content container (up to MAX_CONTENT_CHARS)."""
    clean = sanitize_for_display(text)
    if len(clean) > MAX_CONTENT_CHARS:
        return clean[:MAX_CONTENT_CHARS] + '...'
    return clean


# ── auth ───────────────────────────────────────────────────────────────────────
def _glasses_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if GLASSES_API_KEY:
            auth  = request.headers.get("Authorization", "")
            token = auth.removeprefix("Bearer ").strip()
            if token != GLASSES_API_KEY:
                return jsonify({"error": {"message": "Unauthorized", "type": "auth_error"}}), 401
        return f(*args, **kwargs)
    return decorated


# ── quick-action table ─────────────────────────────────────────────────────────
QUICK_ACTIONS: dict[str, str] = {
    "STOCKS":   "Give me a quick summary of AAPL, BTC, and SPY prices.",
    "WEATHER":  "What's the current weather where I am?",
    "SYSINFO":  "Quick system status: CPU, RAM, disk.",
    "STATUS":   "JARVIS status check — are all systems nominal?",
    "NEWS":     "Top 2 tech news headlines right now.",
    "TIME":     "What time and date is it?",
}

# ── STT via local Whisper ──────────────────────────────────────────────────────

def _transcribe_wav(wav_bytes: bytes) -> str:
    """
    Transcribe audio bytes using a locally available Whisper binary.
    Tries: `whisper` CLI → `whisper_cpp` → raises if neither found.
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = tmp.name
    try:
        if shutil.which("whisper"):
            result = subprocess.run(
                ["whisper", tmp_path, "--model", "tiny", "--output_format", "txt",
                 "--output_dir", tempfile.gettempdir(), "--language", "en"],
                capture_output=True, text=True, timeout=30,
            )
            txt_path = Path(tempfile.gettempdir()) / (Path(tmp_path).stem + ".txt")
            if txt_path.exists():
                return txt_path.read_text().strip()
            return result.stdout.strip() or result.stderr.strip()
        elif shutil.which("whisper-cpp") or shutil.which("main"):
            cmd = shutil.which("whisper-cpp") or shutil.which("main")
            result = subprocess.run(
                [cmd, "-f", tmp_path, "-m", os.path.expanduser("~/.whisper/ggml-tiny.bin"),
                 "--output-txt"],
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip()
        else:
            # Try faster-whisper Python package
            from faster_whisper import WhisperModel  # type: ignore
            model = WhisperModel("tiny", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(tmp_path)
            return " ".join(s.text for s in segments).strip()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── chat history persistence ───────────────────────────────────────────────────

def _save_chat_history(payload: dict) -> None:
    CHAT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CHAT_HISTORY_FILE.open("a") as f:
        f.write(json.dumps(payload) + "\n")


# ── Ollama proxy helpers ───────────────────────────────────────────────────────

def _ollama_models() -> list[dict]:
    try:
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return [
            {"id": m["name"], "object": "model", "owned_by": "ollama"}
            for m in data.get("models", [])
        ]
    except Exception:
        return [{"id": OLLAMA_MODEL, "object": "model", "owned_by": "ollama"}]


def _proxy_chat_stream(messages: list, model: str):
    """Stream an Ollama /api/chat response as OpenAI SSE chunks."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_predict": 512, "temperature": 0.7, "num_ctx": 3072},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    chunk_id = f"chatcmpl-glasses-{int(time.time())}"
    yield f"data: {json.dumps({'id': chunk_id, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': ''}, 'finish_reason': None}]})}\n\n"
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            for raw in r:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    content = d.get("message", {}).get("content", "")
                    done    = d.get("done", False)
                    if content:
                        chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(chunk)}\n\n"
                    if done:
                        break
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"
    yield "data: [DONE]\n\n"


def _proxy_chat_complete(messages: list, model: str) -> dict:
    """Non-streaming Ollama /api/chat call, returns OpenAI-shaped dict."""
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"num_predict": 512, "temperature": 0.7, "num_ctx": 3072},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    content = data.get("message", {}).get("content", "")
    return {
        "id": f"chatcmpl-glasses-{int(time.time())}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "model": model,
    }


# ── blueprint factory ──────────────────────────────────────────────────────────

def register_glasses_routes(app, brain) -> None:
    bp = Blueprint("glasses", __name__)

    # ── OpenAI-compatible: models ──────────────────────────────────────────────
    @bp.route("/api/glasses/v1/models")
    @_glasses_auth
    def glasses_models():
        return jsonify({"object": "list", "data": _ollama_models()})

    # ── OpenAI-compatible: health/test ────────────────────────────────────────
    @bp.route("/api/glasses/v1/test")
    @_glasses_auth
    def glasses_test():
        models = _ollama_models()
        return jsonify({
            "ok": True,
            "ollama": OLLAMA_URL,
            "model": OLLAMA_MODEL,
            "models_available": len(models),
            "stt": bool(shutil.which("whisper") or shutil.which("whisper-cpp")),
        })

    # ── OpenAI-compatible: chat completions ───────────────────────────────────
    @bp.route("/api/glasses/v1/chat/completions", methods=["POST"])
    @_glasses_auth
    def glasses_chat_completions():
        """
        Drop-in OpenAI /v1/chat/completions shim backed by Ollama.
        EvenHub-LocalLLM points its chatEndpoint here.
        Supports both streaming (stream=true) and non-streaming responses.
        """
        data     = request.get_json(force=True, silent=True) or {}
        messages = data.get("messages", [])
        model    = data.get("model", OLLAMA_MODEL)
        stream   = data.get("stream", False)

        if not messages:
            return jsonify({"error": {"message": "No messages", "type": "invalid_request_error"}}), 400

        if stream:
            return Response(
                stream_with_context(_proxy_chat_stream(messages, model)),
                mimetype="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                         "Access-Control-Allow-Origin": "*"},
            )
        else:
            try:
                result = _proxy_chat_complete(messages, model)
                return jsonify(result)
            except Exception as e:
                return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500

    # ── OpenAI-compatible: STT (Whisper) ──────────────────────────────────────
    @bp.route("/api/glasses/v1/audio/transcriptions", methods=["POST"])
    @_glasses_auth
    def glasses_stt():
        """
        OpenAI /v1/audio/transcriptions shim.
        EvenHub-LocalLLM posts WAV audio here; returns { "text": "..." }.
        Requires local `whisper` CLI or `faster_whisper` Python package.
        """
        audio_file = request.files.get("file")
        if not audio_file:
            return jsonify({"error": {"message": "No file uploaded", "type": "invalid_request_error"}}), 400
        try:
            wav_bytes = audio_file.read()
            text      = _transcribe_wav(wav_bytes)
            return jsonify({"text": text})
        except ImportError:
            return jsonify({
                "error": {
                    "message": "No local Whisper found. Install with: pip install faster-whisper",
                    "type": "server_error",
                }
            }), 500
        except Exception as e:
            return jsonify({"error": {"message": str(e), "type": "server_error"}}), 500

    # ── OpenAI-compatible: save chat history ──────────────────────────────────
    @bp.route("/api/glasses/v1/chats", methods=["POST"])
    @_glasses_auth
    def glasses_save_chat():
        """Accepts EvenHub chat history payloads and appends them to a local JSONL."""
        data = request.get_json(force=True, silent=True) or {}
        try:
            _save_chat_history({"ts": int(time.time()), **data})
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # ── native: voice/text input ───────────────────────────────────────────────
    @bp.route("/api/glasses/input", methods=["POST"])
    @_glasses_auth
    def glasses_input():
        """
        Receives voice transcripts or button events from the G1 bridge.
        Body: { "text": "...", "source": "voice|button" }
        Streams SSE with processing pill markers, then fans out the HUD line
        to all connected /api/glasses/stream subscribers.
        """
        data = request.get_json(force=True, silent=True) or {}
        text = (data.get("text") or "").strip()
        if not text:
            return jsonify({"ok": False, "msg": "No input text"}), 400

        def generate():
            # Processing pill — matches EvenHub animation style
            yield f"data: {json.dumps({'type': 'processing', 'hud': 'Processing ▶  '})}\n\n"

            reply_parts: list[str] = []
            try:
                for event in brain.stream_think(text):
                    if "chunk" in event:
                        chunk = event["chunk"]
                        clean = re.sub(r'\[[A-Z_]+:[^\]]*\]', '', chunk)
                        clean = re.sub(r'\[[A-Z_]+\]', '', clean)
                        if clean:
                            reply_parts.append(clean)
                            yield f"data: {json.dumps({'type': 'chunk', 'chunk': clean})}\n\n"
                    elif "tool" in event:
                        yield f"data: {json.dumps({'type': 'tool', 'tool': event['tool'], 'result': str(event.get('result', ''))[:200]})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
                return

            full_reply = "".join(reply_parts)
            hud_line   = _hud_format(full_reply)
            content    = _content_format(full_reply)

            # fan-out to stream subscribers
            push_event = f"data: {json.dumps({'hud': hud_line, 'content': content, 'type': 'reply'})}\n\n"
            with _clients_lock:
                for q in list(_clients):
                    try:
                        q.put_nowait(push_event)
                    except queue.Full:
                        pass

            yield f"data: {json.dumps({'type': 'done', 'hud': hud_line, 'content': content})}\n\n"

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Access-Control-Allow-Origin": "*"},
        )

    # ── native: persistent HUD stream ─────────────────────────────────────────
    @bp.route("/api/glasses/stream")
    @_glasses_auth
    def glasses_stream():
        """
        Persistent SSE channel for the G1 display.
        Receives HUD lines from glasses_input() and proactive_push() (storage/trading alerts).
        The G1 companion bridge should reconnect on drop (EventSource handles this).
        """
        client_q = _add_client()

        def generate():
            yield f"data: {json.dumps({'type': 'connected', 'ts': int(time.time())})}\n\n"
            last_ka = time.time()
            try:
                while True:
                    try:
                        event   = client_q.get(timeout=5)
                        yield event
                        last_ka = time.time()
                    except queue.Empty:
                        if time.time() - last_ka >= SSE_KA_INTERVAL:
                            yield ": keepalive\n\n"
                            last_ka = time.time()
            except GeneratorExit:
                pass
            finally:
                _remove_client(client_q)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Access-Control-Allow-Origin": "*"},
        )

    # ── native: quick-action shortcuts ────────────────────────────────────────
    @bp.route("/api/glasses/quick-action", methods=["POST"])
    @_glasses_auth
    def glasses_quick_action():
        """
        Hardware shortcut handler.
        Body: { "action": "STOCKS" | "WEATHER" | "SYSINFO" | "STATUS" | "NEWS" | "TIME" }
        Returns: { "ok": true, "hud": "...", "content": "...", "full": "..." }
        """
        data   = request.get_json(force=True, silent=True) or {}
        action = (data.get("action") or "").upper().strip()

        if action not in QUICK_ACTIONS:
            return jsonify({
                "ok": False,
                "msg": f"Unknown action. Available: {list(QUICK_ACTIONS.keys())}",
            }), 400

        try:
            full    = brain.think(QUICK_ACTIONS[action])
            hud     = _hud_format(full)
            content = _content_format(full)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

        push_event = f"data: {json.dumps({'hud': hud, 'content': content, 'type': 'quick', 'action': action})}\n\n"
        with _clients_lock:
            for q in list(_clients):
                try:
                    q.put_nowait(push_event)
                except queue.Full:
                    pass

        return jsonify({"ok": True, "action": action, "hud": hud, "content": content, "full": full})

    app.register_blueprint(bp)
    print("  [JARVIS] Even Realities G1/G2 routes registered → /api/glasses/*")
    print(f"  [JARVIS]   EvenHub chatEndpoint  → /api/glasses/v1/chat/completions")
    print(f"  [JARVIS]   EvenHub sttEndpoint   → /api/glasses/v1/audio/transcriptions")
    print(f"  [JARVIS]   EvenHub chatsEndpoint → /api/glasses/v1/chats")
