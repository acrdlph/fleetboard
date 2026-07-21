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

    python3 orchestra.py --root ~/code
    python3 orchestra.py --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > orchestra.config.json (next to this
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
    "exclude_accounts": [],    # account labels never AUTO-picked for dispatch
    "reserve_percent": {},     # {label: pct} buffer kept free before AUTO-pick treats account as full ("*" = default)
    "resume_delay_s": 60,      # auto-resume fires this long after the limit reset
    "resume_message": "continue",  # what auto-resume types at the stalled agent
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
    ap.add_argument("--port", type=int, help="port (default 4242, env ORCHESTRA_PORT)")
    ap.add_argument("--host", help="bind address (default 127.0.0.1 — the board serves your transcript text; do not expose it)")
    ap.add_argument("--window-h", type=float, help="ignore transcripts idle longer than this many hours (default 48)")
    ap.add_argument("--config", metavar="FILE", help="path to a orchestra.config.json")
    ap.add_argument("--demo", action="store_true", help="serve fictional demo data (for screenshots)")
    args = ap.parse_args(argv)

    global CONFIG_PATH
    candidates = [Path(args.config)] if args.config else [
        HERE / "orchestra.config.json", Path.cwd() / "orchestra.config.json"]
    for p in candidates:
        if p.is_file():
            try:
                CFG.update(json.loads(p.read_text()))
            except (ValueError, OSError) as e:
                sys.exit(f"orchestra: bad config {p}: {e}")
            CONFIG_PATH = p
            break
    if CONFIG_PATH is None:  # where a UI edit will create/persist config
        CONFIG_PATH = Path(args.config) if args.config else HERE / "orchestra.config.json"
    if os.environ.get("ORCHESTRA_PORT"):
        CFG["port"] = int(os.environ["ORCHESTRA_PORT"])
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


def pair_sessions_with_procs(sessions, wt_procs):
    """sid -> the process running that session, for one worktree.

    A worktree with several agents can't say which terminal belongs to which
    chat on process data alone; CLAUDE_CONFIG_DIR is the only thing tying a
    process to an account, and sessions know their account. Exact account
    matches are claimed first so a fresher session can't take a process that
    demonstrably belongs to an older one. Anything unmatched falls back to
    freshness order — the behaviour the bare slot count had on its own, which
    is what you get when the environment can't be read.

    `sessions` must be freshest-first. At most one process per session, so the
    number of live sessions can never exceed the number of processes.
    """
    pool = list(wt_procs)
    owner = {}
    for s in sessions:
        match = next((p for p in pool if p.get("account")
                      and p["account"] == s["account"]), None)
        if match:
            pool.remove(match)
            owner[s["sid"]] = match
    for s in sessions:
        if s["sid"] not in owner and pool:
            owner[s["sid"]] = pool.pop(0)
    return owner


def _pid_config_dirs(pids):
    """pid -> CLAUDE_CONFIG_DIR, so a terminal can be attributed to an account.

    Sessions know their account and processes don't, which is why a worktree
    running two agents can't say which terminal belongs to which chat. The env
    is the only place that link exists — claude doesn't hold its transcript
    open, so there's nothing to match on via lsof."""
    dirs = {}
    if not pids:
        return dirs
    if sys.platform.startswith("linux"):
        for pid in pids:
            try:
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    for kv in fh.read().split(b"\0"):
                        if kv.startswith(b"CLAUDE_CONFIG_DIR="):
                            dirs[pid] = kv.split(b"=", 1)[1].decode("utf-8", "replace")
                            break
            except OSError:
                pass
    else:  # macOS/BSD: `ps eww` appends the environment to the command column
        _, out = run(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                     timeout=10)
        for line in out.splitlines():
            m = re.match(r"\s*(\d+)\s+(.*)", line)
            if not m:
                continue
            env = re.search(r"CLAUDE_CONFIG_DIR=(\S+)", m.group(2))
            if env:
                dirs[int(m.group(1))] = env.group(1)
    return dirs


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


# The Bash tool wraps every command — foreground or run_in_background — in a
# `zsh/bash -c` child of the claude process. The wrapper is recognisable by any
# of: the shell-snapshot it sources, its setopt prelude, or the cwd file it
# records into /tmp/claude-XXXX-cwd on exit.
_BASH_WRAPPER = re.compile(
    r"shell-snapshots/snapshot-|setopt NO_EXTENDED_GLOB|pwd -P >\| /tmp/claude-\w+-cwd")


def shell_children(table, claude_pids):
    """claude pid -> count of live Bash-tool shells running under it.

    A live wrapper shell proves its claude is mid-tool even when the transcript
    has gone quiet — a backgrounded build writes nothing to the transcript
    until it exits, at which point the agent resumes on its own."""
    counts = {}
    for pid, (ppid, _tty, cmd) in table.items():
        head = cmd.split(" ", 1)[0].rsplit("/", 1)[-1]
        if head not in ("zsh", "bash", "sh") or not _BASH_WRAPPER.search(cmd):
            continue
        anc = ppid
        for _ in range(6):  # observed depth is 1; allow a few wrapper hops
            if anc in claude_pids:
                counts[anc] = counts.get(anc, 0) + 1
                break
            anc = table.get(anc, (None,))[0]
            if not anc or anc == 1:
                break
    return counts


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
    shells = shell_children(table, {p["pid"] for p in procs})
    cwds = _pid_cwds([p["pid"] for p in procs])
    cfgdirs = _pid_config_dirs([p["pid"] for p in procs])
    # If the lookup came back empty for every process the mechanism itself
    # failed (no permission, ps unavailable) — leave accounts unknown so
    # pairing degrades to freshness rather than filing everything under "main".
    env_readable = bool(cfgdirs)
    pane_maps = {}
    for p in procs:
        p["shells"] = shells.get(p["pid"], 0)
        p["cwd"] = cwds.get(p["pid"])
        cfg = cfgdirs.get(p["pid"])
        # bare `claude` with no CLAUDE_CONFIG_DIR set is the default home
        p["account"] = account_label(Path(cfg)) if cfg else \
            ("main" if env_readable else None)
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
           "last_assistant": None, "last_user": None, "pending_workflows": 0,
           "pending_bg_agents": 0}
    pending = {}  # tool_use id -> tool name
    for e in main:
        out["cwd"] = e.get("cwd") or out["cwd"]
        out["branch"] = e.get("gitBranch") or out["branch"]
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            # a turn that ended still awaiting workflows or background agents
            # ("✻ Waiting for 1 background agent to finish") is NOT the user's
            # turn — the harness resumes the session when they report back
            out["pending_workflows"] = e.get("pendingWorkflowCount") or 0
            out["pending_bg_agents"] = e.get("pendingBackgroundAgentCount") or 0
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


