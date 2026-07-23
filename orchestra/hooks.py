"""orchestra.hooks — the Claude Code hook edge: what the CLI itself says.

Every other signal this board reads is a FOOTPRINT. A transcript byte, a `ps`
row, an mtime — evidence left behind by an agent, read afterwards by a stranger.
This module is the one place the CLI speaks in the first person, and ADR 0007
says why that matters: `■ BLOCKED` and `◆ YOUR TURN` are the same footprint. A
live process and an idle transcript. No amount of faster looking separates them,
because the difference is not on disk at all — it is a dialog on a screen.

Three things live here and nothing else:

  * `HOOK_STATUS` / `NOTIFY_STATUS` — the event vocabulary, MEASURED against
    Claude Code 2.1.218 by running a driven session with all 30 events wired to
    a logger. Not remembered, not read off a doc. See the table below.
  * `HookEdges` — the store. Edge-triggered, TTL'd, bounded, thread-safe: the
    server thread writes it, the sweep thread reads it.
  * `settings_fragment` / `install` — the adoption mechanism, and the whole of
    ADR 0007's "open" section. It writes a settings file of its OWN and hands
    the path to `claude --settings`. It never writes the user's settings.json.

WHAT THIS MODULE MUST NEVER DO: assert a status the ladder cannot check. A hook
is a latency reduction that is allowed to be wrong; `status.classify_session`
keeps every veto it had, and `HOOK_TTL_S` guarantees that a hook that stops
arriving stops being consulted. A dropped hook costs latency, never truth
(ENGINE.md §7.2).

--------------------------------------------------------------------------
THE VOCABULARY, MEASURED
--------------------------------------------------------------------------

Claude Code 2.1.218 defines 30 hook events. The seven that fired on a real
driven session, in order, with the exact payload keys observed:

    SessionStart      session_id transcript_path cwd hook_event_name source
    UserPromptSubmit  … prompt_id permission_mode prompt
    PreToolUse        … tool_name tool_input tool_use_id
    PostToolUse       … tool_response duration_ms
    PostToolBatch     … tool_calls[]
    MessageDisplay    … turn_id message_id index final delta
    Stop              … stop_hook_active last_assistant_message
                        background_tasks[] session_crons[]
    SessionEnd        … reason

and, from an interactive session left alone / asked for a Write outside cwd:

    Notification      … message notification_type   ("idle_prompt",
                                                     "permission_prompt")
    PermissionRequest … tool_name tool_input permission_suggestions[]

`session_id` is the transcript filename stem — EXACTLY the key
`transcripts.scan_sessions` already builds its sessions on (`fp.stem`). That is
the whole join, and it is why this costs one dict.

Three payload facts are load-bearing and were all wrong in one document or
another before they were measured:

  * `PermissionRequest` carries NO `tool_use_id`, though the CLI's own embedded
    help says it does. We do not use it, and this note is why.
  * `Notification` carries NO `permission_mode` and no `prompt_id` — it is built
    from a different helper. Anything reading those off a Notification gets
    `None`.
  * `Stop` carries `background_tasks` and `session_crons`. The CLI knows what it
    delegated. We do not read them yet; `transcripts.parse_session_tail` gets
    the same answer off disk, and adding a second source for one fact before
    there is a bug is how this project's docs got 14 things wrong.
"""

import json
import os
import re
import shlex
import tempfile
import threading
import time

from . import config

# ENGINE.md §7.2, and it is the rule that makes hooks safe to ship at all.
#
# A hook is EDGE-triggered. One dropped — the server was restarting, the agent
# ran under `--bare`, curl lost a race with sleep — would otherwise pin a
# session to a status nothing ever corrects, which is the exact failure the
# whole architecture exists to prevent. After this many seconds the edge is
# simply not consulted and inference resumes.
#
# 90 is from the document and it is the right order of magnitude for the reason
# the document does not give: the longest gap between hook events within one
# genuinely-working turn is a single long tool call, and `Stop`/`PreToolUse`
# bracket every one of those. The band this must cover is "the agent is sitting
# at a permission dialog", where the LAST event was `Notification` and the next
# will not arrive until the human acts — so a live dialog goes back to inferred
# after 90 s, and inference (an unresolved tool_use past `block_grace_s`) says
# BLOCKED anyway. The TTL expiring on a real dialog therefore costs confidence,
# not correctness, which is the only shape of expiry that is allowed here.
HOOK_TTL_S = 90.0

