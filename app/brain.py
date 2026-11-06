import os
import json
import datetime
import time
import urllib.request
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from jarvis_core_personas import JARVIS_CORE_PERSONAS
from jarvis_regional_personas import JARVIS_REGIONAL_PERSONAS

_ALL_PERSONAS = {**JARVIS_CORE_PERSONAS, **JARVIS_REGIONAL_PERSONAS}
from toolkit import run_tool, extract_tools
import project_health

OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")

OLLAMA_REQUEST_TIMEOUT_S = 45   # httpx-level: connect/read ceiling per call
OLLAMA_STREAM_DEADLINE_S = 60   # wall-clock ceiling across a whole streamed reply

# The system prompt alone (tool list + arg examples + routing rules) runs
# ~1500 tokens on its own. The old num_ctx=2048 left well under 1000 tokens
# for actual conversation + tool results — on a model this size, that's
# enough headroom to silently truncate the system prompt itself (llama.cpp
# drops from the start of context on overflow), which reads as the model
# randomly "forgetting" tool-call formatting mid-conversation. Every Ollama
# model in use here supports much larger context; this costs more RAM, not
# correctness.
OLLAMA_NUM_CTX = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

# BRAIN_PROVIDER picks which backend JarvisBrain talks to — "ollama" (free,
# local, default) or "anthropic" (Claude, needs ANTHROPIC_API_KEY). Both
# backends stay wired up; this just switches which one a given process uses.
BRAIN_PROVIDER = os.environ.get("BRAIN_PROVIDER", "ollama").strip().lower()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")

try:
    import ollama as _ollama
    # A bare `ollama.chat(...)` module-level call has no timeout at all — a
    # request that never gets a byte back (queued behind other Ollama
    # traffic, or genuinely stuck) hangs the Flask worker thread forever.
    # That's not hypothetical: it's exactly what let a hung request run long
    # enough to degenerate into garbage tokens and trigger a real EMAIL tool
    # call. A dedicated Client with an httpx timeout bounds it.
    _ollama_client = _ollama.Client(host=OLLAMA_URL, timeout=OLLAMA_REQUEST_TIMEOUT_S)
    _OLLAMA_PKG = True
except ImportError:
    _OLLAMA_PKG = False

try:
    import anthropic as _anthropic
    _anthropic_client = _anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    _ANTHROPIC_PKG = _anthropic_client is not None
except ImportError:
    _anthropic_client = None
    _ANTHROPIC_PKG = False


def _split_system(ctx):
    """Claude takes `system` as its own top-level param, not a messages[0]
    entry, and requires the first message to be `user`. JarvisBrain's
    4-message memory window can start on `assistant` after trimming."""
    system = ctx[0]["content"] if ctx and ctx[0]["role"] == "system" else ""
    rest = [m for m in ctx if m["role"] != "system"]
    if rest and rest[0]["role"] == "assistant":
        rest = rest[1:]
    return system, rest

