#!/usr/bin/env python3
"""
sovereign_nexus_tts.py — Sovereign Nexus TTS Daemon
Cross-platform, token-free, $0.00 local TTS with hardware-routed engine selection.

Engine routing by hardware tier:
  apple_silicon_high (32GB+) / cuda_high (8GB+ VRAM) → Fish Audio S2 Pro
  apple_silicon_mid  (16GB)  / cuda_mid               → Chatterbox TTS
  apple_silicon_low  (<16GB) / cpu                    → Kokoro TTS
  fallback (nothing installed)                         → macOS `say` / espeak

FastAPI endpoints:
  POST /v1/tts      { text, persona?, emotion?, voice_id?, speed? }
  GET  /v1/engines  → hardware profile + available engines
  GET  /health      → daemon status
"""

import os, sys, time, uuid, platform, subprocess, shutil, json, hashlib, threading
from pathlib import Path
from typing import Optional, Literal
from dataclasses import dataclass, field

# ── Output dir ────────────────────────────────────────────────────────────────
TTS_OUT = Path(os.environ.get("TTS_OUT_DIR", "/tmp/sovereign_tts"))
TTS_OUT.mkdir(parents=True, exist_ok=True)
TTS_PORT = int(os.environ.get("TTS_PORT", 5002))

# ── Unified voice registry ─────────────────────────────────────────────────────
_REGISTRY_PATH = Path(__file__).parent / "voices_registry.json"
_PERSONA_SPEED: dict[str, float] = {}
_REGISTRY: dict = {}

def _load_registry():
    """Load voices_registry.json — single source of truth for all engines."""
    global _REGISTRY, PERSONA_VOICE
    if not _REGISTRY_PATH.exists():
        return
    try:
        _REGISTRY = json.loads(_REGISTRY_PATH.read_text())
        # Populate PERSONA_VOICE from registry kokoro persona_map (en primary)
        for pk, entry in _REGISTRY.get("persona_map", {}).items():
            kokoro_langs = entry.get("kokoro", {})
            v = kokoro_langs.get("en") or next(iter(kokoro_langs.values()), None)
            if v:
                PERSONA_VOICE[pk] = v
        print(f"[TTS] Registry loaded: {len(_REGISTRY.get('persona_map', {}))} personas, "
              f"{len(_REGISTRY.get('engines', {}).get('kokoro', {}).get('voices', {}))} Kokoro voices",
              file=sys.stderr)
    except Exception as e:
        print(f"[TTS] registry load warning: {e}", file=sys.stderr)

def resolve_voice(persona: str, lang: str = "en", engine: str = "kokoro") -> str | None:
    """
    Return the best voice ID for (persona, lang, engine).
    Priority: Fish > Kokoro. Falls back to lang default, then 'af_heart'.
    """
    pm = _REGISTRY.get("persona_map", {}).get(persona, {})
    # Try Fish first for requested lang
    fish_map = pm.get("fish", {})
    if engine == "fish_audio_s2" and fish_map.get(lang):
        return fish_map[lang]
    # Try Kokoro for requested lang
    kokoro_map = pm.get("kokoro", {})
    if kokoro_map.get(lang):
        return kokoro_map[lang]
    # Try lang default from registry
    ld = _REGISTRY.get("lang_defaults", {}).get(lang, {})
    default_v = ld.get("kokoro_f") or ld.get("kokoro_m")
    if default_v:
        return default_v
    # Hard fallback
    return PERSONA_VOICE.get(persona) or PERSONA_VOICE.get("default", "af_heart")

def get_fish_voice_id(persona: str, lang: str = "en") -> str | None:
    """Return Fish Audio reference_id for (persona, lang)."""
    pm = _REGISTRY.get("persona_map", {}).get(persona, {})
    fish_map = pm.get("fish", {})
    return fish_map.get(lang) or fish_map.get("en") or None

def get_lang_default(lang: str, gender: str = "f") -> str | None:
    """Return the default Kokoro voice for a language when no persona is set."""
    ld = _REGISTRY.get("lang_defaults", {}).get(lang, {})
    key = f"kokoro_{gender}"
    return ld.get(key) or ld.get("kokoro_f") or ld.get("kokoro_m")

# ── Persona → voice affinity (base defaults, overridden by registry) ───────────
PERSONA_VOICE: dict[str, str] = {
    "noir_detective":                   "am_onyx",
    "pirate_captain":                   "bm_lewis",
    "dramatic_theater_director":        "bm_daniel",
    "surfer_dude":                      "af_sky",
    "hyper_corporate_manager":          "am_fenrir",
    "conspiracy_cryptid":               "am_echo",
    "overly_dramatic_anime_protagonist":"am_puck",
    "grumpy_old_mechanic":              "am_onyx",
    "medieval_fantasy_tavern_keeper":   "bm_george",
    "panicked_intern":                  "af_nova",
    "tech_bro_guru":                    "am_fenrir",
    "glitchy_chatbot":                  "af_alloy",
    "1800s_aristocrat":                 "bm_george",
    "hyper_active_toddler":             "af_jessica",
    "space_marine_drill_sergeant":      "am_onyx",
    "passive_aggressive_roommate":      "af_bella",
    "regional_new_york":                "af_nova",
    "regional_california":              "af_sky",
    "regional_dmv":                     "am_echo",
    "regional_chicago":                 "am_michael",
    "regional_atlanta":                 "am_fenrir",
    "default":                          "af_heart",
}

# ── Emotion → TTS parameters ──────────────────────────────────────────────────
EMOTION_PARAMS: dict[str, dict] = {
    "neutral":  {"speed": 1.00, "exaggeration": 0.50, "cfg": 0.50},
    "excited":  {"speed": 1.15, "exaggeration": 0.80, "cfg": 0.60},
    "calm":     {"speed": 0.88, "exaggeration": 0.30, "cfg": 0.40},
    "urgent":   {"speed": 1.25, "exaggeration": 0.75, "cfg": 0.65},
    "sad":      {"speed": 0.82, "exaggeration": 0.40, "cfg": 0.35},
    "angry":    {"speed": 1.10, "exaggeration": 0.90, "cfg": 0.70},
    "curious":  {"speed": 1.00, "exaggeration": 0.50, "cfg": 0.45},
    "happy":    {"speed": 1.10, "exaggeration": 0.70, "cfg": 0.55},
}

_load_registry()

# ── Hardware probe ─────────────────────────────────────────────────────────────
@dataclass
class HardwareProfile:
    system: str             # Darwin / Windows / Linux
    accelerator: str        # apple_silicon_high/mid/low / cuda_high/mid / cpu
    memory_gb: float
    device: str             # mps / cuda:0 / cpu
    cuda_available: bool = False
    cuda_vram_gb: float = 0.0
    gpu_name: str = ""

    def tier(self) -> int:
        """3=high(Fish), 2=mid(Chatterbox), 1=low(Kokoro)"""
        if self.accelerator in ("apple_silicon_high", "cuda_high"):
            return 3
        if self.accelerator in ("apple_silicon_mid", "cuda_mid"):
            return 2
        return 1


def _mac_memory_gb() -> float:
    try:
        return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()) / (1024 ** 3)
    except Exception:
        return 8.0


def probe_hardware() -> HardwareProfile:
    sys_name = platform.system()

    if sys_name == "Darwin":
        mem = _mac_memory_gb()
        if mem >= 32:
            acc = "apple_silicon_high"
        elif mem >= 16:
            acc = "apple_silicon_mid"
        else:
            acc = "apple_silicon_low"
        return HardwareProfile(system=sys_name, accelerator=acc,
                               memory_gb=mem, device="mps")

    # Windows / Linux — probe CUDA
    try:
        import torch
        if torch.cuda.is_available():
            vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            name = torch.cuda.get_device_name(0)
            acc  = "cuda_high" if vram >= 8 else "cuda_mid"
            return HardwareProfile(system=sys_name, accelerator=acc,
                                   memory_gb=0, device="cuda:0",
                                   cuda_available=True, cuda_vram_gb=round(vram, 1),
                                   gpu_name=name)
    except ImportError:
        pass

    try:
        import psutil
        mem = psutil.virtual_memory().total / (1024 ** 3)
    except ImportError:
        mem = 0
    return HardwareProfile(system=sys_name, accelerator="cpu", memory_gb=mem, device="cpu")


