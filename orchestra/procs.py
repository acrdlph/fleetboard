"""orchestra.procs — what is actually running: live `claude` CLI processes.

The board's other half. `scan_sessions` reads what agents WROTE; this module
reads what is ALIVE — one `ps` for the whole tree, then cwd (lsof on macOS,
/proc on Linux), CLAUDE_CONFIG_DIR (the only link from a process to an
account), the hosting terminal app or tmux pane, and the count of Bash-tool
shells running underneath. Everything a card needs to be actionable: without a
pid there is nobody to focus, to type at, or to resume.

`pair_sessions_with_procs` is the join between the two halves, and it lives
here because it reasons about processes: transcripts know their account,
processes know theirs only via the environment, and the exact-account pass has
to claim first so freshness order can't steal a terminal that demonstrably
belongs to someone else.

Nothing here writes. Every subprocess goes through `shell.run`, which never
raises, so a machine that refuses `ps eww` degrades to unknown accounts rather
than an empty board.
"""

import os
import re
import sys
from pathlib import Path

from . import config, shell


# ---------------------------------------------------------------- collectors

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
        _, out = shell.run(["lsof", "-a", "-d", "cwd", "-p", ",".join(map(str, pids)), "-Fn"],
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
        _, out = shell.run(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
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
    rc, out = shell.run(cmd)
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
    rc, out = shell.run(["ps", "-axo", "pid=,ppid=,tty=,pcpu=,etime=,command="])
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
        p["account"] = config.account_label(Path(cfg)) if cfg else \
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
