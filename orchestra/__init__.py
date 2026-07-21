#!/usr/bin/env python3
"""orchestra — local mission control for parallel Claude Code agents.

Watches your git worktrees, your Claude Code home directories (multi-account
setups included), and live `claude` processes; serves three views on
http://127.0.0.1:4242 — the board (who's working / who needs you / which
worktree is free), the map (real git topology of every branch), and limits
(per-account usage via cclimits) — plus a click-only control plane: chat with
any agent, resume a limit-stuck one when its session limit resets, dispatch
new tmux-hosted agents into free worktrees, and finish a done mission (an
agent lands the branch; the worktree goes free).

Watching is read-only and touches nothing. Acting (chat/resume/dispatch/
finish) happens only on an explicit request — dispatch spends account usage,
and finish hands a closeout brief to an agent that merges and pushes. When
the branch has already landed, finish skips the agent: it parks the worktree
back on the trunk itself (switch + pull — the one provably-safe case where
the board runs git write commands). Zero dependencies — python3 stdlib only.

    python3 -m orchestra --root ~/code
    python3 -m orchestra --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > orchestra.config.json (next to this
script, else cwd) > defaults. See README.md.
"""

import json
import os
import re
import shlex
import threading
import time
from http.server import BaseHTTPRequestHandler

from . import (config, shell, status, gitrepo, procs, transcripts, limits,
               observer, terminal, chat, finish, dispatch)

# ---- public surface (facade). Re-exported so tests, tools and
# tests/characterize.py can keep saying `orchestra.<name>`. DEMO,
# CONFIG_PATH and DISPATCH_LOG are deliberately NOT re-exported: they are
# rebound at runtime, so a facade copy would go stale and `orchestra.DEMO =
# True` would be a patch that lies. Reach them as `orchestra.config.DEMO`.
from .config import CFG, HOME, HERE, load_config, account_label
from .shell import run
from .status import classify_session, closeout_step, card_availability
from .gitrepo import (munge, match_worktree, discover_worktrees, git_info,
                      _base_ref, branch_topology, demo_topology,
                      cached_topology, TOPO_TTL_S, _topo)
from .procs import (claude_processes, pair_sessions_with_procs, shell_children,
                    _pid_cwds, _pid_config_dirs, _host_of, _tmux_pane_map)
from .transcripts import (claude_homes, _read_chunk, _clean, _real_prompt,
                          session_topic, last_assistant_text, find_last_user,
                          parse_session_tail, scan_sessions,
                          TAIL_BYTES, HEAD_BYTES)
from .limits import (cached_limits, account_reserve, _model_remaining,
                     model_candidates, set_reserve, limits_by_account,
                     demo_limits, _limit_active_until, _cclimits_bin,
                     LIMITS_TTL_S, _limits)
from .observer import collect_state, cached_state, demo_state, _cache, STATE_TTL_S
from .terminal import focus_process, send_to_process, _osa_escape
from .chat import read_chat
from .finish import (_park_on_trunk, _reachable, _closeouts, CLOSEOUT_TEXT,
                     SLIM_CLOSEOUT_TEXT, CLOSEOUT_NUDGE_TEXT)
from .dispatch import (start_dispatch, dispatch_status, read_dispatch_log,
                       deliver_text, kickoff_sent, composer_idle,
                       closeout_shell, _pick_defaults, _run_dispatch,
                       _jobs, FLEET_SOCK)


# ------------------------------------------------------------- finish