# Languages natively supported by Kokoro (fast, no voice cloning needed)
# Languages Kokoro can actually phonemize via espeak on this machine.
# zh omitted — espeak lacks Chinese support; Chinese falls through to macOS say.
_KOKORO_NATIVE: frozenset = frozenset({"en", "en-gb", "ja", "fr", "es", "hi", "it", "pt"})


# ── Engine base ───────────────────────────────────────────────────────────────
class BaseEngine:
    name: str = "base"

    def probe(self) -> bool:
        return False

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        raise NotImplementedError

    def close(self):
        pass


# ── Fish Audio / Fish Speech S2 ───────────────────────────────────────────────
class FishEngine(BaseEngine):
    name = "fish_audio_s2"
    _model = None
    _lock = threading.Lock()

    def probe(self) -> bool:
        try:
            import fish_speech  # noqa
            return True
        except ImportError:
            return False

    def _load(self, device: str):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                # fish-speech >= 1.5 Python API
                from fish_speech.inference_engine import TTSInferenceEngine
                from fish_speech.models.text2semantic.inference import ModelArgs
                checkpoint = os.environ.get(
                    "FISH_CKPT",
                    os.path.expanduser("~/.cache/fish-speech/fish-speech-1.5"),
                )
                self._model = TTSInferenceEngine(
                    checkpoint_path=checkpoint,
                    device=device,
                    precision="half" if device != "cpu" else "float",
                    compile=False,
                )
            except Exception as e:
                print(f"[FishEngine] load failed: {e}", file=sys.stderr)
                self._model = None

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        device = os.environ.get("FISH_DEVICE", "auto")
        self._load(device)
        if self._model is None:
            raise RuntimeError("FishEngine model not loaded")
        try:
            import soundfile as sf, numpy as np
            from fish_speech.inference_engine import ServeTTSRequest
            req = ServeTTSRequest(
                text=text,
                references=[],
                reference_id=voice_id if voice_id else None,
                format="wav",
            )
            result = list(self._model.inference(req))
            audio = np.concatenate([r for r in result if isinstance(r, np.ndarray)])
            sf.write(str(out_path), audio, 44100)
        except Exception as e:
            raise RuntimeError(f"FishEngine synthesis failed: {e}") from e
        return out_path

    def close(self):
        self._model = None


# ── Fish Audio REST API (cloud, no local model needed) ────────────────────────
class FishAudioAPIEngine(BaseEngine):
    """Calls Fish Audio's cloud TTS API using reference_id voice cloning."""
    name = "fish_audio_api"
    _API = "https://api.fish.audio/v1/tts"
    _SAMPLE_RATE = 44100

    def probe(self) -> bool:
        return bool(os.environ.get("FISH_AUDIO_API_KEY", "").strip())

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        import urllib.request, urllib.error, struct, wave as _wave

        api_key = os.environ.get("FISH_AUDIO_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("FISH_AUDIO_API_KEY not set")

        payload = json.dumps({
            "text": text,
            "reference_id": voice_id,
            "format": "mp3",
            "latency": "normal",
        }).encode()

        req = urllib.request.Request(
            self._API,
            data=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "model": "speech-1.6",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                mp3_bytes = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Fish Audio API error {e.code}: {e.read().decode(errors='ignore')}") from e

        # Convert MP3 → WAV via ffmpeg (always available on this system)
        wav_path = str(out_path)
        mp3_tmp  = wav_path.replace(".wav", "_fish.mp3")
        with open(mp3_tmp, "wb") as f:
            f.write(mp3_bytes)
        ret = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_tmp, "-ar", "44100", "-ac", "1", wav_path],
            capture_output=True,
        )
        try: os.unlink(mp3_tmp)
        except: pass
        if ret.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {ret.stderr.decode(errors='ignore')}")
        return out_path

    def close(self):
        pass


# ── ElevenLabs ─────────────────────────────────────────────────────────────────
class ElevenLabsEngine(BaseEngine):
    """Cloud TTS via ElevenLabs. Previously only reachable through the
    standalone SPEAK tool (app/toolkit.py) — this makes it a real tier in the
    main conversational voice path too, once ELEVENLABS_API_KEY is set."""
    name = "elevenlabs"
    _API = "https://api.elevenlabs.io/v1/text-to-speech"

    def probe(self) -> bool:
        return bool(os.environ.get("ELEVENLABS_API_KEY", "").strip())

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        import urllib.request, urllib.error

        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("ELEVENLABS_API_KEY not set")
        # voice_id here is a Kokoro/Fish-style id (e.g. "af_heart") from the
        # persona_map — there's no ElevenLabs slot in that registry yet, so
        # always use the one configured voice rather than passing it through.
        eleven_voice = os.environ.get("ELEVENLABS_VOICE_ID", "pNInz6obpgDQGcFmaJgB")

        payload = json.dumps({
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": speed},
        }).encode()
        req = urllib.request.Request(
            f"{self._API}/{eleven_voice}",
            data=payload,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                mp3_bytes = resp.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"ElevenLabs API error {e.code}: {e.read().decode(errors='ignore')}") from e

        wav_path = str(out_path)
        mp3_tmp  = wav_path.replace(".wav", "_eleven.mp3")
        with open(mp3_tmp, "wb") as f:
            f.write(mp3_bytes)
        ret = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_tmp, "-ar", "44100", "-ac", "1", wav_path],
            capture_output=True,
        )
        try: os.unlink(mp3_tmp)
        except: pass
        if ret.returncode != 0:
            raise RuntimeError(f"ffmpeg conversion failed: {ret.stderr.decode(errors='ignore')}")
        return out_path

    def close(self):
        pass


def _fish_ref_wav(voice_id: str, near_path: Path) -> str | None:
    """Convert a Fish Audio reference MP3 to a temp WAV for Chatterbox, return path or None."""
    if not voice_id or len(voice_id) != 32:
        return None
    try:
        reg = json.loads(_REGISTRY_PATH.read_text())
        entry = reg.get("engines", {}).get("fish", {}).get("voices", {}).get(voice_id, {})
        mp3 = entry.get("sample_path", "")
        if not mp3 or not Path(mp3).exists():
            return None
        ref_wav = str(near_path).replace(".wav", "_cloneref.wav")
        ret = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3, "-ar", "24000", "-ac", "1", ref_wav],
            capture_output=True,
        )
        if ret.returncode == 0 and Path(ref_wav).exists():
            return ref_wav
    except Exception as e:
        print(f"[fish_ref_wav] {e}", file=sys.stderr)
    return None


# ── Local Fish Speech HTTP server (zero-cost voice cloning via ICL) ───────────
# Languages confirmed to work on openaudio-s1-mini / fish-speech-1.5.
# Russian, Arabic, Portuguese, Spanish, Korean, French all timeout — the model
# silently hangs rather than returning an error for unsupported scripts.
_LOCAL_FISH_LANGS = {"en", "en-gb", "ja", "zh", "zh-tw", "zh-hk"}

_FISH_LOCAL_TRANSCRIBE_LOCK = threading.Lock()


