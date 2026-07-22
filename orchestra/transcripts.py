"""orchestra.transcripts — what the agents WROTE: Claude Code's .jsonl files.

Claude Code keeps one transcript per session under
`<home>/projects/<munged-cwd>/<session-id>.jsonl`, append-only, one JSON
object per line, with workflows and subagents writing into a sibling
`<session-id>/` directory. This module is the only place that knows that
shape. It finds the homes (multi-account setups included), reads a bounded
chunk off either end of a file — never the whole thing, transcripts run to
hundreds of megabytes — and turns the last few hundred kilobytes into the
handful of facts a card needs: topic, model, last thing said either way,
which tools are still unresolved.

`_clean` and `_real_prompt` are the filter between a transcript and a human
eye: strip ANSI and tags, collapse whitespace, and refuse the machine text
the harness injects (system reminders, command stubs, tool-use ids) so a card
never shows the plumbing back to you.

`scan_sessions` is the top of this file's stack and the join point of the
whole observe layer: it maps every recent session onto a worktree (gitrepo),
hands the per-worktree session list and process list to `procs` for pairing,
and asks `status` what each one MEANS. Everything is read-only — the board
opens transcripts, it never writes one.

`StatMemo` is what makes that affordable under a perpetual sweep: a transcript
is append-only, so a read of it is a pure function of `(dev, ino, size,
mtime_ns)` and can be reused verbatim until one of those four moves. It is the
one piece of state in this file, and it is state that cannot go stale — see the
comment on it for why that is a property of the key and not a hope, and for the
bound that keeps it from growing with the corpus.
"""

import datetime
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path

from . import config, gitrepo, procs, status

TAIL_BYTES = 128 * 1024
HEAD_BYTES = 16 * 1024


# ------------------------------------------------------------------- the memo
#
# What a sweep spends here, measured with getrusage(SELF)+(CHILDREN) over the
# real fleet — 9 worktrees, 539 transcripts under matched projects, 48 of them
# inside the 48 h window, 18,187 `.jsonl` under 1,071 subagent directories:
#
#   stage                              CPU     share
#   _subagent_files   (539 trees)    105 ms     54 %   <- 44 ms walk, 61 ms stat
#   parse_session_tail (48 files)     43 ms     22 %
#   find_last_user     (13 files)     27 ms     14 %
#   session_topic      (48 files)      6 ms      3 %
#   scan_sessions (whole)            196 ms
#
# Between two sweeps a second apart essentially none of that input changed. So
# every read here is keyed on the stat of what it read, and reused when the key
# is identical — ENGINE.md §4.3's stat memo, not §4.2's rejected byte offsets.
#
# TWO RULES, and they are the whole reason this is safe:
#
# * THE KEY CONTAINS EVERYTHING THAT CAN CHANGE THE ANSWER — everything a real
#   writer changes, at any rate; `StatMemo`'s docstring records exactly where
#   that stops being true and why the reconcile below bounds it. Size AND
#   mtime_ns, so an append is a miss; `(st_dev, st_ino)` and not just the path,
#   so a transcript that is rotated or replaced under its own name is a miss
#   too. That trap is not hypothetical: `_proven_in_transcript` kept a byte
#   offset with no identity check, a delivered message read as undelivered, and
#   an agent redid its work three times at 3x usage (ENGINE.md §4.2).
# * IT IS BOUNDED. The corpus grows ~982 `.jsonl`/day, so an unbounded map is a
#   leak with a multi-day uptime (§4.7). LRU cap plus an idle sweep, below.
#
# And it is audited: a cold sweep (RECONCILE_S, every 60 s) bypasses the memo,
# re-reads from the file, and counts any disagreement into `drift`. A memo
# nobody audits is worse than a slow sweep. Unlike the git cadence's drift —
# which is a measured cost, the cadence never claimed to be current — drift
# here is a LIE, and any non-zero `scan_drift`/`tree_drift` is a bug.
#
# After, same fleet: 196 -> 76 CPU-ms warm (2.6x), 98 % / 96 % hit rate. The
# cold reconcile costs 260 ms — a third more than an uncached scan, because it
# reads the directories twice to compare — once every 60 s, 0.4 % duty.