def match_worktree(proj_name, wt_prefixes):
    """Map a munged project-dir name to a worktree path by the LONGEST matching
    prefix, so 'myapp' doesn't swallow 'myapp-audit'. Returns None if none match.
    `wt_prefixes` is {worktree_path: munged_prefix}."""
    best = None
    for path, pref in wt_prefixes.items():
        if proj_name == pref or proj_name.startswith(pref + "-"):
            if best is None or len(pref) > len(wt_prefixes[best]):
                best = path
    return best


def classify_session(age_s, alive, pending_tools, delegated,
                     skip_perms, working_s, shells=0):
    """Base session status from observable signals (before limit/handoff
    overrides). `delegated` counts pending workflows + background agents.
    Returns (status, tool_running)."""
    pend = pending_tools or []
    if age_s < working_s:
        return "working", False
    if alive and "AskUserQuestion" in pend:
        return "needs_input", False
    if alive and delegated:                  # awaiting its own workflows or
        return "working", False              # background agents — not the
                                             # user's turn
    if alive and shells:                     # a Bash shell is still running —
        return "working", True               # backgrounded ones leave the
                                             # transcript idle until they exit
    if alive and pend and skip_perms:        # long tool run, nothing to approve
        return "working", True
    if alive and pend:
        return "blocked", False
    if alive:
        return "waiting", False
    return "ended", False


def closeout_step(paired_status, any_working, sent, now, nudge_after_s=60):
    """Step two of ✓ finish (✕ close) when the landing won't verify: decide,
    from THIS worktree's live session, whether to refuse, send the user to
    ✉ chat, or type one nudge at the idle agent. Pure (no subprocess/tmux) so
    the whole table is unit-testable.

    The deadlock this breaks: an agent finished its closeout but left one dirty
    file it judged "another session's in-flight work" — and no other session
    was live, so nothing would ever converge. ✕ close refused forever while the
    agent idled forever. A nudge that names the leftovers and says they are
    this agent's to judge is the only exit.

    Ordering is the point. Any working session (it might be mid-closeout)
    refuses before anything else, so we never type over live work. A stuck
    agent goes to chat rather than get a nudge typed across its open dialog.
    Only a plainly idle ('waiting') agent, briefed at least nudge_after_s ago
    (the anti-double-type guard against rapid clicks), gets nudged. A scan that
    couldn't pair any session with the live pid returns None here and we refuse
    — never type into a terminal we can't classify.
    """
    if any_working:
        return "refuse"
    if paired_status in ("needs_input", "blocked"):
        return "chat"
    if paired_status == "waiting":
        return "nudge" if now - sent >= nudge_after_s else "refuse"
    return "refuse"


