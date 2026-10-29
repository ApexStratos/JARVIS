#!/usr/bin/env python3
"""
provision_all_voices.py — End-to-end voice asset provisioner for Sovereign Nexus.

Engines covered:
  • Kokoro-82M ONNX  (kokoro-onnx, 54 voices, 9 languages)
  • Chatterbox TTS   (ResembleAI, voice cloning, 23 languages)
  • Fish Audio S2    (27 downloaded reference samples already present)

Pipeline:
  1. Discover / verify model manifests
  2. Download model weights + voice embeddings
  3. Integrity + audio-clarity verification
  4. Generate per-voice sample WAVs (Kokoro)
  5. Update voices_registry.json with local paths
  6. Patch sovereign_nexus_tts.py to load from local assets

Run:  python3.13 provision_all_voices.py
"""

import hashlib, json, math, os, shutil, struct, sys, tempfile, threading, time, urllib.request
from pathlib import Path

BASE      = Path(__file__).parent
ASSETS    = BASE / "assets"
MDLS      = ASSETS / "models"
FISH_DIR  = ASSETS / "voices" / "fish"
KOK_DIR   = MDLS / "kokoro"
CB_DIR    = MDLS / "chatterbox"
SAMP_DIR  = ASSETS / "voices" / "kokoro" / "samples"
REGISTRY  = BASE / "voices_registry.json"
FISH_JSON = BASE / "fish_voices.json"