def _get_ref_transcript(mp3_path: str) -> str:
    """
    Return the spoken text in a Fish Audio reference MP3.
    Runs faster-whisper on first call; result cached as a .txt sidecar file.
    Thread-safe: only one transcription job runs at a time.
    """
    txt_path = Path(mp3_path).with_suffix(".txt")
    if txt_path.exists():
        cached = txt_path.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    with _FISH_LOCAL_TRANSCRIBE_LOCK:
        # Re-check inside lock — another thread may have written it
        if txt_path.exists():
            cached = txt_path.read_text(encoding="utf-8").strip()
            if cached:
                return cached
        try:
            from faster_whisper import WhisperModel
            model = WhisperModel("base", device="cpu", compute_type="int8")
            segments, _ = model.transcribe(mp3_path, beam_size=1)
            transcript = " ".join(s.text.strip() for s in segments).strip()
            if transcript:
                txt_path.write_text(transcript, encoding="utf-8")
                print(f"[LocalFish] transcript cached for {Path(mp3_path).name}: "
                      f"{transcript[:60]!r}", file=sys.stderr)
            return transcript
        except Exception as e:
            print(f"[LocalFish] transcription failed for {Path(mp3_path).name}: {e}",
                  file=sys.stderr)
            return ""


def _get_local_ref_wav_bytes(mp3_path: str, max_sec: int = 3) -> bytes | None:
    """
    Convert a Fish Audio reference MP3 to 44 100 Hz mono WAV bytes trimmed to
    max_sec seconds. 3s is the sweet spot: enough voice character for ICL but
    short enough that MPS reference-encoding doesn't hang on some voices.
    Result is cached on disk as a .ref3s.wav sidecar next to the source MP3.
    """
    ref_wav = Path(mp3_path).with_suffix(f".ref{max_sec}s.wav")
    if not ref_wav.exists():
        ret = subprocess.run(
            ["ffmpeg", "-y", "-i", mp3_path,
             "-t", str(max_sec),
             "-ar", "44100", "-ac", "1", str(ref_wav)],
            capture_output=True,
        )
        if ret.returncode != 0 or not ref_wav.exists():
            print(f"[LocalFish] ffmpeg failed for {Path(mp3_path).name}: "
                  f"{ret.stderr.decode(errors='ignore')}", file=sys.stderr)
            return None
    try:
        return ref_wav.read_bytes()
    except Exception as e:
        print(f"[LocalFish] could not read ref WAV: {e}", file=sys.stderr)
        return None


def _fish_token_budget(text: str) -> int:
    """
    Bound max_new_tokens to roughly the text length instead of a flat 1024 ceiling.

    Measured directly on this model/voice: a flat 1024-token ceiling gives the
    autoregressive generator enough runway to loop/repeat part of a sentence
    instead of stopping cleanly — a 24-word (139-char) sentence produced 20s+
    of audio with an audible repeated segment. Capping proportionally to input
    length (calibrated: 139 chars needed ~350 tokens to finish naturally with
    zero repeats and a clean natural fade — this uses ~3.2 chars/token for
    headroom) bounds the worst case without cutting sentences off early. Floor
    of 180 protects very short text; ceiling keeps the original 1024 safety
    net for unusually long input.
    """
    return min(1024, max(180, int(len(text) * 3.2)))


def generate_local_fish_speech(
    text: str,
    voice_id: str,
    out_path: Path,
    base_url: str | None = None,
    speed: float = 1.0,
    lang: str = "en",
) -> Path:
    """
    Synthesize speech via a locally-running Fish Speech HTTP server using
    in-context learning (ICL) from the voice's reference audio + auto-transcript.

    Zero-cost, zero-cloud, zero API key — the reference MP3 from voices_registry.json
    acts as the voice prompt; faster-whisper transcribes it once and caches the result.

    Server URL: FISH_LOCAL_URL env var (default http://localhost:8080).
    Start the server with:  python -m fish_speech.webui.app

    Payload format: msgpack when ormsgpack is installed (native Fish Speech protocol),
    otherwise falls back to JSON with base64-encoded audio.

    Raises RuntimeError on any failure so callers can fall back to Kokoro / macOS say.
    """
    import urllib.request, urllib.error

    base_url = (base_url or os.environ.get("FISH_LOCAL_URL", "http://localhost:8080")).rstrip("/")
    endpoint = f"{base_url}/v1/tts"

    # Resolve reference audio path from voices_registry.json
    reg = json.loads(_REGISTRY_PATH.read_text())
    entry = (
        reg.get("engines", {})
           .get("fish", {})
           .get("voices", {})
           .get(voice_id, {})
    )
    mp3_path = entry.get("sample_path", "")
    if not mp3_path or not Path(mp3_path).exists():
        raise RuntimeError(
            f"[LocalFish] reference audio not found for voice_id={voice_id!r} — "
            "run provision_all_voices.py to download fish reference files"
        )

    # Get transcript (auto-generated once per voice, then read from .txt sidecar)
    transcript = _get_ref_transcript(mp3_path)

    # Get reference WAV bytes (converted once, cached as .ref.wav sidecar)
    ref_bytes = _get_local_ref_wav_bytes(mp3_path)
    if ref_bytes is None:
        raise RuntimeError(f"[LocalFish] failed to prepare reference WAV from {mp3_path}")

    # ICL payload: text to synthesize + reference audio/transcript pair for voice cloning
    references = [{"audio": ref_bytes, "text": transcript}] if transcript else []
    payload: dict = {
        "text": text,
        "references": references,
        "format": "wav",
        "streaming": False,
        # Fish Speech's normalize step runs Chinese-specific text normalization
        # (ChnNormedText) unconditionally — for non-Chinese text it was corrupting
        # plain digits into Chinese numerals (e.g. "number 1" -> "number 一"),
        # confusing the model and producing wildly inconsistent, overlong generations.
        "normalize": lang == "zh",
        "use_memory_cache": "on",  # cache encoded reference tokens — skip re-encoding on repeat calls
        "chunk_length": 50,        # 50 is the sweet spot on MPS (vs 300 default which is ~25% slower)
        "max_new_tokens": _fish_token_budget(text),
        "top_p": 0.8,
        "temperature": 0.7,
        "repetition_penalty": 1.1,
    }

    # Use msgpack (native Fish Speech wire format) when available; fall back to JSON
    try:
        import ormsgpack
        body = ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_NUMPY)
        content_type = "application/msgpack"
    except ImportError:
        import base64 as _b64
        json_payload = dict(payload)
        json_payload["references"] = [
            {"audio": _b64.b64encode(r["audio"]).decode(), "text": r["text"]}
            for r in references
        ]
        body = json.dumps(json_payload).encode()
        content_type = "application/json"

    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as resp:
            audio_bytes = resp.read()
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"[LocalFish] server unreachable at {endpoint}: {e} — "
            "start it with: python -m fish_speech.webui.app"
        ) from e
    except Exception as e:
        raise RuntimeError(f"[LocalFish] request failed: {e}") from e

    if len(audio_bytes) < 200:
        raise RuntimeError(
            f"[LocalFish] server returned only {len(audio_bytes)} bytes — "
            "check fish-speech server logs for synthesis error"
        )

    out_path.write_bytes(audio_bytes)
    return out_path


