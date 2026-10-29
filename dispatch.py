import pyautogui
import mss
import base64
import io
import time
from PIL import Image

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

def screenshot(region=None):
    with mss.mss() as sct:
        monitor = sct.monitors[1] if not region else region
        raw = sct.grab(monitor)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode()

def execute(command: str) -> dict:
    parts = command.strip().split(" ", 3)
    action = parts[0].lower()

    try:
        if action == "screenshot":
            return {"ok": True, "image": screenshot()}

        elif action == "click":
            x, y = int(parts[1]), int(parts[2])
            pyautogui.click(x, y)
            return {"ok": True, "msg": f"Clicked ({x}, {y})"}

        elif action == "rightclick":
            x, y = int(parts[1]), int(parts[2])
            pyautogui.rightClick(x, y)
            return {"ok": True, "msg": f"Right-clicked ({x}, {y})"}

        elif action == "doubleclick":
            x, y = int(parts[1]), int(parts[2])
            pyautogui.doubleClick(x, y)
            return {"ok": True, "msg": f"Double-clicked ({x}, {y})"}

        elif action == "move":
            x, y = int(parts[1]), int(parts[2])
            pyautogui.moveTo(x, y, duration=0.2)
            return {"ok": True, "msg": f"Moved to ({x}, {y})"}

        elif action == "type":
            text = " ".join(parts[1:])
            pyautogui.typewrite(text, interval=0.04)
            return {"ok": True, "msg": f"Typed: {text}"}

        elif action == "write":
            text = " ".join(parts[1:])
            pyautogui.write(text, interval=0.04)
            return {"ok": True, "msg": f"Wrote: {text}"}

        elif action == "key":
            combo = parts[1]
            pyautogui.hotkey(*combo.split("+"))
            return {"ok": True, "msg": f"Key: {combo}"}

        elif action == "scroll":
            x, y = int(parts[1]), int(parts[2])
            amount = int(parts[3]) if len(parts) > 3 else 3
            pyautogui.scroll(amount, x=x, y=y)
            return {"ok": True, "msg": f"Scrolled {amount} at ({x}, {y})"}

        elif action == "drag":
            x1, y1, x2, y2 = int(parts[1]), int(parts[2]), int(parts[3].split(",")[0]), int(parts[3].split(",")[1])
            pyautogui.dragTo(x2, y2, duration=0.4)
            return {"ok": True, "msg": f"Dragged to ({x2}, {y2})"}

        elif action == "position":
            x, y = pyautogui.position()
            return {"ok": True, "msg": f"Cursor at ({x}, {y})", "x": x, "y": y}

        elif action == "size":
            w, h = pyautogui.size()
            return {"ok": True, "msg": f"Screen: {w}x{h}", "w": w, "h": h}

        else:
            return {"ok": False, "msg": f"Unknown command: {action}"}

    except Exception as e:
        return {"ok": False, "msg": str(e)}
