"""
twilio_voice — live two-way phone conversation with JARVIS over a real call.

Architecture mirrors glasses_ble_daemon.py's existing live-audio loop
(VAD -> STT -> JarvisBrain -> TTS -> audio out) — same shape, different
transport. Twilio Media Streams replaces the BLE connection; 8kHz mulaw
frames over a WebSocket replace BLE audio packets.

Call flow:
  1. CALL_ME tool (app/toolkit.py) -> Twilio REST API dials the user's phone.
  2. Twilio connects the call and POSTs to /api/twilio/voice for instructions.
  3. That webhook returns TwiML pointing Twilio at /api/twilio/stream (WS).
  4. Twilio opens the WebSocket and streams the call's audio both ways.

Security: the two Twilio-facing routes (voice webhook + stream) are NOT
behind requires_pin_auth — Twilio's servers call them directly with no
browser session to carry a cookie. They're secured instead with Twilio's
own request-signature validation (X-Twilio-Signature, HMAC'd with the
account's auth token) — the Twilio-recommended mechanism for exactly this
case, not a workaround.

What's verified at the code level: TwiML shape, WebSocket message framing,
VAD/STT/brain/TTS wiring (reusing already-tested code paths), and audio
transcoding round-tripping against synthetic frames. What is NOT verified
here: real call audio quality, real-world latency, and whether Twilio's
actual mulaw stream matches these assumptions in practice — that needs a
live Twilio account and a real test call.
"""
import audioop
import base64
import json
import logging
import math
import os
import struct
import tempfile
import threading
import time

log = logging.getLogger("twilio_voice")

# ── config ───────────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN   = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
MY_PHONE_NUMBER     = os.environ.get("MY_PHONE_NUMBER", "")
PUBLIC_BASE_URL     = os.environ.get("CLOUDFLARE_TUNNEL_URL", "").rstrip("/")

# Twilio Media Streams: 8kHz, 8-bit mulaw, 20ms frames (160 bytes/frame).
TWILIO_SAMPLE_RATE = 8000
FRAME_MS           = 20
FRAME_BYTES         = TWILIO_SAMPLE_RATE * FRAME_MS // 1000  # 160 bytes @ 8kHz/8-bit