def stream_local_fish_speech(
    text: str,
    voice_id: str,
    base_url: str | None = None,
    lang: str = "en",
    chunk_size: int = 8192,
) -> "Generator[bytes, None, None]":
    """
    Generator that yields WAV audio chunks from the local fish server as they
    arrive (streaming=True). First chunk arrives in ~3s; caller can pipe directly
    to an HTTP response so the browser starts playing without waiting for full audio.

    Raises RuntimeError on any failure (caller should catch and fall back).
    """
    import urllib.request, urllib.error

    base_url = (base_url or os.environ.get("FISH_LOCAL_URL", "http://localhost:8080")).rstrip("/")
    endpoint = f"{base_url}/v1/tts"

    # Resolve reference audio
    reg = json.loads(_REGISTRY_PATH.read_text())
    entry = reg.get("engines", {}).get("fish", {}).get("voices", {}).get(voice_id, {})
    mp3_path = entry.get("sample_path", "")
    if not mp3_path or not Path(mp3_path).exists():
        raise RuntimeError(f"[LocalFishStream] reference audio not found for {voice_id!r}")

    transcript = _get_ref_transcript(mp3_path)
    ref_bytes  = _get_local_ref_wav_bytes(mp3_path)
    if ref_bytes is None:
        raise RuntimeError(f"[LocalFishStream] failed to prepare reference WAV from {mp3_path}")

    references = [{"audio": ref_bytes, "text": transcript}] if transcript else []
    payload: dict = {
        "text": text,
        "references": references,
        "format": "wav",
        "streaming": True,
        # Fish Speech's normalize step runs Chinese-specific text normalization
        # (ChnNormedText) unconditionally — for non-Chinese text it was corrupting
        # plain digits into Chinese numerals (e.g. "number 1" -> "number 一"),
        # confusing the model and producing wildly inconsistent, overlong generations.
        "normalize": lang == "zh",
        "use_memory_cache": "on",
        # Smaller chunk_length = fewer tokens generated before the first decodable
        # audio chunk is ready to stream out. Measured on this hardware: 50 -> 10s
        # to first byte, 20 -> 3.3s, 8 -> 2.5s but total generation became unstable
        # (nearly 2x longer). 20 is the point that meaningfully cuts perceived
        # latency for the streaming path without destabilizing total generation
        # time — this only applies here, not to the non-streaming path below,
        # which has nothing to gain from a smaller chunk since it waits for the
        # complete file regardless.
        "chunk_length": 20,
        "max_new_tokens": _fish_token_budget(text),
        "top_p": 0.8,
        "temperature": 0.7,
        "repetition_penalty": 1.1,
    }

    try:
        import ormsgpack
        body         = ormsgpack.packb(payload, option=ormsgpack.OPT_SERIALIZE_NUMPY)
        content_type = "application/msgpack"
    except ImportError:
        import base64 as _b64
        jp = dict(payload)
        jp["references"] = [{"audio": _b64.b64encode(r["audio"]).decode(), "text": r["text"]}
                             for r in references]
        body         = json.dumps(jp).encode()
        content_type = "application/json"

    req = urllib.request.Request(endpoint, data=body,
                                 headers={"Content-Type": content_type}, method="POST")
    # 20s applies per socket read, not to the whole request — each resp.read()
    # below gets its own fresh 20s allowance, so this is really an inter-chunk
    # stall timeout. Healthy chunks arrive every ~4-5s at chunk_length=20; 20s
    # with zero new bytes means the server has wedged, not just fallen behind.
    # This used to be 120s, which meant a hang left the client's audio element
    # (and the frontend's TTS sentence queue behind it) stuck for up to two
    # full minutes with no error and no way to recover.
    _STREAM_READ_TIMEOUT = 20
    try:
        resp = urllib.request.urlopen(req, timeout=_STREAM_READ_TIMEOUT)
    except urllib.error.URLError as e:
        raise RuntimeError(f"[LocalFishStream] server unreachable: {e}") from e

    try:
        while True:
            try:
                chunk = resp.read(chunk_size)
            except (TimeoutError, OSError) as e:
                raise RuntimeError(
                    f"[LocalFishStream] stalled mid-stream (no data for "
                    f"{_STREAM_READ_TIMEOUT}s) — Fish Speech likely wedged: {e}"
                ) from e
            if not chunk:
                break
            yield chunk
    finally:
        resp.close()


def prewarm_fish_references():
    """
    Pre-convert all fish reference MP3s to .ref.wav and transcribe them.
    Runs once at startup in a daemon thread so the first real synthesis call
    has zero overhead for reference loading or transcription.
    """
    reg = json.loads(_REGISTRY_PATH.read_text())
    voices = reg.get("engines", {}).get("fish", {}).get("voices", {})
    if not voices:
        return
    done = 0
    for vid, entry in voices.items():
        mp3 = entry.get("sample_path", "")
        if not mp3 or not Path(mp3).exists():
            continue
        _get_local_ref_wav_bytes(mp3)   # creates .ref.wav sidecar
        _get_ref_transcript(mp3)        # creates .txt sidecar
        done += 1
    print(f"[TTS] Fish reference pre-warm done ({done}/{len(voices)} voices)", file=sys.stderr)


_LANG_PREWARM_TEXT = {
    "ru": "Привет", "de": "Hallo", "ko": "안녕하세요",
    "ar": "مرحبا",  "sw": "Habari",
}

