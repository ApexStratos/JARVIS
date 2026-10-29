#!/usr/bin/env python3
"""
voice_provisioner.py — Sovereign Nexus Voice Provisioning System
================================================================
Scans all persona definitions, maps each to the optimal voice across
Fish Audio S2 / Chatterbox / Kokoro, downloads required assets, and
writes a voice_manifest.json consumed by the TTS daemon at runtime.

Usage:
  python voice_provisioner.py               # full provision
  python voice_provisioner.py --list        # show mapping without downloading
  python voice_provisioner.py --persona noir_detective  # single persona
  python voice_provisioner.py --verify      # check integrity of existing assets
"""

import os, sys, json, time, hashlib, shutil, argparse, subprocess, re, threading
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE        = Path(__file__).parent
ASSETS      = BASE / "assets" / "voices"
MANIFEST    = BASE / "voice_manifest.json"
KOKORO_CACHE = Path.home() / ".cache" / "kokoro-onnx"

ASSETS.mkdir(parents=True, exist_ok=True)

# ── Import personas ────────────────────────────────────────────────────────────
sys.path.insert(0, str(BASE))
try:
    from jarvis_core_personas     import JARVIS_CORE_PERSONAS
    from jarvis_regional_personas import JARVIS_REGIONAL_PERSONAS
except ImportError:
    JARVIS_CORE_PERSONAS, JARVIS_REGIONAL_PERSONAS = {}, {}

ALL_PERSONAS = {**JARVIS_CORE_PERSONAS, **JARVIS_REGIONAL_PERSONAS}

# ── Persona analysis ───────────────────────────────────────────────────────────
# Each entry: (gender, age, energy, accent, emotion_bias)
# gender:  m=male  f=female  n=neutral
# energy:  calm / neutral / energetic / intense
# accent:  american / british / southern / neutral / regional_*

PERSONA_PROFILE = {
    # Core personas
    "noir_detective":                 ("m", "40s", "calm",      "american",  "cynical"),
    "pirate_captain":                 ("m", "50s", "energetic", "british",   "dramatic"),
    "dramatic_theater_director":      ("m", "50s", "intense",   "british",   "dramatic"),
    "surfer_dude":                    ("m", "20s", "calm",      "american",  "happy"),
    "hyper_corporate_manager":        ("m", "40s", "energetic", "american",  "urgent"),
    "conspiracy_cryptid":             ("m", "40s", "calm",      "american",  "curious"),
    "overly_dramatic_anime_protagonist":("m","teens","intense", "american",  "excited"),
    "grumpy_old_mechanic":            ("m", "60s", "calm",      "american",  "angry"),
    "medieval_fantasy_tavern_keeper": ("m", "50s", "energetic", "british",   "happy"),
    "panicked_intern":                ("f", "20s", "intense",   "american",  "urgent"),
    "tech_bro_guru":                  ("m", "30s", "energetic", "american",  "excited"),
    "glitchy_chatbot":                ("n", "n/a", "neutral",   "neutral",   "curious"),
    "1800s_aristocrat":               ("m", "50s", "calm",      "british",   "neutral"),
    "hyper_active_toddler":           ("f", "5s",  "intense",   "american",  "excited"),
    "space_marine_drill_sergeant":    ("m", "40s", "intense",   "american",  "angry"),
    "passive_aggressive_roommate":    ("f", "20s", "calm",      "american",  "sad"),
    # Regional personas
    "regional_new_york":              ("m", "30s", "energetic", "new_york",  "neutral"),
    "regional_california":            ("f", "20s", "calm",      "california","happy"),
    "regional_dmv":                   ("m", "25s", "neutral",   "dmv",       "neutral"),
    "regional_chicago":               ("m", "30s", "energetic", "chicago",   "neutral"),
    "regional_atlanta":               ("m", "25s", "calm",      "atlanta",   "happy"),
    # Default JARVIS
    "default":                        ("f", "30s", "neutral",   "american",  "neutral"),
}