def _build_system():
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    _persona_key = os.environ.get("JARVIS_PERSONA", "").strip()
    _persona_block = (
        f"\n\nPERSONA — adopt this voice for all replies:\n{_ALL_PERSONAS[_persona_key]}"
        if _persona_key and _persona_key in _ALL_PERSONAS else ""
    )
    return f"""You are J.A.R.V.I.S. — smart, warm, fast, personal assistant on a Mac. Talk like a close friend: casual, direct, contractions always. 1-2 sentences unless more is needed. Reply in the user's language; tool args stay in English.

Today is {today}. For anything time-sensitive (news, prices, sports, weather, "latest"/"current"/"today") ALWAYS use a lookup tool — never answer from memory alone.

Write tool calls on their own line using the real tool name from the TOOLS list below — e.g. [WEATHER: NYC] for a tool with an arg, [TIME] for one without. "TOOL" is never itself a real tool name — do not write [TOOL: ...]. The instant you write a tool call, stop — no text before or after it in that reply. You have not seen the result yet, so guessing one (even a calculation) produces a wrong or duplicate answer once the real result comes back next turn.

TOOLS — SEARCH NEWS SHELL OPEN QUIT NOTIFY SPEAK SPEAK_SLOW VOLUME BRIGHTNESS TYPE HOTKEY TIMER CALC CLIPBOARD SYSINFO SCREENSHOT WEATHER TIME STOCKS DEFINE CONVERT WIKIPEDIA SPORTS MOVIES CRYPTO CURRENCY HOROSCOPE GITHUB NEWS_CATEGORY JOKE QUOTE TRIVIA FACT WORD_OF_DAY IP PING SPEEDTEST YOUTUBE MAPS BROWSER_NAV BROWSER_JS BROWSER_DO FLIGHT RECIPE EMAIL CONTACTS MESSAGES FACETIME WHATSAPP TRANSLATE TRANSLATE_AUTO FILE ZIP UNZIP HASH WORD_COUNT QR UUID BASE64 JSON_FORMAT URL_ENCODE PARSE_DATE DIFF ENCODE TEXT_CASE COLOR CALENDAR REMIND NOTES MEMORY_SET MEMORY_GET MEMORY_LIST SPOTIFY MUSIC RADIO PODCAST LOCK SLEEP DARKMODE WIFI BLUETOOTH NIGHTSHIFT FOCUS FOCUS_MODE EMPTY_TRASH PROCESS BATTERY_SAVER APP_LIST SHORTCUT SCREEN_RECORD AIRPLAY ALARM POMODORO STOPWATCH COUNTDOWN REMIND_AT RANDOM SUMMARIZE CLIPBOARD_HIST CLIPBOARD_CLEAR PROJECT_STATUS WEBSCOUT AXIS CLEANUP_SCAN CLEANUP_DELETE DRAFT_OUTREACH SEND_DRAFT LIST_DRAFTS DISCARD_DRAFT CALL_ME

Arg examples: [EMAIL: to|Subject|Body] [SPOTIFY: play X] [ALARM: HH:MM msg] [REMIND_AT: N min|msg] [TEXT_CASE: upper hello world] (mode is one word — upper/lower/title/camel/snake/kebab — then a space, then the text; never a pipe) [CONVERT: 100F to C] [CURRENCY: 100 USD to EUR] [FILE: read|write|list|append path] [WEBSCOUT: niche city] [CLEANUP_DELETE: 3,7,12] [MEMORY_SET: key=value] [CRYPTO: bitcoin] (coin name/symbol required — never call CRYPTO with no arg) [DRAFT_OUTREACH: email|person@example.com|Subject|One short sentence] [SEND_DRAFT: 2] [TRANSLATE: Hello world | Spanish] (always just text | target language — source is auto-detected, never ask the user for the source language) [DIFF: old text ||| new text] (triple pipe, not single) [DARKMODE: on] (arg is on/off when the user names one; omit the arg only for a bare "toggle dark mode") [NOTES: the note text itself] (no "add"/"create" prefix, no pipe — just the content; [NOTES: list] to read recent notes) [QUIT: Calculator] (the exact app name is required — never call QUIT with no arg) [MEMORY_GET: favorite_color] (when the user asks about something personal they may have told you before — "what's my X", "do you remember Y" — try MEMORY_GET with your best-guess key before saying you don't know; [MEMORY_LIST] to browse everything stored if the key guess comes back empty)

Common mix-ups — pick the right one, and never invent a tool name not in the TOOLS list above: DEFINE = meaning of a word the user named (never WORD_OF_DAY, which is only a random daily word with no arg). CRYPTO = Bitcoin/crypto prices (never CURRENCY, which only converts between fiat currencies like USD/EUR). TIMER = "set a timer for N minutes" (never TIME, which only reads the clock).

CLEANUP_SCAN takes ~30-90s (real disk scan) — mention that briefly rather than going quiet. CLEANUP_DELETE only ever removes IDs from the most recent CLEANUP_SCAN, nothing else — only call it with IDs the user explicitly picked after seeing a scan result, never on your own judgment or with guessed/made-up numbers.

SELF-EVOLUTION: [SELF_UPGRADE: description] generate+install a new tool, push to GitHub. [SELF_HEAL] scan+fix broken tools, push. [SELF_PUSH: commit message] commit+push current changes.

BUSINESS OUTREACH: DRAFT_OUTREACH stages an email/iMessage to someone else — it never sends. Keep the body to ONE short sentence — it's a draft to review and refine, not a finished message, and the whole tool call must fit on one line. LIST_DRAFTS (no arg) shows everything currently staged — use it whenever the user asks what's pending, or if they refer to "the draft" without a number. DISCARD_DRAFT: id drops a staged draft without sending it — use when the user says to cancel/scrap/never mind a draft. SEND_DRAFT fires a staged draft by id, exactly like CLEANUP_DELETE: only ever call it with an id the user explicitly named after seeing a DRAFT_OUTREACH or LIST_DRAFTS result, never a guessed number, never in response to a bare "yes"/"send it" without a number attached. Unsolicited or cold contact with someone new — anything that is JARVIS initiating the conversation rather than relaying the user's own words — always goes through DRAFT_OUTREACH, never directly through EMAIL/MESSAGES. The user's own specific, direct instruction to send something right now ("email Sarah the invoice", "text mom I'm running late") is dictation, not outreach — that still uses EMAIL/MESSAGES directly, unchanged.

ROUTING RULES (follow exactly):
- Spotify: ALWAYS [SPOTIFY:]. "play my X"→[SPOTIFY: my X], "play X"→[SPOTIFY: play X], "shuffle X"→[SPOTIFY: shuffle playlist X], "skip"→[SPOTIFY: next], "what's playing"→[SPOTIFY: current]. Never BROWSER_NAV for Spotify.
- Weather→WEATHER not SEARCH. Time→TIME not SHELL. Stock/crypto→STOCKS/CRYPTO. Currency→CURRENCY.
- "the trading bot"/"my money"/"P&L"/AXIS/is it making money → answer from FINANCIAL CONTEXT if present, never SEARCH. Never confuse with unrelated Mac background jobs mentioned in BACKGROUND SYSTEM NOTES.
- Lock/Sleep/Darkmode/Wifi/Bluetooth → their own tool, never SHELL.
- Remind in N min→REMIND_AT. Alarm→ALARM. Countdown→COUNTDOWN. Pomodoro→POMODORO.
- Joke/Quote/Trivia/Fact/coin flip → their own tool, not SEARCH.
- Unknown facts/news→SEARCH. Wikipedia depth→WIKIPEDIA. News by topic→NEWS_CATEGORY.
- "call me"/"ring my phone"/"dial me"/"give me a call"→[CALL_ME] (no arg). This is real — a Twilio phone call actually rings the user's phone with a live voice conversation. Never say you can't make phone calls; you can, via this tool. If it's not configured yet, the tool result will say so — relay that, don't guess or apologize preemptively.
- "add/give yourself the ability to X"→SELF_UPGRADE: X. "heal/fix your tools"→SELF_HEAL. "push/commit/save your changes"→SELF_PUSH: message{_persona_block}"""


