"""
glasses_ble_daemon.py — At-Home BLE daemon for Even Realities G1/G2.

Runs on the Mac as a background service (see com.jarvis.glasses-daemon.plist).
When the glasses come within BLE range the daemon:

  1. Connects to both L and R glass automatically.
  2. Sends a greeting HUD line confirming JARVIS is live.
  3. Starts the microphone stream on the L glass.
  4. Performs real-time VAD (same algorithm as EvenHub-LocalLLM):
       - Calibrate ambient RMS for CALIBRATION_CHUNKS chunks.
       - Speech detected when RMS > baseline × SILENCE_MULTIPLIER for
         MIN_SPEECH_CHUNKS consecutive chunks.
       - Speech ends after SILENCE_DURATION silent chunks post-speech.
  5. On end-of-utterance: transcribes via local Whisper, sends to JARVIS
     brain HTTP endpoint, and streams the HUD-formatted response back.
  6. Touch events are mapped to quick actions or stop commands.
  7. Sends a heartbeat every 5 s to keep the BLE connection alive.
  8. Auto-reconnects within RECONNECT_DELAY s on any disconnect.

Requirements (install once):
    pip install bleak faster-whisper

The daemon only runs in HOME mode.  glasses_mode_router.py monitors the
WiFi SSID; if the Mac leaves the home network the daemon exits cleanly so
the phone's EvenHub app can take over via the Cloudflare tunnel.
"""

from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import struct
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

try:
    from bleak import BleakClient, BleakScanner
    from bleak.exc import BleakError
except ImportError:
    sys.exit("bleak not installed — run: pip install bleak")

from even_g1_ble import (
    NOTIFY_UUID, WRITE_UUID,
    DEVICE_NAME_LEFT, DEVICE_NAME_RIGHT,
    Cmd, Touch, G1Event,
    decode, pkt_send_text, pkt_clear, pkt_mic_start, pkt_mic_stop,
    pkt_heartbeat, pkt_brightness,
)
from even_glasses import sanitize_for_display, _hud_format, HUD_QUEUE_FILE
from glasses_mode_router import is_home, start_mode_watcher, on_mode_change, Mode

# ── config ─────────────────────────────────────────────────────────────────────
JARVIS_LOCAL_URL   = os.environ.get("LOCAL_JARVIS_URL",  "http://127.0.0.1:5001")
OLLAMA_URL         = os.environ.get("OLLAMA_URL",         "http://localhost:11434")
OLLAMA_MODEL       = os.environ.get("OLLAMA_MODEL",       "llama3.2:latest")
SCAN_TIMEOUT       = 15.0    # BLE scan duration (s)
RECONNECT_DELAY    = 8.0     # seconds before reconnect attempt
HEARTBEAT_INTERVAL = 5.0     # seconds between keep-alive pings
SCAN_INTERVAL      = 20.0    # seconds between scan cycles when disconnected
HUD_QUEUE_POLL_INTERVAL = 2.0  # seconds between checks of even_glasses.py's HUD_QUEUE_FILE

# VAD parameters (mirrors EvenHub-LocalLLM defaults)
SAMPLE_RATE         = 16_000
CALIBRATION_CHUNKS  = 10
MIN_SPEECH_CHUNKS   = 3
SILENCE_MULTIPLIER  = 4.0
SILENCE_DURATION    = 20     # consecutive silent chunks after speech ends

BRIGHTNESS          = int(os.environ.get("GLASSES_BRIGHTNESS", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GLASSES-DAEMON] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("glasses-daemon")


# ── WAV builder (same as EvenHub-LocalLLM) ────────────────────────────────────
def _build_wav(pcm_chunks: list[bytes]) -> bytes:
    pcm = b"".join(pcm_chunks)
    data_size      = len(pcm)
    num_channels   = 1
    bits_per_sample = 16
    byte_rate      = SAMPLE_RATE * num_channels * bits_per_sample // 8
    block_align    = num_channels * bits_per_sample // 8

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16, 1,
        num_channels, SAMPLE_RATE, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + pcm


# ── RMS energy ────────────────────────────────────────────────────────────────
def _rms(chunk: bytes) -> float:
    samples = struct.unpack_from(f"<{len(chunk)//2}h", chunk)
    return math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))