def start_finish(wt_name):
    """One button, tiered by what's actually left to do:
    live agent -> type a brief at it — the slim one if the branch already
    landed, the full closeout otherwise; everything landed and an agent
    idling -> type /exit; no terminal + landed + clean -> park on the trunk
    right here, no agent; anything else -> launch a one-shot closeout agent
    (headless; frees the card itself, or parks as needs-you if the landing
    doesn't verify)."""
    if config.DEMO:
        return {"ok": False, "message": "demo mode — nothing to finish"}
    wt = next((w for w in gitrepo.discover_worktrees() if w["name"] == wt_name), None)
    if not wt:
        return {"ok": False, "message": f"unknown worktree '{wt_name}'"}
    path, git_root = wt["path"], wt["git"]
    trunk = gitrepo._base_ref(git_root)
    if not trunk:
        return {"ok": False, "message": "no trunk ref found for this repo"}
    shell.run(["git", "fetch", "--quiet", "origin"], cwd=git_root, timeout=30)
    landed = shell.run(["git", "merge-base", "--is-ancestor", "HEAD", trunk],
                       cwd=git_root)[0] == 0
    porcelain = [l for l in shell.run(["git", "status", "--porcelain"],
                                      cwd=git_root)[1].splitlines() if l.strip()]
    mine = [p for p in procs.claude_processes() if p.get("cwd")
            and (p["cwd"] == path or p["cwd"].startswith(path + os.sep))]
    live = next((p for p in mine if finish._reachable(p)), None)
    if live:
        if landed and not porcelain:
            res = terminal.send_to_process(live["pid"], "/exit")
            if res["ok"]:
                finish._closeouts.pop(wt_name, None)
                observer._cache["t"] = 0.0    # button reverts on the next poll
            return {"ok": res["ok"], "mode": "exit", "message":
                    "already landed — sent /exit to close the terminal"
                    if res["ok"] else res["message"]}
        sent = finish._closeouts.get(wt_name)
        if sent:
            # step two (✕ close), but the landing still doesn't verify. What to
            # do isn't a fixed refusal — it depends on THIS worktree's live
            # session. The observed deadlock: the agent finished its closeout
            # but left one dirty file it took for another session's in-flight
            # work; nothing else was live, so ✕ close refused forever while the
            # agent idled forever. classify the session and nudge it out of that.
            left = (f"{len(porcelain)} leftover file(s)" if landed
                    else f"branch not landed on {trunk}")
            files = porcelain[:5]          # ≤5 raw lines, for the agent + UI
            now = time.time()
            sessions = transcripts.scan_sessions([wt], mine, now).get(path, [])
            paired = next((s for s in sessions
                           if s.get("pid") == live["pid"]), None)
            any_working = any(s.get("status") == "working" for s in sessions)
            step = status.closeout_step(paired["status"] if paired else None,
                                        any_working, sent, now)
            if step == "nudge":
                # idle agent, nothing else working, briefed ≥60s ago: type the
                # specifics so it stops treating the leftovers as untouchable.
                block = "\n".join(files)
                if len(porcelain) > len(files):
                    block += f"\n… and {len(porcelain) - len(files)} more"
                nudge = finish.CLOSEOUT_NUDGE_TEXT.format(
                    left=left, trunk=trunk, files=(block + "\n") if block else "")
                res = terminal.send_to_process(live["pid"], nudge)
                if not res["ok"]:
                    return {"ok": False, "mode": "nudge", "message": res["message"]}
                finish._closeouts[wt_name] = time.time()   # restart the "sent Xm ago"
                observer._cache["t"] = 0.0           # clock + re-arm the 60s guard
                # `left`/`files` ride along so the card note can say what's
                # blocked without parsing the human message
                return {"ok": True, "mode": "nudge", "left": left,
                        **({"files": files} if files else {}), "message":
                        f"closeout had stalled — sent the agent the specifics "
                        f"({left}); ✕ close works once it reports clean"}
            # otherwise refuse, but hand the frontend the specifics too: `left`
            # (short reason), `files` (≤5 porcelain lines, only when any), and
            # `sent` (the epoch it was briefed). mode "pending" is a plain
            # refusal; mode "chat" is a DISTINCT mode meaning the agent is stuck
            # on a question/approval — a typed nudge would collide with its open
            # dialog, so the frontend must route the user to ✉ chat instead.
            extra = {"left": left, "sent": sent}
            if files:
                extra["files"] = files
            if step == "chat":
                return {"ok": False, "mode": "chat", **extra, "message":
                        f"can't close yet — {left}, and the agent is stuck on a "
                        "question or approval. Answer it in ✉ chat — a typed "
                        "nudge would collide with its open dialog. ✕ close works "
                        "once the landing verifies."}
            mins = int((now - sent) // 60)
            ago = f"{mins}m ago" if mins else "under a minute ago"
            return {"ok": False, "mode": "pending", **extra, "message":
                    f"can't close yet — {left}. The closeout brief went to "
                    f"the agent {ago}; if it looks stuck, ✉ chat with it. "
                    "✕ close works once the landing verifies."}
        brief = (finish.SLIM_CLOSEOUT_TEXT if landed else finish.CLOSEOUT_TEXT)
        res = terminal.send_to_process(live["pid"], brief.format(trunk=trunk))
        if not res["ok"]:
            return {"ok": False, "mode": "slim" if landed else "brief",
                    "message": res["message"]}
        finish._closeouts[wt_name] = time.time()
        observer._cache["t"] = 0.0   # show ✕ close on the next poll, not in 4s
        return {"ok": True, "mode": "slim" if landed else "brief", "message":
                ("already landed — slim brief sent (tidy scratch and park, "
                 "no re-merge)" if landed else
                 "closeout brief sent to the live agent")
                + " — when it reports done, ✕ close verifies the landing "
                  "and closes the terminal"}
    finish._closeouts.pop(wt_name, None)   # no live agent — the two-step is moot
    if mine:
        return {"ok": False, "message":
                "a live process exists but its terminal can't be scripted — "
                "finish from that terminal, or close it and ✓ finish again"}
    if landed and not porcelain:
        branch = shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=git_root)[1].strip()
        if branch == trunk.split("/", 1)[-1]:
            return {"ok": True, "mode": "noop",
                    "message": "already landed and clean — nothing to finish"}
        parked = finish._park_on_trunk(git_root, trunk)
        if parked:
            return parked
        # the switch itself failed — fall through and let an agent sort it out
    # any leftover file — even untracked scratch — goes to an agent: whether
    # it's droppable is a judgment call, not ours. haiku is enough for the
    # mechanical run, a landed branch gets the slim brief so nothing already
    # merged is re-checked, and a failed landing escalates itself (see
    # closeout_shell's rescue line)
    brief = (finish.SLIM_CLOSEOUT_TEXT if landed
             else finish.CLOSEOUT_TEXT).format(trunk=trunk)
    out = dispatch.start_dispatch(brief, worktree=wt_name,
                                  model="haiku", closeout_trunk=trunk)
    out.setdefault("ok", True)
    out["mode"] = "dispatch"
    out.setdefault("message",
                   "no live terminal — launched a one-shot closeout agent; "
                   "the card frees itself once the landing verifies")
    return out