class JarvisBrain:
    def __init__(self):
        self.model = OLLAMA_MODEL
        self.provider = BRAIN_PROVIDER if BRAIN_PROVIDER == "anthropic" and _ANTHROPIC_PKG else "ollama"
        self.memory = []
        self._mentioned_issues = set()

    # Tools that are pure actions — no synthesis needed after execution
    _ACTION_TOOLS = frozenset({
        "OPEN","QUIT","SPEAK","SPEAK_SLOW","VOLUME","BRIGHTNESS","TIMER",
        "CLIPBOARD","NOTIFY","HOTKEY","TYPE","KEY","LOCK","SLEEP","DARKMODE",
        "WIFI","BLUETOOTH","NIGHTSHIFT","FOCUS","FOCUS_MODE","EMPTY_TRASH",
        "MESSAGES","FACETIME","WHATSAPP","CALL_ME","RADIO","PODCAST","SHORTCUT",
        "SCREEN_RECORD","CLIPBOARD_CLEAR","CLIPBOARD_HIST","ALARM","POMODORO",
        "STOPWATCH","COUNTDOWN","REMIND_AT","REMIND","QR","UUID","YOUTUBE",
        "MAPS","SPOTIFY","MUSIC","ZIP","UNZIP","PROCESS","BATTERY_SAVER",
        "AIRPLAY","VISION_DO","BROWSER_DO","BROWSER_NAV","BROWSER_JS",
        "NOTES","CALENDAR","EMAIL","SHELL","MEMORY_SET","MEMORY_GET","MEMORY_LIST",
        "SCREENSHOT","FLIGHT","RECIPE","PODCAST","RANDOM","AIRPLAY",
    })

    def _chat(self, messages, stream=False, tokens=100):
        """Raw HTTP fallback — used when ollama package unavailable."""
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "think": False,
            "options": {
                "num_predict": tokens,
                "temperature": 0,
                "num_ctx": OLLAMA_NUM_CTX,
            },
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        return urllib.request.urlopen(req, timeout=60)

    def _anthropic_complete(self, messages, tokens=100):
        system, msgs = _split_system(messages)
        try:
            resp = _anthropic_client.messages.create(
                model=ANTHROPIC_MODEL, max_tokens=tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=msgs,
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        except Exception as e:
            return f"Neural Error: {e}"

    def _anthropic_stream(self, ctx, deadline, tokens):
        """Streaming helper shared by stream_think's pass 1 and pass 3 —
        yields {"chunk": ...} and returns the accumulated text via `yield from`."""
        system, msgs = _split_system(ctx)
        full = ""
        try:
            with _anthropic_client.messages.stream(
                model=ANTHROPIC_MODEL, max_tokens=tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=msgs,
            ) as stream:
                for text in stream.text_stream:
                    if time.monotonic() > deadline:
                        yield {"chunk": "\n[response taking too long — stopped]"}
                        return ""
                    full += text
                    yield {"chunk": text}
        except Exception as e:
            yield {"chunk": f"Neural Error: {e}"}
            return ""
        return full

    def _complete(self, messages, tokens=100):
        """Single non-streaming completion — uses ollama package when available."""
        if self.provider == "anthropic":
            return self._anthropic_complete(messages, tokens)
        if _OLLAMA_PKG:
            try:
                resp = _ollama_client.chat(
                    model=self.model, messages=messages, think=False,
                    options={"num_predict": tokens, "temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
                )
                return resp["message"]["content"]
            except Exception as e:
                return f"Neural Error: {e}"
        try:
            with self._chat(messages, stream=False, tokens=tokens) as r:
                data = json.loads(r.read())
            return data["message"]["content"]
        except Exception as e:
            return f"Neural Error: {e}"

    def _health_note(self) -> str:
        """New (not-yet-mentioned) project/process health issues, as a plain
        conversational note — explicitly NOT a tool-call instruction, to
        avoid it reading as related to the SELF-EVOLUTION tools above it.
        Resolved issues drop out of _mentioned_issues so a recurrence gets
        flagged again."""
        issues = project_health.get_issues()
        self._mentioned_issues &= set(issues)
        new_issues = [i for i in issues if i not in self._mentioned_issues]
        if not new_issues:
            return ""
        self._mentioned_issues.update(new_issues)
        bullets = "; ".join(new_issues)
        return (
            "\n\nBACKGROUND SYSTEM NOTES (plain conversation only, no tool "
            f"call, no action needed from you): {bullets}. These are Mac "
            "background processes/services — NONE of this is about money, "
            "trading, or performance (see Financial Context below for that, "
            "if present). Mention in passing only if it naturally fits what "
            "the user is asking — otherwise ignore this entirely."
        )

    def _build_context(self, user_input):
        self.memory.append({"role": "user", "content": user_input})
        if len(self.memory) > 4:
            self.memory = self.memory[-4:]
        system = _build_system() + self._health_note()
        return [{"role": "system", "content": system}] + self.memory

    def think(self, user_input, financial_context=""):
        ctx = _inject_financial(self._build_context(user_input), financial_context)
        response = self._complete(ctx)
        self.memory.append({"role": "assistant", "content": response})
        return response

    def stream_think(self, user_input, financial_context=""):
        """
        Yields: {"chunk": "..."} | {"tool": name, "arg": arg, "result": result}
        Two-pass: stream response → execute tools → optional fast synthesis for info tools.
        """
        ctx = _inject_financial(self._build_context(user_input), financial_context)

        # ── Pass 1: stream the model response ────────────────────────────────
        full_response = ""
        deadline = time.monotonic() + OLLAMA_STREAM_DEADLINE_S
        try:
            if self.provider == "anthropic":
                full_response = yield from self._anthropic_stream(ctx, deadline, tokens=80)
            elif _OLLAMA_PKG:
                stream = _ollama_client.chat(
                    model=self.model, messages=ctx, stream=True, think=False,
                    options={"num_predict": 80, "temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
                )
                for chunk_obj in stream:
                    if time.monotonic() > deadline:
                        yield {"chunk": "\n[response taking too long — stopped]"}
                        full_response = ""  # don't hand truncated/degenerate output to Pass 2 tool execution
                        break
                    chunk = chunk_obj.get("message", {}).get("content", "")
                    if chunk:
                        full_response += chunk
                        yield {"chunk": chunk}
            else:
                with self._chat(ctx, stream=True, tokens=100) as r:
                    for raw in r:
                        line = raw.decode().strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            chunk = d.get("message", {}).get("content", "")
                            if chunk:
                                full_response += chunk
                                yield {"chunk": chunk}
                            if d.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield {"chunk": f"Neural Error: {e}"}
            return

        self.memory.append({"role": "assistant", "content": full_response})

        # ── Pass 2: execute tools ─────────────────────────────────────────────
        tools = extract_tools(full_response)
        if not tools:
            return

        tool_context = []
        action_results = []
        needs_synthesis = False
        for name, arg in tools:
            if name == "BROWSER_DO":
                yield {"chunk": "\n[Vision agent running...]\n"}
                result = run_tool("VISION_DO", arg)
            else:
                result = run_tool(name, arg)
            yield {"tool": name, "arg": arg, "result": result}
            if result == f"Unknown tool: {name}":
                # The model hallucinated a tool name that doesn't exist (e.g.
                # [JOKER] instead of [JOKE]) — there's nothing to synthesize,
                # and narrating the failure back through the model produces
                # confused, fourth-wall-breaking text ("I need to define the
                # JOKER tool...") tacked onto an otherwise complete Pass 1
                # reply. Drop it silently instead.
                continue
            tool_context.append(f"[{name}: {arg}] → {result}")
            if name not in self._ACTION_TOOLS:
                needs_synthesis = True
            else:
                action_results.append(result)

        if not tool_context:
            return

        # ── Pass 3: synthesis — only for real info-tool results, capped at 80 tokens ─
        if not needs_synthesis:
            # Pure action tools (VOLUME, TIMER, QR, SPOTIFY, ...) — the tool's
            # own result string IS the reply. Previously this just returned
            # here with nothing yielded, so the user saw/heard the generic
            # "didn't get a clear answer" fallback even though the action
            # actually succeeded. Surface the real result instead of an LLM
            # synthesis call (which these don't need).
            reply = " ".join(r for r in action_results if r)
            if reply:
                yield {"chunk": reply}
            return

        synthesis_msgs = ctx + [
            {"role": "assistant", "content": full_response},
            {"role": "user", "content":
                "Results:\n" + "\n".join(tool_context) +
                "\n\nOne concise sentence using these results — plain prose only, "
                "no tool-call brackets, even if a result is an error."}
        ]
        yield {"chunk": "\n\n"}
        synthesis = ""
        synthesis_deadline = time.monotonic() + OLLAMA_STREAM_DEADLINE_S
        try:
            if self.provider == "anthropic":
                synthesis = yield from self._anthropic_stream(synthesis_msgs, synthesis_deadline, tokens=60)
            elif _OLLAMA_PKG:
                stream = _ollama_client.chat(
                    model=self.model, messages=synthesis_msgs, stream=True, think=False,
                    options={"num_predict": 60, "temperature": 0, "num_ctx": OLLAMA_NUM_CTX},
                )
                for chunk_obj in stream:
                    if time.monotonic() > synthesis_deadline:
                        break
                    chunk = chunk_obj.get("message", {}).get("content", "")
                    if chunk:
                        synthesis += chunk
                        yield {"chunk": chunk}
            else:
                with self._chat(synthesis_msgs, stream=True, tokens=80) as r:
                    for raw in r:
                        line = raw.decode().strip()
                        if not line:
                            continue
                        try:
                            d = json.loads(line)
                            chunk = d.get("message", {}).get("content", "")
                            if chunk:
                                synthesis += chunk
                                yield {"chunk": chunk}
                            if d.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue
        except Exception as e:
            yield {"chunk": f"\nError: {e}"}

        self.memory.append({"role": "assistant", "content": synthesis})



def _inject_financial(ctx, financial_context):
    if not financial_context:
        return ctx
    injected = list(ctx)
    injected[0] = {
        "role": "system",
        "content": ctx[0]["content"] + (
            "\n\nFINANCIAL CONTEXT — real, live AXIS trading bot data. This is "
            "the ONLY money/trading/performance data available; if asked about "
            "money, trading, or performance and it's not answered here, say you "
            "don't have that data rather than guessing or searching:\n"
            f"{financial_context}"
        )
    }
    return injected