def scan_sessions(worktrees, procs, now):
    """All recent sessions across every Claude home, mapped to worktrees."""
    by_wt = {w["path"]: [] for w in worktrees}
    wt_prefixes = {w["path"]: munge(w["path"]) for w in worktrees}
    window_s = CFG["session_window_h"] * 3600

    for home in claude_homes():
        acct = account_label(home)
        for proj in (home / "projects").iterdir():
            wt = match_worktree(proj.name, wt_prefixes)
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
                    "pending_bg_agents": tail["pending_bg_agents"],
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
        # With --dangerously-skip-permissions there are no approval prompts:
        # an unresolved tool call means a long-running tool, not "blocked".
        skip_perms = bool(wt_procs) and all(
            "--dangerously-skip-permissions" in p["cmd"] for p in wt_procs)

        owner = pair_sessions_with_procs(sessions, wt_procs)
        for s in sessions:
            proc = owner.get(s["sid"])
            alive = proc is not None
            shell_n = proc.get("shells", 0) if proc else 0
            s["pid"] = proc["pid"] if proc else None
            # only an account match is a real attribution; a fallback pairing is
            # a guess and must not be presented as one
            s["pid_certain"] = bool(proc and proc.get("account") == s["account"])
            status, tool_running = classify_session(
                s["age_s"], alive, s["pending_tools"],
                s["pending_workflows"] + s["pending_bg_agents"],
                skip_perms, CFG["working_s"], shell_n)
            s["status"] = status
            if tool_running:
                s["tool_running"] = True
                if shell_n and not s["pending_tools"]:
                    s["bg_shell"] = True     # transcript idle, shell alive
        sessions.sort(key=lambda s: (rank[s["status"]], s["age_s"]))
        by_wt[wt] = sessions[: CFG["max_sessions"]]
    return by_wt