# Caps are sized off the per-sweep WORKING SET, not off a memory target: an LRU
# smaller than what one sweep touches thrashes to a 0 % hit rate and pays the
# extra stat for nothing. Overflow is graceful — it degrades to the behaviour
# this file had before the memo existed — but it is silent, so the caps are
# multiples of a measured working set, not round numbers.
#
# `_FACTS` holds one entry per transcript actually parsed: the in-window ones,
# 50 here (48 transcripts + 2 subagent reports). 2,048 is ~40x that; a fleet
# needing more has two thousand agents awake inside 48 hours.
#
# `_TREES` holds one entry per DIRECTORY under a session's subagent tree, and
# every session dir of every matched project is walked on every sweep — the
# walk is what rescues a session whose main transcript is out of window, see
# `_subagent_files` — so its working set is all 1,071 of them, not the 50.
# 4,096 is ~3.8x today's fleet and is §4.7's number.
#
# Measured, deep-sized, on that fleet: _FACTS 50 entries = 0.10 MB, _TREES
# 1,071 = 2.20 MB, i.e. ~2 KB per entry either way (a tree entry is the file
# NAMES of one directory; a facts entry is one card's worth of cleaned text).
# Both caps full would be ~12.6 MB, which is the number to argue with if these
# ever look generous.
MEMO_FILES = 2048
MEMO_DIRS = 4096

# Nothing outside the 48 h window is ever parsed again, and a session dir that
# stops being walked has had its worktree removed. An hour idle means neither
# will come back without a fresh stat anyway, so the entry is pure ballast.
MEMO_IDLE_S = 3600.0


class StatMemo:
    """A pure-function cache whose key is the stat of the thing it read.

    Not a model of the world and not a state machine: the value is a function
    of the bytes, and the key changes whenever the bytes can have. So it does
    not go stale — it is only ever evicted, and eviction costs one re-read.
    "Whenever the bytes can have" is doing real work in that sentence, and it
    is not free: see THE BOUNDARY below for the one write that changes the
    bytes without changing the key.

    `ident` is WHAT was read — `(path, st_dev, st_ino)`. Identity is in the
    ident so a rotated file misses; the path is in it too so a recycled inode
    at a different path cannot be mistaken for a hit. `key` is the part that
    moves — `(st_size, st_mtime_ns)`.

    THE BOUNDARY (ADR 0011). Four numbers decide a hit — `st_size`,
    `st_mtime_ns`, `st_dev`, `st_ino` — so a write that preserves ALL FOUR
    serves stale content: an in-place rewrite of the same byte count, on the
    same inode, with `os.utime(p, ns=…)` putting the nanosecond mtime back.
    Reproduced on disk, not argued from the key — see
    `test_the_boundary_an_in_place_rewrite_preserving_all_four_stats`, which
    exists to make this paragraph go red if the key ever gets stronger. Two
    things keep it a recorded boundary and not a bug:

    * It is adversarial, not realistic. Claude Code transcripts are appended
      to, never rewritten, so `st_size` moves on every real write. Nothing in
      this repo rewrites one; the failure needs a deliberate editor.
    * It is BOUNDED. The cold reconcile (config key `reconcile_s`, 60 s by
      default) bypasses this memo entirely, re-reads from the file, and counts
      a disagreement into `scan_drift`/`tree_drift`. So a defeated key costs at
      most one `reconcile_s` of stale text and is visible afterwards rather
      than silent — which
      is the entire difference between this and the byte-offset cache that let
      a delivered message read as undelivered (§4.2).

    Recorded because the boundary of a cache should be written down where the
    cache is, not discovered later by whoever is holding it. And note how the
    first attempt to demonstrate it FAILED and read as proof of safety:
    `os.utime` with float seconds cannot restore the nanosecond component
    (measured here: ...216723268 came back as ...216723203), so the key moved
    anyway and the memo correctly missed. A negative result from a test that
    could not have succeeded is not evidence — that one is pinned too, as
    `test_a_float_utime_cannot_restore_the_nanoseconds_it_truncated`.

    LOCKED, for the same reason `procs.ProcMemo` is. `scan_sessions` runs on
    the sweep thread AND on any HTTP thread whose `cached_state()` found a
    parked cache — which is every request immediately after a mutation, by
    design. Every method here is a read-then-mutate pair, and none of them is
    atomic: `get` does `_d.get(ident)` and then `move_to_end(ident)`, and
    `expire` does `next(iter(_d))` and then `del`. Reproduced, not theorised —
    four threads on this class raise `RuntimeError: OrderedDict mutated during
    iteration` out of `expire` and `KeyError` out of `get` within seconds. The
    window is narrow at MEMO_IDLE_S=3600 (`expire` almost always breaks on its
    first entry), which is exactly what makes it the kind of bug that ships:
    it surfaces as one 500 on the board, months apart, with no way to reproduce.

    Nothing expensive is ever called under the lock — `_read_facts` and the
    directory walks run outside it, between a `peek`/`get` and a `put`.
    """

    def __init__(self, cap, idle_s=MEMO_IDLE_S):
        self.cap, self.idle_s = cap, idle_s
        self._d = OrderedDict()      # ident -> [key, value, last seen]
        self._lock = threading.Lock()
        self.hits = self.misses = self.evictions = self.drift = 0

    def get(self, ident, key, now):
        with self._lock:
            e = self._d.get(ident)
            if e is None or e[0] != key:
                self.misses += 1
                return None
            e[2] = now
            self._d.move_to_end(ident)
            self.hits += 1
            return e[1]

    def peek(self, ident):
        """What the memo holds, without counting a hit — the cold audit's view
        of 'what would have been served'."""
        with self._lock:
            e = self._d.get(ident)
            return None if e is None else (e[0], e[1])

    def put(self, ident, key, value, now):
        with self._lock:
            self._d[ident] = [key, value, now]
            self._d.move_to_end(ident)
            while len(self._d) > self.cap:
                self._d.popitem(last=False)
                self.evictions += 1

    def expire(self, now):
        """Drop what has not been observed for `idle_s`. Least-recently-seen
        sits at the front, so this stops at the first live entry."""
        with self._lock:
            while self._d:
                ident = next(iter(self._d))
                if now - self._d[ident][2] < self.idle_s:
                    break
                del self._d[ident]
                self.evictions += 1

    def clear(self):
        """Everything is suspect — after a wake, per §4.5."""
        with self._lock:
            self._d.clear()

    def __len__(self):
        return len(self._d)


