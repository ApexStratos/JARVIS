#!/usr/bin/env python3
"""
build_registry.py — builds voices_registry.json
Maps all Kokoro voices + Chatterbox languages + Fish Audio IDs into one
unified registry consumed by the TTS daemon.

Run:  python3.13 build_registry.py
"""
import json, time
from pathlib import Path

BASE = Path(__file__).parent

# ── Kokoro voice catalogue ─────────────────────────────────────────────────────
# prefix legend:
#  af/am = American English female/male
#  bf/bm = British English female/male
#  ef/em = Spanish female/male
#  ff    = French female
#  hf/hm = Hindi female/male
#  if/im = Italian female/male
#  jf/jm = Japanese female/male
#  pf/pm = Portuguese female/male
#  zf/zm = Chinese female/male

KOKORO_VOICES = {
    # ── American English female ──────────────────────────────────────────────
    "af_heart":   {"lang":"en","gender":"f","quality":10,"style":"warm, studio-grade"},
    "af_nova":    {"lang":"en","gender":"f","quality": 9,"style":"bright, energetic, NYC"},
    "af_sky":     {"lang":"en","gender":"f","quality": 9,"style":"smooth, west-coast"},
    "af_bella":   {"lang":"en","gender":"f","quality": 8,"style":"soft, gentle"},
    "af_sarah":   {"lang":"en","gender":"f","quality": 8,"style":"professional, clear"},
    "af_jessica": {"lang":"en","gender":"f","quality": 8,"style":"upbeat, friendly"},
    "af_river":   {"lang":"en","gender":"f","quality": 7,"style":"balanced, clear"},
    "af_nicole":  {"lang":"en","gender":"f","quality": 7,"style":"whisper-warm"},
    "af_alloy":   {"lang":"en","gender":"f","quality": 8,"style":"neutral, versatile"},
    "af_aoede":   {"lang":"en","gender":"f","quality": 7,"style":"soft, artistic"},
    "af_kore":    {"lang":"en","gender":"f","quality": 7,"style":"youthful, clear"},
    # ── American English male ────────────────────────────────────────────────
    "am_michael": {"lang":"en","gender":"m","quality":10,"style":"deep, authoritative"},
    "am_fenrir":  {"lang":"en","gender":"m","quality": 9,"style":"powerful, dynamic"},
    "am_echo":    {"lang":"en","gender":"m","quality": 9,"style":"smooth, professional"},
    "am_onyx":    {"lang":"en","gender":"m","quality": 9,"style":"gravelly, intense authority"},
    "am_adam":    {"lang":"en","gender":"m","quality": 8,"style":"warm baritone"},
    "am_puck":    {"lang":"en","gender":"m","quality": 8,"style":"sharp, crisp, energetic"},
    "am_liam":    {"lang":"en","gender":"m","quality": 7,"style":"young, clear"},
    "am_eric":    {"lang":"en","gender":"m","quality": 7,"style":"mid-range, natural"},
    "am_santa":   {"lang":"en","gender":"m","quality": 6,"style":"warm, jolly"},
    # ── British English female ───────────────────────────────────────────────
    "bf_emma":    {"lang":"en-gb","gender":"f","quality": 9,"style":"clear RP accent"},
    "bf_isabella":{"lang":"en-gb","gender":"f","quality": 8,"style":"refined, aristocratic"},
    "bf_alice":   {"lang":"en-gb","gender":"f","quality": 8,"style":"classic British"},
    "bf_lily":    {"lang":"en-gb","gender":"f","quality": 7,"style":"soft, gentle British"},
    # ── British English male ─────────────────────────────────────────────────
    "bm_george":  {"lang":"en-gb","gender":"m","quality": 9,"style":"authoritative RP"},
    "bm_daniel":  {"lang":"en-gb","gender":"m","quality": 8,"style":"lively, dramatic"},
    "bm_lewis":   {"lang":"en-gb","gender":"m","quality": 8,"style":"modern British"},
    "bm_fable":   {"lang":"en-gb","gender":"m","quality": 7,"style":"warm, storytelling"},
    # ── Spanish ─────────────────────────────────────────────────────────────
    "ef_dora":    {"lang":"es","gender":"f","quality": 8,"style":"clear Spanish female"},
    "em_alex":    {"lang":"es","gender":"m","quality": 8,"style":"natural Spanish male"},
    "em_santa":   {"lang":"es","gender":"m","quality": 6,"style":"warm Spanish male"},
    # ── French ──────────────────────────────────────────────────────────────
    "ff_siwis":   {"lang":"fr","gender":"f","quality": 9,"style":"natural French female"},
    # ── Hindi ───────────────────────────────────────────────────────────────
    "hf_alpha":   {"lang":"hi","gender":"f","quality": 8,"style":"clear Hindi female"},
    "hf_beta":    {"lang":"hi","gender":"f","quality": 7,"style":"soft Hindi female"},
    "hm_omega":   {"lang":"hi","gender":"m","quality": 8,"style":"authoritative Hindi male"},
    "hm_psi":     {"lang":"hi","gender":"m","quality": 7,"style":"natural Hindi male"},
    # ── Italian ─────────────────────────────────────────────────────────────
    "if_sara":    {"lang":"it","gender":"f","quality": 8,"style":"clear Italian female"},
    "im_nicola":  {"lang":"it","gender":"m","quality": 8,"style":"natural Italian male"},
    # ── Japanese ────────────────────────────────────────────────────────────
    "jf_alpha":   {"lang":"ja","gender":"f","quality": 9,"style":"clear Japanese female"},
    "jf_gongitsune":{"lang":"ja","gender":"f","quality": 8,"style":"expressive Japanese female"},
    "jf_nezumi":  {"lang":"ja","gender":"f","quality": 7,"style":"soft Japanese female"},
    "jf_tebukuro":{"lang":"ja","gender":"f","quality": 7,"style":"gentle Japanese female"},
    "jm_kumo":    {"lang":"ja","gender":"m","quality": 8,"style":"calm Japanese male"},
    # ── Portuguese ──────────────────────────────────────────────────────────
    "pf_dora":    {"lang":"pt","gender":"f","quality": 8,"style":"clear Portuguese female"},
    "pm_alex":    {"lang":"pt","gender":"m","quality": 8,"style":"natural Portuguese male"},
    "pm_santa":   {"lang":"pt","gender":"m","quality": 6,"style":"warm Portuguese male"},
    # ── Chinese ─────────────────────────────────────────────────────────────
    "zf_xiaobei": {"lang":"zh","gender":"f","quality": 9,"style":"bright Chinese female"},
    "zf_xiaoni":  {"lang":"zh","gender":"f","quality": 8,"style":"gentle Chinese female"},
    "zf_xiaoxiao":{"lang":"zh","gender":"f","quality": 9,"style":"clear Chinese female, default"},
    "zf_xiaoyi":  {"lang":"zh","gender":"f","quality": 8,"style":"expressive Chinese female"},
    "zm_yunjian": {"lang":"zh","gender":"m","quality": 8,"style":"deep Chinese male"},
    "zm_yunxi":   {"lang":"zh","gender":"m","quality": 9,"style":"natural Chinese male"},
    "zm_yunxia":  {"lang":"zh","gender":"m","quality": 7,"style":"soft Chinese male"},
    "zm_yunyang": {"lang":"zh","gender":"m","quality": 8,"style":"authoritative Chinese male"},
}

