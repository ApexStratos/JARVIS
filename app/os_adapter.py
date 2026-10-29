"""
os_adapter — platform-branched implementations for system-control tools.

macOS keeps its existing, proven subprocess/AppleScript behavior unchanged.
Windows and Linux get real equivalents where a reliable one exists; where it
doesn't (or the expected utility isn't installed), functions return a clear
"can't do this on your setup" string instead of letting a raw subprocess
FileNotFoundError/CalledProcessError bubble up to the user.

Only verified-on-macOS at build time — the Windows/Linux branches are
written against documented OS APIs/CLIs but haven't been run on real
Windows/Linux hardware. Worth a bug report if one of them is wrong.
"""
import platform
import shutil
import subprocess

OS = platform.system()  # "Darwin" | "Windows" | "Linux"
IS_MAC = OS == "Darwin"
IS_WINDOWS = OS == "Windows"
IS_LINUX = OS == "Linux"


def require_macos(feature: str) -> str | None:
    """Return a clear error string if not on macOS, else None. Use as:
    if (msg := os_adapter.require_macos('iMessage')): return msg"""
    if not IS_MAC:
        return f"{feature} needs macOS — not available on {OS}."
    return None


def _find(cmd: str) -> str:
    """Resolve a command to an absolute path, including /sbin and /usr/sbin
    — launchd's default PATH for this app (see the .plist) doesn't include
    either, so bare names like "ping" or "networksetup" 404 as a background
    service even though they resolve fine in an interactive shell. Falls
    back to the bare name (and lets the OS error normally) if not found."""
    found = shutil.which(cmd)
    if found:
        return found
    for d in ("/sbin", "/usr/sbin"):
        p = f"{d}/{cmd}"
        if shutil.which(p):
            return p
    return cmd


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run(cmd, timeout=5, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# ── Volume ───────────────────────────────────────────────────────────────────

def get_volume() -> str:
    if IS_MAC:
        r = _run(["osascript", "-e", "output volume of (get volume settings)"])
        return f"Current volume: {r.stdout.strip()}%" if r.returncode == 0 else "Couldn't read volume."
    if IS_WINDOWS:
        return "Windows volume level isn't readable without an extra tool — try VOLUME: 50 to set it."
    if IS_LINUX:
        if _which("pactl"):
            r = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
            return f"Current volume: {r.stdout.strip()}" if r.returncode == 0 else "Couldn't read volume."
        if _which("amixer"):
            r = _run(["amixer", "get", "Master"])
            return f"Current volume: {r.stdout.strip()[-80:]}" if r.returncode == 0 else "Couldn't read volume."
        return "No pactl/amixer found to read volume."
    return "Volume status isn't supported on this OS."


def set_volume(pct: int) -> str:
    pct = max(0, min(100, pct))
    if IS_MAC:
        _run(["osascript", "-e", f"set volume output volume {pct}"])
        return f"Volume set to {pct}%."
    if IS_WINDOWS:
        # No volume-set cmdlet ships with Windows by default; nircmd is the
        # common lightweight tool for this if the user has it installed.
        if _which("nircmd"):
            _run(["nircmd", "setsysvolume", str(int(pct / 100 * 65535))])
            return f"Volume set to {pct}%."
        return "Setting an exact volume on Windows needs nircmd installed (not bundled) — mute/unmute isn't supported either way yet."
    if IS_LINUX:
        if _which("pactl"):
            _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
            return f"Volume set to {pct}%."
        if _which("amixer"):
            _run(["amixer", "set", "Master", f"{pct}%"])
            return f"Volume set to {pct}%."
        return "No pactl/amixer found to set volume."
    return "Volume control isn't supported on this OS."


# ── Lock / Sleep ─────────────────────────────────────────────────────────────

def lock_screen() -> str:
    if IS_MAC:
        _run(["osascript", "-e",
              'tell application "System Events" to keystroke "q" using {command down, control down}'])
        return "Locked."
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.user32.LockWorkStation()
            return "Locked."
        except Exception as e:
            return f"Couldn't lock: {e}"
    if IS_LINUX:
        for cmd in (["loginctl", "lock-session"], ["xdg-screensaver", "lock"]):
            if _which(cmd[0]):
                _run(cmd)
                return "Locked."
        return "No loginctl/xdg-screensaver found to lock the screen."
    return "Lock isn't supported on this OS."


def sleep_system() -> str:
    if IS_MAC:
        _run(["pmset", "sleepnow"])
        return "Going to sleep."
    if IS_WINDOWS:
        try:
            import ctypes
            ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
            return "Going to sleep."
        except Exception as e:
            return f"Couldn't sleep: {e}"
    if IS_LINUX:
        if _which("systemctl"):
            _run(["systemctl", "suspend"])
            return "Going to sleep."
        return "No systemctl found to suspend the system."
    return "Sleep isn't supported on this OS."


# ── Dark mode ────────────────────────────────────────────────────────────────

def set_dark_mode(cmd: str) -> str:
    """cmd: on|off|toggle (anything else is treated as toggle)"""
    cmd = (cmd or "").lower().strip()
    if IS_MAC:
        if cmd in ("on", "dark"):
            val, label = "true", "on"
        elif cmd in ("off", "light"):
            val, label = "false", "off"
        else:
            val, label = "not (dark mode of (get current desktop))", "toggled"
        _run(["osascript", "-e",
              f'tell app "System Events" to tell appearance preferences to set dark mode to {val}'])
        return f"Dark mode {label}."
    if IS_WINDOWS:
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
                if cmd in ("on", "dark"):
                    value, label = 0, "on"
                elif cmd in ("off", "light"):
                    value, label = 1, "off"
                else:
                    current, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                    value, label = (1, "off") if current == 0 else (0, "on")
                winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, value)
                winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, value)
            return f"Dark mode {label}."
        except Exception as e:
            return f"Couldn't set dark mode: {e}"
    if IS_LINUX:
        if _which("gsettings"):
            if cmd in ("on", "dark"):
                scheme, label = "prefer-dark", "on"
            elif cmd in ("off", "light"):
                scheme, label = "default", "off"
            else:
                r = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
                scheme, label = ("default", "off") if "dark" in r.stdout else ("prefer-dark", "on")
            _run(["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", scheme])
            return f"Dark mode {label}."
        return "Couldn't detect your desktop environment (gsettings not found) to toggle dark mode."
    return "Dark mode isn't supported on this OS."