# The bound (§4.7). One entry is ~200 bytes and entries expire on TTL, so this
# only binds under a peer POSTing garbage session ids as fast as it can — which
# loopback trust permits and which must therefore cost a fixed amount of memory
# rather than an unbounded one. Oldest-first eviction: the newest edge is the
# one worth keeping, always.
MAX_EDGES = 1024

# A session id is a UUID and nothing else. Enforced because this dict is keyed
# by it and a 4 KB "session id" is a 4 KB key; also because a well-formed id is
# the cheapest evidence that the caller really is a Claude Code hook.
SID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

# ------------------------------------------------------------ the vocabulary
#
# event -> the status the CLI is asserting, or absent for "asserts nothing".
#
# READ THE ABSENCES. They are the design:
#
#   * `SessionEnd` asserts NOTHING, and this is the single most tempting entry
#     to add. It fires with `reason` in {clear, logout, prompt_input_exit,
#     other} — and `clear` means the user typed /clear and the session
#     CONTINUES. Mapping it to ○ ENDED would mean a /clear frees the worktree,
#     which feeds `card_availability` -> FREE -> dispatch targeting. That is the
#     ladder's own stated "dangerous one" (`orphan_grace_s`), reached by a
#     shortcut. The process table answers "did it exit" and answers it correctly
#     within one sweep; a hook is not needed and is not safe.
#   * `MessageDisplay` asserts nothing. It fires per streamed delta — dozens per
#     turn — and says only that bytes are arriving, which `PreToolUse` and the
#     transcript both already prove. Ingesting it would multiply the POST rate
#     by ~20 for no status we do not already have. It stays out of the install
#     fragment entirely.
#   * `PostToolUseFailure`, `PreCompact`, `PostCompact`, `SubagentStart` and the
#     rest of the 30 assert nothing YET. They are not in the fragment, so they
#     cost nothing; adding one is a line here and a line there.
#
# `PermissionDenied` -> working is not a typo. It fires when the auto-mode
# classifier denies a call — the agent is told and carries on. Nobody is
# waiting for the human.
HOOK_STATUS = {
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PostToolBatch": "working",
    "PermissionDenied": "working",
    "PermissionRequest": "blocked",   # a permission dialog is ON SCREEN
    "Stop": "waiting",                # the turn closed
    "StopFailure": "waiting",         # …closed by an API error; still your turn
    "SessionStart": "waiting",        # booted, sitting at the prompt
}

# `Notification` is the one event whose meaning lives in a second field, and the
# two that matter are the two ADR 0007 named as ambiguous:
#
#   permission_prompt   "Claude needs your permission"      -> ■ BLOCKED
#   idle_prompt         "Claude is waiting for your input"  -> ◆ YOUR TURN
#
# Both measured. `idle_prompt` is the CLI's own idle timer firing in the TUI,
# which is a genuinely different question from "the transcript went quiet" and
# is the only positive evidence of YOUR TURN that exists anywhere outside the
# process.
#
# `agent_needs_input` is the fleet-view notification for a BACKGROUND agent, not
# for this session, and `worker_permission_prompt` is a teammate's. Neither
# describes the session whose `session_id` is on the payload, so neither is
# mapped — a hook may only speak about itself.
NOTIFY_STATUS = {
    "permission_prompt": "blocked",
    "idle_prompt": "waiting",
    "elicitation_dialog": "needs_input",
}

# What the install fragment wires up. Deliberately NOT `HOOK_STATUS.keys()`:
# these are the events worth a subprocess, and every one not listed is one fewer
# fork per turn. `PostToolUse` is in and `PostToolBatch` is out — the batch
# event says nothing the per-tool event did not, and firing both doubles the
# cost of the most common event in the file.
INSTALLED_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse",
                    "PostToolUse", "PermissionRequest", "Notification",
                    "Stop", "SessionEnd")


