#!/usr/bin/env python3
"""fleetboard — local mission control for parallel Claude Code agents.

Watches your git worktrees, your Claude Code home directories (multi-account
setups included), and live `claude` processes; serves three views on
http://127.0.0.1:4242 — the board (who's working / who needs you / which
worktree is free), the map (real git topology of every branch), and limits
(per-account usage via cclimits) — plus a click-only control plane: chat with
any agent, resume a limit-stuck one when its session limit resets, and
dispatch new tmux-hosted agents into free worktrees.

Watching is read-only and touches nothing. Acting (chat/resume/dispatch)
happens only on an explicit request, and dispatch spends account usage.
Zero dependencies — python3 stdlib only.

    python3 fleetboard.py --root ~/code
    python3 fleetboard.py --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > fleetboard.config.json (next to this
script, else cwd) > defaults. See README.md.
"""

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent

CFG = {
    "host": "127.0.0.1",       # keep loopback: the board serves transcript text
    "port": 4242,
    "roots": [str(Path.cwd())],  # dirs whose git-repo children are watched
    "pattern": "",             # optional regex filter on worktree dir names
    "homes": [],               # Claude home dirs; [] = auto-discover ~/.claude*
    "session_window_h": 48,    # ignore transcripts idle longer than this
    "working_s": 90,           # transcript written within this => working
    "max_sessions": 6,         # per worktree card
    "exclude_accounts": [],    # account labels never AUTO-picked for dispatch/router
    "router_home": None,       # fixed Claude home for the haiku router (else auto)
    "reserve_percent": {},     # {label: pct} buffer kept free before AUTO-pick treats account as full ("*" = default)
}

TAIL_BYTES = 128 * 1024
HEAD_BYTES = 16 * 1024
STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}
DEMO = False
CONFIG_PATH = None             # the config file in effect (for live edits)


def load_config(argv=None):
    ap = argparse.ArgumentParser(description="local mission control for parallel Claude Code agents")
    ap.add_argument("--root", action="append", metavar="DIR",
                    help="directory whose git-repo children are watched (repeatable; default: cwd)")
    ap.add_argument("--pattern", metavar="REGEX", help="only watch dirs matching this regex (case-insensitive)")
    ap.add_argument("--home", action="append", metavar="DIR",
                    help="Claude home dir (repeatable; default: auto-discover ~/.claude*)")
    ap.add_argument("--port", type=int, help="port (default 4242, env FLEETBOARD_PORT)")
    ap.add_argument("--host", help="bind address (default 127.0.0.1 — the board serves your transcript text; do not expose it)")
    ap.add_argument("--window-h", type=float, help="ignore transcripts idle longer than this many hours (default 48)")
    ap.add_argument("--config", metavar="FILE", help="path to a fleetboard.config.json")
    ap.add_argument("--demo", action="store_true", help="serve fictional demo data (for screenshots)")
    args = ap.parse_args(argv)

    global CONFIG_PATH
    candidates = [Path(args.config)] if args.config else [
        HERE / "fleetboard.config.json", Path.cwd() / "fleetboard.config.json"]
    for p in candidates:
        if p.is_file():
            try:
                CFG.update(json.loads(p.read_text()))
            except (ValueError, OSError) as e:
                sys.exit(f"fleetboard: bad config {p}: {e}")
            CONFIG_PATH = p
            break
    if CONFIG_PATH is None:  # where a UI edit will create/persist config
        CONFIG_PATH = Path(args.config) if args.config else HERE / "fleetboard.config.json"
    if os.environ.get("FLEETBOARD_PORT"):
        CFG["port"] = int(os.environ["FLEETBOARD_PORT"])
    if args.root: CFG["roots"] = args.root
    if args.pattern is not None: CFG["pattern"] = args.pattern
    if args.home: CFG["homes"] = args.home
    if args.port: CFG["port"] = args.port
    if args.host: CFG["host"] = args.host
    if args.window_h: CFG["session_window_h"] = args.window_h
    return args


# ---------------------------------------------------------------- collectors

def run(cmd, cwd=None, timeout=6):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception:
        return 1, ""


def munge(path):
    """Claude Code's project-dir name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def discover_worktrees():
    pat = re.compile(CFG["pattern"], re.I) if CFG["pattern"] else None
    wts, seen = [], set()
    for root in CFG["roots"]:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir() or (pat and not pat.search(p.name)) or str(p) in seen:
                continue
            git_root = None
            if (p / ".git").exists():
                git_root = p
            elif (p / "repo" / ".git").exists():  # <worktree>/repo layout
                git_root = p / "repo"
            if git_root:
                seen.add(str(p))
                wts.append({"name": p.name, "path": str(p), "git": str(git_root)})
    return wts


def git_info(git_root):
    info = {"branch": None, "commit": None, "dirty": 0, "ahead": None, "behind": None}
    rc, branch = run(["git", "branch", "--show-current"], cwd=git_root)
    if rc == 0 and branch:
        info["branch"] = branch
    else:
        rc, head = run(["git", "rev-parse", "--short", "HEAD"], cwd=git_root)
        info["branch"] = f"detached@{head}" if rc == 0 else "?"
    rc, log = run(["git", "log", "-1", "--format=%h%x00%ct%x00%s"], cwd=git_root)
    if rc == 0 and log:
        h, ct, s = (log.split("\x00") + ["", "", ""])[:3]
        info["commit"] = {"hash": h, "ts": int(ct or 0), "subject": s}
    rc, status = run(["git", "status", "--porcelain"], cwd=git_root)
    if rc == 0:
        info["dirty"] = len([l for l in status.splitlines() if l.strip()])
    rc, lr = run(["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"], cwd=git_root)
    if rc == 0 and lr:
        parts = lr.split()
        if len(parts) == 2:
            info["behind"], info["ahead"] = int(parts[0]), int(parts[1])
    return info


def _pid_cwds(pids):
    cwds = {}
    if not pids:
        return cwds
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                cwds[pid] = os.readlink(f"/proc/{pid}/cwd")
            except OSError:
                pass
    else:  # macOS and other BSDs: one lsof call for all pids
        _, out = run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fn"],
                     timeout=10)
        cur = None
        for line in out.splitlines():
            if line.startswith("p"):
                cur = int(line[1:])
            elif line.startswith("n") and cur is not None:
                cwds[cur] = line[1:]
    return cwds


_HOST_APPS = [("Terminal.app", "Terminal"), ("iTerm", "iTerm2"), ("Cursor", "Cursor"),
              ("Code Helper", "VS Code"), ("Visual Studio Code", "VS Code"),
              ("Alacritty", "Alacritty"), ("kitty", "kitty"), ("WezTerm", "WezTerm"),
              ("Ghostty", "Ghostty")]


def _host_of(pid, table):
    """Walk a process's ancestry to find what hosts its terminal."""
    seen = set()
    p = table.get(pid, (None,))[0]  # start from parent
    while p and p != 1 and p not in seen:
        seen.add(p)
        ent = table.get(p)
        if not ent:
            break
        ppid, _tty, cmd = ent
        head = cmd.split(" ")[0]
        if head == "tmux" or head.endswith("/tmux"):
            m = re.search(r"-L\s+(\S+)", cmd)
            return "tmux", ("tmux -L " + m.group(1) if m else "tmux")
        for pat, label in _HOST_APPS:
            if pat in cmd:
                return "app", label
        p = ppid
    return None, None


