"""orchestra.resume — the keystroke a limit-stuck agent is waiting for.

A limit-stuck agent needs exactly one keystroke ("continue") typed at it once
its limit resets — but resets land at 3am, or days out on a weekly cap.
Arming a schedule hands that keystroke to the board: at the armed time it
verifies the limit really lifted (re-arming for the next reset if not), then
types the resume message into the session's own terminal — or, when no
terminal can be scripted (Cursor/VS Code, or the window is gone), relaunches
the conversation in a fleet tmux session via `claude --resume`.

Schedules survive both the browser and this server: every mutation is
persisted to resume.schedule.json, and pending entries whose time passed
while the server was down fire on the first loop pass after boot.

Unattended acting is the reason this module is paranoid where the manual
buttons are not. It never borrows another session's terminal — a 'continue'
typed at the wrong agent while nobody is watching is an injected instruction.
And the tmux fallback believes nothing it reads off the screen: a fat
transcript makes the reopened CLI reload and auto-compact for minutes, a
message pasted into either phase vanishes, and the bare composer it leaves
behind reads as delivered. So the only accepted receipt is the session file
itself gaining the message.

Top of the act layer, alongside finish: it imports observe (observer,
gitrepo, transcripts, limits) and act (terminal, dispatch), and nothing
imports it back except `server`. RESUME_STATE is rebound at runtime (tests
point it at a temp file), so it is deliberately NOT re-exported by the
facade — reach it as `resume.RESUME_STATE`.
"""

import json
import os
import re
import shlex
import threading
import time

from . import (config, shell, gitrepo, transcripts, limits, observer,
               terminal, dispatch)


# -------------------------------------------------------- scheduled resumes

RESUME_STATE = config.HERE / "resume.schedule.json"
RESUME_POLL_S = 5.0
RESUME_MAX_ATTEMPTS = 10       # re-arms on "still limited" before giving up
_resumes = {}                  # "worktree|sid" -> schedule dict
_resumes_lock = threading.Lock()


def save_resumes():
    with _resumes_lock:
        snap = json.dumps({"schedules": list(_resumes.values())}, indent=1)
    try:
        RESUME_STATE.write_text(snap + "\n")
    except OSError:
        pass


def load_resumes():
    try:
        data = json.loads(RESUME_STATE.read_text())
    except (OSError, ValueError):
        return
    with _resumes_lock:
        for r in data.get("schedules", []):
            if r.get("worktree") and r.get("sid"):
                _resumes[f"{r['worktree']}|{r['sid']}"] = r


def _resume_set(key, **updates):
    with _resumes_lock:
        r = _resumes.get(key)
        if r:
            r.update(updates)
    save_resumes()


def resume_public():
    """The schedules, shaped for the board (rides along on /api/state)."""
    if config.DEMO:
        return demo_resumes()
    with _resumes_lock:
        return {k: dict(r) for k, r in _resumes.items()}


def demo_resumes():
    return {"orbital-web|demo-limit-1": {
        "worktree": "orbital-web", "sid": "demo-limit-1", "account": "work",
        "model": "opus-4-8", "delay_s": 60, "status": "pending",
        "due_at": time.time() + 7620, "attempts": 0, "message": None}}


def schedule_resume(worktree, sid, account, model=None, delay_s=None,
                    resets_at=None, due_at=None):
    """Arm (or re-arm) an auto-resume. The due time is `due_at` when given
    (the user picked an exact time), else the limit reset + delay. Refuses —
    asking for an exact time — when no reset timestamp is known."""
    if config.DEMO:
        return {"ok": False, "message": "demo mode — nothing to schedule"}
    if not (worktree and sid and account):
        return {"ok": False, "message": "need worktree, sid and account"}
    now = time.time()
    try:
        delay = float(delay_s if delay_s is not None
                      else config.CFG.get("resume_delay_s", 60))
    except (TypeError, ValueError):
        return {"ok": False, "message": "delay must be a number of seconds"}
    delay = max(0.0, min(86400.0, delay))
    if due_at is not None:
        try:
            due = float(due_at)
        except (TypeError, ValueError):
            return {"ok": False, "message": "bad due time"}
    else:
        if resets_at is None:
            # the client normally sends the reset it displays; recompute as a
            # fallback so the API stands on its own
            al = limits.limits_by_account().get(account) or {}
            resets_at = al.get("resets_at") if al.get("exhausted") else None
            if resets_at is None:
                resets_at = min((sx["resets_at"] for sx in
                                 al.get("scoped_exhausted", [])
                                 if sx.get("resets_at")), default=None)
        try:
            resets_at = float(resets_at) if resets_at is not None else None
        except (TypeError, ValueError):
            resets_at = None
        if resets_at is None:
            return {"ok": False, "need_time": True, "message":
                    "no known reset time for this limit — pick an exact time"}
        due = resets_at + delay
    due = max(now + 5, due)   # a reset already past fires on the next pass
    key = f"{worktree}|{sid}"
    with _resumes_lock:
        _resumes[key] = {
            "worktree": worktree, "sid": sid, "account": account,
            "model": model, "delay_s": delay, "resets_at": resets_at,
            "due_at": due, "created_at": now, "attempts": 0,
            "status": "pending", "message": None}
    save_resumes()
    return {"ok": True, "due_at": due, "message":
            "auto-resume armed for " + time.strftime("%H:%M", time.localtime(due))}