# ── local STT ─────────────────────────────────────────────────────────────────
def _transcribe(wav_bytes: bytes) -> str:
    try:
        from faster_whisper import WhisperModel
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav_bytes)
            path = f.name
        model    = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, _ = model.transcribe(path)
        text     = " ".join(s.text for s in segments).strip()
        os.unlink(path)
        return text
    except ImportError:
        log.warning("faster-whisper not installed — STT disabled")
        return ""
    except Exception as e:
        log.error(f"STT error: {e}")
        return ""


# ── JARVIS brain ──────────────────────────────────────────────────────────────
def _ask_jarvis(text: str) -> str:
    """
    HTTP call to the local JARVIS /api/think endpoint.
    Returns the plain-text reply (tool results stripped).
    Falls back to direct Ollama if JARVIS server is unreachable.
    """
    # Try JARVIS server first
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"{JARVIS_LOCAL_URL}/api/think",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        full = ""
        with urllib.request.urlopen(req, timeout=30) as r:
            for line in r:
                line = line.decode().strip()
                if line.startswith("data:"):
                    try:
                        d = json.loads(line[5:])
                        full += d.get("chunk", "")
                    except Exception:
                        pass
        return full.strip()
    except Exception as e:
        log.warning(f"JARVIS server unreachable ({e}), falling back to Ollama")

    # Direct Ollama fallback
    try:
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": text}],
            "stream": False,
            "options": {"num_predict": 80, "temperature": 0},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return data.get("message", {}).get("content", "").strip()
    except Exception as e2:
        return f"Error: {e2}"


# ── Single glass connection ───────────────────────────────────────────────────
class GlassConn:
    """Manages BLE connection + notify handler for one glass (L or R)."""

    def __init__(self, address: str, side: str, on_event):
        self.address  = address
        self.side     = side          # "L" or "R"
        self.on_event = on_event      # async callable(G1Event, side)
        self._client: BleakClient | None = None

    async def connect(self) -> bool:
        try:
            self._client = BleakClient(self.address, timeout=10)
            await self._client.connect()
            await self._client.start_notify(NOTIFY_UUID, self._handle_notify)
            log.info(f"  Connected: G1 {self.side} ({self.address})")
            return True
        except Exception as e:
            log.error(f"  G1 {self.side} connect failed: {e}")
            self._client = None
            return False

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def write(self, packet: bytes) -> None:
        if self.connected:
            try:
                await self._client.write_gatt_char(WRITE_UUID, packet,
                                                    response=False)
            except Exception as e:
                log.warning(f"Write to G1 {self.side} failed: {e}")

    def _handle_notify(self, _handle: int, data: bytearray) -> None:
        event = decode(data)
        if event:
            asyncio.get_event_loop().create_task(self.on_event(event, self.side))