_FACTS = StatMemo(MEMO_FILES)     # transcript -> the facts a card needs
_TREES = StatMemo(MEMO_DIRS)      # subagent directory -> its entry names


def memo_stats():
    """Counters for `observer.stats()`. `scan_drift` is the load-bearing one:
    it is the number of times a cold re-read disagreed with what the memo would
    have served, and it must be 0."""
    return {"scan_hits": _FACTS.hits, "scan_misses": _FACTS.misses,
            "scan_drift": _FACTS.drift, "scan_entries": len(_FACTS),
            "tree_hits": _TREES.hits, "tree_misses": _TREES.misses,
            "tree_drift": _TREES.drift, "tree_entries": len(_TREES),
            "memo_evictions": _FACTS.evictions + _TREES.evictions}


def memo_drift():
    return _FACTS.drift + _TREES.drift


def memo_clear():
    _FACTS.clear()
    _TREES.clear()


# ---------------------------------------------------------------- collectors

def claude_homes():
    # Precedence: --home / config "homes" > CLAUDE_CONFIG_DIRS (colon-separated,
    # same convention as cclimits) > auto-discover ~/.claude*
    explicit = config.CFG["homes"] or [
        h for h in os.environ.get("CLAUDE_CONFIG_DIRS", "").split(":") if h]
    if explicit:
        return [Path(h).expanduser() for h in explicit
                if (Path(h).expanduser() / "projects").is_dir()]
    homes = []
    for p in sorted(config.HOME.iterdir()):
        if (p.name == ".claude" or p.name.startswith(".claude-")) and (p / "projects").is_dir():
            homes.append(p)
    return homes


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
    r"task-notification|\btoolu_[A-Za-z0-9]|\[SYSTEM NOTIFICATION|"
    # The compaction preamble. The CLI writes it as a *user* entry, so without
    # this the board quotes the harness back to you as "the last thing you told
    # it" — on precisely the long-running sessions you most need to read.
    r"This session is being continued from a previous conversation|"
    # Agent-to-agent messages injected by a teammate harness: another machine
    # talking, not this user.
    r"<teammate-message\b|"
    # Terminal mouse-tracking escapes that leak into the transcript when a
    # click lands in the composer (observed: "<64;58;44M58;44M/exit").
    r"<\d+;\d+;\d+[Mm]")


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


