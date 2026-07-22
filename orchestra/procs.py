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

`ProcMemo` is what makes this affordable under a perpetual sweep: two of the
subprocesses answer questions fixed at exec — a process's cwd and its
CLAUDE_CONFIG_DIR — so they are memoised per `(pid, generation)` and only a
pid nobody has seen before costs anything. `generation` is `lstart`, never
bare pid: pids are recycled (ADR 0008).
"""

import os
import re
import sys
import threading
from pathlib import Path

from . import config, shell


# --------------------------------------------- the per-generation memo, §4.3

# WHERE THE PROCESS PROBE'S TIME ACTUALLY GOES. Measured with
# getrusage(RUSAGE_SELF)+(RUSAGE_CHILDREN), 1,010 processes in the table,
# 5 live agents:
#
#   ps -axo …command=       68 ms   61 %   <- the bill
#   lsof -a -d cwd          26 ms   23 %
#   ps eww (environment)    16 ms   14 %
#   tmux list-panes          5 ms    4 %   (only when an agent is tmux-hosted)
#   claude_processes       112 ms
#
# The premise this work started from — "lsof is the expensive half" — is wrong
# on this machine. The full-table `ps` is the expensive half, and it CANNOT be
# narrowed:
#
# * `-p` with the pids we already know would lose DISCOVERY, so a new agent
#   would never appear on the board. That is a worse product than a slow sweep.
# * It would also lose `shell_children`, which must see every zsh/bash on the
#   box to count Bash-tool wrappers, and `_host_of`, which walks an arbitrary
#   ancestor chain to a terminal app. Neither knows its pids in advance.
# * The money is in `command=` — dropping it takes `ps` from 68 ms to 37 ms,
#   because ps then never reads any process's argv. It is also precisely the
#   column those two need. Measured, not assumed.
#
# So `ps` stays whole, and the two per-pid lookups behind it go. Both answer
# questions fixed at exec and neither can change while the pid lives.
#
# THE KEY IS (pid, generation), NEVER pid ALONE. Pids are recycled, and this
# project already has a live bug of exactly that shape — ADR 0008, a chat reply
# addressed by bare pid typed into a different agent after recycling. A memo on
# bare pid would be the same bug in a new place, and a worse one: it would put
# a dead agent's worktree on a stranger's card and hand the act layer the wrong
# cwd. The generation is `lstart` — the ABSOLUTE wall clock of exec, from the
# same `ps` that discovered the pid. NOT `etime`: etime is relative and
# integer-truncated, so it jitters by a second between sweeps; a start time
# derived from it would make a live process miss on one sweep and two different
# processes agree on another.
#
# And it is audited. A cold sweep (observer.RECONCILE_S, every 60 s) bypasses
# both memos, re-runs lsof and `ps eww` for real, and counts every disagreement
# into `drift`. `cwd_drift`/`env_drift` are LIES if non-zero — unlike the git
# cadence, these memos claim to be current — so any non-zero value is a bug.

# §4.7. Not an LRU, and it does not need to be one: `ps` hands us the whole
# process table on every sweep, so `retain` drops every pid that is no longer
# alive and the working set is exactly the number of live agents (5 here). The
# cap is a backstop against a caller that never retains; `proc_memo_evictions`
# going non-zero means that happened.
PROC_MEMO_CAP = 512

_MISS = object()      # "the memo holds nothing" — distinct from a held None


class ProcMemo:
    """pid -> a fact fixed at exec, keyed on that pid's generation.

    A hit means "this is the same process I asked last time", which is the
    whole safety argument: the value is a function of the exec, and the key is
    the exec. A recycled pid presents a different `lstart` and misses.

    `None` is a legitimate value (a process with no CLAUDE_CONFIG_DIR), so
    `get`/`peek` signal absence with `_MISS`, never with None.
    """

    def __init__(self, cap=PROC_MEMO_CAP):
        self.cap = cap
        self._d = {}                 # pid -> (generation, value)
        # Read from the sweep thread and from HTTP threads (terminal.py and
        # finish.py both call `claude_processes`), so the dict ops are locked.
        # The probe itself is never called under the lock.
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = self.drift = 0

    def get(self, pid, gen):
        """The held value, or `_MISS`. A `gen` of None never hits — a process
        we cannot identify a generation for is one we refuse to memoise."""
        with self._lock:
            e = self._d.get(pid)
            if gen is None or e is None or e[0] != gen:
                self.misses += 1
                return _MISS
            self.hits += 1
            return e[1]

    def peek(self, pid, gen):
        """What `get` would have served, without counting a hit — the cold
        audit's view of what the memo was about to claim."""
        with self._lock:
            e = self._d.get(pid)
            return _MISS if (gen is None or e is None or e[0] != gen) else e[1]

    def put(self, pid, gen, value):
        if gen is None:
            return
        with self._lock:
            self._d[pid] = (gen, value)
            while len(self._d) > self.cap:
                self._d.pop(next(iter(self._d)))
                self.evictions += 1

    def retain(self, live):
        """Drop every pid not in `live`. This is the bound, and it is exact —
        normal turnover, so it is not counted as an eviction."""
        with self._lock:
            for pid in [p for p in self._d if p not in live]:
                del self._d[pid]

    def clear(self):
        """Everything is suspect — after a wake, per §4.5."""
        with self._lock:
            self._d.clear()

    def __len__(self):
        return len(self._d)


_CWDS = ProcMemo()    # pid -> its working directory
_CFGS = ProcMemo()    # pid -> its CLAUDE_CONFIG_DIR, or None if it has none