# ── Language default voices (non-character, used for detected language) ────────
LANG_DEFAULTS = {
    "en":    {"f": "af_heart",    "m": "am_michael"},
    "en-gb": {"f": "bf_emma",     "m": "bm_george"},
    "es":    {"f": "ef_dora",     "m": "em_alex"},
    "fr":    {"f": "ff_siwis",    "m": None},          # no French male in Kokoro
    "hi":    {"f": "hf_alpha",    "m": "hm_omega"},
    "it":    {"f": "if_sara",     "m": "im_nicola"},
    "ja":    {"f": "jf_alpha",    "m": "jm_kumo"},
    "pt":    {"f": "pf_dora",     "m": "pm_alex"},
    "zh":    {"f": "zf_xiaoxiao", "m": "zm_yunxi"},
}

# ── Persona → Kokoro voice (en primary, additional per-lang where fitting) ─────
# Rule: only map if the voice character matches the persona description
PERSONA_KOKORO = {
    "default":                          {"en": "af_heart",    "en-gb": "bf_emma"},
    "noir_detective":                   {"en": "am_onyx"},        # gravelly, measured
    "pirate_captain":                   {"en-gb": "bm_lewis"},    # British, modern/lively
    "dramatic_theater_director":        {"en-gb": "bm_daniel"},   # lively, dramatic British
    "surfer_dude":                      {"en": "af_sky"},          # smooth west-coast
    "hyper_corporate_manager":          {"en": "am_fenrir"},       # powerful, dynamic
    "conspiracy_cryptid":               {"en": "am_echo"},         # smooth, measured — fits paranoid monotone
    "overly_dramatic_anime_protagonist":{"en": "am_puck"},         # sharp, crisp, high energy
    "grumpy_old_mechanic":              {"en": "am_onyx"},         # gravelly, rough
    "medieval_fantasy_tavern_keeper":   {"en-gb": "bm_george"},   # authoritative RP
    "panicked_intern":                  {"en": "af_nova"},         # bright, energetic, urgent
    "tech_bro_guru":                    {"en": "am_fenrir"},       # powerful, dynamic, confident
    "glitchy_chatbot":                  {"en": "af_alloy"},        # neutral, versatile — glitchy processing
    "1800s_aristocrat":                 {"en-gb": "bm_george",    # authoritative RP
                                         "fr": "ff_siwis"},        # French aristocrat
    "hyper_active_toddler":             {"en": "af_jessica"},      # upbeat, friendly, high energy
    "space_marine_drill_sergeant":      {"en": "am_onyx"},         # gravelly, intense authority
    "passive_aggressive_roommate":      {"en": "af_bella"},        # soft, gentle — passive energy
    "regional_new_york":                {"en": "af_nova"},          # bright, NYC energy
    "regional_california":              {"en": "af_sky"},           # smooth west-coast
    "regional_dmv":                     {"en": "am_echo"},          # smooth, measured
    "regional_chicago":                 {"en": "am_michael"},       # deep, authoritative
    "regional_atlanta":                 {"en": "am_fenrir"},        # dynamic, energetic
}