def hook_status(event, notification_type=None):
    """What this event asserts about the session, or None for "nothing".

    Pure, total, and the only reader of the two tables above — so a test that
    pins this pins the vocabulary, and `tests/characterize.py` can enumerate it.
    An event this module has never heard of returns None rather than raising:
    the CLI adds events, and an unknown one must degrade to inference, not to a
    500 on a route agents call.
    """
    if event == "Notification":
        return NOTIFY_STATUS.get(notification_type)
    return HOOK_STATUS.get(event)


class Edge:
    """One session's most recent hook, and only the most recent.

    NOT a queue. The board renders a state, not a history, and the newest edge
    supersedes every older one by construction — a `Stop` after a `PreToolUse`
    means the turn ended, and keeping the `PreToolUse` around could only ever
    produce a wrong answer more slowly.
    """
    __slots__ = ("event", "notification_type", "at", "status", "n")

    def __init__(self, event, notification_type, at, status, n=1):
        self.event = event
        self.notification_type = notification_type
        self.at = at
        self.status = status
        self.n = n              # events seen for this sid since it appeared

    def fresh(self, now, ttl_s=HOOK_TTL_S):
        return (now - self.at) < ttl_s

    def __repr__(self):
        return (f"<Edge {self.event}"
                f"{'/' + self.notification_type if self.notification_type else ''}"
                f" -> {self.status} at {self.at:.0f}>")


class HookEdges:
    """The store. Written by the SERVER thread, read by the SWEEP thread.

    One lock, held for the length of a dict operation and never across a
    callback — the sweep must never be able to block a hook POST, because a
    blocked POST is a blocked agent (Claude Code waits on its hooks).

    Expiry is lazy and happens on both paths: a `record` prunes, and a `status`
    lookup refuses a stale edge without needing to have pruned it. There is no
    timer thread, because a store whose correctness depends on a thread running
    is a store that is wrong while that thread is wedged.
    """

    def __init__(self, ttl_s=None, max_edges=MAX_EDGES):
        self.ttl_s = HOOK_TTL_S if ttl_s is None else float(ttl_s)
        self.max_edges = max_edges
        self._lock = threading.Lock()
        self._edges = {}         # sid -> Edge
        self.received = 0        # every POST that carried a usable session_id
        self.ignored = 0         # …of which, asserted no status (MessageDisplay…)
        self.evicted = 0
        self.drift = 0           # hook and inference disagreed; see `note_drift`

    # ------------------------------------------------------------ writing

    def record(self, sid, event, at=None, notification_type=None):
        """Ingest one hook. Returns the status it asserts, or None.

        Total and forgiving: a malformed sid is dropped, an unknown event is
        recorded as asserting nothing. This runs on the request thread of a
        route an agent blocks on, so it does one dict write and returns.
        """
        if not sid or not SID_RE.match(str(sid)):
            return None
        at = time.time() if at is None else float(at)
        st = hook_status(event, notification_type)
        with self._lock:
            self.received += 1
            if st is None:
                self.ignored += 1
            prev = self._edges.get(sid)
            n = (prev.n + 1) if prev else 1
            if st is None and prev is not None:
                # An event that asserts nothing must not RESET the clock on an
                # edge that does. A stream of MessageDisplay POSTs would
                # otherwise hold a stale `Stop` alive indefinitely — a TTL that
                # any traffic can renew is not a TTL.
                prev.n = n
                return None
            self._edges[sid] = Edge(event, notification_type, at, st, n)
            if len(self._edges) > self.max_edges:
                self._prune(at)
        return st

    def _prune(self, now):
        """Caller holds the lock. Expired first, then oldest, down to the cap."""
        for sid in [s for s, e in self._edges.items() if not e.fresh(now, self.ttl_s)]:
            del self._edges[sid]
            self.evicted += 1
        while len(self._edges) > self.max_edges:
            sid = min(self._edges, key=lambda s: self._edges[s].at)
            del self._edges[sid]
            self.evicted += 1

    def note_drift(self, n=1):
        """The hook and the ladder disagreed n times this sweep.

        Counted rather than logged, and counted where every other drift term
        already lives (`observer.stats()`), because ENGINE.md §7.3's promise is
        that a disagreement is VISIBLE. Unlike `scan_drift` this one is not a
        bug: a hook overruling stale inference is the feature. A `hook_drift` of
        zero over a hooked fleet means the hooks are buying nothing.
        """
        with self._lock:
            self.drift += n

    # ------------------------------------------------------------ reading

    def status(self, sid, now=None):
        """The status a LIVE hook edge asserts for this session, or None.

        The whole read API the sweep needs. Returns None for: no edge, an edge
        past its TTL, an edge whose event asserts nothing. Never raises.
        """
        now = time.time() if now is None else now
        with self._lock:
            e = self._edges.get(sid)
            if e is None or e.status is None or not e.fresh(now, self.ttl_s):
                return None
            return e.status

    def edge(self, sid, now=None):
        """The live Edge, for a caller that wants the event name too."""
        now = time.time() if now is None else now
        with self._lock:
            e = self._edges.get(sid)
            return e if (e is not None and e.fresh(now, self.ttl_s)) else None

    def live(self, now=None):
        """`sid -> status` for every unexpired, status-asserting edge.

        ONE snapshot per sweep, taken under one lock acquisition. The sweep must
        not take this lock once per session: `scan_sessions` walks every home
        and every project directory between the first session and the last, and
        a lock reacquired eighty times across a 300 ms scan is eighty chances to
        stall a hook POST — i.e. eighty chances to stall an AGENT.
        """
        now = time.time() if now is None else now
        with self._lock:
            return {s: e.status for s, e in self._edges.items()
                    if e.status is not None and e.fresh(now, self.ttl_s)}

    def stats(self, now=None):
        now = time.time() if now is None else now
        with self._lock:
            live = sum(1 for e in self._edges.values() if e.fresh(now, self.ttl_s))
            return {"hook_received": self.received, "hook_ignored": self.ignored,
                    "hook_sessions": len(self._edges), "hook_live": live,
                    "hook_evicted": self.evicted, "hook_drift": self.drift,
                    "hook_ttl_s": self.ttl_s}


