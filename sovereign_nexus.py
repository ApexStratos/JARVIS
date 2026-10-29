"""
sovereign_nexus.py — Autonomous OS-level visual navigation agent.
Modeled on Claude Computer Use: same tool names, same PTAE loop structure.

Speed techniques:
  - Streaming JSON stop: both models stop the moment their JSON object closes
  - Description cache: skip llava entirely when screen hash is unchanged
  - Compact JSON describe: llava outputs structured JSON (~70 tokens) not prose
  - 1024px/q65 images: half the bytes vs 1440/q78, same accuracy
  - Background evaluate: post-action screenshot taken in parallel thread
  - num_predict caps: hard ceiling so neither model ever over-generates
  - Both models pre-warmed concurrently at import time

Pipeline per step (target 4-8s total):
  PERCEIVE  → 0.3s  screencapture + 64px grid overlay at 1024px
  DESCRIBE  → 3-5s  llava streams typed-element JSON → stops on closing }
  THINK     → 1-2s  llama3 streams action JSON → stops on closing }
  ACT       → 0.1s  pyautogui executes (Claude-aligned tool names)
  EVALUATE  → 0.0s  uses background thread screenshot already taken

Claude-aligned tools: left_click right_click double_click middle_click
  mouse_move drag key type scroll screenshot wait log_event
"""
import os, json, time, threading, base64, io, subprocess, tempfile, hashlib, pathlib, datetime, re
import urllib.request
import pyautogui
from PIL import Image, ImageDraw
from jarvis_core_personas import JARVIS_CORE_PERSONAS
from jarvis_regional_personas import JARVIS_REGIONAL_PERSONAS

_ALL_PERSONAS = {**JARVIS_CORE_PERSONAS, **JARVIS_REGIONAL_PERSONAS}

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = pathlib.Path(__file__).parent
DATA_DIR       = BASE_DIR / "data"
STATE_FILE     = DATA_DIR / "nexus_state.json"
PERIMETER_FILE = BASE_DIR / "safety_perimeter.json"
AUDIT_LOG      = BASE_DIR / "nexus_audit.log"
DATA_DIR.mkdir(parents=True, exist_ok=True)

pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.06

# ── Models ────────────────────────────────────────────────────────────────────
OLLAMA_BASE  = "http://localhost:11434"
VISION_MODEL = "llava:latest"
REASON_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")

_running    = False
_stop_event = threading.Event()
_lock       = threading.Lock()

# ── Prompts ───────────────────────────────────────────────────────────────────
# llava outputs typed-element JSON — forces precision and enables streaming early-stop
_DESCRIBE_SYSTEM = (
    'Analyze this macOS screenshot. Yellow labels show ACTUAL screen pixel coordinates.\n'
    'Reply ONLY with this JSON (no prose, no markdown):\n'
    '{"app":"app·title","state":"screen state ≤8 words",'
    '"focused":"type:label at (x,y) or null",'
    '"elements":["button:label at (x,y)","input:label at (x,y) [empty|has-text]",'
    '"text:content at (x,y)","menu:item at (x,y)","tab:label at (x,y)",'
    '"icon:label at (x,y)","scroll:region at (x,y)","link:label at (x,y)"],'
    '"text":"key readable text ≤20 words","dialog":false,"loading":false}\n\n'
    'Rules:\n'
    '- Each element: "type:label at (x,y)" — types: button input text menu tab icon scroll link checkbox list\n'
    '- List 8-12 most interactive elements using grid-label coordinates\n'
    '- input elements: add [empty] or [has-text] suffix\n'
    '- dialog:true if modal/alert/sheet is blocking the main UI\n'
    '- loading:true if spinner/progress bar visible'
)

_REASON_SYSTEM = (
    'You are Sovereign Nexus, a macOS computer-use agent modeled on Claude Computer Use.\n'
    'Output ONE action as JSON ONLY:\n'
    '{"thought":"observe→plan→reason in ≤40 words",'
    '"action":{"tool_name":"left_click","parameters":{"x":0,"y":0}},'
    '"confidence":8,"done":false,"roadblock":false,"roadblock_description":""}\n\n'
    'TOOLS (use exact names):\n'
    '  left_click   {"x","y"}                           — click button/link/field/tab\n'
    '  right_click  {"x","y"}                           — context menu\n'
    '  double_click {"x","y"}                           — open file or activate\n'
    '  middle_click {"x","y"}                           — open link in new tab\n'
    '  mouse_move   {"x","y"}                           — hover to reveal tooltip/submenu\n'
    '  drag         {"x","y","end_x":0,"end_y":0}       — click-drag (sliders, selections)\n'
    '  key          {"key":"Return|escape|cmd+space|cmd+a|Tab|cmd+c|cmd+v|cmd+tab|..."}\n'
    '  type         {"text":"..."}                      — type into focused input\n'
    '  scroll       {"x":0,"y":0,"direction":"down","amount":3}  — scroll (up/down/left/right)\n'
    '  screenshot   {}                                  — take fresh screenshot to re-assess\n'
    '  wait         {}                                  — wait for UI to load/animate\n'
    '  log_event    {"text":"..."}                      — audit note\n\n'
    'RULES (follow exactly):\n'
    '  • Coordinates come from screen description — use them exactly, never guess\n'
    '  • x=0,y=0 means "unknown" — use screenshot action instead of clicking (0,0)\n'
    '  • Always left_click a text field in the prior step before type\n'
    '  • key "Return" submits; "escape" dismisses dialogs; "Tab" moves focus\n'
    '  • Use screenshot when unsure of current state or after navigation\n'
    '  • confidence 1-10: ≥7=clear target, ≤4=uncertain → auto-screenshot\n'
    '  • done=true ONLY when goal is visibly achieved on screen\n'
    '  • roadblock=true if goal is blocked (wrong app, auth wall, missing feature)\n'
    '  • NEVER delete files, send unauthorized messages, or make purchases'
)