# ── Chatterbox language support ────────────────────────────────────────────────
# Chatterbox is voice-cloning — it needs a reference audio file.
# We use downloaded Fish Audio samples as reference where available,
# otherwise fall back to Kokoro for that language.
# Marked as SLOW on Apple Silicon — only active on CUDA tier.

CHATTERBOX_LANGS = [
    "ar","da","de","el","en","es","fi","fr","he",
    "hi","it","ja","ko","ms","nl","no","pl","pt",
    "ru","sv","sw","tr","zh"
]

# Fish reference audio to use per language (from fish_voices.json)
CHATTERBOX_FISH_REF = {
    # lang → fish_voice_id to use as cloning reference
    "en": "87e80db50da94c0ab7eec7f2cd3be6fd",   # New York Baddie — clear English
    "ru": "4c774871d6524aa6b91f98612284daa0",   # Рената Литвинова — clear Russian female
    "ja": "0f9db5385a964ee68631a5e23ae61d26",   # 情熱大陸 — clear Japanese male
    "de": "13533a60000342348698dda798564e72",   # Metal Sonic — German (robotic but clear)
    "ar": "4ac8915eb1e04bb5a46d1e1889222f75",   # بنت سورية — Arabic female
    "pt": "2bd0cd1a49474aeab129894f4512e8a6",   # Neymar — Brazilian Portuguese
    "ko": "e0982d58855f457a9d50f63e3276c6f1",   # NAYEON — clear Korean female
    "zh": "zf_xiaoxiao",                         # use Kokoro zh (no zh Fish sample with good description)
    "fr": "da31468f7d0248838545b75fdfe6ffd1",   # Macron — clear French male
    "sw": "1e17dc57eaba4f27ac3ee8d50cb8d040",   # Steal a Brainrot — Swahili
}