def cancel_resume(worktree, sid):
    key = f"{worktree}|{sid}"
    with _resumes_lock:
        found = _resumes.pop(key, None)
    save_resumes()
    return {"ok": bool(found), "message":
            "auto-resume disarmed" if found else "nothing armed for this session"}


def _session_on_board(state, worktree, sid):
    """(session, its own live proc) for a schedule key, from board state."""
    card = next((w for w in state["worktrees"] if w["name"] == worktree), None)
    if not card:
        return None, None
    s = next((x for x in card["sessions"] if x.get("sid") == sid), None)
    proc = None
    if s and s.get("pid"):
        proc = next((p for p in card["live_procs"] if p["pid"] == s["pid"]), None)
    return s, proc


RESUME_READY_S = 420.0   # --resume on a fat session auto-compacts for minutes


def _wait_composer_idle(name, timeout_s):
    """Block until the reopened CLI can provably receive input: the composer
    idle on two consecutive looks. One look lies — the CLI idles for a beat
    between finishing its reload and starting the auto-compact."""
    deadline = time.time() + timeout_s
    streak = 0
    while time.time() < deadline:
        _, pane = shell.run(["tmux", "-L", dispatch.FLEET_SOCK,
                             "capture-pane", "-p", "-t", name])
        streak = streak + 1 if dispatch.composer_idle(pane) else 0
        if streak >= 2:
            return True
        time.sleep(3)
    return False