# -------------------------------------------------------- scheduled resumes

# A limit-stuck agent needs exactly one keystroke ("continue") typed at it
# once its limit resets — but resets land at 3am, or days out on a weekly cap.
# Arming a schedule hands that keystroke to the board: at the armed time it
# verifies the limit really lifted (re-arming for the next reset if not), then
# types the resume message into the session's own terminal — or, when no
# terminal can be scripted (Cursor/VS Code, or the window is gone), relaunches
# the conversation in a fleet tmux session via `claude --resume`.
#
# Schedules survive both the browser and this server: every mutation is
# persisted to resume.schedule.json, and pending entries whose time passed
# while the server was down fire on the first loop pass after boot.

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


def _proven_in_transcript(fp, offset, text, timeout_s=20.0):
    """True once the session file gains a user entry carrying `text` beyond
    `offset` — receipt at the source, not read off the screen."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with open(fp, "rb") as f:
                f.seek(offset)
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
            offset = fp.stat().st_size if fp else 0
        except OSError:
            fp, offset = None, 0
        sent = dispatch.deliver_text(name, msg)
        if fp and _proven_in_transcript(fp, offset, msg):
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
    if s and s["status"] != "limit":
        return _resume_set(key, status="done", fired_at=now, message=
                           f"session is {s['status']} — no resume needed")

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


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            # schedules ride along so the board needs no second fetch
            body = json.dumps({**observer.cached_state(),
                               "resumes": resume_public()}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/focus"):
            m = re.search(r"pid=(\d+)", self.path)
            result = terminal.focus_process(int(m.group(1))) if m else {"ok": False, "message": "missing pid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/topology"):
            body = json.dumps(gitrepo.cached_topology()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/limits"):
            body = json.dumps(limits.cached_limits(refresh="refresh=1" in self.path)).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatchlog"):
            body = json.dumps(dispatch.read_dispatch_log()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatch/status"):
            m = re.search(r"job=([\w-]+)", self.path)
            body = json.dumps(dispatch.dispatch_status(m.group(1)) if m
                              else {"ok": False, "error": "no job"}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/chat"):
            qa = re.search(r"account=([^&]+)", self.path)
            qs = re.search(r"sid=([0-9a-fA-F-]+)", self.path)
            result = chat.read_chat(qa.group(1), qs.group(1)) if qa and qs else \
                {"ok": False, "error": "need account & sid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] in ("/", "/index", "/index.html"):
            body = (config.HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/map"):
            body = (config.HERE / "map.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/limits"):
            body = (config.HERE / "limits.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/guide"):
            body = (config.HERE / "guide.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, OSError):
            payload = {}
        if self.path.startswith("/api/reserve"):
            result = limits.set_reserve(payload.get("account"), payload.get("percent"))
        elif self.path.startswith("/api/resume/schedule"):
            result = schedule_resume(
                payload.get("worktree"), payload.get("sid"),
                payload.get("account"), model=payload.get("model"),
                delay_s=payload.get("delay_s"),
                resets_at=payload.get("resets_at"), due_at=payload.get("due_at"))
        elif self.path.startswith("/api/resume/cancel"):
            result = cancel_resume(payload.get("worktree"), payload.get("sid"))
        elif self.path.startswith("/api/send"):
            result = terminal.send_to_process(int(payload.get("pid") or 0), payload.get("text") or "")
        elif self.path.startswith("/api/finish"):
            result = start_finish(payload.get("worktree") or "")
        elif self.path.startswith("/api/dispatch"):
            result = dispatch.start_dispatch(
                payload.get("mission"), payload.get("worktree") or None,
                payload.get("account") or None,
                payload.get("model") or None, payload.get("effort") or None,
                bool(payload.get("force_model")))
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
