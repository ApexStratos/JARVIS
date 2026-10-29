import os
import json
import time
import threading
import dispatch
import anthropic
from safety_perimeter import load_perimeter as _load_perimeter
from safety_perimeter import check_perimeter as _check_perimeter

_client = None
def _get_client():
    global _client
    if _client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client

_stop_flag = threading.Event()
_running = False

SYSTEM = """You are J.A.R.V.I.S., an autonomous screen control agent.
You see a screenshot of the user's Mac and must take one action per step to accomplish the given goal.

Reply ONLY with valid JSON — no markdown, no explanation:
{"action": "click 500 300", "reason": "clicking the search bar", "done": false}

Action syntax:
  click x y           left click at x,y
  rightclick x y      right click
  doubleclick x y     double click
  move x y            move cursor
  type <text>         type text (use for input fields)
  key <combo>         hotkey e.g. cmd+c, cmd+v, cmd+space, escape, return, tab
  scroll x y <n>      scroll at x,y by n (positive=up, negative=down)
  wait                pause this step (use when waiting for something to load)

Rules:
- One action per response, no chains
- Set done=true only when the goal is fully complete and visible on screen
- If stuck after 3 waits, set done=true with reason explaining the block
- Never guess coordinates; base them on what you actually see in the screenshot"""

def _vision_step(goal, history, img_b64):
    history_text = ""
    if history:
        last = history[-5:]
        history_text = "\nRecent actions:\n" + "\n".join(
            f"  step {h['step']}: {h['action']} — {h['reason']}" for h in last
        )

    prompt = f"Goal: {goal}{history_text}\n\nWhat is the single next action?"

    resp = _get_client().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64}
                },
                {"type": "text", "text": prompt}
            ]
        }]
    )
    raw = resp.content[0].text.strip()
    # strip markdown fences if model adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def run(goal: str, max_steps: int = 60, step_delay: float = 0.8):
    """Generator: yields events live as each step executes."""
    global _running
    _running = True
    _stop_flag.clear()
    history = []
    perimeter = _load_perimeter()

    yield {"type": "start", "goal": goal, "max_steps": max_steps}

    try:
        wait_streak = 0
        for step in range(1, max_steps + 1):
            if _stop_flag.is_set():
                yield {"type": "stopped", "step": step}
                break

            img = dispatch.screenshot()
            yield {"type": "screenshot", "step": step, "image": img}

            try:
                decision = _vision_step(goal, history, img)
            except Exception as e:
                yield {"type": "error", "step": step, "msg": f"Vision error: {e}"}
                break

            action = decision.get("action", "wait").strip()
            reason = decision.get("reason", "")
            done = decision.get("done", False)

            yield {"type": "decision", "step": step, "action": action, "reason": reason, "done": done}
            history.append({"step": step, "action": action, "reason": reason})

            if done:
                yield {"type": "complete", "step": step, "reason": reason}
                break

            if action == "wait":
                wait_streak += 1
                if wait_streak >= 4:
                    yield {"type": "complete", "step": step, "reason": "Stalled — stopped after 4 waits."}
                    break
                time.sleep(1.5)
                continue
            else:
                wait_streak = 0

            _key  = action[4:].strip() if action.lower().startswith("key ") else ""
            _text = action[5:].strip() if action.lower().startswith("type ") else ""
            if _key or _text:
                allowed, block_reason = _check_perimeter(key=_key, text=_text, perimeter=perimeter)
                if not allowed:
                    yield {"type": "result", "step": step, "ok": False, "msg": f"Blocked: {block_reason}"}
                    history.append({"step": step, "action": action, "reason": f"BLOCKED: {block_reason}"})
                    continue

            result = dispatch.execute(action)
            yield {"type": "result", "step": step, "ok": result["ok"], "msg": result.get("msg", "")}

            if not result["ok"]:
                yield {"type": "error", "step": step, "msg": f"Dispatch failed: {result.get('msg')}"}
                break

            time.sleep(step_delay)

        else:
            yield {"type": "complete", "step": max_steps, "reason": "Reached step limit."}

    except Exception as e:
        yield {"type": "error", "step": 0, "msg": str(e)}
    finally:
        _running = False


def stop():
    _stop_flag.set()


def is_running():
    return _running