# ------------------------------------------------- delegated background work
#
# `pendingWorkflowCount` / `pendingBackgroundAgentCount` ride the CLI's own
# end-of-turn record and are right when non-zero — but they are INCOMPLETE.
# Measured over the 154 end-of-turn claims of step 5: 23 saw the agent speak
# again with no human prompt in between, and on every one of those the two
# counts read 0 while a `<task-notification>` — a background task reporting
# back, which resumes the session — was what woke it. So the board's
# "delegated" guard has to be able to see the delegation itself.
#
# WHAT A BACKGROUND LAUNCH LOOKS LIKE ON DISK. Not the tool_use `input`:
# `run_in_background: true` is neither necessary nor sufficient. A foreground
# Bash that outruns its timeout is MOVED to the background by the harness (16
# of 485 notified Bash launches on this corpus arrived that way, with no
# `run_in_background` in the input at all), and 337 `Agent` calls that look
# identical in the input ran in the foreground and returned their report
# inline. What is decisive is the tool_RESULT, which the harness writes and
# which says so in words:
#
#   Bash      "Command running in background with ID: b3l1ahei … You will be
#              notified when it completes."
#   Bash      "Command did not complete within its 180s timeout and was moved
#              to the background (ID: bx1dek5dm). … You will be notified…"
#   Workflow  "Workflow launched in background. Task ID: w8oz82r5k"
#   Agent     "The agent is working in the background. You will be notified
#              automatically when it completes."
#   Monitor   "Monitor started (task b1h0ax9vj, persistent…). You will be
#              notified on each event."
#   SendMsg   "…resumed from transcript in the background with your message.
#              You'll be notified when it finishes."
#
# One phrase spans all six and is the actual contract — "you will be notified"
# is the harness promising to resume this session — so that is what is matched,
# rather than a list of tool names a future release would silently outgrow.
# Measured over 49,115 tool_use records in ~/.claude*: 2,069 results carry it
# (Bash 1,017, Workflow 541, Agent 442, Monitor 36, SendMessage 33), and no
# foreground result does.
#
# WHAT COMES BACK. A `<task-notification>` carrying BOTH `<tool-use-id>` and a
# terminal `<status>`. The pairing is exact, not heuristic: of 1,163 distinct
# tool-use-ids seen in notifications, 1,163 resolved to a tool_use in the
# corpus — 100 %.
#
# The `<status>` half is, on today's corpus, a SECOND LOCK ON THE SAME DOOR and
# is kept deliberately. What it is guarding against is real — a Monitor emits
# interim `<event>` notifications while it is still streaming, and reading one
# as "reported back" would drop the guard mid-stream — but those interim
# entries carry no `<tool-use-id>` either (397 of them, none with an id), so
# the id alone already excludes them. Measured: of 5,494 notifications carrying
# a tool-use-id, ZERO lack a terminal status (completed 5,065, failed 291,
# killed 81, stopped 4). It stays because the day an interim event gains an id
# is the day this guard fails silently, and the check costs one regex.
#
# The notification is written in three different shapes and all three must be
# read, which is why `_notification_texts` exists rather than a look at
# `message.content`: as a plain `user` entry (1,303), as a `queue-operation`
# with the text at top-level `content` (2,626 enqueue + 1,168 remove), and as
# an `attachment` whose text is at `attachment.prompt` (898).
_BG_LAUNCHED = re.compile(r"(?:will|'ll) be notified", re.I)
_NOTIFIED_ID = re.compile(r"<tool-use-id>\s*(toolu_[A-Za-z0-9_-]+)\s*</tool-use-id>")
_NOTIFIED_DONE = re.compile(r"<status>\s*(?:completed|failed|killed|stopped)\s*</status>")