# ── Kokoro voice catalogue ─────────────────────────────────────────────────────
# Format: voice_id → (gender, quality_score, accent, energy, notes)
KOKORO_VOICES = {
    # Female American
    "af_heart":    ("f", 10, "american",   "neutral",   "warm, studio-grade, default JARVIS"),
    "af_nova":     ("f",  9, "american",   "energetic", "bright, clear, NYC energy"),
    "af_sky":      ("f",  9, "california", "calm",      "smooth west-coast cadence"),
    "af_bella":    ("f",  8, "american",   "calm",      "soft, gentle"),
    "af_sarah":    ("f",  8, "american",   "neutral",   "professional clarity"),
    "af_nicole":   ("f",  7, "american",   "calm",      "whisper-warm"),
    "af_jessica":  ("f",  7, "american",   "energetic", "upbeat, friendly"),
    "af_river":    ("f",  7, "american",   "neutral",   "balanced, clear"),
    # Male American
    "am_michael":  ("m", 10, "american",   "neutral",   "deep, authoritative"),
    "am_fenrir":   ("m",  9, "american",   "energetic", "powerful, dynamic"),
    "am_echo":     ("m",  9, "american",   "calm",      "smooth, professional"),
    "am_puck":     ("m",  8, "american",   "energetic", "sharp, crisp"),
    "am_adam":     ("m",  8, "american",   "calm",      "warm baritone"),
    "am_liam":     ("m",  7, "american",   "neutral",   "young, clear"),
    "am_onyx":     ("m",  7, "american",   "intense",   "gravelly authority"),
    "am_santa":    ("m",  6, "american",   "happy",     "warm, jolly"),
    # British
    "bf_emma":     ("f",  9, "british",    "neutral",   "clear RP accent"),
    "bf_isabella": ("f",  8, "british",    "calm",      "refined, aristocratic"),
    "bm_george":   ("m",  9, "british",    "calm",      "authoritative RP"),
    "bm_lewis":    ("m",  8, "british",    "neutral",   "modern British"),
    "bm_daniel":   ("m",  8, "british",    "energetic", "lively, dramatic"),
}

# ── Persona → Kokoro voice mapping ─────────────────────────────────────────────
def _select_kokoro_voice(persona_key: str) -> str:
    """Pick the best Kokoro voice for this persona based on profile analysis."""
    profile = PERSONA_PROFILE.get(persona_key,
              PERSONA_PROFILE.get("default", ("f", "30s", "neutral", "american", "neutral")))
    gender, age, energy, accent, emotion = profile

    # Accent-first routing
    if "british" in accent or persona_key in ("pirate_captain", "dramatic_theater_director",
                                               "medieval_fantasy_tavern_keeper", "1800s_aristocrat"):
        if gender == "m":
            if energy == "intense" or emotion == "dramatic": return "bm_daniel"
            if energy == "calm":                             return "bm_george"
            return "bm_lewis"
        return "bf_emma"

    # American routing by gender + energy
    if gender == "f":
        if energy == "intense" or emotion in ("urgent", "excited"): return "af_nova"
        if accent == "california":                                   return "af_sky"
        if energy == "calm" or emotion in ("sad", "neutral"):        return "af_bella"
        return "af_heart"

    # Male American
    if energy == "intense" or emotion == "angry":  return "am_onyx"
    if energy == "energetic" or emotion == "excited": return "am_fenrir"
    if energy == "calm" and accent in ("new_york", "dmv"): return "am_echo"
    if accent in ("chicago", "atlanta"):            return "am_michael"
    if persona_key == "glitchy_chatbot":            return "af_heart"  # neutral/androgynous
    return "am_michael"

# ── Speed override per persona ─────────────────────────────────────────────────
PERSONA_SPEED = {
    "hyper_active_toddler":              1.35,
    "overly_dramatic_anime_protagonist": 1.20,
    "panicked_intern":                   1.15,
    "hyper_corporate_manager":           1.10,
    "surfer_dude":                       0.88,
    "noir_detective":                    0.90,
    "grumpy_old_mechanic":               0.85,
    "1800s_aristocrat":                  0.88,
    "glitchy_chatbot":                   1.05,
}

