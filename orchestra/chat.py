"""orchestra.chat — the drawer: what you and an agent actually said.

One verb. `read_chat` re-reads a session's transcript from the end and
renders the last few dozen turns as `{role, text, ts}` — the human half of
what `transcripts` already parses for the card. It is a reader, not an
actuator: replying happens through `terminal.send_to_process`, not here.

Bounded like everything else that touches a transcript: 512 KB off the tail,
never the whole file. It reuses the transcripts filters rather than
re-implementing them, so the drawer refuses exactly the machine text a card
does — system reminders, command stubs, tool plumbing — and each turn is
capped at 900 characters so one runaway answer can't own the drawer.
"""

import json

from . import config, transcripts


# ------------------------------------------------------------- chat reader

def read_chat(account, sid, limit=40):
    """Last conversation turns of a session, from its transcript."""
    home = next((h for h in transcripts.claude_homes()
                 if config.account_label(h) == account), None)
    if not home:
        return {"ok": False, "error": f"unknown account {account}"}
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    if not fp:
        return {"ok": False, "error": "transcript not found"}
    msgs = []
    for line in transcripts._read_chunk(fp, 512 * 1024, from_end=True).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):        # a valid-JSON scalar/array line: 42, "s", [1]
            continue
        if e.get("isSidechain") or e.get("isMeta"):
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        c = msg.get("content")
        if e.get("type") == "user":
            texts = [c] if isinstance(c, str) else [
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
            for t in texts:
                if transcripts._real_prompt(t):
                    msgs.append({"role": "you", "text": transcripts._clean(t, 900),
                                 "ts": e.get("timestamp")})
        elif e.get("type") == "assistant" and isinstance(c, list):
            parts = [b["text"] for b in c
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
            if parts:
                msgs.append({"role": "agent", "text": transcripts._clean(" ".join(parts), 900),
                             "ts": e.get("timestamp")})
    return {"ok": True, "messages": msgs[-limit:]}
