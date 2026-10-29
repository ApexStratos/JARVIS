"""
outreach — approval-gated business outreach.

draft() stages an email/iMessage without sending it. send() only ever fires
a draft that is actually present in _pending, addressed by id — the same
"a hallucinated/guessed id can't do anything" shape as disk_cleanup.py's
scan()/delete(). Nothing in this module sends on its own; a draft only goes
out on a second, explicit, human-driven SEND_DRAFT call naming a real id.

EMAIL/MESSAGES in toolkit.py are untouched and still send immediately —
those are for the user's own direct, specific instruction ("email Sarah the
invoice"), which is dictation, not outreach. This module is specifically
for JARVIS initiating contact with someone on its own, which is exactly the
class of action that needs a human in the loop before anything leaves the
building.
"""
import time

_pending: dict[int, dict] = {}
_next_id = 1

_VALID_CHANNELS = ("email", "imessage")


def draft(channel: str, recipient: str, subject: str, body: str, reason: str = "") -> dict:
    global _next_id
    channel = channel.strip().lower()
    if channel not in _VALID_CHANNELS:
        return {"ok": False, "error": f"Unknown channel {channel!r} — use 'email' or 'imessage'."}
    if not recipient.strip():
        return {"ok": False, "error": "No recipient given."}

    did = _next_id
    _next_id += 1
    d = {
        "id": did,
        "channel": channel,
        "recipient": recipient.strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "reason": reason.strip(),
        "ts": time.time(),
    }
    _pending[did] = d
    return {"ok": True, "draft": d}


def get(draft_id: int) -> dict | None:
    return _pending.get(draft_id)


def list_pending() -> list[dict]:
    return list(_pending.values())


def discard(draft_id: int) -> bool:
    return _pending.pop(draft_id, None) is not None


def send(draft_id: int) -> str:
    """Only ever sends a draft present in _pending — an id that isn't there
    (hallucinated, mistyped, or already sent) is rejected, never guessed at.
    The draft is removed whether the send succeeds or fails, so a retry
    requires drafting again rather than silently resending."""
    d = _pending.pop(draft_id, None)
    if d is None:
        return f"No pending draft #{draft_id} — call DRAFT_OUTREACH first, or check the id."

    # Lazy import: toolkit.py's dispatch table imports this module, so this
    # module importing toolkit at module scope would be circular. By the
    # time send() actually runs, both modules are already fully loaded.
    import toolkit

    if d["channel"] == "email":
        cmd = f'{d["recipient"]}|{d["subject"]}|{d["body"]}'
        result = toolkit._tool_email(cmd)
    else:  # imessage
        cmd = f'{d["recipient"]}|{d["body"]}'
        result = toolkit._tool_messages(cmd)

    return f"Sent draft #{draft_id}: {result}"