def _entry_ts(e):
    """An entry's epoch seconds, or None if it is not dated.

    None is not an error and is not treated as one: the launch is simply not
    counted, so an undated transcript degrades to the behaviour this file had
    before the outstanding set existed. The alternative — dating it "now" —
    would hold a session at WORKING off an entry nobody can place in time.
    """
    t = e.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        return datetime.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _notification_texts(e):
    """Every place one entry can be carrying a `<task-notification>`.

    Never an `assistant` entry, and that is a rule before it is an
    optimisation: a notification is something the harness writes INTO a
    session, so an agent that merely quotes the tag back in its own prose — 14
    entries in the corpus do, none of them carrying a tool-use-id — must not be
    able to talk its own guard away. It happens to be worth 1.5 of the 7.4
    CPU-ms this whole signal adds to a cold parse of the fleet's 58 in-window
    transcripts (49.0 -> 54.8, median of 7), because assistant entries are half
    a tail and carry the long text blocks.

    The remaining +5.9 ms is the search of every tool_result for the receipt,
    and it is left alone: the stat memo means a sweep pays it only for
    transcripts that actually moved (~0.1 ms each), and the 60 s cold reconcile
    pays all of it for 0.01 % of a core.
    """
    if e.get("type") == "assistant":
        return ()
    out = []
    c = (e.get("message") or {}).get("content")
    if isinstance(c, str):
        out.append(c)
    elif isinstance(c, list):
        out += [b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"]
    if isinstance(e.get("content"), str):        # queue-operation
        out.append(e["content"])
    att = e.get("attachment")                    # attachment / queued_command
    if isinstance(att, dict) and isinstance(att.get("prompt"), str):
        out.append(att["prompt"])
    return out


def _result_text(b):
    c = b.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return ""


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
           "pending_bg_agents": 0, "turn_ended": False, "bg_launched_at": ()}
    pending = {}  # tool_use id -> tool name
    bg = {}       # tool_use id -> epoch of a background launch not yet reported
    for e in main:
        out["cwd"] = e.get("cwd") or out["cwd"]
        out["branch"] = e.get("gitBranch") or out["branch"]
        # BEFORE the `message` gate below: two of the three shapes a
        # notification arrives in (queue-operation, attachment) have no
        # `message` at all and would never be read past it.
        for text in _notification_texts(e):
            if "<task-notification>" not in text or not _NOTIFIED_DONE.search(text):
                continue
            for tid in _NOTIFIED_ID.findall(text):
                bg.pop(tid, None)
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            # POSITIONAL, not "a turn_duration exists somewhere in the tail":
            # every completed turn in history leaves one, so the mere presence
            # of the record is true almost always and would report a busy agent
            # as idle — the worst failure this board can have. `turn_ended` is
            # about the LATEST turn only, so a later `assistant` entry (the
            # agent speaking again) clears it below.
            out["turn_ended"] = True
            # a turn that ended still awaiting workflows or background agents
            # ("✻ Waiting for 1 background agent to finish") is NOT the user's
            # turn — the harness resumes the session when they report back.
            # These ride the same entry, so whenever `turn_ended` survives to
            # the end of the loop these two counts describe THAT turn, not a
            # stale one.
            out["pending_workflows"] = e.get("pendingWorkflowCount") or 0
            out["pending_bg_agents"] = e.get("pendingBackgroundAgentCount") or 0
        elif e.get("type") == "assistant":
            out["turn_ended"] = False    # the agent is talking again
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
                    # …and a human who has just typed is NOT waiting on
                    # themselves. The marker is stale from the moment the next
                    # prompt lands, seconds before the agent's first token —
                    # calling that "◆ YOUR TURN" would summon the user to the
                    # session they are typing in. FRESHNESS.md §4.2 resets the
                    # turn marker on "any later assistant / tool_result / human
                    # prompt"; this is the third of those. It can only ever
                    # WITHDRAW a claim of idleness, never make one.
                    out["turn_ended"] = False
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
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                pending.pop(b.get("tool_use_id"), None)
                # A background launch RESOLVES its tool_use immediately — the
                # result is the harness's receipt, not the work — so it must be
                # picked up here and not from `pending`, which is empty again
                # the moment the task starts. That is precisely why a
                # backgrounded agent looked idle.
                text = _result_text(b)
                if "<tool_use_error>" in text or not _BG_LAUNCHED.search(text):
                    continue
                launched = _entry_ts(e)
                if launched is not None:
                    bg[b.get("tool_use_id")] = launched
    out["pending_tools"] = sorted(set(pending.values()))
    # Timestamps, not a count: the bound belongs to whoever holds the clock.
    # This function is memoised on the transcript's stat (`_facts`) and must
    # stay a pure function of the bytes — a count that decayed with wall time
    # would be served frozen for as long as the file sat still. `scan_sessions`
    # ages them against `now`.
    out["bg_launched_at"] = tuple(sorted(bg.values()))
    return out


def _dir_entries(d):
    """(subdir names, *.jsonl names) for one directory, None if unreadable."""
    try:
        it = os.scandir(d)
    except OSError:
        return None                       # gone, or not a directory at all
    subs, files = [], []
    with it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False):
                    subs.append(e.name)
                elif e.name.endswith(".jsonl"):
                    files.append(e.name)
            except OSError:
                continue                  # raced with a subagent finishing
    return tuple(subs), tuple(files)


def _tree_jsonl(sub_dir, memo=None, now=0.0, read=True, write=True):
    """Every *.jsonl path under a session's subagent tree, in walk order.

    `memo` halves the walk, and only the half that can be halved honestly. A
    directory's mtime moves when an entry is created, removed or renamed in it,
    so it is a sound key for WHICH FILES EXIST — that is what is memoised here.
    It does NOT move when a file inside is appended to, which is why the mtimes
    are not this function's business: see `_subagent_files`.

    Names are stored, not paths, so a renamed tree cannot serve stale children.
    `read=False` re-reads every directory for real but still re-keys the memo;
    `write=False` consults it without refreshing it. The cold audit runs one of
    each and compares them.
    """
    stack = [str(sub_dir)]
    while stack:
        d = stack.pop()
        if memo is None:
            ent = _dir_entries(d)
        else:
            try:
                st = os.stat(d)
            except OSError:
                continue                  # gone, or not a directory at all
            ident = (d, st.st_dev, st.st_ino)
            key = (st.st_size, st.st_mtime_ns)
            ent = memo.get(ident, key, now) if read else None
            if ent is None:
                ent = _dir_entries(d)
                if ent is not None and write:
                    memo.put(ident, key, ent, now)
        if ent is None:
            continue
        subs, names = ent
        stack.extend(os.path.join(d, s) for s in subs)
        for n in names:
            yield os.path.join(d, n)