def card_availability(st, has_live):
    """Card-level triage from its sessions' statuses (post handoff filter)."""
    if not (has_live or "working" in st):
        return "free"          # safe to point a new agent here
    if any(k in st for k in ("needs_input", "blocked")):
        return "attention"     # hard blocker — needs you
    if "waiting" in st and "working" not in st:
        return "attention"     # everyone parked at the prompt — needs direction
    if "limit" in st and "working" not in st:
        # out of juice, not out of instructions — nothing you can do until the
        # limit resets, so it is NOT "needs you". (This card-level "waiting" is
        # unrelated to the session status "waiting" = idle at the prompt.)
        return "waiting"
    return "busy"              # something is actively working


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
            smodel = (s["model"] or "").lower()
            lim = None
            if al and al["exhausted"]:
                # account-wide cap (session / umbrella weekly) — bites every model here
                lim = {"worst": al["worst"], "group": al["group"],
                       "resets_in": al["resets_in"], "resets_at": al["resets_at"]}
            elif al:
                # a model-scoped cap only strands a session running THAT model
                hit = next((sx for sx in al.get("scoped_exhausted", [])
                            if (sx["label"] or "").lower() in smodel), None)
                if hit:
                    lim = {"worst": hit["label"], "group": hit["group"],
                           "resets_in": hit["resets_in"], "resets_at": hit["resets_at"]}
            if lim:
                s["status"] = "limit"
                s["limit"] = lim
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
                            "account": p.get("account"),
                            "tmux": p.get("tmux_target"),
                            "reachable": bool(p.get("tmux_target") or
                                              (p["host"] in ("Terminal", "iTerm2") and p["tty"])),
                            "subdir": os.path.relpath(p["cwd"], w["path"])
                            if p["cwd"] != w["path"] else None} for p in live],
        })

    for c in cards:
        c["availability"] = card_availability(
            _attention_statuses(c["sessions"]), bool(c["live_procs"]))
        # two-step finish: while a closeout brief is with this card's live
        # agent, the button reads ✕ close. The flag dies with the terminal,
        # so a card never offers to close an agent that no longer exists.
        ts = _closeouts.get(c["name"])
        if ts:
            if c["live_procs"]:
                c["closeout_sent"] = ts
            else:
                _closeouts.pop(c["name"], None)

    matched = {p["pid"] for c in cards for p in c["live_procs"]}
    other = [p for p in procs if p["pid"] not in matched]

    def severity(c):
        st = _attention_statuses(c["sessions"])
        if "needs_input" in st: return 0
        if "blocked" in st: return 1
        if "waiting" in st and "working" not in st: return 2
        if "working" in st: return 3
        if "limit" in st: return 4   # un-actionable — parked behind the busy ones
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

    seq = [0]

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None, sid=None):
        seq[0] += 1
        return {"id": "demo0000", "sid": sid or f"demo-{seq[0]}",
                "account": acct, "status": status, "age_s": age,
                "cwd": "/demo", "subdir": subdir, "branch": None, "model": model,
                "pending_tools": pend or [], "topic": topic, "last_assistant": said}

    def card(name, avail, branch, dirty, ahead, behind, cts, subject, sessions, pids):
        procs = [{"pid": p, "cpu": 4.2, "etime": "02:14:33", "subdir": None,
                  "tty": f"ttys{p % 1000:03d}", "host": "Terminal",
                  "account": None, "tmux": None, "reachable": True} for p in pids]
        # mirror the real pairing: each live session owns one terminal, and the
        # process advertises that session's account
        live = [s for s in sessions if s["status"] != "ended"]
        for s, p in zip(live, procs):
            s["pid"], s["pid_certain"] = p["pid"], True
            p["account"] = s["account"]
        return {"name": name, "path": "/demo/" + name, "git_root": "",
                "git": {"branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind,
                        "commit": {"hash": "a1b2c3d", "ts": int(now - cts), "subject": subject}},
                "sessions": sessions, "availability": avail, "live_procs": procs}

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
                      "I'll continue once usage is available again.",
                      sid="demo-limit-1"),
                      limit={"worst": "Session", "group": "session",
                             "resets_in": 7560, "resets_at": now + 7560}),
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
            acc["fb_label"] = label   # orchestra's label (cclimits slug may differ)
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
    """account label -> {exhausted, worst, resets_in, headroom, reserve, available,
    scoped_exhausted}.

    `exhausted`/`available` reflect only ACCOUNT-WIDE caps (session + umbrella
    weekly). A maxed model-scoped cap (e.g. Fable) is a per-model constraint,
    not an account-wide block — it lands in `scoped_exhausted` instead, so an
    account whose Fable is gone but that still has 40% all-model headroom stays
    pickable for an Opus/Sonnet mission. Collapsing every limit into one
    exhausted flag wrote such accounts off wholesale."""
    data = _limits["data"] if not DEMO else demo_limits()
    if not data or not data.get("available"):
        return {}
    fetched = (data.get("fetched_at") or time.time())
    out = {}
    for acc in data.get("accounts", []):
        if not acc.get("ok"):
            continue
        label = account_label(Path(acc["config_dir"]))
        ex = [l for l in acc.get("limits", []) if l.get("exhausted_now")]
        blocking = [l for l in ex if not l.get("model_scoped")]   # session / umbrella weekly
        worst = min(blocking, key=lambda l: l.get("resets_in_seconds") or 0) if blocking else None
        resets_in = worst.get("resets_in_seconds") if worst else None
        headroom = acc.get("headroom_percent")
        reserve = account_reserve(label)
        # reserve-blocked: less than the required buffer remains → treat as full
        reserve_blocked = reserve > 0 and headroom is not None and headroom < reserve
        out[label] = {
            "headroom": headroom,
            "exhausted": bool(blocking),
            "worst": worst["label"] if worst else None,
            "worst_scoped": False,   # `worst` is always an account-wide cap now
            "group": worst.get("group") if worst else None,
            "resets_in": resets_in,
            "resets_at": fetched + resets_in if resets_in else None,
            "reserve": reserve,
            "reserve_blocked": reserve_blocked,
            # model-scoped caps that are used up — only strand a session
            # actually running that model, not the whole account
            "scoped_exhausted": [
                {"label": l.get("label"), "group": l.get("group"),
                 "resets_in": l.get("resets_in_seconds"),
                 "resets_at": (fetched + l["resets_in_seconds"]) if l.get("resets_in_seconds") else None}
                for l in ex if l.get("model_scoped")],
            # usable for AUTO dispatch: real all-model headroom above its buffer
            "available": (not blocking) and not reserve_blocked,
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


# ------------------------------------------------------------- finish

# The full closeout brief, for a branch that still needs landing. ✓ finish
# hands it to an agent: the live one if a terminal exists, a freshly
# dispatched one if not. (Once a branch HAS landed, finish stops delegating —
# see SLIM_CLOSEOUT_TEXT and _park_on_trunk below.)
CLOSEOUT_TEXT = (
    "Close out this worktree now: "
    "1) wait for (or stop) any background agents and workflows you started; "
    "2) commit remaining meaningful work — drop scratch files; "
    "3) land the branch: merge {trunk} into it, resolve any conflicts — but "
    "if a conflict needs real judgment about the code, stop and report it "
    "instead of guessing — push the branch, then push it to the trunk and "
    "verify with `git merge-base --is-ancestor HEAD {trunk}`; "
    "4) switch this worktree to the trunk branch and pull, so it starts the "
    "next mission clean; "
    "5) reply with a one-line summary of what landed."
)

# The slim brief for a branch that already landed (HEAD is an ancestor of the
# trunk): nothing merged gets re-checked — only whatever is actually left.
SLIM_CLOSEOUT_TEXT = (
    "This worktree's branch has already landed on {trunk} — do not re-merge, "
    "re-push, or re-verify any of that. Close out only what's left: "
    "1) stop (or wait for) any background agents and workflows you started; "
    "2) drop scratch files; if meaningful uncommitted work remains, commit it "
    "and land it on {trunk} like a normal closeout; "
    "3) switch this worktree to the trunk branch and pull, so it starts the "
    "next mission clean; "
    "4) reply with one line saying what, if anything, was left to do."
)

# Follow-up typed at a live agent when step two (✕ close) still can't verify a
# clean landing AND nothing else is working in this worktree — so the leftover
# files are this agent's call, not "another session's in-flight work" it can
# quietly ignore. That misjudgment is the exact deadlock this breaks: without
# the nudge the agent idles forever and ✕ close refuses forever, because no
# other session exists to ever converge the tree. {files} is up to five raw
# `git status --porcelain` lines (blank when the branch simply hasn't landed).
CLOSEOUT_NUDGE_TEXT = (
    "Step two of the closeout still can't verify a clean landing: {left}. "
    "{files}"
    "No other session is working in this worktree, so these files are yours to "
    "judge — none of them is another session's in-flight work. Commit and land "
    "anything meaningful, drop scratch, and leave the tree clean on {trunk}. If "
    "any of it genuinely needs the user's decision, stop and ask explicitly — "
    "don't just leave it dirty."
)

# worktree name -> epoch seconds when a closeout brief was typed at its live
# agent. Step two of ✓ finish: while this is set (and the terminal lives) the
# board shows ✕ close instead — which only verifies the landing and /exits;
# it never re-types the brief into a mid-closeout agent. The flag dies with
# the terminal, so a fresh mission never inherits it.
_closeouts = {}


def _park_on_trunk(git_root, trunk):
    """Landed and clean: park the worktree on the trunk branch right here —
    two git commands don't need an agent. Returns None if the switch fails so
    the caller can hand it to an agent instead."""
    branch = trunk.split("/", 1)[-1]
    if run(["git", "switch", branch], cwd=git_root, timeout=30)[0] != 0:
        return None
    pulled = run(["git", "pull", "--ff-only", "--quiet"], cwd=git_root,
                 timeout=60)[0] == 0
    return {"ok": True, "mode": "parked", "message":
            f"already landed — parked on {branch}, no agent needed"
            + ("" if pulled else " (pull failed; next dispatch refreshes)")}


def closeout_shell(home, model, brief, trunk):
    """The tmux command for a one-shot closeout: run claude headless, then let
    git itself verify the landing. Verified clean -> exit, the tmux session
    dies, the card reads FREE with no second ✓ finish. Anything else -> resume
    the conversation interactively, so a failed closeout parks as needs-you
    instead of masquerading as free."""
    model_flag = f" --model {shlex.quote(model)}" if model else ""
    return (
        f"export CLAUDE_CONFIG_DIR={shlex.quote(str(home))}\n"
        f"claude --dangerously-skip-permissions{model_flag} -p {shlex.quote(brief)}\n"
        f"if git merge-base --is-ancestor HEAD {shlex.quote(trunk)} 2>/dev/null "
        '&& [ -z "$(git status --porcelain)" ]; then exit 0; fi\n'
        "echo; echo '⚠ closeout could not verify a clean landing — resuming the session:'\n"
        # no --model here on purpose: the rescue resumes on the account's
        # default (stronger) model, so a haiku closeout escalates on failure
        "exec claude --dangerously-skip-permissions --continue\n"
    )


def _reachable(p):
    return bool(p.get("tmux_target")) or (
        p.get("host") in ("Terminal", "iTerm2") and p.get("tty"))


def start_finish(wt_name):
    """One button, tiered by what's actually left to do:
    live agent -> type a brief at it — the slim one if the branch already
    landed, the full closeout otherwise; everything landed and an agent
    idling -> type /exit; no terminal + landed + clean -> park on the trunk
    right here, no agent; anything else -> launch a one-shot closeout agent
    (headless; frees the card itself, or parks as needs-you if the landing
    doesn't verify)."""
    if DEMO:
        return {"ok": False, "message": "demo mode — nothing to finish"}
    wt = next((w for w in discover_worktrees() if w["name"] == wt_name), None)
    if not wt:
        return {"ok": False, "message": f"unknown worktree '{wt_name}'"}
    path, git_root = wt["path"], wt["git"]
    trunk = _base_ref(git_root)
    if not trunk:
        return {"ok": False, "message": "no trunk ref found for this repo"}
    run(["git", "fetch", "--quiet", "origin"], cwd=git_root, timeout=30)
    landed = run(["git", "merge-base", "--is-ancestor", "HEAD", trunk],
                 cwd=git_root)[0] == 0
    porcelain = [l for l in run(["git", "status", "--porcelain"],
                                cwd=git_root)[1].splitlines() if l.strip()]
    mine = [p for p in claude_processes() if p.get("cwd")
            and (p["cwd"] == path or p["cwd"].startswith(path + os.sep))]
    live = next((p for p in mine if _reachable(p)), None)
    if live:
        if landed and not porcelain:
            res = send_to_process(live["pid"], "/exit")
            if res["ok"]:
                _closeouts.pop(wt_name, None)
                _cache["t"] = 0.0    # button reverts on the next poll
            return {"ok": res["ok"], "mode": "exit", "message":
                    "already landed — sent /exit to close the terminal"
                    if res["ok"] else res["message"]}
        sent = _closeouts.get(wt_name)
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
            sessions = scan_sessions([wt], mine, now).get(path, [])
            paired = next((s for s in sessions
                           if s.get("pid") == live["pid"]), None)
            any_working = any(s.get("status") == "working" for s in sessions)
            step = closeout_step(paired["status"] if paired else None,
                                 any_working, sent, now)
            if step == "nudge":
                # idle agent, nothing else working, briefed ≥60s ago: type the
                # specifics so it stops treating the leftovers as untouchable.
                block = "\n".join(files)
                if len(porcelain) > len(files):
                    block += f"\n… and {len(porcelain) - len(files)} more"
                nudge = CLOSEOUT_NUDGE_TEXT.format(
                    left=left, trunk=trunk, files=(block + "\n") if block else "")
                res = send_to_process(live["pid"], nudge)
                if not res["ok"]:
                    return {"ok": False, "mode": "nudge", "message": res["message"]}
                _closeouts[wt_name] = time.time()   # restart the "sent Xm ago"
                _cache["t"] = 0.0                    # clock + re-arm the 60s guard
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
        brief = (SLIM_CLOSEOUT_TEXT if landed else CLOSEOUT_TEXT)
        res = send_to_process(live["pid"], brief.format(trunk=trunk))
        if not res["ok"]:
            return {"ok": False, "mode": "slim" if landed else "brief",
                    "message": res["message"]}
        _closeouts[wt_name] = time.time()
        _cache["t"] = 0.0            # show ✕ close on the next poll, not in 4s
        return {"ok": True, "mode": "slim" if landed else "brief", "message":
                ("already landed — slim brief sent (tidy scratch and park, "
                 "no re-merge)" if landed else
                 "closeout brief sent to the live agent")
                + " — when it reports done, ✕ close verifies the landing "
                  "and closes the terminal"}
    _closeouts.pop(wt_name, None)    # no live agent — the two-step is moot
    if mine:
        return {"ok": False, "message":
                "a live process exists but its terminal can't be scripted — "
                "finish from that terminal, or close it and ✓ finish again"}
    if landed and not porcelain:
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                     cwd=git_root)[1].strip()
        if branch == trunk.split("/", 1)[-1]:
            return {"ok": True, "mode": "noop",
                    "message": "already landed and clean — nothing to finish"}
        parked = _park_on_trunk(git_root, trunk)
        if parked:
            return parked
        # the switch itself failed — fall through and let an agent sort it out
    # any leftover file — even untracked scratch — goes to an agent: whether
    # it's droppable is a judgment call, not ours. haiku is enough for the
    # mechanical run, a landed branch gets the slim brief so nothing already
    # merged is re-checked, and a failed landing escalates itself (see
    # closeout_shell's rescue line)
    brief = (SLIM_CLOSEOUT_TEXT if landed else CLOSEOUT_TEXT).format(trunk=trunk)
    out = start_dispatch(brief, worktree=wt_name,
                         model="haiku", closeout_trunk=trunk)
    out.setdefault("ok", True)
    out["mode"] = "dispatch"
    out.setdefault("message",
                   "no live terminal — launched a one-shot closeout agent; "
                   "the card frees itself once the landing verifies")
    return out


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