# ── Build registry ─────────────────────────────────────────────────────────────
def build():
    # Load fish_voices for reference paths
    fish_json = BASE / "fish_voices.json"
    fish_data = json.loads(fish_json.read_text()) if fish_json.exists() else {}
    fish_voices = fish_data.get("voices", {})
    fish_persona_map = fish_data.get("persona_fish_map", {})

    # Build Kokoro section
    kokoro_section = {
        "voices": KOKORO_VOICES,
        "lang_defaults": LANG_DEFAULTS,
        "persona_map": PERSONA_KOKORO,
    }

    # Build Chatterbox section
    chatterbox_ref_paths = {}
    for lang, ref_id in CHATTERBOX_FISH_REF.items():
        if ref_id in fish_voices:
            path = fish_voices[ref_id].get("sample_path", "")
            if path and Path(path).exists():
                chatterbox_ref_paths[lang] = {"fish_id": ref_id, "path": path}
    chatterbox_section = {
        "supported_langs": CHATTERBOX_LANGS,
        "note": "Voice cloning only — requires reference audio. SLOW on Apple Silicon.",
        "reference_audio": chatterbox_ref_paths,
    }

    # Merge persona map: Fish > Kokoro (Fish takes priority per lang)
    unified_persona_map = {}
    all_personas = set(list(PERSONA_KOKORO.keys()) + list(fish_persona_map.keys()))
    for pk in sorted(all_personas):
        entry = {
            "kokoro": PERSONA_KOKORO.get(pk, {}),
            "fish":   fish_persona_map.get(pk, {}),
        }
        # Resolved = best engine per lang (Fish preferred, Kokoro fallback)
        resolved = {}
        all_langs = set(list(entry["kokoro"].keys()) + list(entry["fish"].keys()))
        for lang in all_langs:
            if lang in entry["fish"]:
                resolved[lang] = {"engine": "fish", "id": entry["fish"][lang]}
            else:
                resolved[lang] = {"engine": "kokoro", "id": entry["kokoro"][lang]}
        entry["resolved"] = resolved
        unified_persona_map[pk] = entry

    registry = {
        "version": "1.0",
        "built": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "engines": {
            "kokoro":     kokoro_section,
            "chatterbox": chatterbox_section,
            "fish":       {"voices": fish_voices, "persona_map": fish_persona_map},
        },
        "persona_map": unified_persona_map,
        "lang_defaults": {
            **{lang: {"kokoro_f": v["f"], "kokoro_m": v["m"]}
               for lang, v in LANG_DEFAULTS.items()},
            # Fish lang defaults (highest quality sample per language)
            "ru": {"kokoro_f": None, "kokoro_m": None,
                   "fish_f": "4c774871d6524aa6b91f98612284daa0",
                   "fish_m": "0a690dbeb3984a9f88cd39353880775f"},
            "ja": {"kokoro_f": "jf_alpha", "kokoro_m": "jm_kumo",
                   "fish_m": "0f9db5385a964ee68631a5e23ae61d26"},
            "de": {"kokoro_f": None, "kokoro_m": None,
                   "fish_m": "8ea94a74f4894080b6d5f5bc8910da6d"},
            "ar": {"kokoro_f": None, "kokoro_m": None,
                   "fish_f": "4ac8915eb1e04bb5a46d1e1889222f75",
                   "fish_m": "b8a16ccac9ad4bc78f5f3f5dd46dc6b5"},
            "ko": {"kokoro_f": None, "kokoro_m": None,
                   "fish_f": "e0982d58855f457a9d50f63e3276c6f1",
                   "fish_m": "8cf5ee4cb0224c109852a206f185a05f"},
            "sw": {"kokoro_f": None, "kokoro_m": None,
                   "fish_m": "1e17dc57eaba4f27ac3ee8d50cb8d040"},
        },
    }

    out = BASE / "voices_registry.json"
    out.write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    print(f"✓ voices_registry.json written ({out.stat().st_size // 1024}KB)")
    print(f"  Kokoro voices:    {len(KOKORO_VOICES)}")
    print(f"  Lang defaults:    {len(LANG_DEFAULTS)} languages (Kokoro)")
    print(f"  Chatterbox langs: {len(CHATTERBOX_LANGS)} languages")
    print(f"  Fish personas:    {len(fish_persona_map)}")
    print(f"  Unified personas: {len(unified_persona_map)}")
    return registry

if __name__ == "__main__":
    build()