def proc_memo_stats():
    """Counters for `observer.stats()`. `cwd_drift`/`env_drift` are the
    load-bearing ones: the number of times a cold re-probe disagreed with what
    the memo would have served. Both must be 0."""
    return {"cwd_hits": _CWDS.hits, "cwd_misses": _CWDS.misses,
            "cwd_drift": _CWDS.drift, "cwd_entries": len(_CWDS),
            "env_hits": _CFGS.hits, "env_misses": _CFGS.misses,
            "env_drift": _CFGS.drift, "env_entries": len(_CFGS),
            "proc_memo_evictions": _CWDS.evictions + _CFGS.evictions}


def proc_memo_drift():
    return _CWDS.drift + _CFGS.drift


def proc_memo_clear():
    _CWDS.clear()
    _CFGS.clear()


def _resolve(memo, pids, gens, probe, cold, keep_absent):
    """Answer `pids` out of `memo`, calling `probe` only for what it lacks.

    `probe(pids) -> {pid: value}` is the expensive lookup. A pid the probe does
    not answer for is memoised only when `keep_absent`: for the environment,
    "this process has no CLAUDE_CONFIG_DIR" is as fixed at exec as any other
    answer and worth holding, and only when the probe answered for SOMEBODY, so
    a `ps eww` that failed outright is not recorded as fact. For the cwd an
    unanswered pid is a FAILED lookup — every live process has a cwd — and
    caching it would make one denied `lsof` stick for the life of the process.

    `cold` is the reconcile (§4.3 #1): probe everything, compare against what
    the memo would have served, and count each disagreement into `drift`. Only
    a real, current answer that CONTRADICTS the memo counts — a probe that came
    back empty for a pid proves nothing, so it neither counts as drift nor
    blanks the board; the held value is served instead. drift is therefore
    exactly "the memo lied", which is what makes a non-zero value a bug.
    """
    out, need = {}, []
    for pid in pids:
        held = _MISS if cold else memo.get(pid, gens.get(pid))
        if held is _MISS:
            need.append(pid)
        elif held is not None:
            out[pid] = held
    if not need:
        return out
    got = probe(need)
    answered = bool(got)
    for pid in need:
        gen, val, was = gens.get(pid), got.get(pid), memo.peek(pid, gens.get(pid))
        if val is None:
            if was is not _MISS and was is not None:
                out[pid] = was            # unanswered proves nothing; keep it
            elif keep_absent and answered:
                memo.put(pid, gen, None)
            continue
        if cold and was is not _MISS and was != val:
            memo.drift += 1
        out[pid] = val
        memo.put(pid, gen, val)
    return out


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


_PS_FIELDS = "pid=,ppid=,tty=,pcpu=,etime=,lstart=,command="
_PS_FIELDS_PLAIN = "pid=,ppid=,tty=,pcpu=,etime=,command="

# `lstart` is the generation column: "Tue Jul 14 03:47:57 2026". It is optional
# in the pattern so that a `ps` without it still parses — see `_ps_lines`.
_PS_ROW = re.compile(r"\s*(\d+)\s+(\d+)\s+(\S+)\s+([\d.]+)\s+(\S+)"
                     r"(?:\s+(\w{3}\s+\w{3}\s+\d+\s+\d+:\d+:\d+\s+\d{4}))?\s+(.*)")


def _ps_lines():
    """The whole process table, with a start time per row where we can get one.

    `lstart` costs 1.8 ms of the 68 (measured) and is supported by both BSD ps
    and procps. If some ps refuses the keyword it exits non-zero with no output
    — and an empty table is an empty BOARD — so fall back to the format this
    file used before there was a memo. Every generation is then None, nothing
    is memoised, and the sweep costs exactly what it always did.
    """
    rc, out = shell.run(["ps", "-axo", _PS_FIELDS])
    if rc != 0 or not out:
        rc, out = shell.run(["ps", "-axo", _PS_FIELDS_PLAIN])
    return out.splitlines()


def claude_processes(cold=False):
    """Live `claude` CLI processes with cwd, tty, and hosting terminal app.

    `cold` is the reconcile sweep (§4.3 #1): it bypasses the per-generation
    memos so cwd and CLAUDE_CONFIG_DIR are re-probed for real and any
    disagreement is counted into `drift`. The act paths (`terminal`, `finish`)
    leave it False and read through the memo — safely, because the key is the
    process's generation, so a memo hit is proof of identity, which is the
    exact property ADR 0008 is about.
    """
    table, gens, procs = {}, {}, []
    for line in _ps_lines():
        m = _PS_ROW.match(line)
        if not m:
            continue
        pid, ppid, tty, cpu, etime, lstart, cmd = m.groups()
        table[int(pid)] = (int(ppid), tty, cmd)
        gens[int(pid)] = lstart
        if cmd == "claude" or cmd.startswith("claude "):
            procs.append({"pid": int(pid), "cpu": float(cpu), "etime": etime,
                          "cmd": cmd, "tty": None if tty in ("??", "-") else tty})
    shells = shell_children(table, {p["pid"] for p in procs})
    pids = [p["pid"] for p in procs]
    # The bound (§4.7): every pid that is no longer in the table is gone for
    # good, and `ps` just told us the whole table.
    live = set(pids)
    _CWDS.retain(live)
    _CFGS.retain(live)
    cwds = _resolve(_CWDS, pids, gens, _pid_cwds, cold, keep_absent=False)
    cfgdirs = _resolve(_CFGS, pids, gens, _pid_config_dirs, cold, keep_absent=True)
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