def _pick_defaults(model=None):
    """Deterministic auto-picks, no AI in the loop: the cleanest FREE worktree,
    and the account with the most headroom that can actually run `model`
    (falling back to overall headroom when none clears its reserve — that only
    happens on a forced model, where the user already chose to push through)."""
    state = cached_state()
    cached_limits()  # ensure the account picker isn't working from a cold cache
    limits = limits_by_account()
    free = [w for w in state["worktrees"] if w["availability"] == "free"]
    free.sort(key=lambda w: (w["git"]["dirty"] or 0))
    wt = free[0]["name"] if free else None
    acct = None
    if model:
        acct = next((c["label"] for c in model_candidates(model) if c["ok"]), None)
    if acct is None:
        excl = set(CFG.get("exclude_accounts") or [])
        accounts = [(a, d) for a, d in limits.items() if d["available"] and a not in excl]
        accounts.sort(key=lambda x: -(x[1]["headroom"] or 0))
        acct = accounts[0][0] if accounts else None
    return wt, acct


_jobs = {}                 # job_id -> {progress, done, result}
_jobs_lock = threading.Lock()
_job_seq = [0]


def _log(job, line):
    with _jobs_lock:
        job["progress"].append(line)


def start_dispatch(mission, worktree=None, account=None,
                   model=None, effort=None, force_model=False,
                   closeout_trunk=None):
    """Kick a dispatch off in the background; return a job id to poll.
    Routing is deterministic — cleanest free worktree, most-headroom account
    that can run the chosen model — and choosing model + effort is the
    caller's job (only closeouts run without them). If the chosen model has
    no account with enough headroom (above reserve), return a needs_decision
    instead of launching — unless force_model.
    With closeout_trunk set the mission runs as a one-shot closeout (headless
    claude that verifies its own landing against that trunk ref)."""
    if not closeout_trunk and not (model and effort):
        return {"ok": False, "message":
                "pick a model and an effort first — routing is deterministic, "
                "nothing is chosen for you"}
    # closeouts skip the headroom dialog: ✓ finish must always just run, and
    # _pick_defaults already prefers an account with headroom for the model
    if not DEMO and model and not force_model and not closeout_trunk:
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
        job, mission, worktree, account, model, effort,
        closeout_trunk)).start()
    return {"job": job_id}


