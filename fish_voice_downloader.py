#!/usr/bin/env python3
"""
fish_voice_downloader.py
Searches Fish Audio public API for the requested voices, downloads reference
audio samples, and writes fish_voices.json + patches voice_manifest.json.

Usage:
  python3.13 fish_voice_downloader.py
"""

import os, sys, json, time, urllib.request, urllib.parse, urllib.error
from pathlib import Path

BASE   = Path(__file__).parent
FISH_DIR = BASE / "assets" / "voices" / "fish"
FISH_DIR.mkdir(parents=True, exist_ok=True)

FISH_API = "https://api.fish.audio"

# ── Voice search targets ───────────────────────────────────────────────────────
# (search_title, lang, persona_keys, gender, notes)
TARGETS = [
    # English
    ("New York Baddie",            "en", ["regional_new_york", "passive_aggressive_roommate"], "f", "blunt nyc female"),
    ("Mortal Kombat Announcer",    "en", ["space_marine_drill_sergeant"],                      "m", "dramatic announcer"),
    ("Super Smash Bros. 4/Ultimate Announcer", "en", ["dramatic_theater_director"],            "m", "cinematic announcer"),
    ("adam adam",                  "en", ["tech_bro_guru", "surfer_dude"],                     "m", "friendly energetic male"),
    ("E-girl",                     "en", ["panicked_intern", "hyper_active_toddler"],          "f", "energetic female"),
    ("British",                    "en", ["1800s_aristocrat", "pirate_captain"],               "m", "british accent"),
    ("Hatsune Miku (Text To Speech)", "en", ["overly_dramatic_anime_protagonist"],            "m", "anime energetic"),
    # Russian
    ("Меллстрой",                  "ru", ["hyper_corporate_manager", "conspiracy_cryptid"],   "m", "ru energetic male"),
    ("Mita Miside (Russian voice)","ru", ["passive_aggressive_roommate", "panicked_intern"],   "f", "ru soft female"),
    ("Рената Литвинова",           "ru", ["dramatic_theater_director"],                       "f", "ru dramatic female"),
    # Japanese
    ("情熱大陸",                   "ja", ["noir_detective", "1800s_aristocrat"],              "m", "ja calm narrator"),
    ("元気な女性",                  "ja", ["regional_california", "hyper_active_toddler"],    "f", "ja youthful female"),
    # German
    ("Metal Sonic",                "de", ["glitchy_chatbot"],                                  "m", "robotic metallic"),
    ("Zkush",                      "de", ["space_marine_drill_sergeant", "grumpy_old_mechanic"],"m","de aggressive male"),
    # Arabic
    ("بنت سورية 77 الف",           "ar", ["passive_aggressive_roommate"],                    "f", "ar female"),
    ("ميماتي",                     "ar", ["grumpy_old_mechanic", "conspiracy_cryptid"],       "m", "ar male"),
    # Portuguese (Brazilian)
    ("Neymar",                     "pt", ["surfer_dude", "tech_bro_guru"],                    "m", "pt-br warm male"),
    ("Isabela",                    "pt", ["regional_california"],                              "f", "pt-br breathy female"),
    ("waldo",                      "pt", ["space_marine_drill_sergeant"],                      "m", "pt-br deep male"),
    # Korean
    ("NAYEON",                     "ko", ["regional_california", "panicked_intern"],           "f", "ko energetic female"),
    ("보이스1",                    "ko", ["noir_detective"],                                   "m", "ko dramatic male"),
    # Chinese
    ("AD学姐",                     "zh", ["dramatic_theater_director"],                       "f", "zh female"),
    ("千早爱音",                   "zh", ["hyper_active_toddler", "regional_california"],     "f", "zh bright female"),
    ("沉稳财经男声",               "zh", ["hyper_corporate_manager", "1800s_aristocrat"],     "m", "zh steady male"),
    # French
    ("Bonheur",                    "fr", ["passive_aggressive_roommate"],                     "f", "fr warm female"),
    ("Macron",                     "fr", ["1800s_aristocrat", "hyper_corporate_manager"],     "m", "fr authoritative male"),
    # Swahili / other
    ("Steal a Brainrot (Brainrots Voice)", "sw", ["hyper_active_toddler"],                   "m", "playful rhythmic"),
]