def _subagent_files(sub_dir, memo=None, now=0.0, read=True, write=True):
    """[(mtime, path)] for every *.jsonl under a session's subagent tree.

    A session running a Workflow writes only under <session-id>/ while its main
    transcript sits untouched, so this tree decides whether such a session is
    live — the ⚙ indicator, and the reason liveness cannot be judged from the
    main transcript's mtime alone. That makes it tempting to skip the walk for
    sessions whose main transcript is already out of window; don't. The
    out-of-window case is exactly the one this rescues, and it is silent when
    wrong: the session simply vanishes from the board.

    So the walk stays complete and gets cheap instead. os.scandir reuses one
    directory read per level and skips building a Path per entry; measured over
    163 real subagent dirs holding ~16k files, 146ms -> 84ms for an identical
    result. Then the directory reads themselves are memoised (`_tree_jsonl`):
    of 105 CPU-ms over 539 trees, 44 was reading directories and 61 was
    stat-ing the 18,187 files inside them.

    THE 61 STAYS, EVERY SWEEP, DELIBERATELY. `sub_mtime` is the liveness of a
    workflow session and it is never a remembered number: a directory's mtime
    does not move when a file inside it is appended to, so a memo keyed on it
    would report an active subagent as idle and the session would drop off the
    board with nothing to show it had.
    """
    out = []
    for p in _tree_jsonl(sub_dir, memo, now, read, write):
        try:
            out.append((os.lstat(p).st_mtime, Path(p)))
        except OSError:
            continue                      # raced with a subagent finishing
    return out


def _subagent_files_audited(sub_dir, now):
    """The cold path (§4.3 #1): re-read every directory for real, and count a
    disagreement with what the memo would have served into `_TREES.drift`.

    Only the SET OF FILES is compared, never their mtimes — the mtimes were
    never memoised, and the two walks stat them at two different instants, so
    an mtime that moved between them is a subagent writing, not a memo lying.
    """
    before = sorted(_tree_jsonl(sub_dir, _TREES, now, write=False))
    fresh = _subagent_files(sub_dir, memo=_TREES, now=now, read=False)
    if before != sorted(str(p) for _, p in fresh):
        _TREES.drift += 1
    return fresh


def _read_facts(fp):
    """Everything a card needs from ONE transcript's bytes, in one place.

    Bundled deliberately: these three reads share a key, so memoising them
    together is one stat instead of three and one entry instead of three.
    `find_last_user` re-reads a megabyte and only earns it when the 128 KB tail
    was all tool traffic — that stays true, it is just no longer paid per sweep.
    """
    tail = parse_session_tail(fp)
    return {"cwd": tail["cwd"], "branch": tail["branch"],
            "model": tail["model"],
            "pending_tools": tuple(tail["pending_tools"]),
            "pending_workflows": tail["pending_workflows"],
            "pending_bg_agents": tail["pending_bg_agents"],
            "bg_launched_at": tail["bg_launched_at"],
            "turn_ended": tail["turn_ended"],
            "last_assistant": tail["last_assistant"],
            "last_user": tail["last_user"] or find_last_user(fp),
            "topic": session_topic(fp)}


def _facts(fp, st, now, cold=False):
    """`_read_facts`, memoised on the transcript's stat — and audited when cold.

    Warm: `(path, dev, ino)` + `(size, mtime_ns)`. A transcript is append-only,
    so an append moves the size; a rotation moves the inode; either is a miss.

    Cold (§4.3 #1/#4): read from the file regardless, then compare against what
    the memo WOULD have served. The comparison only counts when the file's key
    is identical before and after the read — otherwise the file changed under
    us and a difference is the world moving, not the memo lying.
    """
    ident = (str(fp), st.st_dev, st.st_ino)
    key = (st.st_size, st.st_mtime_ns)
    if not cold:
        val = _FACTS.get(ident, key, now)
        if val is None:
            val = _read_facts(fp)
            _FACTS.put(ident, key, val, now)
        return val
    was = _FACTS.peek(ident)
    val = _read_facts(fp)
    try:
        after = os.stat(fp)
    except OSError:
        after = None
    stable = after is not None and (after.st_size, after.st_mtime_ns) == key
    if stable and was is not None and was[0] == key and was[1] != val:
        _FACTS.drift += 1
    _FACTS.put(ident, key, val, now)
    return val