# ── Emotion → TTS params (extended) ───────────────────────────────────────────
EMOTION_PARAMS = {
    "neutral":  {"speed": 1.00, "exaggeration": 0.45, "cfg": 0.50},
    "excited":  {"speed": 1.18, "exaggeration": 0.82, "cfg": 0.62},
    "calm":     {"speed": 0.88, "exaggeration": 0.28, "cfg": 0.38},
    "urgent":   {"speed": 1.28, "exaggeration": 0.78, "cfg": 0.68},
    "sad":      {"speed": 0.80, "exaggeration": 0.38, "cfg": 0.32},
    "angry":    {"speed": 1.12, "exaggeration": 0.92, "cfg": 0.72},
    "curious":  {"speed": 1.00, "exaggeration": 0.52, "cfg": 0.48},
    "happy":    {"speed": 1.10, "exaggeration": 0.72, "cfg": 0.56},
    "dramatic": {"speed": 0.92, "exaggeration": 0.88, "cfg": 0.65},
    "cynical":  {"speed": 0.90, "exaggeration": 0.42, "cfg": 0.40},
    "intense":  {"speed": 1.05, "exaggeration": 0.90, "cfg": 0.70},
}

# ── Kokoro model assets ────────────────────────────────────────────────────────
KOKORO_ASSETS = {
    "kokoro-v1.0.int8.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
    "voices-v1.0.bin":        "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}

# ── Data classes ───────────────────────────────────────────────────────────────
@dataclass
class VoiceProfile:
    persona_key:   str
    display_name:  str
    gender:        str
    energy:        str
    emotion_bias:  str
    accent:        str
    kokoro_voice:  str
    speed:         float
    emotion_params: dict
    engine_priority: list   # ordered list of engines to try
    assets:        dict = field(default_factory=dict)  # filename → local path


@dataclass
class Manifest:
    version:   str
    generated: str
    profiles:  dict   # persona_key → VoiceProfile dict
    kokoro_model:  str
    kokoro_voices: str
    emotion_params: dict


# ── Asset download ─────────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def _download(url: str, dest: Path, min_size: int = 1000) -> bool:
    """Download url to dest. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        result = subprocess.run(
            ["curl", "-fsSL", "--retry", "3", "--retry-delay", "2",
             "-o", str(tmp), url],
            capture_output=True, timeout=300,
        )
        if result.returncode != 0 or tmp.stat().st_size < min_size:
            print(f"  ✗ download failed: {dest.name}")
            tmp.unlink(missing_ok=True)
            return False
        tmp.rename(dest)
        print(f"  ✓ {dest.name}  ({dest.stat().st_size // 1024}KB)")
        return True
    except Exception as e:
        print(f"  ✗ {dest.name}: {e}")
        tmp.unlink(missing_ok=True)
        return False

def provision_kokoro_models() -> dict:
    """Download Kokoro ONNX model + voices if not present. Returns paths."""
    paths = {}
    print("\n[Kokoro] Checking model assets…")
    for fname, url in KOKORO_ASSETS.items():
        dest = KOKORO_CACHE / fname
        if dest.exists() and dest.stat().st_size > 1_000_000:
            print(f"  ✓ {fname} already present")
        else:
            print(f"  ↓ downloading {fname}…")
            KOKORO_CACHE.mkdir(parents=True, exist_ok=True)
            _download(url, dest, min_size=1_000_000)
        if dest.exists():
            paths[fname] = str(dest)
    return paths

def _generate_persona_sample(persona_key: str, voice_id: str, speed: float,
                              ep: dict, out_dir: Path) -> Optional[Path]:
    """
    Generate a 3-second reference WAV for this persona using Kokoro.
    Used for Chatterbox/Fish voice cloning reference when those engines are active.
    """
    samples = {
        "noir_detective":      "The city never sleeps, and neither do I. What have you got for me?",
        "pirate_captain":      "Arrr, the seas be rough tonight but we sail on regardless, mate.",
        "dramatic_theater_director": "The stage is set. Every function call is a soliloquy of pure intent.",
        "surfer_dude":         "Dude, that was like, totally gnarly execution, right on bro.",
        "hyper_corporate_manager": "Let us action this deliverable and synergize our key performance indicators.",
        "conspiracy_cryptid":  "They are watching. The logs never lie. Trust nothing, verify everything.",
        "panicked_intern":     "Okay okay okay, I can fix this, just give me one more second please.",
        "grumpy_old_mechanic": "Back in my day we did not need fourteen layers of abstraction to print hello world.",
        "tech_bro_guru":       "Optimizing your stack is just optimizing your mind, king. Ship it.",
        "noir_detective":      "The rain keeps falling and the bugs keep coming. Story of my life.",
        "default":             "Hello. I am JARVIS, your personal AI assistant. How can I help you today?",
        "regional_new_york":   "Deadass though, what do you need? Let us get this done already.",
        "regional_california": "Hey, that is totally bussin. Hella stoked to help you out right now.",
        "regional_dmv":        "Stamp, I got you moe. What is the move today?",
        "regional_chicago":    "Finna get this handled for you. On grave, we are going to figure this out.",
        "regional_atlanta":    "Twin, you already know I got you. Let us get this motion going.",
    }
    text = samples.get(persona_key, samples["default"])

    out_path = out_dir / f"{persona_key}_ref.wav"
    if out_path.exists() and out_path.stat().st_size > 5000:
        return out_path

    try:
        from kokoro_onnx import Kokoro
        model_f  = str(KOKORO_CACHE / "kokoro-v1.0.int8.onnx")
        voices_f = str(KOKORO_CACHE / "voices-v1.0.bin")
        if not Path(model_f).exists() or not Path(voices_f).exists():
            return None
        model = Kokoro(model_f, voices_f)
        samples_arr, sr = model.create(text, voice=voice_id, speed=speed, lang="en-us")
        import soundfile as sf
        sf.write(str(out_path), samples_arr, sr)
        return out_path
    except Exception as e:
        print(f"    ⚠ sample gen failed for {persona_key}: {e}")
        return None

# ── Core provisioning ──────────────────────────────────────────────────────────
def build_profiles() -> dict[str, VoiceProfile]:
    profiles = {}
    for key in list(ALL_PERSONAS.keys()) + ["default"]:
        profile = PERSONA_PROFILE.get(key, PERSONA_PROFILE.get("default"))
        gender, age, energy, accent, emotion = profile

        voice_id = _select_kokoro_voice(key)
        speed    = PERSONA_SPEED.get(key, 1.0)
        ep       = EMOTION_PARAMS.get(emotion, EMOTION_PARAMS["neutral"])

        # Determine display name
        display = key.replace("_", " ").title()

        # Engine priority: Kokoro always first on macOS (fast, local, no GPU needed)
        # Fish/Chatterbox promoted if CUDA available
        try:
            import torch
            cuda = torch.cuda.is_available()
        except ImportError:
            cuda = False
        priority = (["fish_audio_s2", "kokoro", "macos_say"]
                    if cuda else ["kokoro", "macos_say"])

        profiles[key] = VoiceProfile(
            persona_key=key,
            display_name=display,
            gender=gender,
            energy=energy,
            emotion_bias=emotion,
            accent=accent,
            kokoro_voice=voice_id,
            speed=speed,
            emotion_params=ep,
            engine_priority=priority,
        )
    return profiles

def provision_voice_samples(profiles: dict[str, VoiceProfile],
                            parallel: int = 4) -> None:
    """Generate reference WAV samples for all personas in parallel."""
    samples_dir = ASSETS / "samples"
    samples_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[Samples] Generating reference WAVs → {samples_dir}")

    def _gen(key_profile):
        key, vp = key_profile
        path = _generate_persona_sample(key, vp.kokoro_voice, vp.speed,
                                        vp.emotion_params, samples_dir)
        if path:
            vp.assets["reference_wav"] = str(path)
            return key, True
        return key, False

    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futs = {pool.submit(_gen, item): item[0] for item in profiles.items()}
        ok = err = 0
        for fut in as_completed(futs):
            key, success = fut.result()
            if success:
                print(f"  ✓ {key}")
                ok += 1
            else:
                print(f"  - {key}  (skipped — model not available yet)")
                err += 1
    print(f"  Generated {ok} samples, {err} skipped.")

def write_manifest(profiles: dict[str, VoiceProfile],
                   kokoro_paths: dict) -> None:
    manifest = {
        "version":        "2.0",
        "generated":      time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kokoro_model":   kokoro_paths.get("kokoro-v1.0.int8.onnx",
                          str(KOKORO_CACHE / "kokoro-v1.0.int8.onnx")),
        "kokoro_voices":  kokoro_paths.get("voices-v1.0.bin",
                          str(KOKORO_CACHE / "voices-v1.0.bin")),
        "emotion_params": EMOTION_PARAMS,
        "profiles":       {
            k: asdict(vp) for k, vp in profiles.items()
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f"\n[Manifest] Written → {MANIFEST}")

def verify_assets() -> None:
    """Check integrity of all registered assets in the manifest."""
    if not MANIFEST.exists():
        print("No manifest found. Run provisioner first.")
        return
    m = json.loads(MANIFEST.read_text())
    print(f"Manifest v{m['version']} generated {m['generated']}")
    print(f"\nKokoro model:  {m['kokoro_model']}")
    print(f"Kokoro voices: {m['kokoro_voices']}")
    ok = err = 0
    for key, vp in m["profiles"].items():
        ref = vp.get("assets", {}).get("reference_wav", "")
        if ref and Path(ref).exists():
            sz = Path(ref).stat().st_size
            print(f"  ✓ {key:35s}  voice={vp['kokoro_voice']:14s}  "
                  f"sample={sz//1024}KB")
            ok += 1
        else:
            print(f"  - {key:35s}  voice={vp['kokoro_voice']:14s}  "
                  f"no sample")
            err += 1
    print(f"\n{ok} with samples, {err} without.")

def print_mapping(profiles: dict[str, VoiceProfile]) -> None:
    print(f"\n{'Persona':<35} {'Voice':<16} {'Speed':>5}  Emotion")
    print("─" * 75)
    for key, vp in sorted(profiles.items()):
        print(f"  {key:<33} {vp.kokoro_voice:<16} {vp.speed:>4.2f}  {vp.emotion_bias}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(description="Sovereign Nexus Voice Provisioner")
    p.add_argument("--list",    action="store_true", help="Show mapping, no downloads")
    p.add_argument("--verify",  action="store_true", help="Verify existing assets")
    p.add_argument("--persona", help="Provision single persona only")
    p.add_argument("--no-samples", action="store_true", help="Skip WAV sample generation")
    p.add_argument("--parallel", type=int, default=4, help="Parallel sample threads")
    args = p.parse_args()

    if args.verify:
        verify_assets(); return

    print("╔══════════════════════════════════════════════════╗")
    print("║   Sovereign Nexus — Voice Provisioner v2.0      ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"  Personas loaded: {len(ALL_PERSONAS)} (+ default)")
    print(f"  Assets dir:      {ASSETS}")

    profiles = build_profiles()

    if args.persona:
        if args.persona not in profiles:
            print(f"Unknown persona: {args.persona}")
            print(f"Available: {', '.join(profiles)}")
            return
        profiles = {args.persona: profiles[args.persona]}

    print_mapping(profiles)

    if args.list:
        return

    # Download Kokoro model assets
    kokoro_paths = provision_kokoro_models()

    # Generate reference WAV samples (for voice cloning / quality checks)
    if not args.no_samples:
        provision_voice_samples(profiles, parallel=args.parallel)
    else:
        print("\n[Samples] Skipped (--no-samples)")

    # Write manifest
    write_manifest(profiles, kokoro_paths)

    print("\n✓ Provisioning complete.")
    print(f"  Manifest: {MANIFEST}")
    print("  Restart the TTS daemon to pick up new profiles.\n")

if __name__ == "__main__":
    main()