# ── Main daemon ───────────────────────────────────────────────────────────────
class GlassesDaemon:
    def __init__(self):
        self._left:  GlassConn | None = None
        self._right: GlassConn | None = None
        self._running = True

        # VAD state
        self._pcm_chunks:    list[bytes] = []
        self._speech_det     = False
        self._speech_chunks  = 0
        self._silent_chunks  = 0
        self._baseline: float | None = None
        self._cal_sum   = 0.0
        self._cal_count = 0

    # ── BLE scan ──────────────────────────────────────────────────────────────
    async def _scan(self) -> tuple[str | None, str | None]:
        """Scan for G1 L+R addresses. Returns (left_addr, right_addr)."""
        log.info("Scanning for Even Realities G1/G2 glasses…")
        left_addr  = None
        right_addr = None
        try:
            devices = await BleakScanner.discover(timeout=SCAN_TIMEOUT)
            for d in devices:
                name = d.name or ""
                if DEVICE_NAME_LEFT in name and not left_addr:
                    left_addr = d.address
                    log.info(f"  Found L glass: {name} @ {d.address}")
                elif DEVICE_NAME_RIGHT in name and not right_addr:
                    right_addr = d.address
                    log.info(f"  Found R glass: {name} @ {d.address}")
        except Exception as e:
            log.error(f"Scan error: {e}")
        return left_addr, right_addr

    # ── HUD helpers ───────────────────────────────────────────────────────────
    async def _hud(self, text: str) -> None:
        """Send a HUD-formatted, sanitized string to both glasses."""
        safe   = _hud_format(text)          # sanitize + truncate
        packet = pkt_send_text(safe)
        for glass in (self._left, self._right):
            if glass:
                await glass.write(packet)

    async def _clear(self) -> None:
        for glass in (self._left, self._right):
            if glass:
                await glass.write(pkt_clear())

    # ── Event handler ─────────────────────────────────────────────────────────
    async def _on_event(self, event: G1Event, side: str) -> None:
        # ── audio (VAD) ───────────────────────────────────────────────────────
        if event.cmd == Cmd.EVT_AUDIO:
            await self._handle_audio(event.audio_pcm)
            return

        # ── touch events ──────────────────────────────────────────────────────
        touch = event.touch_type
        if touch is None:
            return

        if touch == Touch.DOUBLE:
            # Double tap → stop mic, clear HUD, restart listening
            log.info("Double-tap: resetting session")
            await self._stop_mic()
            await self._clear()
            await asyncio.sleep(0.5)
            await self._start_mic()
            return

        if touch == Touch.LONG_PRESS:
            # Long press → quick SYSINFO snapshot
            log.info("Long-press: SYSINFO quick action")
            await self._hud("Processing ▶")
            reply = await asyncio.get_event_loop().run_in_executor(
                None, _ask_jarvis, "Quick system status: CPU, RAM, disk."
            )
            await self._hud(reply)
            return

        if touch == Touch.SWIPE_FWD:
            log.info("Swipe: STOCKS quick action")
            await self._hud("Processing ▶")
            reply = await asyncio.get_event_loop().run_in_executor(
                None, _ask_jarvis, "Quick AAPL, BTC, SPY prices summary."
            )
            await self._hud(reply)
            return

        if touch == Touch.SWIPE_BACK:
            log.info("Swipe back: WEATHER quick action")
            await self._hud("Processing ▶")
            reply = await asyncio.get_event_loop().run_in_executor(
                None, _ask_jarvis, "Current weather summary."
            )
            await self._hud(reply)
            return

    # ── VAD + STT pipeline ────────────────────────────────────────────────────
    async def _handle_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._pcm_chunks.append(pcm)
        chunk_rms = _rms(pcm)

        # Phase 1: ambient calibration
        if self._baseline is None:
            self._cal_sum   += chunk_rms
            self._cal_count += 1
            if self._cal_count >= CALIBRATION_CHUNKS:
                self._baseline = self._cal_sum / self._cal_count
                log.info(f"VAD baseline calibrated: {self._baseline:.0f}")
            return

        threshold = self._baseline * SILENCE_MULTIPLIER

        if chunk_rms > threshold:
            self._speech_chunks += 1
            self._silent_chunks  = 0
            if not self._speech_det and self._speech_chunks >= MIN_SPEECH_CHUNKS:
                self._speech_det = True
                log.info("VAD: speech detected")
        else:
            self._speech_chunks = 0
            if self._speech_det:
                self._silent_chunks += 1
                if self._silent_chunks >= SILENCE_DURATION:
                    await self._process_utterance()

    async def _process_utterance(self) -> None:
        log.info(f"VAD: utterance ended ({len(self._pcm_chunks)} chunks)")
        chunks = self._pcm_chunks[:]
        self._reset_vad()

        await self._hud("Processing ▶")
        wav  = _build_wav(chunks)

        # STT in thread pool (blocking)
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, _transcribe, wav)
        log.info(f"STT: '{text}'")

        if not text:
            await self._hud("Couldn't hear that — try again")
            return

        # Brain query in thread pool
        reply = await loop.run_in_executor(None, _ask_jarvis, text)
        log.info(f"Reply: {reply[:80]}…")
        await self._hud(reply)

    def _reset_vad(self) -> None:
        self._pcm_chunks   = []
        self._speech_det   = False
        self._speech_chunks = 0
        self._silent_chunks = 0
        # keep baseline — no need to recalibrate mid-session

    # ── mic control ──────────────────────────────────────────────────────────
    async def _start_mic(self) -> None:
        if self._left:
            await self._left.write(pkt_mic_start())
            log.info("Mic started")

    async def _stop_mic(self) -> None:
        if self._left:
            await self._left.write(pkt_mic_stop())
            self._reset_vad()

    # ── heartbeat loop ────────────────────────────────────────────────────────
    async def _heartbeat_loop(self) -> None:
        while self._running:
            for glass in (self._left, self._right):
                if glass:
                    await glass.write(pkt_heartbeat())
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── proactive-push queue loop ─────────────────────────────────────────────
    async def _hud_queue_loop(self) -> None:
        """Drains HUD_QUEUE_FILE — the only way a proactive push (storage
        alerts, the AWAY->HOME welcome-back message below, etc.) reaches the
        physical glasses, since this daemon is a separate process from the
        Flask server that originates those pushes. Renamed-then-read instead
        of read-then-delete so a push written mid-drain isn't lost: a
        concurrent writer just starts a fresh queue file under the original
        name rather than racing this file handle."""
        while self._running:
            try:
                if HUD_QUEUE_FILE.exists():
                    tmp = HUD_QUEUE_FILE.with_suffix(".consuming")
                    HUD_QUEUE_FILE.rename(tmp)
                    for line in tmp.read_text().splitlines():
                        if not line.strip():
                            continue
                        try:
                            text = json.loads(line)["text"]
                        except Exception:
                            continue
                        await self._hud(text)
                        await asyncio.sleep(1.5)
                    tmp.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except Exception as e:
                log.debug(f"HUD queue poll error: {e}")
            await asyncio.sleep(HUD_QUEUE_POLL_INTERVAL)

    # ── mode watcher callback ─────────────────────────────────────────────────
    def _on_mode_change(self, old: "Mode", new: "Mode") -> None:
        if new == "away" and self._running:
            log.info("Left home network — daemon shutting down (phone takes over)")
            self._running = False
        elif old == "away" and new == "home":
            # Not connected yet at this exact moment (run()'s loop hasn't
            # scanned/reconnected after the mode flip) — queue it the same
            # way proactive_push() does, so _hud_queue_loop displays it once
            # the reconnect that's about to happen actually completes.
            try:
                HUD_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
                with open(HUD_QUEUE_FILE, "a") as f:
                    f.write(json.dumps({"text": "Welcome home — JARVIS back online", "ts": time.time()}) + "\n")
            except Exception:
                pass

    # ── main run loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        on_mode_change(self._on_mode_change)

        while self._running:
            # Check mode before each scan cycle
            if not is_home():
                log.info("Not on home network — waiting…")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            left_addr, right_addr = await self._scan()
            if not left_addr and not right_addr:
                log.info(f"Glasses not found — retrying in {SCAN_INTERVAL}s")
                await asyncio.sleep(SCAN_INTERVAL)
                continue

            # Build connections
            if left_addr:
                self._left = GlassConn(left_addr,  "L", self._on_event)
            if right_addr:
                self._right = GlassConn(right_addr, "R", self._on_event)

            # Connect
            ok_l = await self._left.connect()  if self._left  else True
            ok_r = await self._right.connect() if self._right else True

            if not ok_l and not ok_r:
                log.warning("Both glasses failed to connect — retrying")
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            # Initial HUD greeting
            await asyncio.sleep(0.5)
            await self._hud.write(pkt_brightness(BRIGHTNESS)) if self._left else None
            await self._hud("JARVIS online ▶  Listening…")

            # Start mic on L glass
            await self._start_mic()

            # Launch heartbeat + proactive-push queue drain
            hb_task  = asyncio.create_task(self._heartbeat_loop())
            hud_task = asyncio.create_task(self._hud_queue_loop())

            # Monitor connection health
            try:
                while self._running:
                    l_ok = self._left.connected  if self._left  else True
                    r_ok = self._right.connected if self._right else True
                    if not l_ok and not r_ok:
                        log.warning("Both glasses disconnected")
                        break
                    await asyncio.sleep(2)
            finally:
                hb_task.cancel()
                hud_task.cancel()
                await self._stop_mic()
                if self._left:
                    await self._left.disconnect()
                if self._right:
                    await self._right.disconnect()
                self._left  = None
                self._right = None
                log.info(f"Disconnected — reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

        log.info("Daemon stopped.")


# ── entry point ───────────────────────────────────────────────────────────────
def main():
    start_mode_watcher()
    if not is_home():
        log.info("Not on home network at startup — daemon will wait for home WiFi")

    daemon = GlassesDaemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("Interrupted by user")


if __name__ == "__main__":
    main()
