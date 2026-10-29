"""
even_g1_ble.py — Even Realities G1/G2 BLE protocol layer.

Encodes/decodes all raw packets that travel over the glasses' UART-style
BLE service.  The command byte map below was derived from community BLE
traffic captures of the official Even Hub companion app.

If a command byte stops working after a firmware update, re-capture traffic
with an iOS BLE sniffer (PacketLogger / Wireshark + btsnoop HCI log) and
update the constants here — nothing else needs changing.

BLE transport:
  Service  0x{SERVICE_UUID}
  Write    0x{WRITE_UUID}   (central → glasses, WriteWithoutResponse)
  Notify   0x{NOTIFY_UUID}  (glasses → central, Notify)

Left glass holds the microphone; right glass handles some UI events.
Both must be connected for full duplex.
"""

from __future__ import annotations
import struct
import time
from dataclasses import dataclass
from enum import IntEnum

# ── GATT UUIDs ────────────────────────────────────────────────────────────────
# Custom Even Realities service (capture confirmed; update if firmware changes)
SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID   = "0000fff2-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID  = "0000fff1-0000-1000-8000-00805f9b34fb"

# Device advertisement name prefixes
DEVICE_NAME_LEFT  = "Even G1_L"
DEVICE_NAME_RIGHT = "Even G1_R"

# ── Command bytes ─────────────────────────────────────────────────────────────
class Cmd(IntEnum):
    # Host → Glasses
    SEND_TEXT     = 0x4E   # display UTF-8 text on HUD
    CLEAR_DISPLAY = 0x18   # blank the HUD
    MIC_START     = 0x0E   # begin streaming PCM audio from L glass mic
    MIC_STOP      = 0x0F   # stop mic stream
    HEARTBEAT     = 0x25   # keep-alive ping (send every 5 s)
    BRIGHTNESS    = 0x01   # set display brightness 0-100

    # Glasses → Host (inbound event types)
    EVT_TOUCH     = 0x05   # tap / double-tap / long-press event
    EVT_AUDIO     = 0x0A   # raw PCM audio chunk (16 kHz mono int16 LE)
    EVT_BATTERY   = 0x2B   # battery level event

# ── Touch event sub-codes ─────────────────────────────────────────────────────
class Touch(IntEnum):
    SINGLE     = 0x01
    DOUBLE     = 0x02
    LONG_PRESS = 0x03
    SWIPE_FWD  = 0x04   # temple swipe toward nose
    SWIPE_BACK = 0x05   # temple swipe away from nose

# ── Inbound event ─────────────────────────────────────────────────────────────
@dataclass
class G1Event:
    cmd:    int
    seq:    int
    data:   bytes

    @property
    def touch_type(self) -> Touch | None:
        if self.cmd == Cmd.EVT_TOUCH and self.data:
            try:
                return Touch(self.data[0])
            except ValueError:
                return None
        return None

    @property
    def audio_pcm(self) -> bytes:
        """Raw PCM bytes from an EVT_AUDIO packet."""
        return self.data if self.cmd == Cmd.EVT_AUDIO else b""

    @property
    def battery_pct(self) -> int | None:
        if self.cmd == Cmd.EVT_BATTERY and self.data:
            return self.data[0]
        return None


# ── Packet codec ─────────────────────────────────────────────────────────────
# Wire format:  [cmd:1][seq:1][len:2 LE][payload:len]
def encode(cmd: Cmd, seq: int, payload: bytes = b"") -> bytes:
    return bytes([cmd, seq & 0xFF]) + struct.pack("<H", len(payload)) + payload


def decode(raw: bytes | bytearray) -> G1Event | None:
    if len(raw) < 4:
        return None
    cmd, seq  = raw[0], raw[1]
    length    = struct.unpack_from("<H", raw, 2)[0]
    data      = bytes(raw[4 : 4 + length])
    return G1Event(cmd=cmd, seq=seq, data=data)


# ── Command builders ──────────────────────────────────────────────────────────
_seq = 0
def _next_seq() -> int:
    global _seq
    _seq = (_seq + 1) & 0xFF
    return _seq


def pkt_send_text(text: str, page: int = 0) -> bytes:
    """
    Build a SEND_TEXT packet.
    The G1 display clips text at ~2 lines / ~120 chars; callers should
    pre-format with sanitize_for_display() before calling here.
    page=0 → first screen; page=1 → second screen (scroll).
    """
    payload = bytes([page]) + text.encode("ascii", errors="replace")
    return encode(Cmd.SEND_TEXT, _next_seq(), payload)


def pkt_clear() -> bytes:
    return encode(Cmd.CLEAR_DISPLAY, _next_seq())


def pkt_mic_start() -> bytes:
    return encode(Cmd.MIC_START, _next_seq())


def pkt_mic_stop() -> bytes:
    return encode(Cmd.MIC_STOP, _next_seq())


def pkt_heartbeat() -> bytes:
    ts = struct.pack("<I", int(time.time()) & 0xFFFFFFFF)
    return encode(Cmd.HEARTBEAT, _next_seq(), ts)


def pkt_brightness(level: int) -> bytes:
    """level: 0-100"""
    return encode(Cmd.BRIGHTNESS, _next_seq(), bytes([max(0, min(100, level))]))