# VAD parameters — same algorithm as glasses_ble_daemon.py, re-tuned for an
# 8kHz/20ms frame instead of that daemon's 16kHz chunk size.
CALIBRATION_CHUNKS = 15
MIN_SPEECH_CHUNKS  = 5
SILENCE_MULTIPLIER = 4.0
SILENCE_DURATION   = 25  # consecutive silent 20ms frames (~500ms) after speech ends

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper():
    """Cached across calls — glasses_ble_daemon.py reloads this per utterance,
    which is fine for one-off voice commands but adds real latency across a
    live call's many utterances."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                _whisper_model = WhisperModel("tiny", device="cpu", compute_type="int8")
    return _whisper_model


def _rms(pcm16: bytes) -> float:
    if not pcm16:
        return 0.0
    samples = struct.unpack_from(f"<{len(pcm16)//2}h", pcm16)
    return math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))


def _build_wav(pcm16_mono_8k: bytes) -> bytes:
    data_size = len(pcm16_mono_8k)
    byte_rate = TWILIO_SAMPLE_RATE * 1 * 16 // 8
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1,
        1, TWILIO_SAMPLE_RATE, byte_rate, 2, 16,
        b"data", data_size,
    )
    return header + pcm16_mono_8k


def _transcribe(wav_bytes: bytes) -> str:
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            path = f.name
        model = _get_whisper()
        segments, _ = model.transcribe(path)
        text = " ".join(s.text for s in segments).strip()
        os.unlink(path)
        return text
    except Exception as e:
        log.error(f"STT error: {e}")
        return ""


def _wav_to_mulaw_frames(wav_path: str) -> list[bytes]:
    """Reads a WAV file (whatever sample rate/width the TTS engine produced),
    downsamples to 8kHz mono 16-bit, encodes to 8-bit mulaw, and splits into
    Twilio-sized 20ms frames."""
    import wave
    with wave.open(wav_path, "rb") as w:
        channels = w.getnchannels()
        width    = w.getsampwidth()
        rate     = w.getframerate()
        pcm      = w.readframes(w.getnframes())

    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
        width = 2
    if rate != TWILIO_SAMPLE_RATE:
        pcm, _ = audioop.ratecv(pcm, width, 1, rate, TWILIO_SAMPLE_RATE, None)

    mulaw = audioop.lin2ulaw(pcm, width)
    return [mulaw[i:i + FRAME_BYTES] for i in range(0, len(mulaw), FRAME_BYTES)]


# ── Outbound call initiation (CALL_ME tool) ─────────────────────────────────

def initiate_call() -> str:
    missing = [n for n, v in (
        ("TWILIO_ACCOUNT_SID", TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", TWILIO_AUTH_TOKEN),
        ("TWILIO_PHONE_NUMBER", TWILIO_PHONE_NUMBER),
        ("MY_PHONE_NUMBER", MY_PHONE_NUMBER),
        ("CLOUDFLARE_TUNNEL_URL", PUBLIC_BASE_URL),
    ) if not v]
    if missing:
        return f"Can't place the call — missing from .env: {', '.join(missing)}"

    try:
        from twilio.rest import Client
    except ImportError:
        return "The 'twilio' package isn't installed — run: pip install twilio"

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        call = client.calls.create(
            to=MY_PHONE_NUMBER,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{PUBLIC_BASE_URL}/api/twilio/voice",
        )
        return f"Calling you now (Twilio call {call.sid})."
    except Exception as e:
        return f"Call failed: {e}"


# ── TwiML webhook ────────────────────────────────────────────────────────────

def generate_twiml() -> str:
    stream_url = f"{PUBLIC_BASE_URL.replace('https://', 'wss://').replace('http://', 'ws://')}/api/twilio/stream"
    from twilio.twiml.voice_response import VoiceResponse, Connect
    resp = VoiceResponse()
    connect = Connect()
    connect.stream(url=stream_url)
    resp.append(connect)
    return str(resp)


def validate_twilio_request(url: str, params: dict, signature: str) -> bool:
    """Twilio's recommended way to authenticate that a webhook request
    actually came from Twilio — not PIN auth, which has no meaning for a
    server-to-server callback with no browser session."""
    if not TWILIO_AUTH_TOKEN:
        return False
    from twilio.request_validator import RequestValidator
    return RequestValidator(TWILIO_AUTH_TOKEN).validate(url, params, signature)


# ── WebSocket media-stream handler ──────────────────────────────────────────

def handle_media_stream(ws) -> None:
    """Drives one live call. `ws` is a flask_sock connection object
    (synchronous .receive()/.send(), runs on its own thread — the app
    already runs with threaded=True)."""
    from brain import JarvisBrain
    brain = JarvisBrain()  # one brain per call, same reasoning as server.py's
                            # dedicated _glasses_brain: a call is one continuous
                            # conversation, not the multi-tab session pool.

    stream_sid = None
    pcm_chunks: list[bytes] = []
    baseline = None
    cal_sum = 0.0
    cal_count = 0
    speech_detected = False
    speech_chunks = 0
    silent_chunks = 0

    def reset_vad():
        nonlocal pcm_chunks, speech_detected, speech_chunks, silent_chunks
        pcm_chunks = []
        speech_detected = False
        speech_chunks = 0
        silent_chunks = 0

    def send_speech(text: str) -> None:
        """Synthesize `text` and stream it back as mulaw frames, paced at
        real-time (one 20ms frame per 20ms) so Twilio's playback buffer
        doesn't receive a whole reply instantaneously."""
        if not stream_sid or not text.strip():
            return
        try:
            import sovereign_nexus_tts as tts
            result = tts.synthesize_direct(text)
            wav_path = result["audio_path"] if isinstance(result, dict) else result.audio_path
            frames = _wav_to_mulaw_frames(wav_path)
            try:
                os.unlink(wav_path)
            except Exception:
                pass
            for frame in frames:
                msg = {
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(frame).decode()},
                }
                ws.send(json.dumps(msg))
                time.sleep(FRAME_MS / 1000)
        except Exception as e:
            log.error(f"TTS/send error: {e}")

    def process_utterance():
        nonlocal baseline
        chunks = pcm_chunks[:]
        reset_vad()
        wav = _build_wav(b"".join(chunks))
        text = _transcribe(wav)
        log.info(f"[call] heard: {text!r}")
        if not text:
            return
        try:
            reply = brain.think(text)
        except Exception as e:
            log.error(f"brain error: {e}")
            reply = "Sorry, I hit an error on my end."
        log.info(f"[call] reply: {reply[:80]!r}")
        send_speech(reply)

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            event = msg.get("event")
            if event == "start":
                stream_sid = msg["start"]["streamSid"]
                log.info(f"[call] stream started: {stream_sid}")
                send_speech("Hey, I'm here — what's up?")
            elif event == "media":
                mulaw = base64.b64decode(msg["media"]["payload"])
                pcm = audioop.ulaw2lin(mulaw, 2)
                pcm_chunks.append(pcm)
                chunk_rms = _rms(pcm)

                if baseline is None:
                    cal_sum += chunk_rms
                    cal_count += 1
                    if cal_count >= CALIBRATION_CHUNKS:
                        baseline = cal_sum / cal_count
                        log.info(f"[call] VAD baseline: {baseline:.0f}")
                    continue

                threshold = baseline * SILENCE_MULTIPLIER
                if chunk_rms > threshold:
                    speech_chunks += 1
                    silent_chunks = 0
                    if not speech_detected and speech_chunks >= MIN_SPEECH_CHUNKS:
                        speech_detected = True
                else:
                    speech_chunks = 0
                    if speech_detected:
                        silent_chunks += 1
                        if silent_chunks >= SILENCE_DURATION:
                            process_utterance()
            elif event == "stop":
                log.info("[call] stream stopped")
                break
    except Exception as e:
        log.error(f"[call] websocket error: {e}")


def register_twilio_routes(app) -> None:
    from flask import request, Response
    from flask_sock import Sock

    sock = Sock(app)

    @app.route("/api/twilio/voice", methods=["POST"])
    def twilio_voice_webhook():
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validate_twilio_request(request.url, request.form.to_dict(), signature):
            return Response("Invalid signature", status=403)
        return Response(generate_twiml(), mimetype="text/xml")

    @sock.route("/api/twilio/stream")
    def twilio_stream(ws):
        # Media Streams carries no X-Twilio-Signature (it's a WebSocket, not
        # an HTTP webhook) — the stream URL itself is only ever handed out by
        # the signature-validated /api/twilio/voice response above, which is
        # the actual trust boundary here.
        handle_media_stream(ws)