def _tmux_pane_map(sock_flag):
    """pane shell pid -> 'session:win.pane' for one tmux server."""
    cmd = ["tmux"] + (["-L", sock_flag] if sock_flag else []) + \
          ["list-panes", "-a", "-F", "#{pane_pid}|#{session_name}:#{window_index}.#{pane_index}"]
    rc, out = run(cmd)
    panes = {}
    if rc == 0:
        for line in out.splitlines():
            pid, _, target = line.partition("|")
            if pid.isdigit():
                panes[int(pid)] = target
    return panes


def claude_processes():
    """Live `claude` CLI processes with cwd, tty, and hosting terminal app."""
    rc, out = run(["ps", "-axo", "pid=,ppid=,tty=,pcpu=,etime=,command="])
    table, procs = {}, []
    for line in out.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\S+)\s+(.*)", line)
        if not m:
            continue
        pid, ppid, tty, cpu, etime, cmd = m.groups()
        table[int(pid)] = (int(ppid), tty, cmd)
        if cmd == "claude" or cmd.startswith("claude "):
            procs.append({"pid": int(pid), "cpu": float(cpu), "etime": etime,
                          "cmd": cmd, "tty": None if tty in ("??", "-") else tty})
    cwds = _pid_cwds([p["pid"] for p in procs])
    pane_maps = {}
    for p in procs:
        p["cwd"] = cwds.get(p["pid"])
        kind, label = _host_of(p["pid"], table)
        p["host_kind"], p["host"] = kind, label
        p["tmux_sock"] = p["tmux_target"] = None
        if kind == "tmux":
            m = re.search(r"-L\s+(\S+)", label or "")
            sock = m.group(1) if m else None
            if sock not in pane_maps:
                pane_maps[sock] = _tmux_pane_map(sock)
            panes = pane_maps[sock]
            anc = p["pid"]
            for _ in range(8):  # claude's ancestor chain reaches the pane shell
                if anc in panes:
                    p["tmux_sock"], p["tmux_target"] = sock, panes[anc]
                    break
                anc = table.get(anc, (None,))[0]
                if not anc or anc == 1:
                    break
    return procs


def claude_homes():
    # Precedence: --home / config "homes" > CLAUDE_CONFIG_DIRS (colon-separated,
    # same convention as cclimits) > auto-discover ~/.claude*
    explicit = CFG["homes"] or [
        h for h in os.environ.get("CLAUDE_CONFIG_DIRS", "").split(":") if h]
    if explicit:
        return [Path(h).expanduser() for h in explicit
                if (Path(h).expanduser() / "projects").is_dir()]
    homes = []
    for p in sorted(HOME.iterdir()):
        if (p.name == ".claude" or p.name.startswith(".claude-")) and (p / "projects").is_dir():
            homes.append(p)
    return homes


def account_label(home):
    name = home.name.lstrip(".")
    if name == "claude":
        return "main"
    return re.sub(r"^claude-?", "", name) or name