# ── Helpers ────────────────────────────────────────────────────────────────────
def _get(url: str, headers: dict = {}) -> dict | bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/2.0", **headers})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read()
        ct = r.headers.get("Content-Type", "")
        if "json" in ct:
            return json.loads(data)
        return data

def search_voice(title: str, lang: str) -> dict | None:
    """Search Fish Audio for a voice model by title. Returns first match."""
    q = urllib.parse.urlencode({"title": title, "page_size": 5})
    url = f"{FISH_API}/model?{q}"
    try:
        resp = _get(url)
        items = resp.get("items", [])
        if not items:
            return None
        # Prefer exact title match
        for item in items:
            if item.get("title", "").strip().lower() == title.strip().lower():
                return item
        return items[0]
    except Exception as e:
        print(f"  ✗ search error for '{title}': {e}")
        return None

def download_sample(voice_id: str, sample_url: str) -> Path | None:
    """Download the reference MP3 sample for a voice."""
    dest = FISH_DIR / f"{voice_id}.mp3"
    if dest.exists() and dest.stat().st_size > 5000:
        return dest
    try:
        data = _get(sample_url)
        if isinstance(data, bytes) and len(data) > 5000:
            dest.write_bytes(data)
            return dest
    except Exception as e:
        print(f"    ✗ sample download failed: {e}")
    return None

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print("╔══════════════════════════════════════════════════╗")
    print("║   Fish Audio Voice Downloader for JARVIS         ║")
    print("╚══════════════════════════════════════════════════╝\n")

    results   = {}  # voice_id → metadata
    persona_fish_map = {}  # persona_key → {lang → voice_id}

    for title, lang, personas, gender, notes in TARGETS:
        print(f"  [{lang.upper()}] {title} …", flush=True)
        item = search_voice(title, lang)
        if not item:
            print(f"  ✗ not found\n")
            continue

        vid  = item["_id"]
        name = item["title"]
        samples = item.get("samples", [])
        sample_url = samples[0]["audio"] if samples else None

        # Download reference audio
        sample_path = None
        if sample_url:
            sample_path = download_sample(vid, sample_url)

        results[vid] = {
            "id":           vid,
            "title":        name,
            "lang":         lang,
            "gender":       gender,
            "notes":        notes,
            "personas":     personas,
            "sample_path":  str(sample_path) if sample_path else None,
            "sample_url":   sample_url,
        }

        # Build persona → lang → voice_id map
        for pk in personas:
            if pk not in persona_fish_map:
                persona_fish_map[pk] = {}
            persona_fish_map[pk][lang] = vid

        status = f"✓ {vid[:12]}…  sample={'downloaded' if sample_path else 'FAILED'}"
        print(f"    {status}")
        time.sleep(0.25)  # be polite to the API

    # ── Write fish_voices.json ─────────────────────────────────────────────────
    fish_json = BASE / "fish_voices.json"
    fish_json.write_text(json.dumps({
        "downloaded":       time.strftime("%Y-%m-%dT%H:%M:%S"),
        "voices":           results,
        "persona_fish_map": persona_fish_map,
    }, indent=2, ensure_ascii=False))
    print(f"\n[fish_voices.json] Written → {fish_json}")
    print(f"  {len(results)} voices, {len(persona_fish_map)} personas mapped")

    # ── Patch voice_manifest.json ──────────────────────────────────────────────
    manifest_path = BASE / "voice_manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        for pk, lang_map in persona_fish_map.items():
            if pk in manifest.get("profiles", {}):
                manifest["profiles"][pk]["fish_voices"] = lang_map
            else:
                manifest.setdefault("profiles", {})[pk] = {"fish_voices": lang_map}
        manifest["fish_voices_path"] = str(fish_json)
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        print(f"[voice_manifest.json] Patched with fish_voices entries")
    else:
        print("[voice_manifest.json] Not found — run voice_provisioner.py first to generate it")

    print("\n✓ Done. Restart the TTS daemon to pick up Fish Audio voices.\n")

if __name__ == "__main__":
    main()