def kickoff_sent(pane):
    """True once the composer no longer holds the brief: the CLI is visibly
    mid-turn, or the input line at the bottom of the pane is bare again."""
    if "esc to interrupt" in pane:
        return True
    prompts = [l.strip() for l in pane.splitlines()
               if l.lstrip().startswith(("❯", ">"))]
    return bool(prompts) and prompts[-1] in ("❯", ">")


def composer_idle(pane):
    """True when the composer is bare with no turn or compaction in flight —
    the only state where a paste reliably lands in the input line. Distinct
    from kickoff_sent: there a bare prompt proves the brief LEFT the composer;
    here it must prove the CLI is ready to RECEIVE, so any activity vetoes."""
    if "esc to interrupt" in pane or "ompacting" in pane:
        return False
    prompts = [l.strip() for l in pane.splitlines()
               if l.lstrip().startswith(("❯", ">"))]
    return bool(prompts) and prompts[-1] in ("❯", ">")


def deliver_text(name, text):
    """Put `text` into a fleet tmux session's claude composer and press Enter
    until the send is proven (see kickoff_sent). Pasting atomically (bracketed,
    via a tmux buffer) sidesteps the CLI's paste heuristic, which chops a rapid
    send-keys burst into '[Pasted text #N]' chips that swallow the Enter."""
    run(["tmux", "-L", FLEET_SOCK, "set-buffer", "-b", "orchestra-kickoff", text])
    run(["tmux", "-L", FLEET_SOCK, "paste-buffer", "-p", "-d",
         "-b", "orchestra-kickoff", "-t", name])
    time.sleep(1)
    for _ in range(3):
        run(["tmux", "-L", FLEET_SOCK, "send-keys", "-t", name, "Enter"])
        time.sleep(2)
        _, pane = run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        if kickoff_sent(pane):
            return True
    return False