# ── WiFi status ──────────────────────────────────────────────────────────────

def wifi_status() -> str:
    if IS_MAC:
        r = _run(["/usr/sbin/networksetup", "-getairportnetwork", "en0"])
        return r.stdout.strip() or "Wi-Fi status unknown."
    if IS_WINDOWS:
        r = _run(["netsh", "wlan", "show", "interfaces"])
        for line in r.stdout.splitlines():
            if line.strip().startswith("SSID") and "BSSID" not in line:
                return f"Connected to {line.split(':', 1)[1].strip()}."
        return "Couldn't determine the current Wi-Fi network."
    if IS_LINUX:
        if _which("nmcli"):
            r = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
            for line in r.stdout.splitlines():
                if line.startswith("yes:"):
                    return f"Connected to {line.split(':', 1)[1]}."
            return "Not connected to Wi-Fi."
        if _which("iwgetid"):
            r = _run(["iwgetid", "-r"])
            return f"Connected to {r.stdout.strip()}." if r.stdout.strip() else "Not connected to Wi-Fi."
        return "No nmcli/iwgetid found to check Wi-Fi status."
    return "Wi-Fi status isn't supported on this OS."


# ── Play a downloaded audio file (mp3/wav) through system audio ────────────────

def play_audio_file(path: str, timeout: int = 120) -> bool:
    try:
        if IS_MAC:
            subprocess.run(["afplay", path], timeout=timeout)
        elif IS_LINUX:
            # aplay only handles WAV; ffplay (already used elsewhere for RADIO)
            # handles mp3/wav alike and is the more likely of the two to be
            # installed given this app's install.sh pulls in ffmpeg.
            if _which("ffplay"):
                subprocess.run(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path], timeout=timeout)
            elif _which("aplay"):
                subprocess.run(["aplay", path], timeout=timeout)
            else:
                return False
        elif IS_WINDOWS:
            subprocess.run(
                ["powershell", "-c", f"(New-Object Media.SoundPlayer '{path}').PlaySync()"],
                timeout=timeout,
            )
        else:
            return False
        return True
    except Exception:
        return False


# ── Local-voice TTS fallback (used only when no cloud TTS engine is configured) ─

def speak_native(text: str, rate: int | None = None) -> bool:
    """Best-effort local TTS. Returns True if it spoke, False if unavailable."""
    if IS_MAC:
        cmd = ["say", "-v", "Samantha"]
        if rate:
            cmd += ["-r", str(rate)]
        cmd.append(text)
        try:
            subprocess.run(cmd, timeout=60)
            return True
        except Exception:
            return False
    try:
        import pyttsx3
        engine = pyttsx3.init()
        if rate:
            engine.setProperty("rate", rate * 1.4)  # pyttsx3's scale runs faster than `say`'s wpm
        engine.say(text)
        engine.runAndWait()
        return True
    except Exception:
        return False


# ── Notifications ────────────────────────────────────────────────────────────

def notify(title: str, message: str, sound: bool = False) -> None:
    """Fire a system notification. Best-effort — never raises."""
    if IS_MAC:
        script = f'display notification "{message}" with title "{title}"'
        if sound:
            script += ' sound name "Glass"'
        try:
            subprocess.run(["osascript", "-e", script], timeout=5)
        except Exception:
            pass
        return
    try:
        from plyer import notification as _plyer_notification
        _plyer_notification.notify(title=title, message=message, timeout=10)
    except Exception:
        pass


# ── Ping ─────────────────────────────────────────────────────────────────────

def ping(host: str, count: int = 4) -> str:
    flag = "-n" if IS_WINDOWS else "-c"
    try:
        r = _run([_find("ping"), flag, str(count), host], timeout=count * 2 + 5)
        return r.stdout.strip() or r.stderr.strip() or "No response."
    except FileNotFoundError:
        return "No ping utility found on this system."
    except Exception as e:
        return f"Ping failed: {e}"


# ── Keyboard modifier key (pyautogui is cross-platform; only the modifier name differs) ─

def modifier_key() -> str:
    return "command" if IS_MAC else "ctrl"


# ── Open a local file with its default application ─────────────────────────

def open_path(path: str) -> None:
    if IS_MAC:
        subprocess.run(["open", path], timeout=3)
    elif IS_WINDOWS:
        import os as _os
        _os.startfile(path)
    else:
        subprocess.run(["xdg-open", path], timeout=3)