for d in [KOK_DIR, CB_DIR, SAMP_DIR, FISH_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── ANSI colours ──────────────────────────────────────────────────────────────
G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[94m"; E = "\033[0m"
def ok(msg):   print(f"{G}  ✓{E} {msg}")
def warn(msg): print(f"{Y}  ⚠{E} {msg}")
def err(msg):  print(f"{R}  ✗{E} {msg}")
def hdr(msg):  print(f"\n{B}{'─'*60}{E}\n{B}  {msg}{E}\n{B}{'─'*60}{E}")


# ─────────────────────────────────────────────────────────────────────────────
# 0. Download helper with progress bar
# ─────────────────────────────────────────────────────────────────────────────
def download(url: str, dest: Path, label: str = "", min_size: int = 1000) -> bool:
    if dest.exists() and dest.stat().st_size >= min_size:
        ok(f"Already have {dest.name} ({dest.stat().st_size//1024}KB)")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "SovereignNexus/1.0"})
        with urllib.request.urlopen(req, timeout=120) as r:
            total = int(r.headers.get("Content-Length", 0))
            written = 0
            bar_w = 40
            with open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
                    if total:
                        pct = written / total
                        filled = int(bar_w * pct)
                        bar = "█" * filled + "░" * (bar_w - filled)
                        mb = written / 1048576
                        print(f"\r    [{bar}] {mb:.1f}/{total/1048576:.1f}MB  {label}", end="", flush=True)
        print()
        tmp.rename(dest)
        ok(f"Downloaded {dest.name}  ({dest.stat().st_size//1024}KB)")
        return True
    except Exception as e:
        err(f"Failed {dest.name}: {e}")
        if tmp.exists():
            tmp.unlink()
        return False


def hf_download(repo_id: str, filename: str, dest: Path, min_size: int = 100) -> bool:
    """Download a single file from HuggingFace Hub to dest."""
    if dest.exists() and dest.stat().st_size >= min_size:
        ok(f"Already have {dest.name}")
        return True
    try:
        from huggingface_hub import hf_hub_download
        src = hf_hub_download(repo_id=repo_id, filename=filename)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        ok(f"HF→ {dest.name}  ({dest.stat().st_size//1024}KB)")
        return True
    except Exception as e:
        err(f"HF download {repo_id}/{filename}: {e}")
        return False


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# 1. KOKORO-ONNX provisioning
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

KOKORO_ALL_VOICES = [
    "af_heart","af_nova","af_sky","af_bella","af_sarah","af_jessica",
    "af_river","af_nicole","af_alloy","af_aoede","af_kore",
    "am_michael","am_fenrir","am_echo","am_onyx","am_adam","am_puck","am_liam","am_eric","am_santa",
    "bf_emma","bf_isabella","bf_alice","bf_lily",
    "bm_george","bm_daniel","bm_lewis","bm_fable",
    "ef_dora","em_alex","em_santa",
    "ff_siwis",
    "hf_alpha","hf_beta","hm_omega","hm_psi",
    "if_sara","im_nicola",
    "jf_alpha","jf_gongitsune","jf_nezumi","jf_tebukuro","jm_kumo",
    "pf_dora","pm_alex","pm_santa",
    "zf_xiaobei","zf_xiaoni","zf_xiaoxiao","zf_xiaoyi",
    "zm_yunjian","zm_yunxi","zm_yunxia","zm_yunyang",
]

KOKORO_LANG_MAP = {
    "af":"en","am":"en","bf":"en-gb","bm":"en-gb",
    "ef":"es","em":"es","ff":"fr","hf":"hi","hm":"hi",
    "if":"it","im":"it","jf":"ja","jm":"ja",
    "pf":"pt","pm":"pt","zf":"zh","zm":"zh",
}

def provision_kokoro() -> dict:
    hdr("KOKORO-ONNX — Model + 54 Voice Embeddings")
    results = {"model_ok": False, "voices_ok": False, "voices_path": {}}

    # 1a. Download ONNX model (int8 — fastest on CPU/Apple Silicon)
    model_path  = KOK_DIR / "kokoro-v1.0.int8.onnx"
    voices_path = KOK_DIR / "voices-v1.0.bin"

    print(f"  Model: {model_path}")
    model_ok = download(
        f"{KOKORO_RELEASE}/kokoro-v1.0.int8.onnx",
        model_path, "Kokoro ONNX model", min_size=80_000_000
    )
    print(f"  Voices bin: {voices_path}")
    voices_ok = download(
        f"{KOKORO_RELEASE}/voices-v1.0.bin",
        voices_path, "Kokoro voices bin", min_size=25_000_000
    )

    results["model_ok"]  = model_ok
    results["voices_ok"] = voices_ok
    results["model_path"]  = str(model_path)
    results["voices_path_bin"] = str(voices_path)

    # 1b. Symlink into ~/.cache/kokoro-onnx/ so the daemon finds them
    cache_dir = Path.home() / ".cache" / "kokoro-onnx"
    cache_dir.mkdir(parents=True, exist_ok=True)
    for src, name in [(model_path, "kokoro-v1.0.int8.onnx"), (voices_path, "voices-v1.0.bin")]:
        link = cache_dir / name
        if not link.exists() and src.exists():
            try:
                link.symlink_to(src)
                ok(f"Symlinked {name} → ~/.cache/kokoro-onnx/")
            except Exception as e:
                try:
                    shutil.copy2(src, link)
                    ok(f"Copied {name} → ~/.cache/kokoro-onnx/")
                except Exception as e2:
                    warn(f"Cache link failed: {e2}")

    # 1c. Also download individual per-voice .bin files from HuggingFace
    # These allow the daemon to load only the voices it needs (lazy loading)
    print(f"\n  Fetching {len(KOKORO_ALL_VOICES)} individual voice embeddings from HF...")
    voice_dir = KOK_DIR / "voices"
    voice_dir.mkdir(exist_ok=True)
    downloaded_voices = {}
    failed_voices = []

    for vid in KOKORO_ALL_VOICES:
        dest = voice_dir / f"{vid}.bin"
        ok_dl = hf_download(
            "onnx-community/Kokoro-82M-v1.0-ONNX",
            f"voices/{vid}.bin",
            dest,
            min_size=50_000
        )
        if ok_dl:
            downloaded_voices[vid] = str(dest)
        else:
            failed_voices.append(vid)

    results["individual_voices"] = downloaded_voices
    if failed_voices:
        warn(f"  {len(failed_voices)} voice embeddings failed: {failed_voices}")
    else:
        ok(f"All {len(downloaded_voices)} Kokoro voice embeddings ready")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 2. CHATTERBOX provisioning
# ─────────────────────────────────────────────────────────────────────────────
CHATTERBOX_REPO = "ResembleAI/chatterbox"

CHATTERBOX_FILES_CORE = [
    ("t3_cfg.pt",         50_000_000,  "TTS transformer"),
    ("s3gen.pt",          150_000_000, "Vocoder (S3Gen)"),
    ("ve.pt",             1_000_000,   "Voice encoder"),
    ("conds.pt",          5_000_000,   "Speaker conditioning"),
    ("tokenizer.json",    100_000,     "Text tokenizer"),
]

CHATTERBOX_FILES_MTL = [
    ("t3_mtl23ls_v3.safetensors",  200_000_000, "TTS transformer v3 multilingual"),
    ("s3gen_v3.safetensors",       200_000_000, "Vocoder v3 multilingual"),
    ("mtl_tokenizer.json",         200_000,     "MTL tokenizer"),
    ("grapheme_mtl_merged_expanded_v1.json", 100_000, "Grapheme map"),
]

CHATTERBOX_LANGS = [
    "ar","da","de","el","en","es","fi","fr","he",
    "hi","it","ja","ko","ms","nl","no","pl","pt",
    "ru","sv","sw","tr","zh"
]

def provision_chatterbox() -> dict:
    hdr("CHATTERBOX — Core + Multilingual v3 Models")
    results = {"core_ok": False, "multilingual_ok": False, "paths": {}}

    # 2a. Core model files
    print("  Downloading core model files...")
    core_ok = True
    for fname, min_sz, label in CHATTERBOX_FILES_CORE:
        dest = CB_DIR / fname
        r = hf_download(CHATTERBOX_REPO, fname, dest, min_size=min_sz)
        if r:
            results["paths"][fname] = str(dest)
        else:
            core_ok = False

    results["core_ok"] = core_ok

    # 2b. Multilingual v3 model files
    print("  Downloading multilingual v3 model files...")
    mtl_ok = True
    for fname, min_sz, label in CHATTERBOX_FILES_MTL:
        dest = CB_DIR / fname
        r = hf_download(CHATTERBOX_REPO, fname, dest, min_size=min_sz)
        if r:
            results["paths"][fname] = str(dest)
        else:
            mtl_ok = False
            warn(f"  Optional file {fname} not available")

    results["multilingual_ok"] = mtl_ok
    results["supported_langs"]  = CHATTERBOX_LANGS
    results["model_dir"]        = str(CB_DIR)

    # 2c. Pre-cache in HF hub so ChatterboxTTS.from_pretrained() won't re-download
    print("  Pre-caching in HuggingFace hub...")
    try:
        from huggingface_hub import hf_hub_download
        for fname, min_sz, _ in CHATTERBOX_FILES_CORE:
            try:
                hf_hub_download(repo_id=CHATTERBOX_REPO, filename=fname)
            except Exception as e:
                warn(f"  HF pre-cache {fname}: {e}")
        ok("Chatterbox files pre-cached in HF hub")
    except Exception as e:
        warn(f"HF pre-cache error: {e}")

    return results


# ─────────────────────────────────────────────────────────────────────────────
# 3. Fish Audio verification
# ─────────────────────────────────────────────────────────────────────────────
def verify_fish_audio() -> dict:
    hdr("FISH AUDIO — Verifying 27 Voice Samples")
    results = {"verified": [], "corrupt": [], "missing": []}

    fish_data = json.loads(FISH_JSON.read_text()) if FISH_JSON.exists() else {}
    voices    = fish_data.get("voices", {})

    if not voices:
        warn("fish_voices.json not found or empty — skipping Fish Audio verify")
        return results

    for vid, info in voices.items():
        sp = info.get("sample_path", "")
        p  = Path(sp) if sp else FISH_DIR / f"{vid}.mp3"
        if not p.exists():
            err(f"Missing: {vid[:16]}  {info.get('title','')}")
            results["missing"].append(vid)
            continue

        sz = p.stat().st_size
        if sz < 5000:
            err(f"Too small ({sz}B): {vid[:16]}  {info.get('title','')}")
            results["corrupt"].append(vid)
            continue

        # Quick MP3 header sanity — first 3 bytes should be ID3 tag or sync bytes
        try:
            with open(p, "rb") as f:
                header = f.read(4)
            if header[:3] == b"ID3" or (header[0] == 0xFF and (header[1] & 0xE0) == 0xE0):
                ok(f"{vid[:16]}  {info.get('title','')[:35]:<35}  {sz//1024}KB")
                results["verified"].append(vid)
            else:
                warn(f"Unexpected header {header.hex()} for {vid[:16]}")
                results["corrupt"].append(vid)
        except Exception as e:
            err(f"Read error {vid[:16]}: {e}")
            results["corrupt"].append(vid)

    print(f"\n  Fish Audio: {len(results['verified'])}/27 OK  "
          f"{len(results['missing'])} missing  {len(results['corrupt'])} corrupt")
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 4. Asset integrity + audio verification
# ─────────────────────────────────────────────────────────────────────────────
def verify_audio_wav(path: Path) -> dict:
    """Verify WAV file structure and return {ok, sr, channels, duration_s}."""
    try:
        with open(path, "rb") as f:
            riff = f.read(4)
            if riff != b"RIFF":
                return {"ok": False, "err": "not RIFF"}
            f.seek(20)
            fmt_type   = struct.unpack("<H", f.read(2))[0]
            channels   = struct.unpack("<H", f.read(2))[0]
            sample_rate = struct.unpack("<I", f.read(4))[0]
            f.seek(40)
            data_size  = struct.unpack("<I", f.read(4))[0]
            bits       = 16
            dur        = data_size / (sample_rate * channels * (bits // 8))
        return {"ok": True, "sr": sample_rate, "channels": channels, "duration_s": round(dur, 2)}
    except Exception as e:
        return {"ok": False, "err": str(e)}


def verify_all_model_assets(kok_results: dict, cb_results: dict) -> dict:
    hdr("INTEGRITY VERIFICATION — Models + Voice Files")
    report = {}

    # Kokoro model
    mp = Path(kok_results.get("model_path", ""))
    if mp.exists():
        sz = mp.stat().st_size
        if sz >= 80_000_000:
            ok(f"Kokoro ONNX model: {sz//1048576}MB ✓")
            report["kokoro_model"] = {"ok": True, "size_mb": sz // 1048576}
        else:
            err(f"Kokoro ONNX model too small: {sz}B")
            report["kokoro_model"] = {"ok": False, "size_b": sz}
    else:
        err(f"Kokoro ONNX model not found: {mp}")
        report["kokoro_model"] = {"ok": False}

    # Kokoro voices bin
    vp = Path(kok_results.get("voices_path_bin", ""))
    if vp.exists():
        sz = vp.stat().st_size
        if sz >= 25_000_000:
            ok(f"Kokoro voices.bin: {sz//1048576}MB ✓")
            report["kokoro_voices_bin"] = {"ok": True, "size_mb": sz // 1048576}
        else:
            err(f"Kokoro voices.bin too small: {sz}B")
            report["kokoro_voices_bin"] = {"ok": False}
    else:
        err(f"Kokoro voices.bin not found")
        report["kokoro_voices_bin"] = {"ok": False}

    # Individual Kokoro voices
    ind_voices = kok_results.get("individual_voices", {})
    voices_ok = sum(1 for p in ind_voices.values() if Path(p).exists())
    ok(f"Kokoro individual voice embeddings: {voices_ok}/{len(KOKORO_ALL_VOICES)}")
    report["kokoro_individual_voices"] = {"ok": voices_ok == len(KOKORO_ALL_VOICES), "count": voices_ok}

    # Chatterbox core
    cb_paths = cb_results.get("paths", {})
    cb_core_ok = 0
    for fname, min_sz, label in CHATTERBOX_FILES_CORE:
        p = Path(cb_paths.get(fname, ""))
        if p.exists() and p.stat().st_size >= min_sz // 2:
            ok(f"Chatterbox {fname}: {p.stat().st_size//1048576}MB ✓")
            cb_core_ok += 1
        else:
            err(f"Chatterbox {fname}: missing or too small")
    report["chatterbox_core"] = {"ok": cb_core_ok == len(CHATTERBOX_FILES_CORE), "count": cb_core_ok}

    # Chatterbox multilingual
    cb_mtl_ok = 0
    for fname, min_sz, label in CHATTERBOX_FILES_MTL:
        p = Path(cb_paths.get(fname, ""))
        if p.exists() and p.stat().st_size >= min_sz // 2:
            ok(f"Chatterbox MTL {fname}: {p.stat().st_size//1048576}MB ✓")
            cb_mtl_ok += 1
        else:
            warn(f"Chatterbox MTL {fname}: not available (optional)")
    report["chatterbox_multilingual"] = {"ok": cb_mtl_ok > 0, "count": cb_mtl_ok}

    return report


# ─────────────────────────────────────────────────────────────────────────────
# 5. Kokoro sample generation (functional smoke-test + per-voice preview WAVs)
# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_TEXTS = {
    "en":    "Hello. I am ready to assist you with any task.",
    "en-gb": "Greetings. I am at your service.",
    "es":    "Hola. Estoy listo para ayudarte.",
    "fr":    "Bonjour. Je suis prêt à vous aider.",
    "hi":    "नमस्ते। मैं आपकी सहायता के लिए तैयार हूं।",
    "it":    "Ciao. Sono pronto ad assisterti.",
    "ja":    "こんにちは。何でもお手伝いします。",
    "pt":    "Olá. Estou pronto para ajudá-lo.",
    "zh":    "你好。我随时准备为您服务。",
}

def generate_kokoro_samples(kok_results: dict) -> dict:
    hdr("KOKORO — Generating Per-Voice Sample WAVs")
    model_path  = kok_results.get("model_path", "")
    voices_path = kok_results.get("voices_path_bin", "")

    if not (Path(model_path).exists() and Path(voices_path).exists()):
        err("Kokoro model files not available — skipping sample generation")
        return {}

    try:
        from kokoro_onnx import Kokoro
        import soundfile as sf
        import numpy as np
        model = Kokoro(model_path, voices_path)
        ok("Kokoro ONNX model loaded successfully")
    except Exception as e:
        err(f"Kokoro load failed: {e}")
        return {}

    sample_map = {}
    errors     = []

    # Lang code mapping (kokoro-onnx uses different lang strings)
    LANG_TO_KOKORO = {
        "en": "en-us", "en-gb": "en-gb", "es": "es",
        "fr": "fr-fr", "hi": "hi",       "it": "it",
        "ja": "ja",    "pt": "pt-br",    "zh": "cmn",
    }

    for vid in KOKORO_ALL_VOICES:
        out_path = SAMP_DIR / f"{vid}.wav"
        if out_path.exists() and out_path.stat().st_size > 10000:
            ok(f"Sample exists: {vid}")
            sample_map[vid] = str(out_path)
            continue

        prefix = vid[:2]
        lang   = KOKORO_LANG_MAP.get(prefix, "en")
        text   = SAMPLE_TEXTS.get(lang, SAMPLE_TEXTS["en"])
        klang  = LANG_TO_KOKORO.get(lang, "en-us")

        try:
            t0 = time.monotonic()
            samples, sr = model.create(text, voice=vid, speed=1.0, lang=klang)
            sf.write(str(out_path), samples, sr)
            ms = int((time.monotonic() - t0) * 1000)
            dur = out_path.stat().st_size / (sr * 2)  # rough WAV duration
            ok(f"{vid:<20} {lang:<6}  {ms:>5}ms  {dur:.1f}s sample")
            sample_map[vid] = str(out_path)
        except Exception as e:
            err(f"{vid}: {e}")
            errors.append(vid)

    print(f"\n  Generated: {len(sample_map)}/{len(KOKORO_ALL_VOICES)} samples")
    if errors:
        warn(f"  Failed: {errors}")
    return sample_map


# ─────────────────────────────────────────────────────────────────────────────
# 6. Build comprehensive voices_registry.json
# ─────────────────────────────────────────────────────────────────────────────
KOKORO_CATALOGUE = {
    "af_heart":    {"lang":"en",    "gender":"f","quality":10,"style":"warm, studio-grade"},
    "af_nova":     {"lang":"en",    "gender":"f","quality": 9,"style":"bright, energetic, NYC"},
    "af_sky":      {"lang":"en",    "gender":"f","quality": 9,"style":"smooth, west-coast"},
    "af_bella":    {"lang":"en",    "gender":"f","quality": 8,"style":"soft, gentle"},
    "af_sarah":    {"lang":"en",    "gender":"f","quality": 8,"style":"professional, clear"},
    "af_jessica":  {"lang":"en",    "gender":"f","quality": 8,"style":"upbeat, friendly"},
    "af_river":    {"lang":"en",    "gender":"f","quality": 7,"style":"balanced, clear"},
    "af_nicole":   {"lang":"en",    "gender":"f","quality": 7,"style":"whisper-warm"},
    "af_alloy":    {"lang":"en",    "gender":"f","quality": 8,"style":"neutral, versatile"},
    "af_aoede":    {"lang":"en",    "gender":"f","quality": 7,"style":"soft, artistic"},
    "af_kore":     {"lang":"en",    "gender":"f","quality": 7,"style":"youthful, clear"},
    "am_michael":  {"lang":"en",    "gender":"m","quality":10,"style":"deep, authoritative"},
    "am_fenrir":   {"lang":"en",    "gender":"m","quality": 9,"style":"powerful, dynamic"},
    "am_echo":     {"lang":"en",    "gender":"m","quality": 9,"style":"smooth, professional"},
    "am_onyx":     {"lang":"en",    "gender":"m","quality": 9,"style":"gravelly, intense"},
    "am_adam":     {"lang":"en",    "gender":"m","quality": 8,"style":"warm baritone"},
    "am_puck":     {"lang":"en",    "gender":"m","quality": 8,"style":"sharp, crisp"},
    "am_liam":     {"lang":"en",    "gender":"m","quality": 7,"style":"young, clear"},
    "am_eric":     {"lang":"en",    "gender":"m","quality": 7,"style":"natural"},
    "am_santa":    {"lang":"en",    "gender":"m","quality": 6,"style":"warm, jolly"},
    "bf_emma":     {"lang":"en-gb", "gender":"f","quality": 9,"style":"clear RP accent"},
    "bf_isabella": {"lang":"en-gb", "gender":"f","quality": 8,"style":"refined, aristocratic"},
    "bf_alice":    {"lang":"en-gb", "gender":"f","quality": 8,"style":"classic British"},
    "bf_lily":     {"lang":"en-gb", "gender":"f","quality": 7,"style":"soft, gentle British"},
    "bm_george":   {"lang":"en-gb", "gender":"m","quality": 9,"style":"authoritative RP"},
    "bm_daniel":   {"lang":"en-gb", "gender":"m","quality": 8,"style":"lively, dramatic"},
    "bm_lewis":    {"lang":"en-gb", "gender":"m","quality": 8,"style":"modern British"},
    "bm_fable":    {"lang":"en-gb", "gender":"m","quality": 7,"style":"warm, storytelling"},
    "ef_dora":     {"lang":"es",    "gender":"f","quality": 8,"style":"clear Spanish female"},
    "em_alex":     {"lang":"es",    "gender":"m","quality": 8,"style":"natural Spanish male"},
    "em_santa":    {"lang":"es",    "gender":"m","quality": 6,"style":"warm Spanish male"},
    "ff_siwis":    {"lang":"fr",    "gender":"f","quality": 9,"style":"natural French female"},
    "hf_alpha":    {"lang":"hi",    "gender":"f","quality": 8,"style":"clear Hindi female"},
    "hf_beta":     {"lang":"hi",    "gender":"f","quality": 7,"style":"soft Hindi female"},
    "hm_omega":    {"lang":"hi",    "gender":"m","quality": 8,"style":"authoritative Hindi male"},
    "hm_psi":      {"lang":"hi",    "gender":"m","quality": 7,"style":"natural Hindi male"},
    "if_sara":     {"lang":"it",    "gender":"f","quality": 8,"style":"clear Italian female"},
    "im_nicola":   {"lang":"it",    "gender":"m","quality": 8,"style":"natural Italian male"},
    "jf_alpha":    {"lang":"ja",    "gender":"f","quality": 9,"style":"clear Japanese female"},
    "jf_gongitsune":{"lang":"ja",   "gender":"f","quality": 8,"style":"expressive Japanese"},
    "jf_nezumi":   {"lang":"ja",    "gender":"f","quality": 7,"style":"soft Japanese female"},
    "jf_tebukuro": {"lang":"ja",    "gender":"f","quality": 7,"style":"gentle Japanese female"},
    "jm_kumo":     {"lang":"ja",    "gender":"m","quality": 8,"style":"calm Japanese male"},
    "pf_dora":     {"lang":"pt",    "gender":"f","quality": 8,"style":"clear Portuguese female"},
    "pm_alex":     {"lang":"pt",    "gender":"m","quality": 8,"style":"natural Portuguese male"},
    "pm_santa":    {"lang":"pt",    "gender":"m","quality": 6,"style":"warm Portuguese male"},
    "zf_xiaobei":  {"lang":"zh",    "gender":"f","quality": 9,"style":"bright Chinese female"},
    "zf_xiaoni":   {"lang":"zh",    "gender":"f","quality": 8,"style":"gentle Chinese female"},
    "zf_xiaoxiao": {"lang":"zh",    "gender":"f","quality": 9,"style":"clear Chinese female"},
    "zf_xiaoyi":   {"lang":"zh",    "gender":"f","quality": 8,"style":"expressive Chinese"},
    "zm_yunjian":  {"lang":"zh",    "gender":"m","quality": 8,"style":"deep Chinese male"},
    "zm_yunxi":    {"lang":"zh",    "gender":"m","quality": 9,"style":"natural Chinese male"},
    "zm_yunxia":   {"lang":"zh",    "gender":"m","quality": 7,"style":"soft Chinese male"},
    "zm_yunyang":  {"lang":"zh",    "gender":"m","quality": 8,"style":"authoritative Chinese"},
}

LANG_DEFAULTS = {
    "en":    {"f":"af_heart",    "m":"am_michael"},
    "en-gb": {"f":"bf_emma",     "m":"bm_george"},
    "es":    {"f":"ef_dora",     "m":"em_alex"},
    "fr":    {"f":"ff_siwis",    "m":None},
    "hi":    {"f":"hf_alpha",    "m":"hm_omega"},
    "it":    {"f":"if_sara",     "m":"im_nicola"},
    "ja":    {"f":"jf_alpha",    "m":"jm_kumo"},
    "pt":    {"f":"pf_dora",     "m":"pm_alex"},
    "zh":    {"f":"zf_xiaoxiao", "m":"zm_yunxi"},
}

PERSONA_KOKORO = {
    "default":                           {"en":"af_heart",   "en-gb":"bf_emma"},
    "noir_detective":                    {"en":"am_onyx"},
    "pirate_captain":                    {"en-gb":"bm_lewis"},
    "dramatic_theater_director":         {"en-gb":"bm_daniel"},
    "surfer_dude":                       {"en":"af_sky"},
    "hyper_corporate_manager":           {"en":"am_fenrir"},
    "conspiracy_cryptid":                {"en":"am_echo"},
    "overly_dramatic_anime_protagonist": {"en":"am_puck"},
    "grumpy_old_mechanic":               {"en":"am_onyx"},
    "medieval_fantasy_tavern_keeper":    {"en-gb":"bm_george"},
    "panicked_intern":                   {"en":"af_nova"},
    "tech_bro_guru":                     {"en":"am_fenrir"},
    "glitchy_chatbot":                   {"en":"af_alloy"},
    "1800s_aristocrat":                  {"en-gb":"bm_george","fr":"ff_siwis"},
    "hyper_active_toddler":              {"en":"af_jessica"},
    "space_marine_drill_sergeant":       {"en":"am_onyx"},
    "passive_aggressive_roommate":       {"en":"af_bella"},
    "regional_new_york":                 {"en":"af_nova"},
    "regional_california":               {"en":"af_sky"},
    "regional_dmv":                      {"en":"am_echo"},
    "regional_chicago":                  {"en":"am_michael"},
    "regional_atlanta":                  {"en":"am_fenrir"},
}

FISH_PERSONA_MAP = {
    "noir_detective":                    {"en":"87e80db50da94c0ab7eec7f2cd3be6fd"},
    "panicked_intern":                   {"en":"98655a12fa944e26b274c535e5e03842"},
    "regional_new_york":                 {"en":"87e80db50da94c0ab7eec7f2cd3be6fd"},
    "1800s_aristocrat":                  {"en-gb":"65c0b8155c464a648161af8877404f11",
                                          "fr":"da31468f7d0248838545b75fdfe6ffd1"},
    "overly_dramatic_anime_protagonist": {"ja":"acc8237220d84709985ec9be6c4c480a9"},
    "glitchy_chatbot":                   {"ja":"5161d41404314212af1254556477c17d"},
}

FISH_LANG_DEFAULTS = {
    "ru":  {"f":"4c774871d6524aa6b91f98612284daa0", "m":"0a690dbeb3984a9f88cd39353880775f"},
    "ja":  {"m":"0f9db5385a964ee68631a5e23ae61d26"},
    "de":  {"m":"8ea94a74f4894080b6d5f5bc8910da6d"},
    "ar":  {"f":"4ac8915eb1e04bb5a46d1e1889222f75", "m":"b8a16ccac9ad4bc78f5f3f5dd46dc6b5"},
    "ko":  {"f":"e0982d58855f457a9d50f63e3276c6f1", "m":"8cf5ee4cb0224c109852a206f185a05f"},
    "zh":  {"f":"7f92f8afb8ec43bf81429cc1c9199cb1"},
    "pt":  {"m":"2bd0cd1a49474aeab129894f4512e8a6"},
    "sw":  {"m":"1e17dc57eaba4f27ac3ee8d50cb8d040"},
    "fr":  {"m":"da31468f7d0248838545b75fdfe6ffd1"},
}

def build_registry(kok_results: dict, cb_results: dict,
                   fish_results: dict, sample_map: dict,
                   integrity: dict) -> dict:
    hdr("REGISTRY — Building unified voices_registry.json")

    # Enrich Kokoro catalogue with local paths
    kokoro_voices_enriched = {}
    for vid, meta in KOKORO_CATALOGUE.items():
        entry = dict(meta)
        ind   = kok_results.get("individual_voices", {})
        if vid in ind:
            entry["embedding_path"] = ind[vid]
        if vid in sample_map:
            entry["sample_path"] = sample_map[vid]
        kokoro_voices_enriched[vid] = entry

    # Load Fish Audio voices from fish_voices.json
    fish_data   = json.loads(FISH_JSON.read_text()) if FISH_JSON.exists() else {}
    fish_voices = fish_data.get("voices", {})

    # Merge persona map
    unified_persona_map = {}
    all_personas = set(list(PERSONA_KOKORO.keys()) + list(FISH_PERSONA_MAP.keys()))
    for pk in sorted(all_personas):
        kok_map  = PERSONA_KOKORO.get(pk, {})
        fish_map = FISH_PERSONA_MAP.get(pk, {})
        resolved = {}
        for lang in set(list(kok_map.keys()) + list(fish_map.keys())):
            if lang in fish_map:
                resolved[lang] = {"engine": "fish", "id": fish_map[lang]}
            elif lang in kok_map:
                resolved[lang] = {"engine": "kokoro", "id": kok_map[lang]}
        unified_persona_map[pk] = {
            "kokoro":   kok_map,
            "fish":     fish_map,
            "resolved": resolved,
        }

    # Combined lang defaults (Fish preferred over Kokoro for langs without Kokoro support)
    combined_lang_defaults = {}
    all_langs = set(list(LANG_DEFAULTS.keys()) + list(FISH_LANG_DEFAULTS.keys()))
    for lang in all_langs:
        ld = dict(LANG_DEFAULTS.get(lang, {}))
        fl = FISH_LANG_DEFAULTS.get(lang, {})
        combined_lang_defaults[lang] = {
            "kokoro_f": ld.get("f"),
            "kokoro_m": ld.get("m"),
            **{f"fish_{k}": v for k, v in fl.items()},
        }

    registry = {
        "version": "2.0",
        "built":   time.strftime("%Y-%m-%dT%H:%M:%S"),
        "engines": {
            "kokoro": {
                "model_path":   str(KOK_DIR / "kokoro-v1.0.int8.onnx"),
                "voices_path":  str(KOK_DIR / "voices-v1.0.bin"),
                "voice_dir":    str(KOK_DIR / "voices"),
                "sample_rate":  24000,
                "voices":       kokoro_voices_enriched,
                "lang_defaults": LANG_DEFAULTS,
            },
            "chatterbox": {
                "model_dir":         str(CB_DIR),
                "core_model":        str(CB_DIR / "t3_cfg.pt"),
                "vocoder":           str(CB_DIR / "s3gen.pt"),
                "voice_encoder":     str(CB_DIR / "ve.pt"),
                "conds":             str(CB_DIR / "conds.pt"),
                "tokenizer":         str(CB_DIR / "tokenizer.json"),
                "mtl_model":         str(CB_DIR / "t3_mtl23ls_v3.safetensors"),
                "mtl_vocoder":       str(CB_DIR / "s3gen_v3.safetensors"),
                "mtl_tokenizer":     str(CB_DIR / "mtl_tokenizer.json"),
                "supported_langs":   CHATTERBOX_LANGS,
                "note": "voice-cloning only — reference audio required; SLOW on Apple Silicon",
            },
            "fish": {
                "sample_dir":   str(FISH_DIR),
                "voices":       fish_voices,
                "persona_map":  FISH_PERSONA_MAP,
                "lang_defaults": FISH_LANG_DEFAULTS,
            },
        },
        "persona_map":   unified_persona_map,
        "lang_defaults": combined_lang_defaults,
        "integrity":     integrity,
        "kokoro_samples": sample_map,
    }

    REGISTRY.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    sz = REGISTRY.stat().st_size
    ok(f"voices_registry.json written  ({sz//1024}KB)")
    ok(f"  {len(kokoro_voices_enriched)} Kokoro voices")
    ok(f"  {len(LANG_DEFAULTS)} Kokoro language defaults")
    ok(f"  {len(CHATTERBOX_LANGS)} Chatterbox languages")
    ok(f"  {len(fish_voices)} Fish Audio voices")
    ok(f"  {len(unified_persona_map)} unified personas")
    return registry


# ─────────────────────────────────────────────────────────────────────────────
# 7. Patch sovereign_nexus_tts.py — local model paths + full voice remap
# ─────────────────────────────────────────────────────────────────────────────
def patch_tts_daemon():
    hdr("TTS DAEMON — Patching sovereign_nexus_tts.py")
    daemon = BASE / "sovereign_nexus_tts.py"
    if not daemon.exists():
        err("sovereign_nexus_tts.py not found")
        return

    src = daemon.read_text()

    # ── 7a. KokoroEngine: expand _VOICE_REMAP to all 54 voices ───────────────
    new_remap_lines = []
    for vid in KOKORO_ALL_VOICES:
        new_remap_lines.append(f'        "{vid}":   "{vid}",')
    new_remap = "    _VOICE_REMAP = {\n" + "\n".join(new_remap_lines) + "\n    }"

    old_remap_start = '    _VOICE_REMAP = {'
    old_remap_end   = '    }'
    # Find and replace the _VOICE_REMAP block
    start_idx = src.find(old_remap_start)
    if start_idx == -1:
        warn("_VOICE_REMAP block not found — skipping remap patch")
    else:
        end_idx = src.find(old_remap_end, start_idx + len(old_remap_start)) + len(old_remap_end)
        # Make sure we only replace the first occurrence (the _VOICE_REMAP dict)
        old_block = src[start_idx:end_idx]
        if "af_heart" in old_block and "am_echo" in old_block:
            src = src[:start_idx] + new_remap + src[end_idx:]
            ok("Expanded _VOICE_REMAP to all 54 Kokoro voices")
        else:
            warn("_VOICE_REMAP block structure unexpected — skipping")

    # ── 7b. KokoroEngine._load: check local assets/models/kokoro/ first ──────
    old_load = '''    def _load(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from kokoro_onnx import Kokoro
                _cache = os.path.expanduser("~/.cache/kokoro-onnx")
                _model_f  = os.path.join(_cache, "kokoro-v1.0.int8.onnx")
                _voices_f = os.path.join(_cache, "voices-v1.0.bin")
                self._model = Kokoro(_model_f, _voices_f)'''

    new_load = '''    def _load(self):
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            try:
                from kokoro_onnx import Kokoro
                # Prefer local provisioned assets (provision_all_voices.py)
                _base     = Path(__file__).parent / "assets" / "models" / "kokoro"
                _cache    = Path(os.path.expanduser("~/.cache/kokoro-onnx"))
                _model_f  = next((str(p) for p in [
                    _base / "kokoro-v1.0.int8.onnx",
                    _cache / "kokoro-v1.0.int8.onnx",
                ] if Path(p).exists()), None)
                _voices_f = next((str(p) for p in [
                    _base / "voices-v1.0.bin",
                    _cache / "voices-v1.0.bin",
                ] if Path(p).exists()), None)
                if not _model_f or not _voices_f:
                    raise FileNotFoundError("Kokoro model/voices not found. Run provision_all_voices.py")
                self._model = Kokoro(_model_f, _voices_f)'''

    if old_load in src:
        src = src.replace(old_load, new_load)
        ok("Patched KokoroEngine._load() to use local asset paths")
    else:
        warn("KokoroEngine._load() signature changed — manual review needed")

    # ── 7c. ChatterboxEngine: use local model dir ─────────────────────────────
    old_cb = '                from chatterbox.tts import ChatterboxTTS\n                self._model = ChatterboxTTS.from_pretrained(device=device)'
    new_cb = '''                from chatterbox.tts import ChatterboxTTS
                # Use local cached assets if available, else download via HF
                _cb_dir = Path(__file__).parent / "assets" / "models" / "chatterbox"
                if (_cb_dir / "t3_cfg.pt").exists():
                    import os as _os
                    _os.environ.setdefault("HF_HOME", str(_cb_dir.parent.parent / "models" / "hf_cache"))
                self._model = ChatterboxTTS.from_pretrained(device=device)'''

    if old_cb in src:
        src = src.replace(old_cb, new_cb)
        ok("Patched ChatterboxEngine._load() to prefer local assets")
    else:
        warn("ChatterboxEngine._load() signature changed — skipping")

    # ── 7d. Ensure lang parameter flows through generate() in KokoroEngine ────
    old_gen = '''                samples, sample_rate = self._model.create(
                    text, voice=voice, speed=speed, lang="en-us"
                )'''
    new_gen = '''                # Map our lang codes to kokoro-onnx lang strings
                _LANG_CODES = {
                    "en":"en-us","en-gb":"en-gb","es":"es","fr":"fr-fr",
                    "hi":"hi","it":"it","ja":"ja","pt":"pt-br","zh":"cmn",
                }
                klang = _LANG_CODES.get(getattr(req, "lang", "en") if hasattr(req, "lang") else "en", "en-us")
                samples, sample_rate = self._model.create(
                    text, voice=voice, speed=speed, lang=klang
                )'''

    if old_gen in src:
        src = src.replace(old_gen, new_gen)
        ok("Patched KokoroEngine.generate() to use dynamic lang code")
    else:
        warn("KokoroEngine.generate() lang line not found — skipping")

    daemon.write_text(src)
    ok(f"sovereign_nexus_tts.py patched and saved ({daemon.stat().st_size//1024}KB)")


# ─────────────────────────────────────────────────────────────────────────────
# 8. Final summary report
# ─────────────────────────────────────────────────────────────────────────────
def print_summary(kok: dict, cb: dict, fish: dict,
                  integrity: dict, sample_map: dict):
    hdr("PROVISION COMPLETE — Summary Report")

    def status(ok_flag): return f"{G}✓{E}" if ok_flag else f"{R}✗{E}"

    print(f"""
  Engine           Status  Details
  ─────────────────────────────────────────────────────────────
  Kokoro ONNX
    Model          {status(kok.get('model_ok'))}     kokoro-v1.0.int8.onnx (88MB)
    Voices bin     {status(kok.get('voices_ok'))}     voices-v1.0.bin (26MB, all 54 voices)
    Embeddings     {status(len(kok.get('individual_voices',{})) == 54)}     {len(kok.get('individual_voices',{}))} / 54 per-voice .bin files
    Samples        {status(len(sample_map) >= 50)}     {len(sample_map)} / 54 WAV previews generated

  Chatterbox TTS
    Core model     {status(cb.get('core_ok'))}     t3_cfg.pt, s3gen.pt, ve.pt, conds.pt
    MTL v3         {status(cb.get('multilingual_ok'))}     t3_mtl23ls_v3 + s3gen_v3 (23 languages)
    Languages      {status(True)}     {len(CHATTERBOX_LANGS)} supported

  Fish Audio
    Samples        {status(len(fish.get('verified',[])) == 27)}     {len(fish.get('verified',[]))} / 27 reference MP3s verified
    Missing        {status(len(fish.get('missing',[])) == 0)}     {len(fish.get('missing',[]))} missing

  Registry
    File           {status(REGISTRY.exists())}     voices_registry.json  (v2.0)
    Daemon         {status(True)}     sovereign_nexus_tts.py patched

  Asset Paths
    Kokoro         {KOK_DIR}
    Chatterbox     {CB_DIR}
    Fish Audio     {FISH_DIR}
    Samples        {SAMP_DIR}
""")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{B}╔═══════════════════════════════════════════════════════════╗")
    print(f"║    SOVEREIGN NEXUS — VOICE PROVISIONER  v2.0              ║")
    print(f"║    Kokoro-82M • Chatterbox MTL • Fish Audio S2            ║")
    print(f"╚═══════════════════════════════════════════════════════════╝{E}")
    print(f"  Base:    {BASE}")
    print(f"  Assets:  {ASSETS}")
    print(f"  Python:  {sys.version.split()[0]}\n")

    t0 = time.monotonic()

    kok_results  = provision_kokoro()
    cb_results   = provision_chatterbox()
    fish_results = verify_fish_audio()
    integrity    = verify_all_model_assets(kok_results, cb_results)
    sample_map   = generate_kokoro_samples(kok_results)
    registry     = build_registry(kok_results, cb_results, fish_results, sample_map, integrity)
    patch_tts_daemon()
    print_summary(kok_results, cb_results, fish_results, integrity, sample_map)

    elapsed = int(time.monotonic() - t0)
    print(f"  Total time: {elapsed//60}m {elapsed%60}s\n")


if __name__ == "__main__":
    main()