def _run_dispatch(job, mission, worktree, account, model, effort,
                  closeout_trunk=None):
    def finish(result):
        with _jobs_lock:
            job["result"] = result
            job["done"] = True

    if DEMO:
        return finish({"ok": False, "message": "demo mode — dispatch disabled"})
    mission = (mission or "").strip()
    if not mission:
        return finish({"ok": False, "message": "empty mission"})

    if not (worktree and account):
        dw, da = _pick_defaults(model)
        worktree, account = worktree or dw, account or da
        _log(job, f"① picked → {worktree} · [{account}] "
                  "(cleanest free worktree · most model headroom)")
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

    if closeout_trunk:
        # One-shot closeout: no branch header (the branch IS the mission), no
        # effort dance (a headless run takes no slash commands). The wrapper
        # verifies the landing itself — see closeout_shell.
        name = ("closeout-" + re.sub(r"[^a-zA-Z0-9]+", "-", worktree)
                .strip("-").lower() + time.strftime("-%H%M%S"))
        _log(job, f"② launching one-shot closeout {name}…")
        rc, out = run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                       "-c", wt["path"],
                       closeout_shell(home, model, mission, closeout_trunk)])
        if rc != 0:
            return finish({"ok": False,
                           "message": f"tmux failed: {out or 'is tmux installed?'}"})
        try:
            with open(DISPATCH_LOG, "a") as lf:
                lf.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": name,
                    "worktree": worktree, "account": account, "model": model,
                    "closeout": True, "mission_original": mission,
                    "kickoff": mission}) + "\n")
        except OSError:
            pass
        _log(job, "✓ launched")
        return finish({"ok": True, "message":
                       f"one-shot closeout running in {worktree} on [{account}] — "
                       "the card frees itself once the landing verifies; if it "
                       "can't land, the session stays open and needs you",
                       "session": name, "worktree": worktree, "account": account,
                       "attach": f"tmux -L {FLEET_SOCK} attach -t {name}"})

    # branch naming is the agent's call — it reads the mission and knows best
    header = ("If this worktree's current branch is not the right home for this "
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
    kick_sent = deliver_text(name, brief)
    _log(job, "  kickoff " + ("sent ✓" if kick_sent
                              else "UNCONFIRMED ⚠ — attach and press Enter"))

    try:  # audit trail: every dispatch, with the author's original words
        with open(DISPATCH_LOG, "a") as lf:
            lf.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": name,
                "worktree": worktree, "account": account, "model": model,
                "effort": effort,
                "mission_original": mission, "kickoff": kickoff}) + "\n")
    except OSError:
        pass
    effort_note = ""
    if effort:
        effort_note = f" · effort {effort} " + ("✓" if effort_confirmed else "UNCONFIRMED")
    kick_note = "" if kick_sent else \
        " · ⚠ kickoff UNCONFIRMED — attach and press Enter"
    _log(job, "✓ launched" if kick_sent else "⚠ launched, kickoff unconfirmed")
    finish({"ok": True,
            "message": f"launched {name} in {worktree} on [{account}]"
                       + (f" · {model}" if model else "") + effort_note + kick_note,
            "session": name, "worktree": worktree, "account": account,
            "model": model, "effort": effort, "effort_confirmed": effort_confirmed,
            "kickoff_sent": kick_sent, "kickoff": kickoff,
            "attach": f"tmux -L {FLEET_SOCK} attach -t {name}"})


def dispatch_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return {"ok": False, "error": "unknown job"}
        return {"ok": True, "progress": list(job["progress"]),
                "done": job["done"], "result": job["result"]}


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

RESUME_STATE = HERE / "resume.schedule.json"
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
    if DEMO:
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
    if DEMO:
        return {"ok": False, "message": "demo mode — nothing to schedule"}
    if not (worktree and sid and account):
        return {"ok": False, "message": "need worktree, sid and account"}
    now = time.time()
    try:
        delay = float(delay_s if delay_s is not None
                      else CFG.get("resume_delay_s", 60))
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
            al = limits_by_account().get(account) or {}
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