# ----------------------------------------------------------- installation
#
# ADR 0007 left this "open" and it is the part that fails if it is careless.
# THE RULE, and it has no exceptions:
#
#     ORCHESTRA NEVER WRITES A FILE CLAUDE CODE ALREADY OWNS.
#
# Not `~/.claude/settings.json`, not `~/.claude-accountN/settings.json`, not a
# project's `.claude/settings.json`. Seven of the eight homes on this machine
# have no `hooks` key at all and one has two hooks somebody depends on; a
# merge-and-rewrite of the eighth is a data-loss bug that shows up as somebody
# else's tooling silently not running.
#
# What we write instead is a settings file of our own, in orchestra's own
# directory, and hand its path to `claude --settings`. MEASURED, against 2.1.218:
# a `--settings` fragment carrying hooks fires ALONGSIDE the hooks in the
# settings the CLI loaded itself — both ran, for the same events, in the same
# session. `--settings` is an ADDITIONAL layer, not a replacement, which is what
# makes this safe and is exactly what `--setting-sources` implies.
#
# The two things this cannot reach, stated plainly rather than papered over:
#
#   * an agent the user started themselves. No `--settings`, no hooks, and we
#     will not add any. It falls to rank 2-4 and reads exactly as it does today.
#   * `claude --bare`. It skips hooks wholesale — an unmodelled kill switch, and
#     the reason `HOOK_TTL_S` is not optional: under `--bare` no hook ever fires,
#     every session is `status_src: inferred`, and nothing anywhere waits for an
#     edge that is not coming.

HOOK_DIR = config.HERE / ".orchestra"
SETTINGS_PATH = HOOK_DIR / "hooks.settings.json"
SCRIPT_PATH = HOOK_DIR / "post-hook.sh"

# The script. `sh`, not python — this forks once per hook event and Claude Code
# BLOCKS on it, so the budget is a process spawn and a loopback POST, not an
# interpreter start. Measured on this machine: ~9 ms end to end, against a
# python3 -c equivalent at ~45 ms.
#
# Every line of it is a promise not to hurt the agent:
#   * `-m 2`      — a wedged server costs the agent two seconds, not its turn.
#   * `>/dev/null` — stdout from a UserPromptSubmit hook is SHOWN TO CLAUDE. A
#                   stray byte here would be injected into the user's prompt.
#   * `2>/dev/null` — stderr from a non-zero exit is shown to the USER. curl's
#                   "connection refused" is not news the human needs.
#   * `exit 0`    — unconditional, and the most important line in the file. Exit
#                   2 on a PreToolUse BLOCKS THE TOOL CALL. An observability
#                   sidecar that can veto the agent's work is not observability.
SCRIPT = """#!/bin/sh
# orchestra — status hook. Generated by orchestra.hooks.install(); safe to delete.
# Posts one Claude Code hook event to the local board and gets out of the way.
# It can never block, slow or fail an agent: see the exit 0 on the last line.
curl -sS -m 2 -X POST \\
     -H 'Content-Type: application/json' \\
     --data-binary @- \\
     'http://127.0.0.1:%(port)d/api/hook' >/dev/null 2>&1
exit 0
"""