def _subagent_said(fp, st, now, cold=False):
    """Last assistant text of a subagent file — same memo, same key."""
    ident = (str(fp), st.st_dev, st.st_ino, "said")
    key = (st.st_size, st.st_mtime_ns)
    if not cold:
        val = _FACTS.get(ident, key, now)
        if val is None:
            val = (last_assistant_text(fp),)
            _FACTS.put(ident, key, val, now)
        return val[0]
    was = _FACTS.peek(ident)
    val = (last_assistant_text(fp),)
    if was is not None and was[0] == key and was[1] != val:
        _FACTS.drift += 1
    _FACTS.put(ident, key, val, now)
    return val[0]


def scan_sessions(worktrees, all_procs, now, cold=False):
    """All recent sessions across every Claude home, mapped to worktrees.

    `all_procs`, not `procs`: the module object of that name is what the
    session↔process pairing now hangs off, and a parameter would shadow it.
    Callers pass it positionally, so the name is local knowledge.

    `cold` bypasses the stat memo (§4.3 #1): every transcript is re-read from
    the file and every directory re-scanned, and whatever the memo would have
    served is compared against the truth and counted into `drift`. Left False —
    which is every call but the reconcile sweep's — this reuses the parse of any
    transcript whose `(path, dev, ino, size, mtime_ns)` has not moved."""
    by_wt = {w["path"]: [] for w in worktrees}
    wt_prefixes = {w["path"]: gitrepo.munge(w["path"]) for w in worktrees}
    window_s = config.CFG["session_window_h"] * 3600

    for home in claude_homes():
        acct = config.account_label(home)
        for proj in (home / "projects").iterdir():
            wt = gitrepo.match_worktree(proj.name, wt_prefixes)
            if wt is None:
                continue
            for fp in proj.glob("*.jsonl"):
                try:
                    st = fp.stat()
                except OSError:
                    continue
                mtime = st.st_mtime
                # Workflows/subagents write to <session-id>/**/*.jsonl while the
                # main transcript sits untouched — count them toward activity.
                sub_dir = fp.with_suffix("")
                sub_files = (_subagent_files_audited(sub_dir, now) if cold else
                             _subagent_files(sub_dir, memo=_TREES, now=now))
                sub_mtime = max((m for m, _ in sub_files), default=0.0)
                # The newest thing the session "said" may be a subagent's
                # report (Claude Code shows those in the terminal too).
                subagent_said = None
                if sub_mtime > mtime:
                    for _, sf in sorted(sub_files, reverse=True)[:2]:
                        try:
                            sst = sf.stat()
                        except OSError:
                            continue
                        subagent_said = _subagent_said(sf, sst, now, cold)
                        if subagent_said:
                            break
                last_write = max(mtime, sub_mtime)
                age = now - last_write
                if age > window_s:
                    continue
                tail = _facts(fp, st, now, cold)
                cwd = tail["cwd"] or wt
                by_wt[wt].append({
                    "id": fp.stem[:8],
                    "sid": fp.stem,
                    "account": acct,
                    # ABSOLUTE, not now-derived: ENGINE.md §3.4. A field that
                    # moves with the clock makes every card differ on every
                    # publish, so the equality diff the version bump rests on
                    # degenerates to "everything changed". It also lets the
                    # client animate "wrote 2.3s ago" off Date.now() at frame
                    # rate instead of stepping once per poll.
                    # The `age_s` that used to ride beside it is GONE from the
                    # wire (both clients animate off this stamp now). Age is
                    # still computed — below, from `now`, for the ladder and
                    # the sort — it just never leaves the process.
                    "last_write_at": last_write,
                    "cwd": cwd,
                    "subdir": os.path.relpath(cwd, wt) if cwd != wt else None,
                    "branch": tail["branch"],
                    "model": (tail["model"] or "").replace("claude-", ""),
                    # a fresh list per card: the memo hands out one shared
                    # value to every sweep that hits it, and a Snapshot is
                    # frozen — nothing on the wire may alias memo state.
                    "pending_tools": list(tail["pending_tools"]),
                    "pending_workflows": tail["pending_workflows"],
                    "pending_bg_agents": tail["pending_bg_agents"],
                    # The bound, applied here because here is where `now` is.
                    # An outstanding launch is evidence with a shelf life: 3.8 %
                    # of the 2,000 background launches on this corpus never
                    # reported back at all (killed, or the server restarted),
                    # and an unbounded set would hold those sessions at WORKING
                    # for as long as they stayed in the 48 h window — a live
                    # agent that never asks for you is the failure this project
                    # keeps finding. `delegated_s` is the shelf life and
                    # config.py carries the table it was chosen off.
                    "pending_bg_tools": sum(
                        1 for t in tail["bg_launched_at"]
                        if now - t <= config.CFG["delegated_s"]),
                    "topic": tail["topic"],
                    "last_assistant": tail["last_assistant"],
                    "last_user": tail["last_user"],
                    "subagent_said": subagent_said,
                    # Its own clock at last, and a longer one: `working_s` was
                    # measured on a conversation's silences and this tree is an
                    # order of magnitude denser, so 90 s blinked the ⚙ off
                    # mid-flight on 1 subagent run in 15. config.py carries the
                    # distribution and the trade.
                    "subagents_active": bool(
                        sub_mtime and now - sub_mtime < config.CFG["subagent_grace_s"]),
                })
                if tail["turn_ended"]:
                    # conditional, like tool_running/bg_shell: present means
                    # "the CLI wrote an end-of-turn marker after the agent's
                    # last word", i.e. this status was OBSERVED and not decayed
                    # out of the 90 s window. Absent says nothing either way.
                    by_wt[wt][-1]["turn_ended"] = True

    # §4.7. The LRU cap is the hard bound; this is the one that matters in
    # practice, because the corpus grows ~982 .jsonl/day and a file that has
    # left the 48 h window is never read again.
    _FACTS.expire(now)
    _TREES.expire(now)

    rank = {"needs_input": 0, "blocked": 1, "working": 2, "waiting": 3, "ended": 4}
    for wt, sessions in by_wt.items():
        # FRESHEST FIRST. This used to read `key=age_s` ascending; the same
        # order off the absolute stamp is `last_write_at` DESCENDING — a bigger
        # age is OLDER, a bigger stamp is NEWER. `reverse=True` rather than a
        # negated key because Python's sort is stable in both directions: equal
        # stamps keep glob order, exactly as equal ages did.
        sessions.sort(key=lambda s: s["last_write_at"], reverse=True)
        # A live process proves at most ONE session is really attended.
        # N procs under a worktree vouch for its N freshest sessions —
        # freshness beats cwd matching (recorded cwds drift as agents cd
        # around, and a stale exact match must not outrank the live session).
        wt_procs = [p for p in all_procs if p.get("cwd") and
                    (p["cwd"] == wt or p["cwd"].startswith(wt + "/"))]
        # With --dangerously-skip-permissions there are no approval prompts:
        # an unresolved tool call means a long-running tool, not "blocked".
        skip_perms = bool(wt_procs) and all(
            "--dangerously-skip-permissions" in p["cmd"] for p in wt_procs)

        owner = procs.pair_sessions_with_procs(sessions, wt_procs)
        for s in sessions:
            proc = owner.get(s["sid"])
            alive = proc is not None
            shell_n = proc.get("shells", 0) if proc else 0
            s["pid"] = proc["pid"] if proc else None
            # only an account match is a real attribution; a fallback pairing is
            # a guess and must not be presented as one
            s["pid_certain"] = bool(proc and proc.get("account") == s["account"])
            # `sess_status`, not `status`: the module object of that name is
            # what classify_session now hangs off, and a local would shadow it.
            # Age is derived HERE, from this sweep's `now`, and handed to the
            # ladder as an argument — it is not a field on the session and
            # never reaches the wire. The old `int(age)` rounding is gone with
            # it, which changes nothing: every threshold is an integer, and
            # `int(a) < T` and `a < T` agree for integer T on a non-negative a.
            sess_status, tool_running = status.classify_session(
                now - s["last_write_at"], alive, s["pending_tools"],
                s["pending_workflows"] + s["pending_bg_agents"]
                + s["pending_bg_tools"],
                skip_perms, config.CFG["working_s"], shell_n,
                turn_ended=s.get("turn_ended", False),
                # All three clocks now come from config, each with its own
                # measurement and its own misfire rate beside it there. They
                # are separate arguments because they answer separate
                # questions: how long a tool may run unheard (block), how long
                # an unseen process may still be there (orphan), how long an
                # unexplained silence is still thought (quiet). `working_s`
                # keeps only the jobs nothing else claims.
                block_grace_s=config.CFG["block_grace_s"],
                orphan_grace_s=config.CFG["orphan_grace_s"],
                quiet_s=config.CFG["quiet_s"])
            s["status"] = sess_status
            if tool_running:
                s["tool_running"] = True
                if shell_n and not s["pending_tools"]:
                    s["bg_shell"] = True     # transcript idle, shell alive
        # severity, then freshest first — same inversion as the sort above
        sessions.sort(key=lambda s: (rank[s["status"]], -s["last_write_at"]))
        by_wt[wt] = sessions[: config.CFG["max_sessions"]]
    return by_wt