def _proven_in_transcript(fp, offset, text, timeout_s=20.0, ident=None):
    """True once the session file gains a user entry carrying `text` beyond
    `offset` — receipt at the source, not read off the screen.

    `offset` is only meaningful while the file it was measured against is still
    the same file, still at least that long. A transcript that gets rotated,
    truncated or replaced (compaction rewrites one) leaves the old offset past
    the new EOF — and seeking past EOF is perfectly legal, so the read returns
    empty, the proof is never found, and the caller re-sends. Unattended that
    costs three sends of real usage for one resume. So verify identity and
    length, and fall back to reading the whole file rather than nothing:
    over-reading can only cost a false positive on text we ourselves just sent,
    while under-reading silently triples the spend."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(fp, "rb") as f:
                st = os.fstat(f.fileno())
                if ident is not None and (st.st_dev, st.st_ino) != ident:
                    start = 0        # different file now — the offset is meaningless
                elif st.st_size < offset:
                    start = 0        # truncated or rewritten — ditto
                else:
                    start = offset
                f.seek(start)
                chunk = f.read()
        except OSError:
            return False
        for line in chunk.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "user":
                continue
            content = (d.get("message") or {}).get("content")
            if isinstance(content, list):
                content = " ".join(x.get("text", "") for x in content
                                   if isinstance(x, dict))
            if isinstance(content, str) and text in content:
                return True
        time.sleep(2)
    return False


def _tmux_resume(worktree, cwd, home, sid):
    """No terminal to type into — reopen the conversation in a fleet tmux
    session (claude --resume <sid>) and send it the resume message there.

    Reopening is the easy half. A fat transcript makes the CLI reload for
    tens of seconds and then auto-compact for minutes, and a message pasted
    into either phase vanishes — while the bare composer it leaves behind
    reads as delivered. So the send waits out reload and compaction, and the
    only accepted receipt is the session file gaining the message; anything
    less retries, then reports failure with the attach command."""
    name = ("resume-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree).strip("-").lower()
            + time.strftime("-%H%M%S"))
    shell_cmd = (f"export CLAUDE_CONFIG_DIR={shlex.quote(str(home))}\n"
                 f"exec claude --dangerously-skip-permissions --resume {shlex.quote(sid)}\n")
    rc, out = shell.run(["tmux", "-L", dispatch.FLEET_SOCK, "new-session", "-d",
                         "-s", name, "-c", cwd, shell_cmd])
    if rc != 0:
        return {"ok": False,
                "message": f"tmux failed: {out or 'is tmux installed?'}"}
    attach = f"tmux -L {dispatch.FLEET_SOCK} attach -t {name}"
    where = f"no scriptable terminal — resumed in tmux · {attach}"
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    msg = config.CFG.get("resume_message", "continue")
    _wait_composer_idle(name, RESUME_READY_S)
    for attempt in range(3):
        if attempt:   # the last paste vanished — let the CLI settle, try again
            _wait_composer_idle(name, 90.0)
        try:
            # identity rides along with the offset: together they are what makes
            # the offset trustworthy on the next read
            stt = fp.stat() if fp else None
            offset = stt.st_size if stt else 0
            ident = (stt.st_dev, stt.st_ino) if stt else None
        except OSError:
            fp, offset, ident = None, 0, None
        sent = dispatch.deliver_text(name, msg)
        if fp and _proven_in_transcript(fp, offset, msg, ident=ident):
            return {"ok": True, "message": where}
        if not fp and sent:
            return {"ok": True, "message":
                    where + " · ⚠ transcript not found — send unproven"}
    return {"ok": False, "message":
            f"reopened in tmux but '{msg}' never reached the conversation — "
            f"attach and type it: {attach}"}


def fire_resume(key):
    """The armed moment. Decision order: already moved on -> done; limit still
    binds -> re-arm for the fresh reset; else type the resume message into the
    session's OWN terminal, or reopen the session in tmux. Unlike the manual
    button, this never borrows another session's terminal — unattended, a
    'continue' typed at the wrong agent is an injected instruction, while the
    tmux fallback targets the sid exactly."""
    with _resumes_lock:
        r = dict(_resumes.get(key) or {})
    if not r or r.get("status") != "pending":
        return
    now = time.time()
    if config.DEMO:
        return _resume_set(key, status="failed", fired_at=now,
                           message="demo mode")
    worktree, sid, account = r["worktree"], r["sid"], r["account"]

    state = observer.cached_state()
    s, proc = _session_on_board(state, worktree, sid)
    if s and s.get("handed_to"):
        return _resume_set(key, status="done", fired_at=now, message=
                           f"work already continued by [{s['handed_to']}] — nothing sent")
    # Mid-something: 'continue' typed at an agent that is working, blocked, or
    # holding a question is an injected instruction, not a resume.
    if s and s["status"] in ("working", "blocked", "needs_input"):
        return _resume_set(key, status="done", fired_at=now, message=
                           f"session is {s['status']} — no resume needed")
    # Everything else is decided on evidence, not on the status string. This
    # used to read `status != "limit" -> done`, which cancelled the very resume
    # it was armed for: the limit join reads a cache only /api/limits populates,
    # so at 3am with no board open a limit-parked session reads WAITING, not
    # LIMIT — and even with a warm cache the flag clears the instant the limit
    # resets, which is precisely when we fire. An idle prompt looks identical
    # whether the agent ran out of juice or finished its turn. A write *after we
    # armed* is the thing that actually proves it moved on under its own steam.
    age = s.get("age_s") if s else None
    if age is not None and now - float(age) > float(r.get("created_at") or 0):
        return _resume_set(key, status="done", fired_at=now, message=
                           f"session moved on since arming ({s['status']}) — nothing sent")

    until = limits._limit_active_until(account, r.get("model"), now)
    if until:
        attempts = int(r.get("attempts", 0)) + 1
        if attempts >= RESUME_MAX_ATTEMPTS:
            return _resume_set(key, status="failed", fired_at=now, message=
                               f"still limited after {attempts} checks — gave up")
        return _resume_set(key, due_at=until + float(r.get("delay_s") or 60),
                           attempts=attempts, message=
                           "still limited — re-armed for the next reset")

    msg = config.CFG.get("resume_message", "continue")
    if proc and proc.get("reachable"):
        res = terminal.send_to_process(proc["pid"], msg)
        if res.get("ok"):
            return _resume_set(key, status="done", fired_at=now,
                               message=f"sent '{msg}' — {res['message']}")
    wt = next((w for w in gitrepo.discover_worktrees() if w["name"] == worktree), None)
    home = next((h for h in transcripts.claude_homes()
                 if config.account_label(h) == account), None)
    if not wt or not home:
        return _resume_set(key, status="failed", fired_at=now, message=
                           "worktree or account no longer known — nothing sent")
    out = _tmux_resume(worktree, (s or {}).get("cwd") or wt["path"], home, sid)
    if out["ok"] and proc:
        # the session's old window survives it — a frozen pre-resume view
        out["message"] += (f" · the old {proc.get('host') or 'terminal'} window"
                           " now shows a stale view — close it, don't type into it")
    return _resume_set(key, status="done" if out["ok"] else "failed",
                       fired_at=now, message=out["message"])


def resume_loop():
    """Daemon: fire due schedules; prune finished ones after a day."""
    while True:
        time.sleep(RESUME_POLL_S)
        now = time.time()
        with _resumes_lock:
            due = [k for k, r in _resumes.items()
                   if r.get("status") == "pending" and r.get("due_at", 0) <= now]
        for k in due:
            try:
                fire_resume(k)
            except Exception as e:   # a broken fire must not kill the loop
                _resume_set(k, status="failed", fired_at=now,
                            message=f"internal error: {e}")
        with _resumes_lock:
            stale = [k for k, r in _resumes.items()
                     if r.get("status") in ("done", "failed")
                     and now - r.get("fired_at", now) > 86400]
            for k in stale:
                del _resumes[k]
        if stale:
            save_resumes()