def settings_fragment(port=None, script=None, events=INSTALLED_EVENTS):
    """The `--settings` payload, as a dict. Pure — the tests read this.

    One matcher-less entry per event, which means "every matcher". A matcher
    would be a filter on `tool_name` (or `notification_type`), and filtering
    here rather than in `hook_status` would put the vocabulary in two places.
    """
    port = config.CFG["port"] if port is None else port
    script = str(SCRIPT_PATH) if script is None else script
    cmd = shlex.quote(script)
    return {"hooks": {e: [{"hooks": [{"type": "command", "command": cmd}]}]
                      for e in events}}


def _atomic_write(path, content, executable=False):
    """Write `content` to `path` via a temp file in the SAME directory, then
    `os.replace` — atomic on one filesystem. `install()` reruns on every
    dispatch while already-running hooked agents fork `post-hook.sh` several
    times a turn; a plain truncate-then-write hands a hook that execs inside the
    window an empty or half-written script — an `sh` syntax error, a non-zero
    exit, and on a PreToolUse that BLOCKS the agent's tool call (the one thing
    this module swears it never does). The replace never exposes a partial file,
    and the executable bit is set on the temp BEFORE the rename so there is no
    non-executable window either.

    Skips the write entirely when the content already matches, which stops mtime
    churn on the common idempotent rerun.
    """
    try:
        if path.read_text() == content:
            return
    except (OSError, ValueError):
        pass
    fd, tmpname = tempfile.mkstemp(dir=str(path.parent),
                                   prefix="." + path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmpname, 0o755 if executable else 0o644)
        os.replace(tmpname, str(path))
    except OSError:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def install(port=None, dirpath=None):
    """Write the fragment and the script. Returns the settings path.

    Idempotent, and it rewrites both files every time on purpose: the port can
    change between runs, and a fragment pointing at a port nothing is listening
    on is the silent-degradation case this whole module is trying to avoid. The
    rewrite is ATOMIC (see `_atomic_write`) because the script it replaces may be
    executing in another agent's hook at the same instant.
    """
    port = config.CFG["port"] if port is None else int(port)
    d = HOOK_DIR if dirpath is None else dirpath
    d.mkdir(parents=True, exist_ok=True)
    sh = d / SCRIPT_PATH.name
    js = d / SETTINGS_PATH.name
    _atomic_write(sh, SCRIPT % {"port": port}, executable=True)
    _atomic_write(js, json.dumps(settings_fragment(port, str(sh)), indent=2) + "\n")
    return js


def settings_arg(port=None):
    """`['--settings', path]` for a dispatch command line, or `[]`.

    `[]` — never a broken flag — is the contract: a dispatch that cannot install
    hooks must still dispatch. The agent then reads exactly as it does today,
    which is the degradation ADR 0007 asked for and the reason this returns a
    list rather than raising.
    """
    try:
        return ["--settings", str(install(port))]
    except OSError:
        return []


def installed():
    """Is a fragment on disk, and does it point at the port we are serving?

    Reported by `/api/health`-adjacent surfaces so "hooks are off" is a thing
    the board can SAY rather than a thing the user infers from every session
    reading `inferred`.
    """
    try:
        frag = json.loads(SETTINGS_PATH.read_text())
        script = SCRIPT_PATH.read_text()
    except (OSError, ValueError):
        return False
    wired = set(frag.get("hooks", {}))
    # BOTH halves, and the port. A fragment listing events whose script points
    # at a dead port is worse than no fragment: it looks installed.
    return (wired >= set(INSTALLED_EVENTS)
            and f":{config.CFG['port']}/api/hook" in script)
