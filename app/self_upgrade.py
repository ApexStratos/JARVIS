"""
JARVIS Self-Upgrade / Self-Heal / Self-Push system.
Lets JARVIS write new tools, fix broken ones, and deploy to Finder + GitHub autonomously.
"""
import os, re, json, subprocess, urllib.request, sys
from pathlib import Path

REPO    = Path(__file__).parent.parent
TOOLKIT = REPO / "app" / "toolkit.py"
BRAIN   = REPO / "app" / "brain.py"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
MODEL      = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")

# Files that are safe to auto-stage and push
_SAFE_FILES = ["app/toolkit.py", "app/brain.py", "app/self_upgrade.py"]

# self_heal() used to deny-list the tools it wouldn't actually execute with
# "test" — but an incomplete deny-list is exactly how a bug once let it run
# a real system-settings AppleScript, fire a real "which"/subprocess.call on
# arbitrary input in an auto-generated tool, and pop 100+ real browser tabs
# (SEARCH/YOUTUBE/MAPS/FLIGHT/RECIPE/SPEEDTEST all shell out to macOS `open`).
# Flipped to an allow-list: only tools verified to be pure computation or a
# read-only network GET are ever actually invoked. Everything else — new
# auto-generated tools included — defaults to "not executed, assumed fine"
# rather than "executed unless someone remembered to list it as dangerous."
_SAFE_TO_EXECUTE = frozenset({
    "CALC","CONVERT","CURRENCY","TEXT_CASE","HASH","WORD_COUNT","UUID",
    "BASE64","JSON_FORMAT","URL_ENCODE","PARSE_DATE","DIFF","ENCODE","COLOR",
    "TRANSLATE","TRANSLATE_AUTO","TIME","SYSINFO","IP","PING","APP_LIST",
    "DEFINE","WIKIPEDIA","CRYPTO","STOCKS","WEATHER","HOROSCOPE","JOKE",
    "QUOTE","TRIVIA","FACT","WORD_OF_DAY","NEWS","SPORTS","GITHUB",
    "MEMORY_LIST","PROJECT_STATUS",
})


# ── helpers ───────────────────────────────────────────────────────────────────

def _ollama(prompt, tokens=500):
    payload = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_predict": tokens, "temperature": 0.2, "num_ctx": 3072},
    }).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())["message"]["content"].strip()


def _strip_fences(code):
    """Remove ```python and ``` markdown fences."""
    return re.sub(r"^```(?:python)?\n?|^```\s*$", "", code, flags=re.MULTILINE).strip()


def _validate_python(code):
    try:
        compile(code, "<jarvis-generated>", "exec")
        return True, None
    except SyntaxError as e:
        return False, str(e)


def _git(args, cwd=None):
    return subprocess.run(
        ["git"] + args, capture_output=True, text=True, cwd=str(cwd or REPO)
    )


# ── self_push ─────────────────────────────────────────────────────────────────

def self_push(message="JARVIS self-upgrade"):
    """Stage safe JARVIS source files, commit, and push to GitHub."""
    staged = []
    for rel in _SAFE_FILES:
        path = REPO / rel
        if path.exists():
            r = _git(["add", str(path)])
            if r.returncode == 0:
                staged.append(rel)

    if not staged:
        return "Nothing new to stage."

    commit_msg = (
        f"{message}\n\n"
        "Co-Authored-By: JARVIS Self-Upgrade <jarvis@local>\n"
        "Claude-Session: https://claude.ai/code/session_01KDMEMn4GT8sU9Zqu8UoxjH"
    )
    r = _git(["commit", "-m", commit_msg])
    if r.returncode != 0:
        out = r.stdout + r.stderr
        if "nothing to commit" in out:
            return "Nothing new to commit — already up to date."
        return f"Commit failed: {out[:200]}"

    short = _git(["rev-parse", "--short", "HEAD"]).stdout.strip()

    # Pushing to the real GitHub repo autonomously, from a tool that a single
    # misheard voice command can trigger, is exactly how this repo ended up
    # with a string of junk "self-upgrade: add [TESTTOOL]" commits — require
    # an explicit opt-in rather than defaulting to "always push."
    if os.environ.get("JARVIS_ALLOW_SELF_PUSH", "").strip().lower() not in ("1", "true", "yes"):
        return (f"Committed locally ({short}) but did not push — set "
                "JARVIS_ALLOW_SELF_PUSH=1 in .env to let self-upgrade/self-heal "
                "push to GitHub on its own.")

    r = _git(["push", "origin", "main"])
    if r.returncode != 0:
        return f"Push failed: {r.stderr[:200]}"

    return f"Pushed to GitHub ({short}): {message}"


# ── self_upgrade ──────────────────────────────────────────────────────────────