def _read_chunk(fp, size, from_end):
    try:
        with open(fp, "rb") as f:
            if from_end:
                f.seek(0, 2)
                n = f.tell()
                f.seek(max(0, n - size))
                data = f.read()
                if n > size:  # drop leading partial line
                    data = data.split(b"\n", 1)[-1]
            else:
                data = f.read(size)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _clean(text, limit=240):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1", text, flags=re.S)
    text = re.sub(r"<[^>]{1,80}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


_MACHINE_TEXT = re.compile(
    r"<local-command-stdout>|<command-message>|<system-reminder>|"
    r"task-notification|\btoolu_[A-Za-z0-9]|\[SYSTEM NOTIFICATION")


def _real_prompt(text):
    """A user text that describes the session (not a slash-command stub,
    caveat, or harness-injected machine noise)."""
    if _MACHINE_TEXT.search(text):
        return None
    t = _clean(text, 140)
    if not t or t.startswith("/") or t.startswith("Caveat:"):
        return None
    return t


def session_topic(fp):
    """Label a session: compaction summary if present, else first real user prompt."""
    for line in _read_chunk(fp, HEAD_BYTES, from_end=False).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "summary" and e.get("summary"):
            return _clean(e["summary"], 140)
        if e.get("type") == "user" and not e.get("isMeta"):
            c = e.get("message", {}).get("content")
            texts = [c] if isinstance(c, str) else [
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
            for t in texts:
                topic = _real_prompt(t)
                if topic:
                    return topic
    return None


def last_assistant_text(fp, size=TAIL_BYTES):
    """Last assistant text in a transcript, no sidechain filter (for subagent
    files, whose entries are all sidechain from the parent's perspective)."""
    last = None
    for line in _read_chunk(fp, size, from_end=True).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    last = _clean(b["text"])
    return last


def find_last_user(fp, size=1024 * 1024):
    """Deeper backward search for the latest real user prompt (fallback when
    the standard tail window is all tool traffic)."""
    for line in reversed(_read_chunk(fp, size, from_end=True).splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain") or e.get("type") != "user" or e.get("isMeta"):
            continue
        c = e.get("message", {}).get("content")
        texts = [c] if isinstance(c, str) else [
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
        for t in texts:
            p = _real_prompt(t)
            if p:
                return p
    return None


def parse_session_tail(fp):
    """Tail-parse a transcript: last activity, pending tools, last assistant text."""
    entries = []
    for line in _read_chunk(fp, TAIL_BYTES, from_end=True).splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    main = [e for e in entries if isinstance(e, dict) and not e.get("isSidechain")]

    out = {"cwd": None, "branch": None, "model": None, "pending_tools": [],
           "last_assistant": None, "last_user": None, "pending_workflows": 0}
    pending = {}  # tool_use id -> tool name
    for e in main:
        out["cwd"] = e.get("cwd") or out["cwd"]
        out["branch"] = e.get("gitBranch") or out["branch"]
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            # a turn that ended still awaiting workflows is NOT the user's turn
            out["pending_workflows"] = e.get("pendingWorkflowCount") or 0
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if e.get("type") == "user" and not e.get("isMeta"):
            texts = [content] if isinstance(content, str) else [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(content, list) else []
            for t in texts:
                prompt = _real_prompt(t)
                if prompt:
                    out["last_user"] = prompt
        if e.get("type") == "assistant":
            model = msg.get("model")
            if model and model != "<synthetic>":
                out["model"] = model
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        pending[b.get("id")] = b.get("name", "?")
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        out["last_assistant"] = _clean(b["text"])
        elif e.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pending.pop(b.get("tool_use_id"), None)
    out["pending_tools"] = sorted(set(pending.values()))
    return out


def scan_sessions(worktrees, procs, now):
    """All recent sessions across every Claude home, mapped to worktrees."""
    by_wt = {w["path"]: [] for w in worktrees}
    wt_prefixes = {w["path"]: munge(w["path"]) for w in worktrees}
    window_s = CFG["session_window_h"] * 3600

    def best_worktree(proj_name):
        # Munged names are ambiguous (myapp vs myapp-audit):
        # the longest matching worktree prefix wins.
        best = None
        for path, pref in wt_prefixes.items():
            if proj_name == pref or proj_name.startswith(pref + "-"):
                if best is None or len(pref) > len(wt_prefixes[best]):
                    best = path
        return best

    for home in claude_homes():
        acct = account_label(home)
        for proj in (home / "projects").iterdir():
            wt = best_worktree(proj.name)
            if wt is None:
                continue
            for fp in proj.glob("*.jsonl"):
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                # Workflows/subagents write to <session-id>/**/*.jsonl while the
                # main transcript sits untouched — count them toward activity.
                sub_files = []
                sub_dir = fp.with_suffix("")
                if sub_dir.is_dir():
                    for sf in sub_dir.rglob("*.jsonl"):
                        try:
                            sub_files.append((sf.stat().st_mtime, sf))
                        except OSError:
                            continue
                sub_mtime = max((m for m, _ in sub_files), default=0.0)
                # The newest thing the session "said" may be a subagent's
                # report (Claude Code shows those in the terminal too).
                subagent_said = None
                if sub_mtime > mtime:
                    for _, sf in sorted(sub_files, reverse=True)[:2]:
                        subagent_said = last_assistant_text(sf)
                        if subagent_said:
                            break
                age = now - max(mtime, sub_mtime)
                if age > window_s:
                    continue
                tail = parse_session_tail(fp)
                cwd = tail["cwd"] or wt
                by_wt[wt].append({
                    "id": fp.stem[:8],
                    "sid": fp.stem,
                    "account": acct,
                    "age_s": int(age),
                    "cwd": cwd,
                    "subdir": os.path.relpath(cwd, wt) if cwd != wt else None,
                    "branch": tail["branch"],
                    "model": (tail["model"] or "").replace("claude-", ""),
                    "pending_tools": tail["pending_tools"],
                    "pending_workflows": tail["pending_workflows"],
                    "topic": session_topic(fp),
                    "last_assistant": tail["last_assistant"],
                    "last_user": tail["last_user"] or find_last_user(fp),
                    "subagent_said": subagent_said,
                    "subagents_active": bool(sub_mtime and now - sub_mtime < CFG["working_s"]),
                })

    rank = {"needs_input": 0, "blocked": 1, "working": 2, "waiting": 3, "ended": 4}
    for wt, sessions in by_wt.items():
        sessions.sort(key=lambda s: s["age_s"])
        # A live process proves at most ONE session is really attended.
        # N procs under a worktree vouch for its N freshest sessions —
        # freshness beats cwd matching (recorded cwds drift as agents cd
        # around, and a stale exact match must not outrank the live session).
        wt_procs = [p for p in procs if p.get("cwd") and
                    (p["cwd"] == wt or p["cwd"].startswith(wt + "/"))]
        slots_left = len(wt_procs)
        # With --dangerously-skip-permissions there are no approval prompts:
        # an unresolved tool call means a long-running tool, not "blocked".
        skip_perms = bool(wt_procs) and all(
            "--dangerously-skip-permissions" in p["cmd"] for p in wt_procs)
        for s in sessions:
            alive = slots_left > 0
            if alive:
                slots_left -= 1
            pend = s["pending_tools"]
            if s["age_s"] < CFG["working_s"]:
                status = "working"
            elif alive and "AskUserQuestion" in pend:
                status = "needs_input"
            elif alive and s["pending_workflows"]:
                status = "working"   # delegated — waiting on its own workflows
            elif alive and pend and skip_perms:
                status = "working"   # long tool run, nothing to approve
                s["tool_running"] = True
            elif alive and pend:
                status = "blocked"
            elif alive:
                status = "waiting"
            else:
                status = "ended"
            s["status"] = status
        sessions.sort(key=lambda s: (rank[s["status"]], s["age_s"]))
        by_wt[wt] = sessions[: CFG["max_sessions"]]
    return by_wt


def collect_state():
    now = time.time()
    worktrees = discover_worktrees()
    procs = claude_processes()
    sessions = scan_sessions(worktrees, procs, now)

    # An agent parked at the prompt on an exhausted account isn't "your turn" —
    # it's out of juice. Joined from the cclimits cache (populated lazily by
    # /api/limits; never fetched on the state path).
    acct_limits = limits_by_account()
    limit_re = re.compile(r"out of usage credits|(reached|hit) your .{0,30}limit", re.I)
    rank = {"needs_input": 0, "limit": 1, "blocked": 2, "working": 3, "waiting": 4, "ended": 5}
    for ss in sessions.values():
        for s in ss:
            if s["status"] not in ("needs_input", "blocked", "waiting"):
                continue
            al = acct_limits.get(s["account"])
            if al and al["exhausted"] and al["worst_scoped"] and \
                    (al["worst"] or "").lower() not in (s["model"] or "").lower():
                al = None  # limit is model-scoped and this session runs another model
            if al and al["exhausted"]:
                s["status"] = "limit"
                s["limit"] = {"worst": al["worst"], "group": al["group"],
                              "resets_in": al["resets_in"], "resets_at": al["resets_at"]}
            elif limit_re.search(s["last_assistant"] or ""):
                # the CLI wrote its limit notice into the transcript —
                # trust it even when the cclimits cache is cold/stale
                s["status"] = "limit"
                s["limit"] = {"worst": None, "group": None, "resets_in": None, "resets_at": None}

    # Handoff awareness: a limit-hit session whose worktree has a FRESHER live
    # session (typically another account continuing from a handoff doc) is no
    # longer the actionable one — annotate the succession and stop treating
    # the stranded session as needing attention.
    for ss in sessions.values():
        alive = [s for s in ss if s["status"] in ("working", "waiting", "needs_input", "blocked")]
        for s in ss:
            if s["status"] == "limit":
                succ = [a for a in alive if a["age_s"] < s["age_s"]]
                if succ:
                    s["handed_to"] = min(succ, key=lambda a: a["age_s"])["account"]
        ss.sort(key=lambda s: (4.5 if s.get("handed_to") else rank[s["status"]], s["age_s"]))

    def _attention_statuses(ss):
        return [s["status"] for s in ss
                if not (s["status"] == "limit" and s.get("handed_to"))]

    cards = []
    for w in worktrees:
        ss = sessions.get(w["path"], [])
        live = [p for p in procs if p.get("cwd") and
                (p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/"))]
        cards.append({
            **w,
            "git": git_info(w["git"]),
            "sessions": ss,
            "live_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                            "tty": p["tty"], "host": p["host"],
                            "tmux": p.get("tmux_target"),
                            "reachable": bool(p.get("tmux_target") or
                                              (p["host"] in ("Terminal", "iTerm2") and p["tty"])),
                            "subdir": os.path.relpath(p["cwd"], w["path"])
                            if p["cwd"] != w["path"] else None} for p in live],
        })

    for c in cards:
        st = _attention_statuses(c["sessions"])
        if c["live_procs"] or "working" in st:
            if any(k in st for k in ("needs_input", "blocked", "limit")):
                c["availability"] = "attention"   # hard blocker — needs you
            elif "waiting" in st and "working" not in st:
                c["availability"] = "attention"   # everyone parked — needs direction
            else:
                c["availability"] = "busy"        # something is actively working
        else:
            c["availability"] = "free"            # safe to point a new agent here

    matched = {p["pid"] for c in cards for p in c["live_procs"]}
    other = [p for p in procs if p["pid"] not in matched]

    def severity(c):
        st = _attention_statuses(c["sessions"])
        if "needs_input" in st: return 0
        if "limit" in st: return 1
        if "blocked" in st: return 2
        if "waiting" in st and "working" not in st: return 3
        if "working" in st: return 4
        return 5
    cards.sort(key=lambda c: (severity(c), c["name"].lower()))

    counts = {"working": 0, "needs_input": 0, "limit": 0, "blocked": 0, "waiting": 0, "ended": 0}
    for c in cards:
        for s in c["sessions"]:
            if s["status"] == "limit" and s.get("handed_to"):
                continue  # informational — work already continues elsewhere
            counts[s["status"]] += 1
    return {
        "generated_at": now,
        "hostname": os.uname().nodename,
        "user": getpass.getuser(),
        "counts": counts,
        "free_worktrees": [c["name"] for c in cards if c["availability"] == "free"],
        "worktrees": cards,
        "other_procs": [{"pid": p["pid"], "cpu": p["cpu"], "etime": p["etime"],
                         "tty": p["tty"], "host": p["host"],
                         "cwd": p.get("cwd")} for p in other],
    }


# ----------------------------------------------------------- branch topology

TOPO_TTL_S = 30.0
_topo = {"t": 0.0, "data": None}


def _base_ref(git_root):
    """The trunk ref this repo's branches are measured against."""
    rc, out = run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=git_root)
    if rc == 0 and out.startswith("refs/remotes/"):
        return out[len("refs/remotes/"):]
    for cand in ("origin/main", "origin/master", "main", "master"):
        rc, _ = run(["git", "rev-parse", "-q", "--verify", cand], cwd=git_root)
        if rc == 0:
            return cand
    return None


def branch_topology():
    """Where every branch really is: fork point from trunk, tip, drift."""
    now = time.time()
    groups = {}
    for w in discover_worktrees():
        g = w["git"]
        rc, origin = run(["git", "remote", "get-url", "origin"], cwd=g)
        key = origin if rc == 0 and origin else "local:" + w["path"]
        base = _base_ref(g)
        if not base:
            continue
        rc, mb = run(["git", "merge-base", "HEAD", base], cwd=g)
        if rc != 0 or not mb:
            continue

        def ts(ref):
            rc2, out2 = run(["git", "show", "-s", "--format=%ct", ref], cwd=g)
            try:
                return int(out2.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return None

        fork_ts, tip_ts, base_ts = ts(mb), ts("HEAD"), ts(base)
        if not (fork_ts and tip_ts):
            continue
        _, ah = run(["git", "rev-list", "--count", f"{mb}..HEAD"], cwd=g)
        _, bh = run(["git", "rev-list", "--count", f"{mb}..{base}"], cwd=g)
        _, cts = run(["git", "log", "--format=%ct", "-40", f"{mb}..HEAD"], cwd=g)
        _, last = run(["git", "log", "-1", "--format=%h%x00%s"], cwd=g)
        h, subj = (last.split("\x00") + ["", ""])[:2]
        _, br = run(["git", "branch", "--show-current"], cwd=g)
        _, dirty = run(["git", "status", "--porcelain"], cwd=g)
        grp = groups.setdefault(key, {
            "repo": re.sub(r"\.git$", "", key.rsplit("/", 1)[-1]),
            "base": base, "trunk_ts": 0, "trunk_commits": [], "_root": g,
            "branches": []})
        if base_ts and base_ts > grp["trunk_ts"]:
            # separate clones fetch at different times — the freshest
            # origin/<main> wins as this repo's trunk tip
            grp["trunk_ts"], grp["_root"] = base_ts, g
        grp["branches"].append({
            "worktree": w["name"], "branch": br or "?",
            "fork_ts": min(fork_ts, tip_ts), "tip_ts": tip_ts,
            "ahead": int(ah or 0), "behind": int(bh or 0),
            "dirty": len([l for l in dirty.splitlines() if l.strip()]),
            "hash": h, "subject": subj,
            "commits": [int(x) for x in cts.split()][:40] if cts else [],
        })
    for grp in groups.values():
        _, tct = run(["git", "log", "--format=%ct", "-40", grp["base"]],
                     cwd=grp.pop("_root"))
        grp["trunk_commits"] = [int(x) for x in tct.split()][:40] if tct else []
    return {"generated_at": now, "groups": list(groups.values())}


def demo_topology():
    now = time.time()
    H = 3600

    def spread(t0, t1, n):
        return [int(t0 + (t1 - t0) * i / max(1, n - 1)) for i in range(n)]

    def br(wt, branch, fork_h, tip_h, ahead, behind, dirty, subj):
        return {"worktree": wt, "branch": branch, "fork_ts": int(now - fork_h * H),
                "tip_ts": int(now - tip_h * H), "ahead": ahead, "behind": behind,
                "dirty": dirty, "hash": "a1b2c3d", "subject": subj,
                "commits": spread(now - fork_h * H + 600, now - tip_h * H, min(ahead, 20))}

    return {"generated_at": now, "groups": [{
        "repo": "orbital", "base": "origin/main", "trunk_ts": int(now - 0.4 * H),
        "trunk_commits": spread(now - 70 * H, now - 0.4 * H, 24),
        "branches": [
            br("orbital-api", "feat/webhook-retries", 68, 0.35, 14, 6, 12,
               "feat(webhooks): exponential backoff with jitter"),
            br("orbital-web", "fix/checkout-race", 34, 0.6, 6, 2, 3,
               "fix(cart): serialize checkout mutations"),
            br("kepler-worker", "perf/batch-inserts", 9, 0.05, 9, 0, 7,
               "perf(db): batch event inserts"),
            br("lander-docs", "docs/quickstart", 46, 26, 3, 11, 2,
               "docs: rewrite quickstart around the new init flow"),
            br("voyager-cli", "main", 48, 48, 0, 9, 0, "chore: release v0.4.1"),
        ]}]}


def cached_topology():
    if DEMO:
        return demo_topology()
    now = time.time()
    if _topo["data"] is None or now - _topo["t"] > TOPO_TTL_S:
        _topo["data"] = branch_topology()
        _topo["t"] = now
    return _topo["data"]


# --------------------------------------------------------------- demo state

def demo_state():
    """Fictional data with the exact shape of collect_state(), for screenshots."""
    now = time.time()

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None):
        return {"id": "demo0000", "account": acct, "status": status, "age_s": age,
                "cwd": "/demo", "subdir": subdir, "branch": None, "model": model,
                "pending_tools": pend or [], "topic": topic, "last_assistant": said}

    def card(name, avail, branch, dirty, ahead, behind, cts, subject, sessions, pids):
        return {"name": name, "path": "/demo/" + name, "git_root": "",
                "git": {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
                        "commit": {"hash": "a1b2c3d", "ts": int(now - cts), "subject": subject}},
                "sessions": sessions, "availability": avail,
                "live_procs": [{"pid": p, "cpu": 4.2, "etime": "02:14:33", "subdir": None}
                               for p in pids]}

    cards = [
        card("orbital-api", "attention", "feat/webhook-retries", 12, 3, 0, 1800,
             "feat(webhooks): exponential backoff with jitter", [
                 sess("needs_input", "work", "fable-5", 340,
                      "Add retry logic to the webhook dispatcher with dead-letter support",
                      "Should failed deliveries older than 24h go to the dead-letter queue or be dropped? I've laid out both options.",
                      pend=["AskUserQuestion"]),
                 sess("ended", "work", "opus-4-8", 9100,
                      "Profile the webhook worker under load", None)], [41234]),
        card("orbital-web", "attention", "fix/checkout-race", 3, 1, 0, 5400,
             "fix(cart): serialize checkout mutations", [
                 dict(sess("limit", "work", "opus-4-8", 3900,
                      "The checkout button double-fires on slow connections — find and fix the race",
                      "I'll continue once usage is available again."),
                      limit={"worst": "Session", "resets_in": 7560}),
                 sess("waiting", "personal", "fable-5", 2100,
                      "Audit the cart telemetry events for double-counting",
                      "Fixed and verified — the mutation is now idempotent and the test suite passes. Ready for review.")], [41567]),
        card("kepler-worker", "busy", "perf/batch-inserts", 7, 0, 0, 600,
             "perf(db): batch event inserts, 40x fewer round-trips", [
                 sess("working", "work", "opus-4-8", 15,
                      "Migrate the event pipeline to batched COPY inserts",
                      "Running the benchmark suite against the staging database now.")], [42901]),
        card("voyager-cli", "free", "main", 0, 0, 0, 86400 * 2,
             "chore: release v0.4.1", [], []),
        card("lander-docs", "free", "docs/quickstart", 2, None, None, 86400,
             "docs: rewrite quickstart around the new init flow", [], []),
    ]
    return {
        "generated_at": now, "hostname": "starbase", "user": "you",
        "counts": {"working": 1, "needs_input": 1, "limit": 1, "blocked": 0, "waiting": 1, "ended": 1},
        "free_worktrees": ["voyager-cli", "lander-docs"],
        "worktrees": cards,
        "other_procs": [{"pid": 40001, "cpu": 1.1, "etime": "15:02", "cwd": "/demo/scratch"}],
    }


def cached_state():
    if DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()
        _cache["t"] = now
    return _cache["state"]


# ------------------------------------------------------------ usage limits

LIMITS_TTL_S = 300.0           # cclimits --json (its own cache) at most this often
_limits = {"t": 0.0, "data": None}


def _cclimits_bin():
    cmd = CFG.get("cclimits_cmd")
    if cmd:
        return cmd
    found = shutil.which("cclimits")
    if found:
        return found
    fallback = HOME / ".local" / "bin" / "cclimits"
    return str(fallback) if fallback.exists() else None


def cached_limits(refresh=False):
    """Per-account usage limits via cclimits (github.com/acrdlph/cclimits).
    Lazy + cached; a network refetch happens only on explicit refresh."""
    if DEMO:
        return demo_limits()
    now = time.time()
    if not refresh and _limits["data"] is not None and now - _limits["t"] < LIMITS_TTL_S:
        return _limits["data"]
    binp = _cclimits_bin()
    if not binp:
        return {"available": False, "error": "cclimits not found — install github.com/acrdlph/cclimits"}
    cmd = [binp, "--json"] + (["--refresh"] if refresh else [])
    rc, out = run(cmd, timeout=90 if refresh else 30)
    if rc != 0 or not out:
        return _limits["data"] or {"available": False, "error": "cclimits failed (see terminal)"}
    try:
        data = json.loads(out)
    except ValueError:
        return _limits["data"] or {"available": False, "error": "cclimits returned non-JSON"}
    data["available"] = True
    data["fetched_at"] = now
    for acc in data.get("accounts", []):
        if acc.get("config_dir"):
            label = account_label(Path(acc["config_dir"]))
            r = account_reserve(label)
            acc["fb_label"] = label   # fleetboard's label (cclimits slug may differ)
            acc["reserve_percent"] = r
            acc["reserve_blocked"] = r > 0 and (acc.get("headroom_percent") or 0) < r
    _limits["data"], _limits["t"] = data, now
    return data


def account_reserve(label):
    """Headroom % this account must keep free before auto-dispatch treats it
    as full. Per-account override, else '*' default, else 0."""
    rp = CFG.get("reserve_percent") or {}
    if not isinstance(rp, dict):
        return 0
    return rp.get(label, rp.get("*", 0)) or 0


def _model_remaining(acc, model):
    """Min remaining % across the limits that running `model` consumes on this
    account: all non-model-scoped limits (session, weekly) + the model-scoped
    limit matching `model`, if the account has one. None if unknown."""
    if not acc.get("ok"):
        return None
    rems = []
    for l in acc.get("limits", []):
        rem = l.get("remaining_percent")
        if rem is None:
            rem = 100 - (l.get("percent") or 0)
        if l.get("model_scoped"):
            if model and model.lower() in (l.get("label", "").lower()):
                rems.append(rem)     # this model's own cap
        else:
            rems.append(rem)         # session / weekly always apply
    return min(rems) if rems else None


def model_candidates(model, only_account=None):
    """Accounts that could run `model`, each with remaining headroom and whether
    it clears its reserve buffer. Sorted by most remaining first."""
    data = _limits["data"] if not DEMO else demo_limits()
    if not data or not data.get("available"):
        return []
    excl = set(CFG.get("exclude_accounts") or [])
    out = []
    for acc in data.get("accounts", []):
        if not acc.get("ok"):
            continue
        label = account_label(Path(acc["config_dir"]))
        if only_account:
            if label != only_account:
                continue
        elif label in excl:
            continue
        rem = _model_remaining(acc, model)
        if rem is None:
            continue
        reserve = account_reserve(label)
        out.append({"label": label, "remaining": round(rem),
                    "reserve": reserve, "ok": rem > 0 and rem >= reserve})
    out.sort(key=lambda x: -x["remaining"])
    return out


def set_reserve(label, percent):
    """Set an account's reserve buffer from the UI: update CFG, persist to the
    config file, and re-apply to the cached limits so it takes effect at once."""
    if not label:
        return {"ok": False, "error": "no account"}
    try:
        percent = max(0, min(95, int(percent)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "percent must be a number"}
    rp = dict(CFG.get("reserve_percent") or {})
    if percent == 0:
        rp.pop(label, None)
    else:
        rp[label] = percent
    CFG["reserve_percent"] = rp
    # persist: merge into the on-disk config (create if missing)
    try:
        disk = {}
        if CONFIG_PATH and CONFIG_PATH.is_file():
            disk = json.loads(CONFIG_PATH.read_text())
        disk["reserve_percent"] = rp
        CONFIG_PATH.write_text(json.dumps(disk, indent=2) + "\n")
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"couldn't write config: {e}"}
    # re-enrich cached limits so the change shows without a refetch
    data = _limits.get("data")
    if data and data.get("accounts"):
        for acc in data["accounts"]:
            if acc.get("config_dir"):
                r = account_reserve(account_label(Path(acc["config_dir"])))
                acc["reserve_percent"] = r
                acc["reserve_blocked"] = r > 0 and (acc.get("headroom_percent") or 0) < r
    return {"ok": True, "label": label, "percent": percent}


def limits_by_account():
    """account label -> {exhausted, worst, resets_in, headroom, reserve, available}."""
    data = _limits["data"] if not DEMO else demo_limits()
    if not data or not data.get("available"):
        return {}
    out = {}
    for acc in data.get("accounts", []):
        if not acc.get("ok"):
            continue
        label = account_label(Path(acc["config_dir"]))
        exhausted = [l for l in acc.get("limits", []) if l.get("exhausted_now")]
        worst = min(exhausted, key=lambda l: l.get("resets_in_seconds") or 0) if exhausted else None
        resets_in = worst.get("resets_in_seconds") if worst else None
        fetched = (data.get("fetched_at") or time.time())
        headroom = acc.get("headroom_percent")
        reserve = account_reserve(label)
        # reserve-blocked: less than the required buffer remains → treat as full
        reserve_blocked = reserve > 0 and headroom is not None and headroom < reserve
        out[label] = {
            "headroom": headroom,
            "exhausted": bool(exhausted),
            "worst": worst["label"] if worst else None,
            "worst_scoped": bool(worst.get("model_scoped")) if worst else False,
            "group": worst.get("group") if worst else None,
            "resets_in": resets_in,
            "resets_at": fetched + resets_in if resets_in else None,
            "reserve": reserve,
            "reserve_blocked": reserve_blocked,
            # usable for AUTO dispatch/router: has real headroom above its buffer
            "available": (not exhausted) and not reserve_blocked,
        }
    return out


def demo_limits():
    now = time.time()
    def lim(label, group, pct, ex, resets_h, scoped=False):
        return {"label": label, "group": group, "percent": pct,
                "remaining_percent": 100 - pct, "model_scoped": scoped,
                "exhausted_now": ex, "resets_at": None,
                "resets_in_seconds": resets_h * 3600}
    return {"available": True, "fetched_at": now, "generated_at": None, "accounts": [
        {"slug": "default", "email": None, "plan": "max", "config_dir": "~/.claude",
         "ok": True, "error": None, "headroom_percent": 62.0, "limits": [
            lim("Session", "session", 21, False, 3.2), lim("Weekly", "weekly", 38, False, 96)]},
        {"slug": "work", "email": None, "plan": "max", "config_dir": "~/.claude-work",
         "ok": True, "error": None, "headroom_percent": 0.0, "limits": [
            lim("Session", "session", 100, True, 2.1), lim("Weekly", "weekly", 91, False, 30)]},
        {"slug": "spare", "email": None, "plan": "pro", "config_dir": "~/.claude-spare",
         "ok": True, "error": None, "headroom_percent": 88.0, "limits": [
            lim("Session", "session", 4, False, 4.8), lim("Weekly", "weekly", 12, False, 120)]},
    ]}


# --------------------------------------------------------------- focus jump

_FOCUS_TERMINAL = '''
tell application "Terminal"
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (tty of t) is "%s" then
          set selected tab of w to t
          set index of w to 1
          set found to true
        end if
      end try
    end repeat
  end repeat
  if found then activate
  return found
end tell'''

_FOCUS_ITERM = '''
tell application "iTerm2"
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if (tty of s) is "%s" then
            tell s to select
            tell t to select
            select w
            set found to true
          end if
        end try
      end repeat
    end repeat
  end repeat
  if found then activate
  return found
end tell'''


def focus_process(pid):
    """Best-effort: bring the terminal window hosting `pid` to the front."""
    proc = next((p for p in claude_processes() if p["pid"] == pid), None)
    if not proc:
        return {"ok": False, "message": f"pid {pid} is gone"}
    tty, host, kind = proc["tty"], proc["host"], proc["host_kind"]
    where = f"pid {pid}" + (f" · {tty}" if tty else "")
    if kind == "tmux":
        # Open a real Terminal window attached to the session (read-write —
        # you can type in it directly). Detach later with Ctrl-b d.
        sock = proc.get("tmux_sock")
        session = (proc.get("tmux_target") or "").split(":", 1)[0]
        if not session:
            return {"ok": False, "message": f"{where}: couldn't resolve tmux session"}
        attach = "tmux" + (f" -L {shlex.quote(sock)}" if sock else "") + \
                 f" attach -t {shlex.quote(session)}"
        script = ('tell application "Terminal"\n  do script "%s"\n  activate\nend tell'
                  % _osa_escape(attach))
        rc, _ = run(["osascript", "-e", script], timeout=8)
        if rc == 0:
            return {"ok": True, "message": f"opened Terminal attached to {session} (Ctrl-b d to detach)"}
        return {"ok": False, "message":
                f"couldn't open Terminal — grant Automation permission, or run:  {attach}"}
    if host in ("Terminal", "iTerm2") and tty:
        script = (_FOCUS_TERMINAL if host == "Terminal" else _FOCUS_ITERM) % f"/dev/{tty}"
        rc, out = run(["osascript", "-e", script], timeout=8)
        if rc == 0 and out.strip() == "true":
            return {"ok": True, "message": f"focused {host} window ({tty})"}
        if rc != 0:
            return {"ok": False, "message":
                    f"couldn't script {host} — grant Automation permission "
                    f"(System Settings → Privacy → Automation), or find {tty} manually"}
        return {"ok": False, "message": f"no {host} tab with {tty} found"}
    if host in ("Cursor", "VS Code"):
        app = "Cursor" if host == "Cursor" else "Visual Studio Code"
        run(["open", "-a", app])
        return {"ok": True, "message":
                f"{where} lives in an embedded terminal inside {host} — "
                f"activated it, check its terminal panel"}
    if host:
        return {"ok": True, "message": f"{where} runs in {host} — look for {tty}"}
    return {"ok": False, "message": f"unknown host for {where}"}


# ----------------------------------------------------- talk to agents (send)

_SEND_TERMINAL = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (tty of t) is "%s" then
          do script "%s" in t
          return true
        end if
      end try
    end repeat
  end repeat
  return false
end tell'''

_SEND_ITERM = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if (tty of s) is "%s" then
            tell s to write text "%s"
            return true
          end if
        end try
      end repeat
    end repeat
  end repeat
  return false
end tell'''


def _osa_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_to_process(pid, text):
    """Type `text` + Enter into the terminal hosting a claude process."""
    if DEMO:
        return {"ok": False, "message": "demo mode — no live agents to talk to"}
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    if not text:
        return {"ok": False, "message": "empty message"}
    proc = next((p for p in claude_processes() if p["pid"] == pid), None)
    if not proc:
        return {"ok": False, "message": f"pid {pid} is gone"}
    if proc.get("tmux_target"):
        sock = ["-L", proc["tmux_sock"]] if proc["tmux_sock"] else []
        rc1, _ = run(["tmux"] + sock + ["send-keys", "-t", proc["tmux_target"], "-l", text])
        rc2, _ = run(["tmux"] + sock + ["send-keys", "-t", proc["tmux_target"], "Enter"])
        ok = rc1 == 0 and rc2 == 0
        return {"ok": ok, "message": "sent via tmux" if ok else "tmux send-keys failed"}
    if proc["host"] in ("Terminal", "iTerm2") and proc["tty"]:
        script = (_SEND_TERMINAL if proc["host"] == "Terminal" else _SEND_ITERM) % (
            f"/dev/{proc['tty']}", _osa_escape(text))
        rc, out = run(["osascript", "-e", script], timeout=10)
        if rc == 0 and out.strip() == "true":
            return {"ok": True, "message": f"typed into {proc['host']} ({proc['tty']})"}
        return {"ok": False, "message":
                f"couldn't reach {proc['host']} — Automation permission? ({proc['tty']})"}
    return {"ok": False, "message":
            f"{proc['host'] or 'unknown host'} terminals can't be scripted — focus it instead"}


# ------------------------------------------------------------- chat reader

def read_chat(account, sid, limit=40):
    """Last conversation turns of a session, from its transcript."""
    home = next((h for h in claude_homes() if account_label(h) == account), None)
    if not home:
        return {"ok": False, "error": f"unknown account {account}"}
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    if not fp:
        return {"ok": False, "error": "transcript not found"}
    msgs = []
    for line in _read_chunk(fp, 512 * 1024, from_end=True).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
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
                if _real_prompt(t):
                    msgs.append({"role": "you", "text": _clean(t, 900), "ts": e.get("timestamp")})
        elif e.get("type") == "assistant" and isinstance(c, list):
            parts = [b["text"] for b in c
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
            if parts:
                msgs.append({"role": "agent", "text": _clean(" ".join(parts), 900),
                             "ts": e.get("timestamp")})
    return {"ok": True, "messages": msgs[-limit:]}


# --------------------------------------------------------------- dispatch

FLEET_SOCK = "fleet"
DISPATCH_LOG = HERE / "dispatch.log.jsonl"


def read_dispatch_log(limit=25):
    """Recent dispatches, newest first, each annotated with whether its tmux
    session is still alive."""
    if not DISPATCH_LOG.exists():
        return {"entries": []}
    rows = []
    try:
        for line in DISPATCH_LOG.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return {"entries": []}
    live = set()
    rc, out = run(["tmux", "-L", FLEET_SOCK, "list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        live = set(out.splitlines())
    for r in rows:
        r["alive"] = r.get("session") in live
    return {"entries": rows[-limit:][::-1]}


def _pick_defaults(mission):
    state = cached_state()
    cached_limits()  # ensure the account picker isn't working from a cold cache
    limits = limits_by_account()
    free = [w for w in state["worktrees"] if w["availability"] == "free"]
    free.sort(key=lambda w: (w["git"]["dirty"] or 0))
    wt = free[0]["name"] if free else None
    excl = set(CFG.get("exclude_accounts") or [])
    accounts = [(a, d) for a, d in limits.items() if d["available"] and a not in excl]
    accounts.sort(key=lambda x: -(x[1]["headroom"] or 0))
    acct = accounts[0][0] if accounts else None
    return wt, acct


def _route_with_claude(mission, on_text=None):
    """One-shot routing call: haiku picks worktree/account/model/effort/branch.
    If on_text is given, streams haiku's output through it as it generates."""
    state = cached_state()
    limits = limits_by_account()
    ex_map = {}
    for acc in (_limits["data"] or {}).get("accounts", []):
        label = account_label(Path(acc["config_dir"]))
        ex_map[label] = [l["label"] for l in acc.get("limits", []) if l.get("exhausted_now")]
    fleet = {
        "worktrees": [{"name": w["name"], "branch": w["git"]["branch"],
                       "dirty": w["git"]["dirty"], "availability": w["availability"]}
                      for w in state["worktrees"]],
        "accounts": [{"label": a, "headroom_pct": d["headroom"],
                      "exhausted": d["exhausted"] or d["reserve_blocked"],
                      "exhausted_limits": ex_map.get(a, []),
                      "reserve_pct": d["reserve"],
                      "reserve_blocked": d["reserve_blocked"],
                      "available": d["available"] and a not in (CFG.get("exclude_accounts") or [])}
                     for a, d in limits.items()],
    }
    # The router runs `claude -p` under some account's config dir. Never let it
    # touch an excluded account (e.g. ~/.claude) — a stale token there could
    # hijack a browser session. A fixed router_home removes the rotation risk.
    router_home = None
    if CFG.get("router_home"):
        rh = Path(CFG["router_home"]).expanduser()
        router_home = rh if (rh / "projects").is_dir() else None
    if router_home is None:
        excl = set(CFG.get("exclude_accounts") or [])
        ok_accounts = sorted((d["headroom"] or 0, a) for a, d in limits.items()
                             if d["available"] and a not in excl)
        if ok_accounts:
            router_home = next((h for h in claude_homes()
                                if account_label(h) == ok_accounts[-1][1]), None)
    prompt = (
        "You route work across git worktrees and Claude accounts. You do NOT "
        "rewrite the mission — the author's text is passed to the agent "
        "verbatim; you only make routing decisions. Fleet state:\n"
        + json.dumps(fleet) + "\n\nMission from the user:\n" + mission +
        "\n\nPick a FREE worktree (prefer clean ones and a thematic fit with its "
        "branch) and an account. ONLY choose an account whose \"available\" is "
        "true — treat exhausted, reserve_blocked (its headroom is below the "
        "reserve buffer it must keep free), or unavailable accounts as off "
        "limits. Prefer the available account with the most headroom. Also pick "
        "the model (fable > opus > sonnet by capability; NEVER a model listed "
        "in that account's exhausted_limits — e.g. if 'Fable' is exhausted "
        "there, pick opus or another account) and the reasoning effort: "
        "'ultracode' for genuinely hard multi-step features, 'xhigh' for "
        "research or medium tasks, 'high' for simple tasks. Finally suggest a "
        "conventional branch name for this mission (feat/fix/docs/...), or "
        "null if the worktree's current branch is clearly the right home. "
        "Reply with ONLY this JSON, no fences:\n"
        '{"worktree": "...", "account": "...", "model": "fable|opus|sonnet|haiku", '
        '"effort": "high|xhigh|ultracode", "branch": "feat/...|null"}')
    env = dict(os.environ)
    if router_home:
        env["CLAUDE_CONFIG_DIR"] = str(router_home)
    # Non-streaming path (kept for callers without a progress sink).
    if on_text is None:
        try:
            p = subprocess.run(["claude", "-p", "--model", "haiku", prompt],
                               capture_output=True, text=True, timeout=120, env=env)
            d = json.loads(re.search(r"\{.*\}", p.stdout, re.S).group(0))
            if d.get("worktree") and d.get("account"):
                return d
        except Exception:
            pass
        return None
    # Streaming path: forward haiku's text as it lands (for display), but parse
    # the authoritative final text from the 'result' event.
    streamed, result_text = [], None
    try:
        proc = subprocess.Popen(
            ["claude", "-p", "--model", "haiku",
             "--output-format", "stream-json", "--verbose", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        for line in proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("type") == "assistant":
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                        streamed.append(b["text"]); on_text(b["text"])
            elif e.get("type") == "result" and e.get("result"):
                result_text = e["result"]
        proc.wait(timeout=120)
        text = result_text if result_text else "".join(streamed)
        d = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        if d.get("worktree") and d.get("account"):
            return d
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    return None


_jobs = {}                 # job_id -> {progress, done, result}
_jobs_lock = threading.Lock()
_job_seq = [0]


def _log(job, line):
    with _jobs_lock:
        job["progress"].append(line)


def start_dispatch(mission, worktree=None, account=None, use_router=False,
                   model=None, effort=None, force_model=False):
    """Kick a dispatch off in the background; return a job id to poll.
    If an explicit model has no account with enough headroom (above reserve),
    return a needs_decision instead of launching — unless force_model."""
    if not DEMO and model and not force_model:
        cached_limits()
        cands = model_candidates(model, only_account=account)
        if not any(c["ok"] for c in cands):
            best = cands[0] if cands else None
            opus = model_candidates("opus", only_account=account)
            best_opus = next((c for c in opus if c["ok"]), None)
            where = f"account [{account}]" if account else "any account"
            if best and best["reserve"] > 0:
                detail = (f"best is [{best['label']}] at {best['remaining']}% "
                          f"left, below its {best['reserve']}% reserve")
            elif best:
                detail = (f"best is [{best['label']}] at {best['remaining']}% — "
                          f"the {model} limit is used up")
            else:
                detail = "no readable account for this model"
            return {"ok": False, "needs_decision": True, "model": model,
                    "message": f"No {model} headroom on {where} — {detail}.",
                    "can_opus": bool(best_opus),
                    "opus_account": best_opus["label"] if best_opus else None,
                    "opus_left": best_opus["remaining"] if best_opus else None}
    _job_seq[0] += 1
    job_id = "job-" + time.strftime("%H%M%S") + f"-{_job_seq[0]}"
    job = {"progress": [], "done": False, "result": None}
    with _jobs_lock:
        _jobs[job_id] = job
        for old in list(_jobs)[:-20]:   # keep only the last 20 jobs
            del _jobs[old]
    threading.Thread(target=_run_dispatch, daemon=True, args=(
        job, mission, worktree, account, use_router, model, effort)).start()
    return {"job": job_id}


def _run_dispatch(job, mission, worktree, account, use_router, model, effort):
    def finish(result):
        with _jobs_lock:
            job["result"] = result
            job["done"] = True

    if DEMO:
        return finish({"ok": False, "message": "demo mode — dispatch disabled"})
    mission = (mission or "").strip()
    if not mission:
        return finish({"ok": False, "message": "empty mission"})

    routed = None
    if use_router and not (worktree and account and model and effort):
        _log(job, "① routing — asking claude (haiku) where this should run…")
        buf = [""]

        def on_text(t):  # stream haiku's output, line by line
            buf[0] += t
            while "\n" in buf[0]:
                head, buf[0] = buf[0].split("\n", 1)
                if head.strip():
                    _log(job, "  haiku › " + head.strip()[:160])
        routed = _route_with_claude(mission, on_text=on_text)
        if buf[0].strip():
            _log(job, "  haiku › " + buf[0].strip()[:160])
        if not routed:
            _log(job, "  router failed — falling back to rule-based picks")

    branch = None
    if routed:
        worktree = worktree or routed["worktree"]
        account = account or routed["account"]
        model = model or routed.get("model")
        effort = effort or routed.get("effort")
        b = routed.get("branch")
        branch = b if isinstance(b, str) and b and b.lower() != "null" else None
        _log(job, f"✓ routed → {worktree} · [{account}]"
                  + (f" · {model}" if model else "") + (f" · {effort}" if effort else "")
                  + (f" · {branch}" if branch else ""))

    if not (worktree and account):
        dw, da = _pick_defaults(mission)
        worktree, account = worktree or dw, account or da
        _log(job, f"✓ picked → {worktree} · [{account}] (rules)")
    if not worktree:
        return finish({"ok": False, "message": "no free worktree available"})
    if not account:
        return finish({"ok": False, "message": "every account is exhausted"})
    wt = next((w for w in discover_worktrees() if w["name"] == worktree), None)
    if not wt:
        return finish({"ok": False, "message": f"unknown worktree {worktree}"})
    home = next((h for h in claude_homes() if account_label(h) == account), None)
    if not home:
        return finish({"ok": False, "message": f"unknown account {account}"})

    header = (f"First check out a new branch {branch} from latest origin/main. "
              if branch else
              "If this worktree's current branch is not the right home for this "
              "work, check out an appropriately named new branch from latest "
              "origin/main first. ")
    kickoff = (header + "Commit as you go. Your mission, in the author's own "
               "words: " + mission)

    name = "mission-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree).strip("-").lower() \
           + time.strftime("-%H%M%S")
    model_flag = f" --model {shlex.quote(model)}" if model else ""
    shell_cmd = (f"CLAUDE_CONFIG_DIR={shlex.quote(str(home))} "
                 f"exec claude --dangerously-skip-permissions{model_flag}")
    _log(job, f"② creating tmux session {name}…")
    rc, out = run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                   "-c", wt["path"], shell_cmd])
    if rc != 0:
        return finish({"ok": False, "message": f"tmux failed: {out or 'is tmux installed?'}"})
    brief = re.sub(r"\s*\n\s*", " ", kickoff).strip()

    def keys(*args):
        run(["tmux", "-L", FLEET_SOCK, "send-keys", "-t", name] + list(args))

    _log(job, "③ booting claude…")
    time.sleep(6)
    effort_confirmed = None
    if effort:
        _log(job, f"④ setting effort {effort}…")
        keys("-l", f"/effort {effort}")
        keys("Enter")
        time.sleep(3)
        _, pane = run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        effort_confirmed = "set effort level" in pane.lower()
        _log(job, "  effort " + ("confirmed ✓" if effort_confirmed else "UNCONFIRMED ⚠"))
        if not effort_confirmed:
            keys("Escape")
            time.sleep(1)
    _log(job, "⑤ sending kickoff brief…")
    keys("-l", brief)
    keys("Enter")

    try:  # audit trail: every dispatch, with the author's original words
        with open(DISPATCH_LOG, "a") as lf:
            lf.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": name,
                "worktree": worktree, "account": account, "model": model,
                "effort": effort, "routed": bool(routed),
                "mission_original": mission, "kickoff": kickoff}) + "\n")
    except OSError:
        pass
    effort_note = ""
    if effort:
        effort_note = f" · effort {effort} " + ("✓" if effort_confirmed else "UNCONFIRMED")
    _log(job, "✓ launched")
    finish({"ok": True,
            "message": f"launched {name} in {worktree} on [{account}]"
                       + (f" · {model}" if model else "") + effort_note
                       + (" (routed by claude)" if routed else " (rule-picked)"),
            "session": name, "worktree": worktree, "account": account,
            "model": model, "effort": effort, "effort_confirmed": effort_confirmed,
            "kickoff": kickoff,
            "attach": f"tmux -L {FLEET_SOCK} attach -t {name}"})


def dispatch_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job"}
        return {"ok": True, "progress": list(job["progress"]),
                "done": job["done"], "result": job["result"]}


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            body = json.dumps(cached_state()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/focus"):
            m = re.search(r"pid=(\d+)", self.path)
            result = focus_process(int(m.group(1))) if m else {"ok": False, "message": "missing pid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/topology"):
            body = json.dumps(cached_topology()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/limits"):
            body = json.dumps(cached_limits(refresh="refresh=1" in self.path)).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatchlog"):
            body = json.dumps(read_dispatch_log()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatch/status"):
            m = re.search(r"job=([\w-]+)", self.path)
            body = json.dumps(dispatch_status(m.group(1)) if m
                              else {"ok": False, "error": "no job"}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/chat"):
            qa = re.search(r"account=([^&]+)", self.path)
            qs = re.search(r"sid=([0-9a-fA-F-]+)", self.path)
            result = read_chat(qa.group(1), qs.group(1)) if qa and qs else \
                {"ok": False, "error": "need account & sid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] in ("/", "/index", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/map"):
            body = (HERE / "map.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/limits"):
            body = (HERE / "limits.html").read_bytes()
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
            result = set_reserve(payload.get("account"), payload.get("percent"))
        elif self.path.startswith("/api/send"):
            result = send_to_process(int(payload.get("pid") or 0), payload.get("text") or "")
        elif self.path.startswith("/api/dispatch"):
            result = start_dispatch(
                payload.get("mission"), payload.get("worktree") or None,
                payload.get("account") or None, bool(payload.get("router")),
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


if __name__ == "__main__":
    args = load_config()
    DEMO = args.demo
    if CFG["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("fleetboard: WARNING — binding beyond loopback serves your "
              "transcript text to the network", file=sys.stderr)
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    mode = " (demo data)" if DEMO else ""
    print(f"fleetboard up → http://{CFG['host']}:{CFG['port']}{mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