def _get_reason_system() -> str:
    key = os.environ.get("JARVIS_PERSONA", "").strip()
    if key and key in _ALL_PERSONAS:
        return _REASON_SYSTEM + (
            f"\n\nPERSONA — adopt this voice for all thought/log_event fields:\n"
            f"{_ALL_PERSONAS[key]}"
        )
    return _REASON_SYSTEM

_DECOMPOSE_SYSTEM = (
    'You are a macOS task planner. Break the goal into 3-7 ordered sub-steps.\n'
    'Each sub-step must be a single concrete UI action (click, type, navigate, verify).\n'
    'Be specific: name the app, the element, and the expected outcome.\n'
    'Reply with JSON array ONLY: ["step 1","step 2",...]'
)

_VERIFY_SYSTEM = (
    'You are verifying whether a computer action achieved its intended result.\n'
    'Reply with JSON ONLY:\n'
    '{"success":true,"correction":null,"reason":""}\n'
    'or if the action failed:\n'
    '{"success":false,"correction":"specific alternative action in ≤15 words","reason":"why it failed in ≤10 words"}\n\n'
    'Rules:\n'
    '- Compare BEFORE and AFTER descriptions strictly\n'
    '- If AFTER shows the expected change (new window, text entered, item selected): success=true\n'
    '- If nothing changed or wrong thing changed: success=false with specific correction\n'
    '- If you cannot tell the difference: assume success=true\n'
    '- correction should name a specific tool: "screenshot to re-assess", "left_click at X,Y", etc.'
)