def prewarm_fish_server_cache(base_url: str = "http://localhost:8080"):
    """
    After the Fish Speech server is ready, synthesize one short phrase for each
    non-Kokoro voice so the server caches their encoded reference tokens.
    Subsequent user requests hit the cache and skip ~1.5s of reference encoding.
    """
    import urllib.request, urllib.error, tempfile
    health_url = f"{base_url}/v1/health"

    # Wait up to 3 minutes for the server to be ready
    for _ in range(90):
        time.sleep(2)
        try:
            req = urllib.request.Request(health_url, data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
    else:
        return  # server never came up

    reg = json.loads(_REGISTRY_PATH.read_text())
    voices = reg.get("engines", {}).get("fish", {}).get("voices", {})
    cached = 0
    with tempfile.TemporaryDirectory() as tmp:
        _silero_langs = set(SileroEngine._LANG_CONFIG.keys())
        for vid, entry in voices.items():
            lang = entry.get("lang", "en")
            if lang in _KOKORO_NATIVE or lang in _silero_langs:
                continue  # handled by faster engines — skip fish server prewarm
            text = _LANG_PREWARM_TEXT.get(lang, "Hello")
            try:
                out = Path(tmp) / f"{vid}_prewarm.wav"
                generate_local_fish_speech(text=text, voice_id=vid,
                                           out_path=out, base_url=base_url, lang=lang)
                cached += 1
            except Exception as e:
                print(f"[TTS] Fish server cache prewarm failed for {vid}: {e}", file=sys.stderr)
    print(f"[TTS] Fish server cache pre-warm done ({cached} voices)", file=sys.stderr)


def prewarm_silero_models():
    """Pre-load Silero ru/de models into memory so first user request is instant."""
    silero = _ENGINE_MAP.get("silero")
    if not silero or not _probe(silero):
        return
    for lang in SileroEngine._LANG_CONFIG:
        try:
            silero._get_model(lang)
            print(f"[TTS] Silero {lang} model loaded", file=sys.stderr)
        except Exception as e:
            print(f"[TTS] Silero {lang} prewarm failed: {e}", file=sys.stderr)


class LocalFishSpeechEngine(BaseEngine):
    """
    Wraps generate_local_fish_speech() as a drop-in engine.
    probe() does a 2-second HEAD to FISH_LOCAL_URL/health — if the server is not
    running the probe fails silently and routing falls through to Kokoro.
    """
    name = "local_fish_speech"

    def probe(self) -> bool:
        import urllib.request, urllib.error
        base_url = os.environ.get("FISH_LOCAL_URL", "http://localhost:8080")
        # fish-speech 1.5 uses POST /v1/health (not GET /health)
        req = urllib.request.Request(
            f"{base_url}/v1/health", data=b"", method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        return generate_local_fish_speech(
            text=text, voice_id=voice_id, out_path=out_path, speed=speed, lang=lang,
        )

    def close(self):
        pass


# ── Chatterbox TTS ────────────────────────────────────────────────────────────
class ChatterboxEngine(BaseEngine):
    name = "chatterbox"
    _model = None
    _lock = threading.Lock()

    def probe(self) -> bool:
        try:
            import chatterbox  # noqa
            return True
        except ImportError:
            return False

    def _load(self, device: str):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from chatterbox.tts import ChatterboxTTS
                # Use local cached assets if available, else download via HF
                _cb_dir = Path(__file__).parent / "assets" / "models" / "chatterbox"
                if (_cb_dir / "t3_cfg.pt").exists():
                    import os as _os
                    _os.environ.setdefault("HF_HOME", str(_cb_dir.parent.parent / "models" / "hf_cache"))
                self._model = ChatterboxTTS.from_pretrained(device=device)
            except Exception as e:
                print(f"[ChatterboxEngine] load failed: {e}", file=sys.stderr)
                self._model = None

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        hw = _HW_PROFILE
        device = "mps" if hw.device == "mps" else ("cuda" if hw.cuda_available else "cpu")
        self._load(device)
        if self._model is None:
            raise RuntimeError("ChatterboxEngine model not loaded")
        try:
            import torchaudio
            # If voice_id is a Fish Audio reference ID, clone from its sample MP3
            ref_wav = _fish_ref_wav(voice_id, out_path)
            wav = self._model.generate(
                text,
                audio_prompt_path=ref_wav,
                exaggeration=float(exaggeration),
                cfg_weight=float(cfg),
            )
            torchaudio.save(str(out_path), wav, self._model.sr)
        except Exception as e:
            raise RuntimeError(f"ChatterboxEngine synthesis failed: {e}") from e
        finally:
            if ref_wav:
                try: os.unlink(ref_wav)
                except: pass
        return out_path

    def close(self):
        self._model = None


# ── Kokoro TTS (kokoro-onnx — no spacy, Python 3.11-3.14 compatible) ──────────
class KokoroEngine(BaseEngine):
    name = "kokoro"
    _model = None
    _voices = None
    _lock = threading.Lock()

    # All 54 Kokoro voices — identity map (voice_id passed through as-is)
    _VOICE_REMAP = {
        "af_heart":   "af_heart",
        "af_nova":   "af_nova",
        "af_sky":   "af_sky",
        "af_bella":   "af_bella",
        "af_sarah":   "af_sarah",
        "af_jessica":   "af_jessica",
        "af_river":   "af_river",
        "af_nicole":   "af_nicole",
        "af_alloy":   "af_alloy",
        "af_aoede":   "af_aoede",
        "af_kore":   "af_kore",
        "am_michael":   "am_michael",
        "am_fenrir":   "am_fenrir",
        "am_echo":   "am_echo",
        "am_onyx":   "am_onyx",
        "am_adam":   "am_adam",
        "am_puck":   "am_puck",
        "am_liam":   "am_liam",
        "am_eric":   "am_eric",
        "am_santa":   "am_santa",
        "bf_emma":   "bf_emma",
        "bf_isabella":   "bf_isabella",
        "bf_alice":   "bf_alice",
        "bf_lily":   "bf_lily",
        "bm_george":   "bm_george",
        "bm_daniel":   "bm_daniel",
        "bm_lewis":   "bm_lewis",
        "bm_fable":   "bm_fable",
        "ef_dora":   "ef_dora",
        "em_alex":   "em_alex",
        "em_santa":   "em_santa",
        "ff_siwis":   "ff_siwis",
        "hf_alpha":   "hf_alpha",
        "hf_beta":   "hf_beta",
        "hm_omega":   "hm_omega",
        "hm_psi":   "hm_psi",
        "if_sara":   "if_sara",
        "im_nicola":   "im_nicola",
        "jf_alpha":   "jf_alpha",
        "jf_gongitsune":   "jf_gongitsune",
        "jf_nezumi":   "jf_nezumi",
        "jf_tebukuro":   "jf_tebukuro",
        "jm_kumo":   "jm_kumo",
        "pf_dora":   "pf_dora",
        "pm_alex":   "pm_alex",
        "pm_santa":   "pm_santa",
        "zf_xiaobei":   "zf_xiaobei",
        "zf_xiaoni":   "zf_xiaoni",
        "zf_xiaoxiao":   "zf_xiaoxiao",
        "zf_xiaoyi":   "zf_xiaoyi",
        "zm_yunjian":   "zm_yunjian",
        "zm_yunxi":   "zm_yunxi",
        "zm_yunxia":   "zm_yunxia",
        "zm_yunyang":   "zm_yunyang",
    }

    def probe(self) -> bool:
        try:
            from kokoro_onnx import Kokoro  # noqa
            _local = Path(__file__).parent / "assets" / "models" / "kokoro"
            _cache = Path(os.path.expanduser("~/.cache/kokoro-onnx"))
            return any((
                (_local / "kokoro-v1.0.int8.onnx").exists(),
                (_cache / "kokoro-v1.0.int8.onnx").exists(),
            ))
        except ImportError:
            return False

    def _load(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from kokoro_onnx import Kokoro
                _local = Path(__file__).parent / "assets" / "models" / "kokoro"
                _cache = Path(os.path.expanduser("~/.cache/kokoro-onnx"))
                _model_f = next((str(p) for p in [
                    _local / "kokoro-v1.0.int8.onnx",
                    _cache / "kokoro-v1.0.int8.onnx",
                ] if Path(p).exists()), None)
                _voices_f = next((str(p) for p in [
                    _local / "voices-v1.0.bin",
                    _cache / "voices-v1.0.bin",
                ] if Path(p).exists()), None)
                if not _model_f or not _voices_f:
                    raise FileNotFoundError("Kokoro model not found. Run provision_all_voices.py")
                self._model = Kokoro(_model_f, _voices_f)
                # Re-create the ONNX session with tuned thread/exec settings.
                # Benchmarked on M3 (4P+4E): SEQUENTIAL + 3 intra-op threads with
                # spinning disabled beats the old 6-thread PARALLEL config (avoids
                # oversubscribing the 4 performance cores and OS-scheduler thrash
                # from busy-spin polling across P/E cores).
                import onnxruntime as _ort
                _opts = _ort.SessionOptions()
                _opts.intra_op_num_threads = 3
                _opts.inter_op_num_threads = 1
                _opts.execution_mode = _ort.ExecutionMode.ORT_SEQUENTIAL
                _opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
                _opts.add_session_config_entry("session.inter_op.allow_spinning", "0")
                self._model.sess = _ort.InferenceSession(
                    _model_f, sess_options=_opts,
                    providers=["CPUExecutionProvider"],
                )
                print(f"[KokoroEngine] loaded from {_model_f} (3 threads, sequential)", file=sys.stderr)
            except Exception as e:
                print(f"[KokoroEngine] kokoro-onnx load failed: {e}", file=sys.stderr)
                try:
                    from kokoro import KPipeline
                    self._model = KPipeline(lang_code="a")
                    self._voices = "legacy"
                except Exception as e2:
                    print(f"[KokoroEngine] kokoro fallback failed: {e2}", file=sys.stderr)
                    self._model = None

    _LANG_CODES: dict[str, str] = {
        "en": "en-us", "en-gb": "en-gb",
        "ja": "ja",    "zh": "zh",
        "fr": "fr-fr", "es": "es",
        "hi": "hi",    "it": "it",
        "pt": "pt-br", "ko": "ko",
        "de": "de",    "ru": "ru",
        "pl": "pl",    "nl": "nl",
        "tr": "tr",    "ar": "ar",
    }

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        self._load()
        if self._model is None:
            raise RuntimeError("KokoroEngine: no model loaded")
        voice = self._VOICE_REMAP.get(voice_id, "af_heart")
        try:
            import numpy as np, soundfile as sf
            if self._voices == "legacy":
                chunks = []
                for _, _, audio in self._model(text, voice=voice, speed=speed):
                    if audio is not None:
                        chunks.append(audio)
                audio_arr = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
                sf.write(str(out_path), audio_arr, 24000)
            else:
                klang = self._LANG_CODES.get(lang, "en-us")
                samples, sample_rate = self._model.create(
                    text, voice=voice, speed=speed, lang=klang
                )
                sf.write(str(out_path), samples, sample_rate)
        except Exception as e:
            raise RuntimeError(f"KokoroEngine synthesis failed: {e}") from e
        return out_path

    def close(self):
        self._model = None
        self._voices = None


# ── macOS `say` — fast neural voices ──────────────────────────────────────────
class MacSayEngine(BaseEngine):
    name = "macos_say"

    # Modern neural voices per language — faster than legacy Samantha/Daniel.
    # Direct WAV output (--data-format=LEI16@22050) skips intermediate AIFF
    # and afconvert, cutting ~0.2s off each call.
    _LANG_VOICE_MAP: dict[str, str] = {
        "en":    "Reed (English (US))",   # 0.41s neural
        "en-gb": "Reed (English (UK))",
        "ja":    "Reed (Japanese (Japan))",
        "zh":    "Tingting",
        "zh-tw": "Meijia",
        "zh-hk": "Sinji",
        "ko":    "Yuna",
        "ar":    "Majed",
        "ru":    "Milena",
        "de":    "Reed (German (Germany))",
        "fr":    "Thomas",
        "es":    "Paulina",
        "pt":    "Luciana",
        "it":    "Alice",
        "hi":    "Lekha",
        "nl":    "Xander",
        "tr":    "Yelda",
        "pl":    "Zosia",
        "sw":    "Reed (English (US))",   # no Swahili voice — fallback
    }

    # Legacy voice_id → macOS voice (used when lang-based lookup misses)
    _VOICE_MAP: dict[str, str] = {
        "af_heart": "Reed (English (US))",
        "af_nova":  "Reed (English (US))",
        "am_echo":  "Reed (English (US))",
    }

    def probe(self) -> bool:
        return platform.system() == "Darwin" and shutil.which("say") is not None

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        voice = self._LANG_VOICE_MAP.get(lang) or self._VOICE_MAP.get(voice_id, "Reed (English (US))")
        rate  = max(100, min(400, int(175 * speed)))
        # Direct WAV output — no intermediate AIFF, no afconvert (~0.2s faster)
        ret = subprocess.run(
            ["say", "-v", voice, "-r", str(rate),
             "-o", str(out_path), "--data-format=LEI16@22050", text],
            capture_output=True,
        )
        if ret.returncode != 0 or not out_path.exists():
            # Fallback: legacy two-step path in case direct WAV isn't supported
            aiff = out_path.with_suffix(".aiff")
            subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(aiff), text],
                           check=True, capture_output=True)
            subprocess.run(["afconvert", str(aiff), str(out_path), "-d", "LEI16", "-f", "WAVE"],
                           check=True, capture_output=True)
            aiff.unlink(missing_ok=True)
        return out_path

    def close(self):
        pass


# ── Silero TTS (fast local, no voice cloning, ru/de) ─────────────────────────
class SileroEngine(BaseEngine):
    name = "silero"
    _models: dict = {}
    _lock = threading.Lock()

    _LANG_CONFIG: dict = {
        "ru": {"speaker": "v3_1_ru", "sample_rate": 48000,
               "voices": {"m": "aidar", "f": "xenia"}},
        "de": {"speaker": "v3_de",   "sample_rate": 48000,
               "voices": {"m": "karlsson", "f": "karlsson"}},
    }

    def probe(self) -> bool:
        try:
            import torch  # noqa
            return True
        except ImportError:
            return False

    def _get_model(self, lang: str):
        if lang in self._models:
            return self._models[lang]
        with self._lock:
            if lang in self._models:
                return self._models[lang]
            cfg = self._LANG_CONFIG[lang]
            import torch
            model, _ = torch.hub.load(
                repo_or_dir="snakers4/silero-models",
                model="silero_tts",
                language=lang,
                speaker=cfg["speaker"],
                trust_repo=True,
            )
            model.to("cpu")  # in-place for torch.package models; do NOT reassign
            self._models[lang] = model
            return model

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        lang_cfg = self._LANG_CONFIG.get(lang)
        if not lang_cfg:
            raise RuntimeError(f"[Silero] unsupported lang: {lang!r}")
        reg = json.loads(_REGISTRY_PATH.read_text())
        gender = reg.get("engines", {}).get("fish", {}).get("voices", {}).get(voice_id, {}).get("gender", "m")
        silero_voice = lang_cfg["voices"].get(gender, lang_cfg["voices"]["m"])
        model = self._get_model(lang)
        import torch, torchaudio, warnings
        sr = lang_cfg["sample_rate"]
        with torch.inference_mode():
            audio = model.apply_tts(text=text.strip(), speaker=silero_voice, sample_rate=sr)
        # Convert to 16-bit PCM — browser <audio> elements can't handle 32-bit float WAV
        audio_i16 = (audio * 32767).clamp(-32768, 32767).to(torch.int16)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            torchaudio.save(str(out_path), audio_i16.unsqueeze(0), sr,
                            encoding="PCM_S", bits_per_sample=16)
        return out_path

    def close(self):
        self._models.clear()


# ── espeak fallback (Linux/Windows) ───────────────────────────────────────────
class ESpeakEngine(BaseEngine):
    name = "espeak"

    def probe(self) -> bool:
        return shutil.which("espeak-ng") is not None or shutil.which("espeak") is not None

    def generate(self, text: str, voice_id: str, speed: float,
                 exaggeration: float, cfg: float, out_path: Path,
                 lang: str = "en") -> Path:
        bin_ = shutil.which("espeak-ng") or "espeak"
        spd  = max(80, min(450, int(175 * speed)))
        subprocess.run(
            [bin_, "-s", str(spd), "-w", str(out_path), text],
            check=True, capture_output=True,
        )
        return out_path

    def close(self):
        pass


# ── Engine router ─────────────────────────────────────────────────────────────
_HW_PROFILE: HardwareProfile = probe_hardware()

_ALL_ENGINES: list[BaseEngine] = [
    ElevenLabsEngine(),
    FishAudioAPIEngine(),
    LocalFishSpeechEngine(),
    FishEngine(),
    ChatterboxEngine(),
    KokoroEngine(),
    SileroEngine(),
    MacSayEngine(),
    ESpeakEngine(),
]

# Tier → preferred engine names (in order).
# kokoro is BEFORE macos_say on all tiers — macos_say was jumping the queue
# every time local_fish_speech was mid-startup (its probe fails until the
# subprocess is fully warm, which happens on every restart), so the very
# first replies after every restart came out in the robotic macOS voice
# instead of the configured Kokoro/Fish voice. Kokoro has no such startup
# race (in-process ONNX model) and is now tuned to ~3s, so it's a much
# less jarring fallback than macos_say while Fish Speech is still booting.
_TIER_PREFERENCES: dict[int, list[str]] = {
    3: ["elevenlabs", "fish_audio_s2", "local_fish_speech", "kokoro", "macos_say", "chatterbox", "espeak"],
    2: ["elevenlabs", "local_fish_speech", "kokoro", "macos_say", "chatterbox", "espeak"],
    1: ["elevenlabs", "local_fish_speech", "kokoro", "macos_say", "espeak"],
}

_ENGINE_MAP: dict[str, BaseEngine] = {e.name: e for e in _ALL_ENGINES}
_probe_cache: dict[str, bool] = {}

# ── Audio cache (LRU, keyed by text+voice+engine) ────────────────────────────
from functools import lru_cache as _lru_cache

_AUDIO_CACHE: dict[str, bytes] = {}
_AUDIO_CACHE_MAX = 64
_AUDIO_CACHE_ORDER: list[str] = []
_AUDIO_CACHE_LOCK = threading.Lock()

def _cache_key(text: str, voice: str, engine: str) -> str:
    return hashlib.md5(f"{engine}|{voice}|{text}".encode()).hexdigest()

def _cache_get(key: str) -> bytes | None:
    with _AUDIO_CACHE_LOCK:
        return _AUDIO_CACHE.get(key)

def _cache_put(key: str, audio: bytes) -> None:
    with _AUDIO_CACHE_LOCK:
        if key in _AUDIO_CACHE:
            return
        _AUDIO_CACHE[key] = audio
        _AUDIO_CACHE_ORDER.append(key)
        if len(_AUDIO_CACHE_ORDER) > _AUDIO_CACHE_MAX:
            old = _AUDIO_CACHE_ORDER.pop(0)
            _AUDIO_CACHE.pop(old, None)


_probe_cache_ts: dict[str, float] = {}
# Engines backed by an external subprocess/API can die mid-session (crash, OOM,
# manual restart) — a positive probe for these must expire, or a dead server
# stays "trusted" forever and every request pays a failed-connection attempt
# instead of degrading gracefully. File/binary-existence probes (kokoro,
# macos_say, espeak, ...) don't go stale like this, so they still cache forever.
_PROBE_TTL_SEC = {"local_fish_speech": 10, "fish_audio_s2": 10}

def _probe(engine: BaseEngine) -> bool:
    ttl = _PROBE_TTL_SEC.get(engine.name)
    if _probe_cache.get(engine.name):
        if ttl is None or (time.monotonic() - _probe_cache_ts.get(engine.name, 0)) < ttl:
            return True
    ok = engine.probe()
    if ok:
        _probe_cache[engine.name] = True
        _probe_cache_ts[engine.name] = time.monotonic()
    return ok


def select_engine(forced: Optional[str] = None) -> BaseEngine:
    """Return the best available engine for current hardware tier."""
    if forced and forced in _ENGINE_MAP:
        e = _ENGINE_MAP[forced]
        if _probe(e):
            return e
        print(f"[TTS] forced engine {forced!r} unavailable (probe failed) — "
              "falling through to tier preference cascade", flush=True)
    tier = _HW_PROFILE.tier()
    skipped = []
    for name in _TIER_PREFERENCES.get(tier, []):
        e = _ENGINE_MAP.get(name)
        if e and _probe(e):
            if skipped:
                # A silent quality/character downgrade — no exception is raised for this,
                # so without this log a degraded voice is invisible until someone notices
                # by ear. This is the actual common-case path (probe timeout/failure),
                # distinct from the explicit-engine-request failures logged in server.py.
                print(f"[TTS] degraded to {name!r} — {skipped!r} unavailable (probe failed)",
                      flush=True)
            return e
        skipped.append(name)
    raise RuntimeError("No TTS engine available — install kokoro, chatterbox, or use macOS")


def engine_status() -> list[dict]:
    return [
        {"name": e.name, "available": _probe(e)}
        for e in _ALL_ENGINES
    ]


# ── FastAPI server ─────────────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse, FileResponse
    from pydantic import BaseModel, Field
    import uvicorn
    _FASTAPI = True
except ImportError:
    _FASTAPI = False


if _FASTAPI:
    app = FastAPI(title="Sovereign Nexus TTS", version="1.0.0")

    class TTSRequest(BaseModel):
        text: str = Field(..., description="Text to synthesize")
        persona: Optional[str] = Field(None, description="JARVIS persona key (e.g. regional_new_york)")
        lang: Optional[str] = Field("en", description="Language code for Fish voice selection (en/ru/ja/de/ar/pt/ko/zh/fr)")
        emotion: Optional[str] = Field("neutral", description="Emotion tag")
        voice_id: Optional[str] = Field(None, description="Override voice ID")
        speed: Optional[float] = Field(None, description="Speed multiplier (0.5-2.0)")
        engine: Optional[str] = Field(None, description="Force specific engine name")
        return_audio: bool = Field(False, description="Return audio file instead of JSON")

    class TTSResponse(BaseModel):
        engine: str
        accelerator: str
        audio_path: str
        duration_ms: int
        voice_id: str
        persona: Optional[str]
        emotion: str
        text_length: int

    @app.post("/v1/tts")
    def synthesize(req: TTSRequest):
        if not req.text or not req.text.strip():
            raise HTTPException(400, "text is required")

        # Resolve voice via unified registry
        lang    = (req.lang or "en") if hasattr(req, "lang") else "en"
        persona = req.persona or "default"
        fish_ref = get_fish_voice_id(persona, lang)
        voice_id = (
            req.voice_id
            or resolve_voice(persona, lang, engine.name)
            or get_lang_default(lang)
            or "af_heart"
        )

        # Resolve emotion params
        emotion = req.emotion or "neutral"
        ep = EMOTION_PARAMS.get(emotion, EMOTION_PARAMS["neutral"])
        persona_speed = _PERSONA_SPEED.get(persona, None)
        speed = req.speed if req.speed is not None else (persona_speed or ep["speed"])

        # Select engine
        try:
            engine = select_engine(req.engine)
        except RuntimeError as e:
            raise HTTPException(503, str(e))

        # Output path
        uid   = uuid.uuid4().hex[:12]
        fpath = TTS_OUT / f"tts_{uid}.wav"

        # When Fish engine is active, inject the reference_id from our downloaded voices
        effective_voice = fish_ref if (engine.name == "fish_audio_s2" and fish_ref) else voice_id

        # Generate
        t0 = time.monotonic()
        try:
            engine.generate(
                text=req.text.strip(),
                voice_id=effective_voice,
                speed=speed,
                exaggeration=ep["exaggeration"],
                cfg=ep["cfg"],
                out_path=fpath,
                lang=lang,
            )
        except Exception as e:
            raise HTTPException(500, f"Synthesis failed [{engine.name}]: {e}")

        duration_ms = int((time.monotonic() - t0) * 1000)

        if req.return_audio:
            return FileResponse(str(fpath), media_type="audio/wav",
                                filename=fpath.name)

        return TTSResponse(
            engine=engine.name,
            accelerator=_HW_PROFILE.accelerator,
            audio_path=str(fpath),
            duration_ms=duration_ms,
            voice_id=voice_id,
            persona=req.persona,
            emotion=emotion,
            text_length=len(req.text),
        )

    @app.get("/v1/engines")
    def get_engines():
        return {
            "hardware": {
                "system":       _HW_PROFILE.system,
                "accelerator":  _HW_PROFILE.accelerator,
                "memory_gb":    round(_HW_PROFILE.memory_gb, 1),
                "device":       _HW_PROFILE.device,
                "cuda_vram_gb": _HW_PROFILE.cuda_vram_gb,
                "gpu_name":     _HW_PROFILE.gpu_name,
                "tier":         _HW_PROFILE.tier(),
            },
            "engines":   engine_status(),
            "preferred": _TIER_PREFERENCES.get(_HW_PROFILE.tier(), [])[0]
                         if any(_probe(_ENGINE_MAP[n])
                                for n in _TIER_PREFERENCES.get(_HW_PROFILE.tier(), [])
                                if n in _ENGINE_MAP)
                         else None,
            "personas":      PERSONA_VOICE,
            "emotions":      list(EMOTION_PARAMS.keys()),
            "lang_defaults": _REGISTRY.get("lang_defaults", {}),
            "kokoro_voices": list(_REGISTRY.get("engines", {}).get("kokoro", {}).get("voices", {}).keys()),
        }

    @app.get("/health")
    def health():
        return {"status": "ok", "port": TTS_PORT, "out_dir": str(TTS_OUT)}

    # ── JARVIS integration helper ─────────────────────────────────────────────
    @app.post("/v1/speak")
    def speak_sync(req: TTSRequest):
        """Generate audio and play it immediately via system audio."""
        resp = synthesize(req)
        audio_path = resp.audio_path if hasattr(resp, "audio_path") else resp["audio_path"]
        sys_name = platform.system()
        try:
            if sys_name == "Darwin":
                subprocess.Popen(["afplay", audio_path])
            elif sys_name == "Linux":
                subprocess.Popen(["aplay", audio_path])
            elif sys_name == "Windows":
                subprocess.Popen(["powershell", "-c",
                                   f"(New-Object Media.SoundPlayer '{audio_path}').PlaySync()"])
        except Exception as e:
            print(f"[speak] playback error: {e}", file=sys.stderr)
        return resp


# ── CLI / direct synthesis (no server) ───────────────────────────────────────
def synthesize_direct(text: str, persona: str = "default", emotion: str = "neutral",
                      voice_id: str = None, speed: float = None,
                      engine_name: str = None, lang: str = "en") -> dict:
    """
    Synthesize without running the FastAPI server.
    Returns the same dict shape as /v1/tts.
    """
    _wants_fish = engine_name in ("fish", "fish_audio_api", "fish_audio_s2")
    _has_fish_key = bool(os.environ.get("FISH_AUDIO_API_KEY", "").strip())
    _SILERO_LANGS = {"ru", "de"}

    # Check fish reference availability BEFORE engine selection so we never
    # route to local_fish_speech only to discover there's no reference audio.
    fish_ref = get_fish_voice_id(persona, lang) if not voice_id else voice_id
    _has_fish_ref = bool(fish_ref and len(str(fish_ref)) == 32)

    if _wants_fish:
        # Always use local fish server — free, zero per-character API cost.
        # FISH_AUDIO_API_KEY is stored in .env but not used here to avoid charges.
        engine = select_engine("local_fish_speech")
    elif engine_name is None and not _has_fish_ref:
        # No fish reference — use macOS say (neural voices, 0.4-0.6s, all langs)
        engine = select_engine("macos_say")
    else:
        engine = select_engine(engine_name)

    if not _wants_fish and not voice_id:
        fish_ref = get_fish_voice_id(persona, lang)
    resolved = (
        voice_id
        or resolve_voice(persona, lang, engine.name)
        or get_lang_default(lang)
        or PERSONA_VOICE.get(persona, PERSONA_VOICE["default"])
    )
    # For fish engines: keep the 32-char reference ID so the engine can do ICL
    if engine.name in ("fish_audio_s2", "local_fish_speech"):
        effective_voice = voice_id or fish_ref or resolved
        if engine.name == "local_fish_speech" and (
            not effective_voice or len(effective_voice) != 32
        ):
            # No reference audio for this persona/lang — fall back to Kokoro
            print(f"[LocalFish] no reference audio for (persona={persona!r}, lang={lang!r}) → Kokoro", flush=True)
            engine = select_engine("kokoro")
            effective_voice = resolved
        # Note: with ICL reference audio, all languages work on openaudio-s1-mini.
        # The _LOCAL_FISH_LANGS restriction only applied to text-only synthesis (no ref audio),
        # which never reaches here since we fall back to macos_say when no API key + no ref.
    else:
        effective_voice = resolved

    ep = EMOTION_PARAMS.get(emotion, EMOTION_PARAMS["neutral"])
    spd = speed if speed is not None else ep["speed"]
    uid = uuid.uuid4().hex[:12]
    fpath = TTS_OUT / f"tts_{uid}.wav"
    t0 = time.monotonic()

    # Check audio cache (keyed by text + voice + engine)
    ckey = _cache_key(text.strip(), effective_voice, engine.name)
    cached = _cache_get(ckey)
    if cached:
        fpath.write_bytes(cached)
        return {
            "engine":      engine.name + "+cache",
            "accelerator": _HW_PROFILE.accelerator,
            "audio_path":  str(fpath),
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "voice_id":    effective_voice,
            "persona":     persona,
            "emotion":     emotion,
            "lang":        lang,
            "text_length": len(text),
        }

    try:
        engine.generate(text.strip(), effective_voice, spd, ep["exaggeration"], ep["cfg"], fpath, lang=lang)
    except Exception as e:
        if engine.name in _PROBE_TTL_SEC:
            # Probe said alive but generation still failed (crashed mid-session,
            # OOM, connection drop between probe and generate) — invalidate the
            # stale positive probe and degrade to Kokoro here, in-process, rather
            # than letting the exception propagate to the caller's own
            # macOS-say-only fallback and skip Kokoro entirely.
            _probe_cache.pop(engine.name, None)
            print(f"[{engine.name}] generate failed ({e}) despite a live probe — "
                  "falling back to Kokoro", flush=True)
            engine = select_engine("kokoro")
            effective_voice = (
                resolve_voice(persona, lang, engine.name)
                or get_lang_default(lang)
                or PERSONA_VOICE.get(persona, PERSONA_VOICE["default"])
            )
            engine.generate(text.strip(), effective_voice, spd, ep["exaggeration"], ep["cfg"], fpath, lang=lang)
        else:
            raise
    # Store in cache (only cache short phrases to avoid memory bloat)
    if len(text) <= 300:
        try:
            _cache_put(ckey, fpath.read_bytes())
        except Exception:
            pass
    return {
        "engine":       engine.name,
        "accelerator":  _HW_PROFILE.accelerator,
        "audio_path":   str(fpath),
        "duration_ms":  int((time.monotonic() - t0) * 1000),
        "voice_id":     effective_voice,
        "persona":      persona,
        "emotion":      emotion,
        "lang":         lang,
        "text_length":  len(text),
    }


# ── Installation helper ───────────────────────────────────────────────────────
def install_guide() -> str:
    tier = _HW_PROFILE.tier()
    hw   = _HW_PROFILE.accelerator
    lines = [
        f"Hardware detected: {hw} (tier {tier})",
        "",
        "Install commands for your tier:",
    ]
    if tier == 3:
        lines += [
            "  # Fish Audio S2 Pro (primary — high-end)",
            "  pip install fish-speech",
            "  # Download checkpoint:",
            "  huggingface-cli download fishaudio/fish-speech-1.5 --local-dir ~/.cache/fish-speech/fish-speech-1.5",
            "",
            "  # Chatterbox (fallback)",
            "  pip install chatterbox-tts",
        ]
    if tier >= 2:
        lines += [
            "",
            "  # Chatterbox (primary mid-tier / fallback for high-end)",
            "  pip install chatterbox-tts",
        ]
    lines += [
        "",
        "  # Kokoro (lightweight fallback — always recommended)",
        "  pip install kokoro soundfile",
        "",
        "  # Audio deps",
        "  pip install soundfile numpy",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Sovereign Nexus TTS Daemon")
    p.add_argument("--port",   type=int, default=TTS_PORT)
    p.add_argument("--host",   default="127.0.0.1")
    p.add_argument("--speak",  help="Quick CLI synthesis: speak this text")
    p.add_argument("--persona", default="default")
    p.add_argument("--emotion", default="neutral")
    p.add_argument("--engine", default=None)
    p.add_argument("--info",   action="store_true", help="Show hardware info + install guide")
    args = p.parse_args()

    if args.info:
        print(json.dumps({
            "hardware": {
                "system":      _HW_PROFILE.system,
                "accelerator": _HW_PROFILE.accelerator,
                "memory_gb":   round(_HW_PROFILE.memory_gb, 1),
                "device":      _HW_PROFILE.device,
                "tier":        _HW_PROFILE.tier(),
            },
            "engines": engine_status(),
        }, indent=2))
        print("\n" + install_guide())
        sys.exit(0)

    if args.speak:
        result = synthesize_direct(args.speak, persona=args.persona,
                                   emotion=args.emotion, engine_name=args.engine)
        print(json.dumps(result, indent=2))
        sys.exit(0)

    if not _FASTAPI:
        print("fastapi + uvicorn required: pip install fastapi uvicorn[standard]")
        sys.exit(1)

    print(f"[Sovereign Nexus TTS] Hardware: {_HW_PROFILE.accelerator} "
          f"({_HW_PROFILE.memory_gb:.0f}GB) → tier {_HW_PROFILE.tier()}")
    print(f"[Sovereign Nexus TTS] Engines: {[s['name'] for s in engine_status() if s['available']]}")
    print(f"[Sovereign Nexus TTS] Listening on http://{args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