def self_upgrade(description):
    """
    Generate a new JARVIS tool from a natural language description,
    install it into toolkit.py + brain.py, write to disk, and push to GitHub.
    """
    if not description.strip():
        return "Describe what the new tool should do."

    # 1. Derive tool name
    name_prompt = (
        f'Tool description: "{description}"\n'
        "Reply with ONLY an UPPER_SNAKE_CASE tool name (1-3 words, no spaces). "
        "Examples: CHECK_WEBSITE, FILE_SEARCH, MORSE_CODE. Nothing else."
    )
    raw_name = _ollama(name_prompt, tokens=15).upper()
    tool_name = re.sub(r"[^A-Z_]", "", raw_name)[:30]
    if not tool_name:
        return "Could not derive a tool name — try a more specific description."

    toolkit_text = TOOLKIT.read_text()
    if f'name == "{tool_name}"' in toolkit_text:
        return f"Tool [{tool_name}] already exists."

    # 2. Generate the function
    func_prompt = (
        f"Write a Python function for a macOS JARVIS assistant.\n\n"
        f"Tool name: {tool_name}\n"
        f"Description: {description}\n\n"
        "Rules:\n"
        f"- Function signature: def _tool_{tool_name.lower()}(arg: str) -> str\n"
        "- arg is the user's argument string (may be empty)\n"
        "- Return a plain string result\n"
        "- Use only Python stdlib: os, subprocess, json, re, time, urllib.request, pathlib\n"
        "- Wrap any non-stdlib import in try/except ImportError\n"
        "- Handle all exceptions — return an error string, never raise\n"
        "- Max 35 lines, no markdown fences, no explanation\n\n"
        "Write ONLY the function:"
    )
    func_code = _strip_fences(_ollama(func_prompt, tokens=400))

    ok, err = _validate_python(func_code)
    if not ok:
        return f"Generated code has syntax error ({err}) — upgrade aborted. Try rephrasing the description."

    # 3. Inject dispatch entry into run_tool
    dispatch_line = f'    elif name == "{tool_name}":   return _tool_{tool_name.lower()}(arg.strip())'
    toolkit_text = toolkit_text.replace(
        '    return f"Unknown tool: {name}"',
        f'{dispatch_line}\n\n    return f"Unknown tool: {{name}}"',
    )

    # 4. Append function to end of toolkit.py
    toolkit_text = toolkit_text.rstrip() + f"\n\n\n# auto-generated: {tool_name}\n{func_code}\n"
    TOOLKIT.write_text(toolkit_text)

    # 5. Add one-liner to brain.py system prompt (before routing rules)
    brain_text = BRAIN.read_text()
    entry = f"[{tool_name}: arg]  {description[:60]}"
    brain_text = brain_text.replace(
        "ROUTING RULES",
        f"{entry}\n\nROUTING RULES",
    )
    BRAIN.write_text(brain_text)

    # 6. Push to GitHub
    push_result = self_push(f"self-upgrade: add [{tool_name}] — {description[:55]}")

    return (
        f"New tool installed: [{tool_name}]\n"
        f"Description: {description}\n"
        f"File: app/toolkit.py (function + dispatch)\n"
        f"Push: {push_result}"
    )


# ── self_heal ─────────────────────────────────────────────────────────────────

def self_heal():
    """
    Test every tool in the dispatch, identify errors, use Ollama to fix them,
    write fixes to toolkit.py, and push to GitHub.
    """
    # Import toolkit fresh in subprocess to avoid stale state
    sys.path.insert(0, str(REPO / "app"))
    try:
        import importlib
        import toolkit as tk
        importlib.reload(tk)
        run_tool = tk.run_tool
    except Exception as e:
        return f"Cannot load toolkit: {e}"

    toolkit_text = TOOLKIT.read_text()
    tool_names = re.findall(r'(?:if|elif) name == "([A-Z_]+)"', toolkit_text)

    broken = []
    skipped = 0
    for name in tool_names:
        if name not in _SAFE_TO_EXECUTE:
            skipped += 1
            continue
        try:
            result = run_tool(name, "test")
            if isinstance(result, str) and result.startswith("Unknown tool"):
                broken.append((name, "dispatch missing"))
        except Exception as e:
            broken.append((name, str(e)[:120]))

    if not broken:
        return (f"Checked {len(tool_names) - skipped} pure/read-only tool(s) — "
                f"no issues found. {skipped} tool(s) with real-world side "
                "effects (app launches, system settings, file writes, git "
                "push, etc.) were left untouched, not executed.")

    report = [f"Found {len(broken)} broken tool(s): {', '.join(n for n, _ in broken)}"]
    fixed = []

    for name, error in broken[:6]:
        # Extract the broken function body
        func_name = f"_tool_{name.lower()}"
        match = re.search(
            rf"(def {re.escape(func_name)}\(.*?)(?=\ndef |\Z)",
            toolkit_text, re.DOTALL,
        )
        if not match:
            report.append(f"  [{name}] — function not found, skipping")
            continue

        broken_func = match.group(1).strip()
        fix_prompt = (
            f"Fix this broken Python function. Error encountered: {error}\n\n"
            f"{broken_func}\n\n"
            "Return ONLY the corrected function — no markdown, no explanation."
        )
        fixed_code = _strip_fences(_ollama(fix_prompt, tokens=350))

        ok, err = _validate_python(fixed_code)
        if not ok:
            report.append(f"  [{name}] — AI fix had syntax error ({err}), skipped")
            continue

        toolkit_text = toolkit_text.replace(broken_func, fixed_code)
        fixed.append(name)
        report.append(f"  [{name}] — fixed")

    if fixed:
        TOOLKIT.write_text(toolkit_text)
        push_result = self_push(f"self-heal: fixed {len(fixed)} tool(s): {', '.join(fixed)}")
        report.append(push_result)
    else:
        report.append("No fixes could be applied automatically.")

    return "\n".join(report)