# ── Streaming Ollama call with early JSON stop ────────────────────────────────
def _ollama_stream_json(model: str, system: str, user: str,
                        images: list = None,
                        num_predict: int = 400,
                        num_ctx: int = 4096,
                        temperature: float = 0.05) -> str:
    """
    Stream Ollama response token-by-token.
    Stops as soon as a top-level JSON object closes (matching braces).
    Falls back to full response on parse issues.
    """
    msg = {"role": "user", "content": user}
    if images:
        msg["images"] = images

    payload = json.dumps({
        "model":   model,
        "messages": [{"role": "system", "content": system}, msg],
        "stream":  True,
        "think":   False,
        "options": {
            "temperature": temperature,
            "num_ctx":     num_ctx,
            "num_predict": num_predict,
        },
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_BASE}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    buf        = ""
    depth      = 0
    in_json    = False
    in_string  = False
    escape_next = False

    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except Exception:
                continue

            tok = chunk.get("message", {}).get("content", "")
            buf += tok

            # Track brace depth (ignore braces inside strings)
            for ch in tok:
                if escape_next:
                    escape_next = False
                    continue
                if ch == '\\' and in_string:
                    escape_next = True
                    continue
                if ch == '"' and not escape_next:
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if ch == '{':
                    depth += 1
                    in_json = True
                elif ch == '}':
                    depth -= 1
                    if in_json and depth == 0:
                        return buf.strip()

            if chunk.get("done"):
                break

    return buf.strip()


def _extract_json(raw: str) -> dict:
    """Robust JSON extraction with 3-tier fallback."""
    s = raw.strip()

    # Strip markdown fences
    if "```" in s:
        for part in s.split("```"):
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{"):
                s = part
                break

    # Tier 1: direct parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Tier 2: find first complete {...} block
    m = re.search(r'\{.*\}', s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    # Tier 3: minimal safe default
    return {
        "thought": "JSON parse failed — waiting",
        "security": "SAFE",
        "action": {"tool_name": "wait", "parameters": {"x": 0, "y": 0, "text": "", "key": ""}},
        "done": False, "roadblock": False, "roadblock_description": "", "confidence": 1,
    }


# ── Screenshot + lightweight grid ─────────────────────────────────────────────
def _screenshot_annotated(max_w: int = 1024, quality: int = 65,
                           draw_grid: bool = True) -> tuple[str, str, float, float, int, int]:
    """
    Capture → resize to max_w → optionally draw coordinate grid.
    Returns (annotated_b64, raw_b64, scale_x, scale_y, screen_w, screen_h).
    Grid labels show ACTUAL screen pixel values so model outputs are used as-is.
    """
    screen_w, screen_h = pyautogui.size()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        path = tmp.name
    subprocess.run(["screencapture", "-x", "-t", "png", path], check=True)
    img = Image.open(path).convert("RGB")
    os.unlink(path)
    native_w, native_h = img.size

    if native_w > max_w:
        ratio = max_w / native_w
        img   = img.resize((max_w, int(native_h * ratio)), Image.LANCZOS)

    img_w, img_h = img.size
    scale_x = native_w / img_w
    scale_y = native_h / img_h

    # Raw b64 for diff hashing (no grid)
    raw_buf = io.BytesIO()
    img.save(raw_buf, format="JPEG", quality=quality - 5)
    raw_b64 = base64.b64encode(raw_buf.getvalue()).decode()

    if draw_grid:
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw    = ImageDraw.Draw(overlay)
        # 64px grid → denser reference points for the vision model
        major   = 128  # bright label every 128px
        minor   = 64   # faint tick every 64px

        x = minor
        while x < img_w:
            is_major = (x % major == 0)
            ax = str(int(x * scale_x))
            alpha = 40 if is_major else 18
            draw.line([(x, 0), (x, img_h)], fill=(255, 255, 255, alpha), width=1)
            if is_major:
                draw.rectangle([x + 1, 0, x + len(ax) * 6 + 3, 12], fill=(0, 0, 0, 170))
                draw.text((x + 2, 1), ax, fill=(255, 230, 50, 255))
            x += minor

        y = minor
        while y < img_h:
            is_major = (y % major == 0)
            ay = str(int(y * scale_y))
            alpha = 40 if is_major else 18
            draw.line([(0, y), (img_w, y)], fill=(255, 255, 255, alpha), width=1)
            if is_major:
                draw.rectangle([0, y + 1, len(ay) * 6 + 3, y + 12], fill=(0, 0, 0, 170))
                draw.text((2, y + 1), ay, fill=(255, 230, 50, 255))
            y += minor

        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    ann_buf = io.BytesIO()
    img.save(ann_buf, format="JPEG", quality=quality)
    ann_b64 = base64.b64encode(ann_buf.getvalue()).decode()

    return ann_b64, raw_b64, scale_x, scale_y, screen_w, screen_h


def _img_hash(b64: str) -> str:
    return hashlib.md5(b64.encode()).hexdigest()[:12]


# ── Two-model pipeline (speed-optimised) ─────────────────────────────────────
_desc_cache: dict[str, str] = {}  # {img_hash: description}


def _describe_screen(ann_b64: str, img_hash: str,
                     screen_w: int, screen_h: int,
                     step: int, goal: str) -> tuple[str, bool]:
    """
    Phase 1: llava → compact JSON description.
    Returns (description_str, from_cache).
    Skips llava entirely if image hash matches cached description.
    """
    if img_hash in _desc_cache:
        return _desc_cache[img_hash], True

    prompt = (
        f"Screen {screen_w}×{screen_h}. Step {step}. Goal: {goal[:60]}\n"
        "List key UI elements with grid coordinates. JSON only."
    )
    try:
        raw = _ollama_stream_json(
            model=VISION_MODEL, system=_DESCRIBE_SYSTEM,
            user=prompt, images=[ann_b64],
            num_predict=120, num_ctx=2048, temperature=0.03,
        )
        # Normalise: if it's valid JSON, pretty-print it, else keep raw
        try:
            desc = json.dumps(json.loads(_extract_json(raw) if not raw.strip().startswith("{") else raw), indent=None)
        except Exception:
            desc = raw
        _desc_cache[img_hash] = desc
        # Keep cache small
        if len(_desc_cache) > 20:
            oldest = next(iter(_desc_cache))
            del _desc_cache[oldest]
        return desc, False
    except Exception as e:
        return f'{{"error":"{e}"}}', False


def _reason_action(description: str, goal: str, history: list,
                   state: dict, sub_goals: list, sub_goal_idx: int = 0,
                   correction: str = "") -> dict:
    """Phase 2: llama3 → action JSON. Streams and stops at closing brace."""
    # Sub-goal context: show completed, current, upcoming
    sub_note = ""
    if sub_goals:
        done_subs  = sub_goals[:sub_goal_idx]
        cur_sub    = sub_goals[sub_goal_idx] if sub_goal_idx < len(sub_goals) else sub_goals[-1]
        next_subs  = sub_goals[sub_goal_idx+1:sub_goal_idx+2]
        sub_note   = f"\nSUB-GOAL {sub_goal_idx+1}/{len(sub_goals)}: {cur_sub}"
        if done_subs:
            sub_note += f"\n  ✓ done: {', '.join(done_subs[-2:])}"
        if next_subs:
            sub_note += f"\n  → then: {next_subs[0]}"

    hist_text = ""
    if history:
        last = history[-5:]
        hist_text = "\nRECENT ACTIONS:\n" + "\n".join(
            f"  s{h['step']} [{h['tool']}] {h.get('thought','')[:45]} → {h.get('summary','')[:35]} {'✓' if h.get('ok') else '✗'}"
            for h in last
        )
        if any(not h.get("ok", True) for h in last[-2:]):
            hist_text += "\n  ⚠ recent failures — try a different element or use screenshot"

    rb = state.get("roadblock_count", 0)
    rb_note = f"\nROADBLOCKS: {rb} — adapt strategy now" if rb else ""
    corr_note = f"\n⚠ SELF-CORRECTION: {correction}" if correction else ""

    prompt = (
        f"GOAL: {goal}{sub_note}\n"
        f"STEP: {state.get('current_step','?')}/{state.get('max_steps','?')}"
        f"{rb_note}{corr_note}{hist_text}\n\n"
        f"SCREEN:\n{description}\n\n"
        "What is the single best next action? JSON only."
    )
    try:
        raw = _ollama_stream_json(
            model=REASON_MODEL, system=_get_reason_system(),
            user=prompt, num_predict=380, num_ctx=6144, temperature=0.07,
        )
        d = _extract_json(raw)
        # Low-confidence: prefer screenshot over wait so we re-assess
        conf = d.get("confidence", 10)
        tool = d.get("action", {}).get("tool_name", "wait")
        params = d.get("action", {}).get("parameters", {})
        bad_coords = (int(params.get("x", 0) or 0) == 0 and
                      int(params.get("y", 0) or 0) == 0 and
                      tool in ("left_click","right_click","double_click","mouse_move","drag"))
        if conf < 5 or bad_coords:
            d["action"] = {"tool_name": "screenshot", "parameters": {}}
            d["confidence"] = 5
        return d
    except Exception as e:
        return {
            "thought": f"Reasoning error: {e}",
            "action": {"tool_name": "screenshot", "parameters": {}},
            "done": False, "roadblock": False, "roadblock_description": "", "confidence": 3,
        }


def _decompose_goal(goal: str) -> list[str]:
    try:
        raw = _ollama_stream_json(
            model=REASON_MODEL, system=_DECOMPOSE_SYSTEM,
            user=f"Goal: {goal}", num_predict=250, num_ctx=2048, temperature=0.15,
        )
        s = raw.strip()
        if "```" in s:
            for p in s.split("```"):
                p = p.strip()
                if p.startswith("json"): p = p[4:].strip()
                if p.startswith("["): s = p; break
        m = re.search(r'\[.*\]', s, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return []


def _verify_step(action: dict, before_desc: str, after_desc: str,
                 goal: str, sub_goal: str) -> dict:
    """
    Quick self-correction check: did the last action achieve what it intended?
    Returns {"success": bool, "correction": str|None, "reason": str}.
    Uses llama3 only (fast) — no image needed, uses cached descriptions.
    """
    if not after_desc or after_desc == before_desc:
        return {"success": True, "correction": None, "reason": ""}

    tool = action.get("tool_name", "?")
    params = action.get("parameters", {})
    action_desc = f"{tool}"
    if params.get("key"):   action_desc += f" {params['key']}"
    if params.get("text"):  action_desc += f' "{params["text"][:30]}"'
    if params.get("x"):     action_desc += f" at ({params['x']},{params['y']})"

    prompt = (
        f"GOAL: {goal}\n"
        f"SUB-GOAL: {sub_goal}\n"
        f"ACTION TAKEN: {action_desc}\n\n"
        f"BEFORE:\n{before_desc[:300]}\n\n"
        f"AFTER:\n{after_desc[:300]}\n\n"
        "Did the action succeed toward the goal? JSON only."
    )
    try:
        raw = _ollama_stream_json(
            model=REASON_MODEL, system=_VERIFY_SYSTEM,
            user=prompt, num_predict=120, num_ctx=3072, temperature=0.03,
        )
        d = _extract_json(raw)
        return {
            "success":    bool(d.get("success", True)),
            "correction": d.get("correction") or None,
            "reason":     d.get("reason", ""),
        }
    except Exception:
        return {"success": True, "correction": None, "reason": ""}


# ── Background evaluate screenshot ────────────────────────────────────────────
class _BgShot:
    """Take a screenshot in a background thread after a delay."""
    def __init__(self, delay: float):
        self._result = None
        self._done   = threading.Event()
        t = threading.Thread(target=self._run, args=(delay,), daemon=True)
        t.start()

    def _run(self, delay: float):
        time.sleep(delay)
        try:
            _, raw, _, _, _, _ = _screenshot_annotated(draw_grid=False)
            self._result = raw
        except Exception:
            self._result = None
        self._done.set()

    def get(self, timeout: float = 4.0) -> str | None:
        self._done.wait(timeout)
        return self._result


# ── Safety perimeter ──────────────────────────────────────────────────────────
# Shared with auto.py and app/toolkit.py's _ollama_vision_agent — one gate
# for all three screen-control agents instead of three different ones (or
# none). See safety_perimeter.py for allow_destructive/allow_file_delete.
from safety_perimeter import load_perimeter as _load_perimeter
from safety_perimeter import check_perimeter as _check_perimeter_core

def _check_perimeter(action: dict, perimeter: dict) -> tuple[bool, str]:
    params = action.get("parameters", {})
    return _check_perimeter_core(
        key=params.get("key", ""), text=params.get("text", ""), perimeter=perimeter,
    )


# ── State / audit ─────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}

def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass

def _init_state(goal: str, max_steps: int) -> dict:
    return {
        "session_id":        datetime.datetime.now().strftime("%Y%m%d_%H%M%S"),
        "goal":              goal,
        "max_steps":         max_steps,
        "current_step":      0,
        "current_objective": goal,
        "completed_steps":   [],
        "roadblock_count":   0,
        "status":            "running",
        "started_at":        datetime.datetime.now().isoformat(),
    }

def _audit(category: str, intent: str, method: str, result: str):
    ts    = datetime.datetime.now().isoformat(timespec="seconds")
    cat   = category.upper().ljust(10)
    line  = f"[{ts}] [{cat}] INTENT: {intent} | METHOD: {method} | RESULT: {result}\n"
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    return line.strip()

def get_audit_tail(n: int = 50) -> list:
    if not AUDIT_LOG.exists():
        return []
    return AUDIT_LOG.read_text(encoding="utf-8").splitlines()[-n:]


# ── Action executor ───────────────────────────────────────────────────────────
_COORD_TOOLS = {"left_click","right_click","double_click","middle_click",
                "mouse_move","drag","scroll","click","rightclick","doubleclick"}

def _validate_action(action: dict, screen_w: int, screen_h: int) -> dict:
    tool   = action.get("tool_name", "wait")
    params = dict(action.get("parameters") or {})
    if tool in _COORD_TOOLS:
        x = int(params.get("x", 0) or 0)
        y = int(params.get("y", 0) or 0)
        # (0,0) from model means "unknown" → take fresh screenshot instead
        if x == 0 and y == 0 and tool != "scroll":
            return {"tool_name": "screenshot", "parameters": {}}
        params["x"] = max(1, min(x, screen_w - 1))
        params["y"] = max(1, min(y, screen_h - 1))
        # Clamp drag endpoints too
        if tool == "drag":
            params["end_x"] = max(1, min(int(params.get("end_x", x) or x), screen_w - 1))
            params["end_y"] = max(1, min(int(params.get("end_y", y) or y), screen_h - 1))
        action = {**action, "parameters": params}
    return action

def _execute(action: dict) -> dict:
    """Execute a Claude-aligned tool action via pyautogui."""
    tool   = action.get("tool_name", "wait")
    params = action.get("parameters") or {}
    x      = int(params.get("x", 0) or 0)
    y      = int(params.get("y", 0) or 0)
    text   = str(params.get("text", "") or "")
    key    = str(params.get("key", "") or "")
    try:
        # ── Click family ─────────────────────────────────────────────────────
        if tool in ("left_click", "click"):
            pyautogui.moveTo(x, y, duration=0.10)
            pyautogui.click()
            return {"ok": True, "msg": f"left_click ({x},{y})"}
        elif tool in ("right_click", "rightclick"):
            pyautogui.moveTo(x, y, duration=0.10)
            pyautogui.rightClick()
            return {"ok": True, "msg": f"right_click ({x},{y})"}
        elif tool in ("double_click", "doubleclick"):
            pyautogui.moveTo(x, y, duration=0.10)
            pyautogui.doubleClick()
            return {"ok": True, "msg": f"double_click ({x},{y})"}
        elif tool == "middle_click":
            pyautogui.middleClick(x, y)
            return {"ok": True, "msg": f"middle_click ({x},{y})"}
        # ── Mouse movement ────────────────────────────────────────────────────
        elif tool == "mouse_move":
            pyautogui.moveTo(x, y, duration=0.15)
            time.sleep(0.25)  # let tooltip/submenu appear
            return {"ok": True, "msg": f"mouse_move → ({x},{y})"}
        # ── Drag ─────────────────────────────────────────────────────────────
        elif tool == "drag":
            ex = int(params.get("end_x", x) or x)
            ey = int(params.get("end_y", y) or y)
            pyautogui.moveTo(x, y, duration=0.10)
            pyautogui.dragTo(ex, ey, duration=0.30, button="left")
            return {"ok": True, "msg": f"drag ({x},{y})→({ex},{ey})"}
        # ── Keyboard ─────────────────────────────────────────────────────────
        elif tool in ("key", "key_combo"):
            keys = [k.strip() for k in re.split(r'\+', key) if k.strip()]
            if len(keys) == 1:
                pyautogui.press(keys[0])
            else:
                pyautogui.hotkey(*keys)
            return {"ok": True, "msg": f"key: {key}"}
        # ── Type ─────────────────────────────────────────────────────────────
        elif tool == "type":
            # Use pyperclip paste for long text (faster + handles special chars)
            if len(text) > 30:
                try:
                    import pyperclip
                    pyperclip.copy(text)
                    pyautogui.hotkey("cmd", "v")
                    return {"ok": True, "msg": f"paste-typed: {text[:60]}"}
                except Exception:
                    pass
            pyautogui.write(text, interval=0.02)
            return {"ok": True, "msg": f"typed: {text[:60]}"}
        # ── Scroll ───────────────────────────────────────────────────────────
        elif tool == "scroll":
            # Legacy: text = integer (negative=down) e.g. text="-3"
            if not params.get("direction") and text and re.match(r'^-?\d+$', text.strip()):
                n = int(text)
                pyautogui.scroll(n, x=x or None, y=y or None)
                return {"ok": True, "msg": f"scroll {n} at ({x},{y})"}
            direction = str(params.get("direction", "down")).lower()
            amount    = int(params.get("amount", 3) or 3)
            if direction == "up":
                pyautogui.scroll(amount,  x=x or None, y=y or None)
            elif direction == "left":
                pyautogui.hscroll(-amount, x=x or None, y=y or None)
            elif direction == "right":
                pyautogui.hscroll(amount,  x=x or None, y=y or None)
            else:  # down
                pyautogui.scroll(-amount, x=x or None, y=y or None)
            return {"ok": True, "msg": f"scroll {direction}×{amount} at ({x},{y})"}
        # ── Screenshot (special: signals PTAE loop to fresh-perceive) ────────
        elif tool == "screenshot":
            return {"ok": True, "msg": "screenshot requested", "screenshot": True}
        # ── Misc ─────────────────────────────────────────────────────────────
        elif tool == "wait":
            time.sleep(0.7)
            return {"ok": True, "msg": "waited"}
        elif tool == "log_event":
            _audit("vision", "log_event", "sovereign_nexus", text[:200])
            return {"ok": True, "msg": f"logged: {text[:60]}"}
        else:
            return {"ok": False, "msg": f"unknown tool: {tool}"}
    except pyautogui.FailSafeException:
        return {"ok": False, "msg": "FAILSAFE: cursor hit corner"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


# ── Anti-drift ────────────────────────────────────────────────────────────────
def _fingerprint(action: dict) -> str:
    p = action.get("parameters") or {}
    return f"{action.get('tool_name')}|{p.get('x')}|{p.get('y')}|{p.get('key')}|{str(p.get('text',''))[:20]}"

def _is_drifting(fps: list) -> bool:
    return len(fps) >= 3 and len(set(fps[-3:])) == 1


# ── Pre-warm both models concurrently ─────────────────────────────────────────
def _prewarm():
    tiny_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    def _warm(model, images=None):
        try:
            msg = {"role": "user", "content": "hi"}
            if images: msg["images"] = images
            payload = json.dumps({
                "model": model,
                "messages": [msg],
                "stream": False,
                "options": {"num_predict": 1, "num_ctx": 512},
            }).encode()
            req = urllib.request.Request(
                f"{OLLAMA_BASE}/api/chat", data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=60).read()
        except Exception:
            pass

    threading.Thread(target=_warm, args=(VISION_MODEL, [tiny_b64]), daemon=True).start()
    threading.Thread(target=_warm, args=(REASON_MODEL,),            daemon=True).start()

# Warm on module load
_prewarm()


# ── Public API ─────────────────────────────────────────────────────────────────
def is_running() -> bool:
    return _running

def stop():
    _stop_event.set()

def get_status() -> dict:
    return {"running": _running, "state": _load_state()}


def run_stream(goal: str, max_steps: int = 40):
    """
    SSE generator. Yields dicts typed:
      start | decompose | perceive | describe | think | act | evaluate |
      complete | error | stopped | drift_recovery | roadblock | eval_note | done
    """
    global _running
    with _lock:
        if _running:
            yield {"type": "error", "msg": "Nexus agent already running"}
            return
        _running = True
        _stop_event.clear()

    state      = _init_state(goal, max_steps)
    perimeter  = _load_perimeter()
    max_steps  = min(max_steps, perimeter.get("max_steps", 60))
    state["max_steps"] = max_steps
    history:         list[dict]  = []
    fingerprints:    list[str]   = []
    no_change_streak             = 0
    prev_raw_id: str | None      = None
    correction_note: str         = ""   # injected into next THINK when last action failed
    sub_goal_idx:    int         = 0    # tracks current sub-goal (advances on verify success)
    screenshot_steps: int        = 0    # screenshot actions don't count toward step limit

    _desc_cache.clear()
    _save_state(state)
    _audit("sovereign", "session_start", "sovereign_nexus",
           f"goal={goal[:80]} vision={VISION_MODEL} reason={REASON_MODEL}")
    yield {"type": "start", "goal": goal, "max_steps": max_steps,
           "session_id": state["session_id"],
           "vision_model": VISION_MODEL, "reason_model": REASON_MODEL}

    # ── Goal decomposition ────────────────────────────────────────────────────
    sub_goals: list[str] = []
    yield {"type": "decompose", "status": "planning…"}
    try:
        sub_goals = _decompose_goal(goal)
        state["sub_goals"] = sub_goals
        _save_state(state)
    except Exception:
        pass
    yield {"type": "decompose", "sub_goals": sub_goals}

    # bg_shot holds the background evaluate screenshot from the previous step
    bg_shot: _BgShot | None = None

    try:
        for step in range(1, max_steps + 1):
            if _stop_event.is_set():
                state["status"] = "stopped"
                _save_state(state)
                yield {"type": "stopped", "step": step}
                break

            t_step = time.monotonic()
            state["current_step"] = step
            _save_state(state)

            # ── PERCEIVE ─────────────────────────────────────────────────────
            try:
                ann_b64, raw_b64, scale_x, scale_y, scr_w, scr_h = _screenshot_annotated(
                    max_w=1024, quality=65, draw_grid=True,
                )
                raw_id = _img_hash(raw_b64)
            except Exception as e:
                yield {"type": "error", "step": step, "phase": "perceive", "msg": str(e)}
                break

            screen_changed = raw_id != prev_raw_id if prev_raw_id else True
            yield {"type": "perceive", "step": step, "img": ann_b64,
                   "img_id": raw_id, "screen": f"{scr_w}×{scr_h}",
                   "changed": screen_changed}

            # ── DESCRIBE (llava, cached if unchanged) ─────────────────────────
            t_desc = time.monotonic()
            description, from_cache = _describe_screen(
                ann_b64, raw_id, scr_w, scr_h, step, goal,
            )
            desc_ms = round((time.monotonic() - t_desc) * 1000)
            yield {"type": "describe", "step": step,
                   "description": description,
                   "cached": from_cache,
                   "ms": desc_ms}

            # ── THINK (llama3) ────────────────────────────────────────────────
            t_think = time.monotonic()
            decision = _reason_action(description, goal, history, state, sub_goals,
                                      sub_goal_idx=sub_goal_idx,
                                      correction=correction_note)
            correction_note = ""  # consumed
            think_ms = round((time.monotonic() - t_think) * 1000)

            thought    = decision.get("thought", decision.get("thought_process", ""))
            sec_raw    = decision.get("security", "SAFE")
            action     = decision.get("action", {"tool_name": "screenshot", "parameters": {}})
            done       = bool(decision.get("done", False))
            roadblock  = bool(decision.get("roadblock", False))
            rb_desc    = decision.get("roadblock_description", "")
            confidence = int(decision.get("confidence", 7))

            action = _validate_action(action, scr_w, scr_h)

            allowed, reason = _check_perimeter(action, perimeter)
            if not allowed:
                sec_raw = f"BLOCKED — {reason}"
                action  = {"tool_name": "log_event", "parameters": {"text": f"Blocked: {reason}"}}

            # confidence handled in _reason_action; screenshot replaces low-conf actions

            yield {
                "type":            "think",
                "step":            step,
                "thought_process": thought,
                "security_check":  sec_raw,
                "action":          action,
                "confidence":      confidence,
                "done":            done,
                "roadblock":       roadblock,
                "roadblock_description": rb_desc,
                "ms":              think_ms,
                "sub_goal_idx":    sub_goal_idx,
                "sub_goals":       sub_goals,
                "execution_state": {
                    "current_objective": state["current_objective"],
                    "completed_steps":   state["completed_steps"][-4:],
                    "roadblock_count":   state["roadblock_count"],
                },
            }

            if done:
                state["status"] = "complete"
                state["completed_steps"].append(f"step {step}: goal complete")
                _save_state(state)
                _audit("sovereign", "goal_complete", "sovereign_nexus",
                       f"step={step} goal={goal[:60]}")
                yield {"type": "complete", "step": step, "reason": thought[:300]}
                break

            if roadblock:
                state["roadblock_count"] += 1
                _save_state(state)
                _audit("sovereign", "roadblock", "sovereign_nexus",
                       f"step={step} count={state['roadblock_count']} {rb_desc[:80]}")
                yield {"type": "roadblock", "step": step, "description": rb_desc,
                       "count": state["roadblock_count"]}
                if state["roadblock_count"] >= perimeter.get("max_roadblocks", 5):
                    yield {"type": "complete", "step": step,
                           "reason": f"Max roadblocks ({state['roadblock_count']}) reached"}
                    break
                action = {"tool_name": "key_combo", "parameters": {"key": "escape"}}

            fp = _fingerprint(action)
            fingerprints.append(fp)
            if _is_drifting(fingerprints):
                _audit("sovereign", "anti_drift", "sovereign_nexus",
                       f"step={step} repeated: {fp}")
                yield {"type": "drift_recovery", "step": step, "fingerprint": fp}
                recovery = "tab" if "key_combo" in fp else "escape"
                action = {"tool_name": "key_combo", "parameters": {"key": recovery}}
                fingerprints.clear()
                _desc_cache.clear()

            # ── ACT ──────────────────────────────────────────────────────────
            tool_name = action.get("tool_name", "wait")

            # screenshot action → clear cache, skip execute, fresh perceive next loop
            if tool_name == "screenshot":
                _desc_cache.clear()
                prev_raw_id   = None   # force "changed" on next perceive
                screenshot_steps += 1
                _audit("sovereign", "screenshot", "sovereign_nexus",
                       f"step={step} re-assessing screen")
                yield {"type": "act", "step": step, "action": action,
                       "ok": True, "msg": "re-capturing screen for fresh assessment"}
                # Don't count this step toward history or fingerprints
                step_ms = round((time.monotonic() - t_step) * 1000)
                yield {"type": "timing", "step": step, "total_ms": step_ms,
                       "describe_ms": desc_ms, "think_ms": think_ms, "cached": from_cache}
                continue

            result = _execute(action)
            _audit("sovereign", tool_name, "sovereign_nexus",
                   result.get("msg", "")[:120])

            yield {"type": "act", "step": step, "action": action,
                   "ok": result["ok"], "msg": result.get("msg", "")}

            if not result["ok"] and "FAILSAFE" in result.get("msg", ""):
                state["status"] = "failsafe_stop"
                _save_state(state)
                yield {"type": "error", "step": step, "phase": "act", "msg": result["msg"]}
                break

            history.append({
                "step":    step,
                "tool":    tool_name,
                "thought": thought[:60],
                "summary": result.get("msg", "")[:70],
                "ok":      result["ok"],
            })
            state["completed_steps"].append(
                f"step {step}: [{tool_name}] {result.get('msg','')[:55]}"
            )
            _save_state(state)

            # ── Start background evaluate screenshot immediately after ACT ─────
            if tool_name not in ("wait", "log_event"):
                wait_time = 0.9 if tool_name in ("type", "key_combo") else 0.5
                bg_shot   = _BgShot(delay=wait_time)

            # ── EVALUATE ─────────────────────────────────────────────────────
            if tool_name not in ("wait", "log_event"):
                post_raw = bg_shot.get(timeout=3.5) if bg_shot else None
                post_id  = _img_hash(post_raw) if post_raw else None
                changed  = post_id != raw_id if post_id else False
                prev_raw_id = post_id or raw_id

                if not changed:
                    no_change_streak += 1
                    _desc_cache.pop(raw_id, None)  # invalidate — UI may have changed subtly
                else:
                    no_change_streak = 0

                yield {"type": "evaluate", "step": step,
                       "changed": changed, "streak": no_change_streak}

                if not changed:
                    yield {"type": "eval_note", "step": step,
                           "msg": f"No visual change (streak {no_change_streak})"}
                    if no_change_streak >= 3:
                        yield {"type": "eval_note", "step": step,
                               "msg": "3 no-change streak → cmd+space reset"}
                        _execute({"tool_name": "key_combo", "parameters": {"key": "cmd+space"}})
                        time.sleep(0.7)
                        no_change_streak = 0
                        _desc_cache.clear()
                else:
                    # ── SELF-CORRECT: verify action achieved its intent ────────
                    after_desc, _ = _describe_screen(
                        ann_b64, post_id or raw_id, scr_w, scr_h, step, goal,
                    )
                    sub_g = sub_goals[sub_goal_idx] if sub_goals and sub_goal_idx < len(sub_goals) else ""
                    verify = _verify_step(action, description, after_desc, goal, sub_g)
                    if verify["success"]:
                        # Advance sub-goal if current one appears complete
                        if sub_goals and sub_goal_idx < len(sub_goals) - 1:
                            sub_goal_idx += 1
                            yield {
                                "type":      "subgoal_advance",
                                "step":      step,
                                "completed": sub_g,
                                "next":      sub_goals[sub_goal_idx],
                                "idx":       sub_goal_idx,
                                "total":     len(sub_goals),
                            }
                    elif verify.get("correction"):
                        correction_note = verify["correction"]
                        _audit("sovereign", "self_correct", "sovereign_nexus",
                               f"step={step} reason={verify['reason'][:60]} → {correction_note[:60]}")
                        yield {
                            "type":       "correct",
                            "step":       step,
                            "reason":     verify["reason"],
                            "correction": correction_note,
                        }
            else:
                prev_raw_id = raw_id
                time.sleep(0.35)

            step_ms = round((time.monotonic() - t_step) * 1000)
            yield {"type": "timing", "step": step, "total_ms": step_ms,
                   "describe_ms": desc_ms, "think_ms": think_ms,
                   "cached": from_cache}

        else:
            state["status"] = "step_limit"
            _save_state(state)
            yield {"type": "complete", "step": max_steps, "reason": "Reached step limit"}

    except Exception as e:
        state["status"] = "error"
        _save_state(state)
        _audit("sovereign", "error", "sovereign_nexus", str(e)[:200])
        yield {"type": "error", "step": state.get("current_step", 0), "msg": str(e)}

    finally:
        _running = False
        _audit("sovereign", "session_end", "sovereign_nexus",
               f"status={state.get('status')} steps={state.get('current_step')}")
        yield {"type": "done", "session_id": state["session_id"],
               "status": state.get("status")}