def _limit_active_until(account, model, now):
    """The freshest word on whether `account` still blocks this session: a
    future reset timestamp while it does, None once it's clear. Refetches
    cclimits — the cached view predates the reset by design. An unreadable
    account verifies as clear: the send costs nothing if the limit holds."""
    data = cached_limits(refresh=True)
    if not data or not data.get("available"):
        return None
    al = limits_by_account().get(account)
    if not al:
        return None
    cands = []
    if al.get("exhausted") and al.get("resets_at"):
        cands.append(al["resets_at"])         # account-wide cap bites every model
    for sx in al.get("scoped_exhausted", []):
        if not sx.get("resets_at"):
            continue
        # a model-scoped cap only blocks a session running that model; with the
        # model unknown, count it — a late resume beats a wasted one
        if not model or (sx.get("label") or "").lower() in model.lower():
            cands.append(sx["resets_at"])
    future = [c for c in cands if c > now + 30]
    return min(future) if future else None


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
        _, pane = run(["tmux", "-L", FLEET_SOCK, "capture-pane", "-p", "-t", name])
        streak = streak + 1 if composer_idle(pane) else 0
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
    shell = (f"export CLAUDE_CONFIG_DIR={shlex.quote(str(home))}\n"
             f"exec claude --dangerously-skip-permissions --resume {shlex.quote(sid)}\n")
    rc, out = run(["tmux", "-L", FLEET_SOCK, "new-session", "-d", "-s", name,
                   "-c", cwd, shell])
    if rc != 0:
        return {"ok": False,
                "message": f"tmux failed: {out or 'is tmux installed?'}"}
    attach = f"tmux -L {FLEET_SOCK} attach -t {name}"
    where = f"no scriptable terminal — resumed in tmux · {attach}"
    fp = next(iter((home / "projects").glob(f"*/{sid}.jsonl")), None)
    msg = CFG.get("resume_message", "continue")
    _wait_composer_idle(name, RESUME_READY_S)
    for attempt in range(3):
        if attempt:   # the last paste vanished — let the CLI settle, try again
            _wait_composer_idle(name, 90.0)
        try:
            offset = fp.stat().st_size if fp else 0
        except OSError:
            fp, offset = None, 0
        sent = deliver_text(name, msg)
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
    if DEMO:
        return _resume_set(key, status="failed", fired_at=now,
                           message="demo mode")
    worktree, sid, account = r["worktree"], r["sid"], r["account"]

    state = cached_state()
    s, proc = _session_on_board(state, worktree, sid)
    if s and s.get("handed_to"):
        return _resume_set(key, status="done", fired_at=now, message=
                           f"work already continued by [{s['handed_to']}] — nothing sent")
    if s and s["status"] != "limit":
        return _resume_set(key, status="done", fired_at=now, message=
                           f"session is {s['status']} — no resume needed")

    until = _limit_active_until(account, r.get("model"), now)
    if until:
        attempts = int(r.get("attempts", 0)) + 1
        if attempts >= RESUME_MAX_ATTEMPTS:
            return _resume_set(key, status="failed", fired_at=now, message=
                               f"still limited after {attempts} checks — gave up")
        return _resume_set(key, due_at=until + float(r.get("delay_s") or 60),
                           attempts=attempts, message=
                           "still limited — re-armed for the next reset")

    msg = CFG.get("resume_message", "continue")
    if proc and proc.get("reachable"):
        res = send_to_process(proc["pid"], msg)
        if res.get("ok"):
            return _resume_set(key, status="done", fired_at=now,
                               message=f"sent '{msg}' — {res['message']}")
    wt = next((w for w in discover_worktrees() if w["name"] == worktree), None)
    home = next((h for h in claude_homes() if account_label(h) == account), None)
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
            body = json.dumps({**cached_state(),
                               "resumes": resume_public()}).encode()
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
        elif self.path.startswith("/guide"):
            body = (HERE / "guide.html").read_bytes()
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
        elif self.path.startswith("/api/resume/schedule"):
            result = schedule_resume(
                payload.get("worktree"), payload.get("sid"),
                payload.get("account"), model=payload.get("model"),
                delay_s=payload.get("delay_s"),
                resets_at=payload.get("resets_at"), due_at=payload.get("due_at"))
        elif self.path.startswith("/api/resume/cancel"):
            result = cancel_resume(payload.get("worktree"), payload.get("sid"))
        elif self.path.startswith("/api/send"):
            result = send_to_process(int(payload.get("pid") or 0), payload.get("text") or "")
        elif self.path.startswith("/api/finish"):
            result = start_finish(payload.get("worktree") or "")
        elif self.path.startswith("/api/dispatch"):
            result = start_dispatch(
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


if __name__ == "__main__":
    args = load_config()
    DEMO = args.demo
    if CFG["host"] not in ("127.0.0.1", "localhost", "::1"):
        print("orchestra: WARNING — binding beyond loopback serves your "
              "transcript text to the network", file=sys.stderr)
    if not DEMO:
        load_resumes()
        threading.Thread(target=resume_loop, daemon=True).start()
    server = ThreadingHTTPServer((CFG["host"], CFG["port"]), Handler)
    mode = " (demo data)" if DEMO else ""
    print(f"orchestra up → http://{CFG['host']}:{CFG['port']}{mode}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
