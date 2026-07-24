"""orchestra.notify — snapshots in, notifications out.

The board already publishes a versioned `Snapshot` every time the composed view
changes (ENGINE.md §2.5, `observer.Observer`). This module is a CONSUMER of
that stream and of two sources beside it — the resume schedules and the
dispatch jobs, neither of which the observer watches. It does four things, in
order, and the order is the design:

  1. PROJECT the live world (a snapshot, the resume schedules, the dispatch
     jobs) down to exactly the fields a notification can turn on — `project`.
     Everything a notification could ever key on is here and nothing else is,
     so the diff below cannot be fooled by a stopwatch moving.
  2. DERIVE typed events by diffing two consecutive projections — `derive`.
     Pure, edge-triggered, no clock of its own beyond the timestamp it stamps.
     This is the LOSSLESS half: every edge becomes an `Event` and every `Event`
     is written to the `EventLog`, which the phone reconciles against on every
     foreground (API.md §9.22). Push can drop; this cannot.
  3. QUALITY CONTROL — `Notifier`. This is the half that decides whether push
     is tolerable or hateful: dedup (never twice for one condition), flap
     suppression (a status that blinks does not summon you), coalescing (three
     agents needing you is ONE notification), quiet hours in the device's own
     zone, and per-type preferences. A dropped push here is a decision, not a
     loss — the event is already in the log.
  4. COMPOSE the wire payload in orchestra's voice — `compose`. Terse, the
     board's own glyph vocabulary (● ▲ ⛔ ■ ◆ ○ ◇), and identifiers only: the
     prose is fetched by the phone's Notification Service Extension over the
     tailnet, never sent through Apple (UX.md §8.5 — "routing that same text
     through APNs would be a direct contradiction" of the no-public-exposure
     premise).

WHY A PROJECTION AND NOT THE SNAPSHOT ITSELF. Two reasons, both measured
elsewhere in this codebase. A card carries stopwatches (`cpu`, `etime`) that
move every second with nothing having happened — `observer._diffable` strips
them before the version diff for exactly this reason, and a notifier diffing
raw cards would fire on a resampled CPU percentage. And the things worth a
notification are not all IN the snapshot: a resume firing and a dispatch
landing are events in modules the observer does not watch, so the projection is
the one place all three join.

THE DIRECTION THAT IS DANGEROUS (METHOD.md §7). For a notification the
expensive error is the FALSE ALARM: a board that cries wolf gets its
notifications turned off at the OS level within a week, taking the P1 that
actually mattered with it. So every rule here fails toward SILENCE — an
unrecognised status emits nothing, a flapping session waits out its dwell, a
coalesced burst is one line, and the global budget drops the overflow into the
event log rather than onto the lock screen. Missing a your-turn by a sweep is
survivable; summoning the user to an agent that is mid-sentence is not.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict, replace

from . import config


# --------------------------------------------------------------- vocabulary

# The board's glyphs, one per status, reused verbatim so a notification reads
# like a row of the board the user already knows (index.html `STATUS`). A title
# NEVER leads with one — VoiceOver speaks "up-pointing triangle" before every
# alert and a Braille display renders the raw codepoint, imposing a mandatory
# noise token on exactly the population the glyph serves (UX.md §8.5). The glyph
# lives in the SUBTITLE line, where the word beside it carries the meaning.
GLYPH = {
    "needs_input": "▲", "blocked": "■", "waiting": "◆", "working": "●",
    "limit": "⛔", "ended": "○", "free": "◇", "resume": "⏱", "dispatch": "⌁",
    "died": "◍",
}

# Every event type, its tier, and its push defaults. Tiers are UX.md §8.4's, and
# they are the whole of "what makes push tolerable" before a single QC rule runs
# — the DEFAULT for a your-turn is OFF, because `waiting` occurs at the end of
# every turn, dozens of times a day, and pushing it is how a user disables
# notifications in week one.
#
#   level     — P1 (needs you now) · P2 (worth knowing) · P3 (quiet, no sound)
#   dwell_s   — how long the condition must HOLD before it may push. Flap
#               suppression: a status that blinks needs_input→working→needs
#               within the dwell fires once, not twice, and a your-turn that
#               the agent takes back (a long think mistaken for the prompt)
#               fires never. Wall-clock seconds, NEVER tick counts — the sweep
#               cadence varies 0.15–45 s and a tick count silently changes
#               meaning with it, slowest exactly when nobody is watching.
#   collapse  — does a later event of this type SUPERSEDE an undelivered
#               earlier one? Only for a state that replaces itself (a count, a
#               badge); never for a discrete fact, where collapse deletes
#               history.
#   default   — pushed unless the device turned it off.
LEVELS = {"P1": 10, "P2": 6, "P3": 3}    # priority, and the per-hour budget

EVENT_TYPES = {
    # a question is on screen — read before any clock in classify_session, so
    # no dwell floor exists or is wanted.
    "session.needs_answer": {"level": "P1", "dwell_s": 0, "collapse": False,
                             "default": True, "glyph": "needs_input"},
    # a permission dialog / unresolved tool. Dwell, because "awaiting approval"
    # and "tool still running" are the same bytes until the silence outlasts a
    # genuine tool run.
    "session.blocked": {"level": "P1", "dwell_s": 40, "collapse": False,
                        "default": True, "glyph": "blocked"},
    # the turn closed and the agent is idle at the prompt. OFF by default — the
    # single most important default in the file.
    "session.your_turn": {"level": "P2", "dwell_s": 20, "collapse": False,
                          "default": False, "glyph": "waiting"},
    "account.limit_hit": {"level": "P2", "dwell_s": 20, "collapse": False,
                          "default": True, "glyph": "limit"},
    "account.limit_reset": {"level": "P3", "dwell_s": 0, "collapse": False,
                            "default": True, "glyph": "working"},
    "resume.armed": {"level": "P3", "dwell_s": 0, "collapse": False,
                     "default": False, "glyph": "resume"},
    "resume.fired": {"level": "P2", "dwell_s": 0, "collapse": False,
                     "default": True, "glyph": "resume"},
    "resume.failed": {"level": "P1", "dwell_s": 0, "collapse": False,
                      "default": True, "glyph": "resume"},
    "dispatch.succeeded": {"level": "P3", "dwell_s": 0, "collapse": False,
                           "default": True, "glyph": "dispatch"},
    "dispatch.failed": {"level": "P1", "dwell_s": 0, "collapse": False,
                        "default": True, "glyph": "dispatch"},
    # a worktree became FREE. Off by default and P3 — informational, and the
    # one most likely to be noise on a busy fleet.
    "worktree.free": {"level": "P3", "dwell_s": 0, "collapse": False,
                      "default": False, "glyph": "free"},
    # was working seconds ago, now gone, with uncommitted or unpushed work.
    "session.died": {"level": "P2", "dwell_s": 30, "collapse": False,
                     "default": True, "glyph": "died"},
}


# --------------------------------------------------------------- projection

# The statuses that COUNT as attention for card-level and account-level rules.
_ALIVE = ("working", "waiting", "needs_input", "blocked")

# The statuses a session must have been in RECENTLY for its disappearance to be
# a death worth a push (API.md §9.23): a session that was idle/ended already is
# not a crash. Edge-triggering supplies the "≤ 300 s ago" bound — `derive` diffs
# against the immediately preceding projection, which the cadence sweep keeps
# under a minute old.
_DIED_FROM = {"working", "needs_input", "blocked"}


def project(snapshot=None, resumes=None, dispatch_jobs=None, accounts=None):
    """The live world, flattened to what a notification can turn on.

    Every value here is diff-stable: a field that moves on its own (a CPU
    sample, an `etime`, `age_s`) is deliberately absent, so two projections
    taken a second apart with nothing having happened compare EQUAL and derive
    no events. That is the whole reason the diff is taken here and not on the
    snapshot — see the module docstring.

    Each argument is optional so a caller can diff one source in isolation (the
    tests do, and so does a deployment that has push but no resume daemon). A
    `None` source contributes an empty sub-map, which diffs against a populated
    one as "everything went away" — correct: if the resume daemon stopped
    reporting, its schedules are no longer armed as far as anyone can see.
    """
    sessions = {}
    worktrees = {}
    if snapshot is not None:
        cards = getattr(snapshot, "cards", None)
        if cards is None and isinstance(snapshot, dict):
            cards = {c["name"]: c for c in snapshot.get("worktrees", [])}
        for name, card in (cards or {}).items():
            worktrees[name] = card.get("availability")
            git = card.get("git") or {}
            # dirty/unlanded at the worktree grain, stamped onto each of its
            # sessions so `derive` can gate `session.died` on it AFTER the
            # session is gone (API.md §9.23): an agent that vanished with clean,
            # landed work is not a crash worth a push. A count (not a stopwatch),
            # reduced to a bool so a saved file that changes the dirty COUNT does
            # not itself read as a session edge.
            dirty = bool(git.get("dirty")) or bool(git.get("ahead"))
            for s in card.get("sessions", []):
                sessions[s["sid"]] = {
                    "status": s.get("status"),
                    "worktree": name,
                    "account": s.get("account"),
                    "model": s.get("model"),
                    "topic": s.get("topic"),
                    # a limit session that has been handed off is NOT an alert —
                    # work already continued elsewhere, which is precisely the
                    # classic false positive (ARCHITECTURE.md §6.3).
                    "handed_to": s.get("handed_to"),
                    "dirty": dirty,
                }

    acct = {}
    for label, a in (accounts or {}).items():
        acct[label] = {"exhausted": bool(a.get("exhausted")),
                       "worst": a.get("worst"), "group": a.get("group"),
                       "resets_at": a.get("resets_at")}

    res = {}
    for key, r in (resumes or {}).items():
        res[key] = {"status": r.get("status"), "worktree": r.get("worktree"),
                    "account": r.get("account"), "message": r.get("message")}

    jobs = {}
    for jid, j in (dispatch_jobs or {}).items():
        # only a FINISHED job carries an edge worth an event; a running one is
        # progress the app polls. `done` gates it, and `ok` splits it.
        result = j.get("result") or {}
        jobs[jid] = {"done": bool(j.get("done")),
                     "ok": bool(result.get("ok")),
                     "worktree": result.get("worktree"),
                     "account": result.get("account"),
                     "message": result.get("message"),
                     "session": result.get("session")}

    return {"sessions": sessions, "worktrees": worktrees, "accounts": acct,
            "resumes": res, "dispatch": jobs}


EMPTY_PROJECTION = {"sessions": {}, "worktrees": {}, "accounts": {},
                    "resumes": {}, "dispatch": {}}


# ------------------------------------------------------------------- events

@dataclass
class Event:
    """One thing that happened, edge-triggered. The durable unit (API.md §9.22).

    `dedupe_key` is the identity of the CONDITION, not of the event: two events
    with the same key are the same fact observed twice, and the QC layer fires
    push for the first and swallows the rest. A key ends in a generation
    counter for the repeatable conditions (a session can need you, be answered,
    and need you again — three distinct questions, three keys) so that a genuine
    re-ask is a new condition rather than a suppressed duplicate.

    `open` marks a condition the user can still resolve at the Mac — a question,
    a block, a your-turn. The phone withdraws any delivered notification whose
    condition is no longer open (API.md §9.22 `/events/open`), which is what
    stops "▲ needs you" sitting on the lock screen after the question was
    answered at the keyboard.
    """
    id: str
    at: float
    type: str
    level: str
    dedupe_key: str
    worktree: str = None
    session_id: str = None
    account: str = None
    model: str = None
    topic: str = None
    open: bool = False
    counts: dict = field(default_factory=dict)
    detail: str = None            # the human sentence — never sent via APNs

    @property
    def glyph(self):
        return GLYPH.get(EVENT_TYPES.get(self.type, {}).get("glyph"), "◇")

    def public(self):
        d = asdict(self)
        d["glyph"] = self.glyph
        return d


def _gen(prev_status, cur_status, prev_gen):
    """The generation counter for a repeatable condition. It advances each time
    the condition is RE-ENTERED from a non-condition state, so a question
    answered and re-asked gets a fresh dedupe key and is not swallowed as a
    duplicate of the first."""
    return (prev_gen or 0) + 1


# The set of statuses that mean "the user can act on this at the Mac", so the
# event is `open` and the phone must withdraw it on resolution.
_OPEN_STATUSES = {"needs_input", "blocked", "waiting"}

# The status each session-scoped pending arm ASSERTS, keyed by its dedupe-base
# type. A pending is cancelled the moment its session stops holding exactly this
# — not merely when it leaves every open status — so a `blocked` arm does not
# survive the block becoming a question.
_EXPECTED_STATUS = {"session.needs_answer": "needs_input",
                    "session.blocked": "blocked",
                    "session.your_turn": "waiting"}


def derive(prev, cur, now=None, gens=None):
    """Diff two projections; return the events on the edges between them.

    PURE. No disk, no clock but the timestamp it stamps, no push. Given the
    same two projections it returns the same events, which is what lets the
    whole event vocabulary be unit-tested against hand-built projections with
    nothing standing in.

    `gens` is an optional `{dedupe_base: count}` the caller threads through
    calls so a re-entered condition gets a fresh generation. Left None, every
    repeatable condition is generation 1 — correct for a single diff, wrong
    only for the counter, which the `Notifier` supplies.

    The events are returned UNSTAMPED with `id=""`; the `EventLog` assigns ids
    from its persisted sequence, because an id is a position in a total order
    and only the log knows the order.
    """
    now = time.time() if now is None else now
    gens = {} if gens is None else gens
    out = []

    def emit(type_, dedupe_key, **kw):
        spec = EVENT_TYPES[type_]
        out.append(Event(id="", at=now, type=type_, level=spec["level"],
                         dedupe_key=dedupe_key, **kw))

    ps, cs = prev["sessions"], cur["sessions"]

    # ---- session status edges ---------------------------------------------
    for sid, s in cs.items():
        old = ps.get(sid, {}).get("status")
        new = s["status"]
        if new == old:
            continue
        base = None
        if new == "needs_input":
            base = f"session.needs_answer|{sid}"
        elif new == "blocked":
            base = f"session.blocked|{sid}"
        elif new == "waiting":
            base = f"session.your_turn|{sid}"
        if base is not None:
            g = gens.get(base, 0) + 1
            gens[base] = g
            type_ = {"needs_input": "session.needs_answer",
                     "blocked": "session.blocked",
                     "waiting": "session.your_turn"}[new]
            emit(type_, f"{base}|{g}", worktree=s["worktree"], session_id=sid,
                 account=s["account"], model=s["model"], topic=s.get("topic"),
                 open=True)
        # a session that flips TO limit, with no handoff, is a limit_hit at the
        # session grain (the account-wide edge is handled below; this catches a
        # model-scoped cap that strands one session without exhausting the
        # account).
        if new == "limit" and not s.get("handed_to"):
            base = f"account.limit_hit|{sid}"
            g = gens.get(base, 0) + 1
            gens[base] = g
            emit("account.limit_hit", f"{base}|{g}", worktree=s["worktree"],
                 session_id=sid, account=s["account"], model=s["model"])

    # ---- account-wide limit edges -----------------------------------------
    pa, ca = prev["accounts"], cur["accounts"]
    for label, a in ca.items():
        was = pa.get(label, {}).get("exhausted", False)
        if a["exhausted"] and not was:
            emit("account.limit_hit", f"account.limit_hit|{label}|{a.get('group')}",
                 account=label)
        elif was and not a["exhausted"]:
            emit("account.limit_reset", f"account.limit_reset|{label}",
                 account=label)

    # ---- resume schedule edges --------------------------------------------
    pr, cr = prev["resumes"], cur["resumes"]
    for key, r in cr.items():
        old = pr.get(key, {}).get("status")
        new = r["status"]
        if new == old:
            continue
        if old is None and new == "pending":
            emit("resume.armed", f"resume.armed|{key}", worktree=r["worktree"],
                 account=r["account"])
        elif new == "done" and old == "pending":
            emit("resume.fired", f"resume.fired|{key}", worktree=r["worktree"],
                 account=r["account"], detail=r.get("message"))
        elif new == "failed":
            emit("resume.failed", f"resume.failed|{key}", worktree=r["worktree"],
                 account=r["account"], detail=r.get("message"))

    # ---- dispatch job edges -----------------------------------------------
    pj, cj = prev["dispatch"], cur["dispatch"]
    for jid, j in cj.items():
        was_done = pj.get(jid, {}).get("done", False)
        if j["done"] and not was_done:
            type_ = "dispatch.succeeded" if j["ok"] else "dispatch.failed"
            emit(type_, f"{type_}|{jid}", worktree=j.get("worktree"),
                 account=j.get("account"), detail=j.get("message"))

    # ---- worktree became free ---------------------------------------------
    pw, cw = prev["worktrees"], cur["worktrees"]
    for name, avail in cw.items():
        if avail == "free" and pw.get(name) not in (None, "free"):
            emit("worktree.free", f"worktree.free|{name}", worktree=name)

    # ---- a recently-live session vanished with uncommitted / unlanded work -
    # The one edge that is triggered by an ABSENCE rather than a transition: a
    # session that was working/blocked/needs_input in `prev` and is now gone (or
    # `ended`) with a dirty or unlanded branch is an agent that crashed on work
    # nobody has saved (API.md §9.23). The dirty gate is what keeps a clean,
    # landed exit — the normal end of a turn — silent; the 30 s dwell (its
    # EVENT_TYPES entry) damps a session a single sweep merely missed.
    for sid, old_s in ps.items():
        if old_s.get("status") not in _DIED_FROM or not old_s.get("dirty"):
            continue
        cur_s = cs.get(sid)
        if cur_s is None or cur_s.get("status") == "ended":
            emit("session.died", f"session.died|{sid}",
                 worktree=old_s.get("worktree"), session_id=sid,
                 account=old_s.get("account"), model=old_s.get("model"))

    return out


# ---------------------------------------------------------------- event log

class EventLog:
    """The durable, ordered, persisted side of push (API.md §9.22).

    Push is lossy by construction — collapse supersedes, expiration discards,
    quiet hours holds, an offline phone gets only the most recent — so the phone
    reconciles against THIS on every foreground. That makes the log, not the
    push, the source of truth, and it is why a dropped push costs latency and
    never a fact.

    Ids are `evt-%06d` from a monotonic sequence that PERSISTS across restarts,
    so `since` is a total order no restart reuses. `epoch` changes when the log
    is truncated or the sequence resets, and a client seeing a new epoch or a
    `since` older than the retained window resyncs from state rather than
    replaying — a replay across a gap is a lie of omission.
    """

    def __init__(self, path=None, cap=500):
        self.path = path
        self.cap = cap
        self._lock = threading.Lock()
        self._events = []          # newest last
        self._seq = 0
        self.epoch = f"{int(time.time()):08x}"
        self.started_at = time.time()
        if path:
            self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.loads(f.read())
        except (OSError, ValueError):
            return
        self._seq = int(data.get("seq", 0))
        self.epoch = data.get("epoch", self.epoch)
        for e in data.get("events", [])[-self.cap:]:
            try:
                self._events.append(Event(**{k: v for k, v in e.items()
                                             if k in Event.__dataclass_fields__}))
            except (TypeError, ValueError):
                continue

    def _save(self):
        if not self.path:
            return
        blob = json.dumps({"seq": self._seq, "epoch": self.epoch,
                           "events": [e.public() for e in self._events]})
        try:
            tmp = str(self.path) + ".tmp"
            # 0o600 on CREATE, exactly as audit.log.jsonl and devices.json in
            # this same (often Dropbox/iCloud-synced) directory: the events log
            # carries topics — the first line of what the user typed — resume and
            # dispatch messages, worktree names and session ids, none of which a
            # second local account or a synced copy should read. os.replace
            # carries the tmp's mode onto the destination, so the live file is
            # 0o600 too and not just the temp.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(blob)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def append(self, events):
        """Assign ids and store. Returns the stored `Event`s, now with ids."""
        if not events:
            return []
        with self._lock:
            for e in events:
                self._seq += 1
                e.id = f"evt-{self._seq:06d}"
                self._events.append(e)
            if len(self._events) > self.cap:
                self._events = self._events[-self.cap:]
            self._save()
            return list(events)

    def since(self, since_id=None, limit=50):
        """Events after `since_id`, oldest first. `reset=True` when the cursor
        fell out of the retained window and the client must resync."""
        with self._lock:
            evs = self._events
            if since_id is None:
                page = evs[-limit:]
                return {"epoch": self.epoch, "events": [e.public() for e in page],
                        "next_since": page[-1].id if page else since_id,
                        "reset": False, "server_started_at": self.started_at}
            idx = next((i for i, e in enumerate(evs) if e.id == since_id), None)
            if idx is None:
                # unknown cursor: either aged out or from another epoch — resync
                return {"epoch": self.epoch, "events": [], "next_since": since_id,
                        "reset": True, "server_started_at": self.started_at}
            page = evs[idx + 1: idx + 1 + limit]
            return {"epoch": self.epoch, "events": [e.public() for e in page],
                    "next_since": page[-1].id if page else since_id,
                    "reset": False, "server_started_at": self.started_at}

    def get(self, event_id):
        with self._lock:
            for e in self._events:
                if e.id == event_id:
                    return e.public()
        return None

    def open_keys(self, now=None):
        """The dedupe keys still OPEN — a condition the user can resolve at the
        Mac that has not been resolved. The reconcile route (`/events/open`):
        the phone withdraws every delivered notification whose key is absent
        here. Only the LATEST event per key counts — a key that was opened and
        then closed (needs_input → waiting → working) is not open.
        """
        with self._lock:
            latest = {}
            for e in self._events:
                latest[e.dedupe_key] = e
            return sorted(k for k, e in latest.items() if e.open)


# --------------------------------------------------------------- preferences

@dataclass
class Preferences:
    """One device's notification settings (API.md §9.23).

    Delivered pushes cannot be filtered ON the device — the payload is already
    on the lock screen — so every toggle here is SERVER state or it is
    decorative (ROADMAP.md M8). The server holds it, the server honours it, and
    a `rules` entry that is absent falls back to the event type's own default.
    """
    quiet_from: str = None          # "23:00", device-local
    quiet_to: str = None            # "08:00"
    quiet_allow_p1: bool = False    # let P1 through quiet hours
    tz_offset_min: int = 0          # device UTC offset; quiet hours in ITS zone
    rules: dict = field(default_factory=dict)   # type -> bool override
    muted_until: float = 0.0        # a hard mute (snooze), absolute epoch
    nudge_min: int = 15

    def wants(self, type_):
        if type_ in self.rules:
            return bool(self.rules[type_])
        return EVENT_TYPES.get(type_, {}).get("default", False)


def _hhmm(s):
    m = re.match(r"^(\d{1,2}):(\d{2})$", s or "")
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h * 60 + mi


def quiet_now(prefs, now=None):
    """Is `now` inside the device's quiet window, in the DEVICE's zone?

    Quiet hours are evaluated against the phone's offset, not the server's — a
    server in London and a phone in California must both mean the phone's
    night. The offset is re-sent on every foreground because iOS gives no
    background callback for a timezone change, and a fixed offset is wrong at
    every DST transition (which is why the client sends an IANA id too, but the
    offset is what the arithmetic here needs).

    Windows that WRAP MIDNIGHT (23:00→08:00) are the normal case and the one a
    naive `from <= t < to` gets backwards — it would mean "quiet only in the
    eight-hour slice from 08:00 to 23:00", i.e. loud all night. Handled
    explicitly.
    """
    now = time.time() if now is None else now
    a, b = _hhmm(prefs.quiet_from), _hhmm(prefs.quiet_to)
    if a is None or b is None:
        return False
    local = (now + prefs.tz_offset_min * 60)
    minutes = int(local // 60) % 1440
    if a == b:
        return False
    if a < b:
        return a <= minutes < b
    return minutes >= a or minutes < b        # wraps midnight


# ------------------------------------------------------------------ compose

# The APNs `aps.category` iOS reads AT DELIVERY to decide whether to hang an
# inline-reply text field on the banner — before the user touches it, and
# without any app code running (there is no NSE, and even one couldn't change
# the reply affordance retroactively). It MUST name a category the app
# registered (`ios/App/PushController.categories`): "ORC_REPLY" carries the
# reply field, "ORC_INFO" carries nothing and just opens the app on tap. And the
# answerable rule MUST match `ios Push.isAnswerable`. There is no shared source
# across the language boundary, so the two are kept in step by this comment and
# `test_notify` — a divergence means either a dead Reply button (server said
# INFO) or a Reply button that goes nowhere (server said REPLY with no sid).
REPLY_CATEGORY = "ORC_REPLY"
INFO_CATEGORY = "ORC_INFO"
# An agent actually waiting on the user — and only these two, per iOS
# `isAnswerable`. `session.your_turn` is a nudge, not a question, so it opens the
# app rather than offering a reply box that would type into a finished turn.
ANSWERABLE_EVENTS = frozenset({"session.needs_answer", "session.blocked"})


def _title(event):
    """The first line, and it carries NO leading glyph — see UX.md §8.5. The
    worktree is the subject because that is what the user navigates by."""
    wt = event.worktree or "orchestra"
    return {
        "session.needs_answer": f"{wt} needs an answer",
        "session.blocked": f"{wt} is blocked",
        "session.your_turn": f"{wt} — your turn",
        "account.limit_hit": (f"[{event.account}] hit its limit"
                              if event.account else f"{wt} hit a limit"),
        "account.limit_reset": f"[{event.account}] limit reset",
        "resume.armed": f"auto-resume armed for {wt}",
        "resume.fired": f"resumed {wt}",
        "resume.failed": f"auto-resume couldn't reach {wt}",
        "dispatch.succeeded": f"mission launched in {wt}",
        "dispatch.failed": f"mission not launched in {wt}",
        "worktree.free": f"{wt} is free",
        "session.died": f"{wt} stopped unexpectedly",
    }.get(event.type, wt)


def _subtitle(event):
    """The status line: glyph + one uppercase word + the account tag. This is
    where the glyph lives, spoken after the word, so a screen reader hears the
    meaning and then the shape rather than the codepoint first."""
    tag = f" · [{event.account}]" if event.account else ""
    model = f" · {event.model}" if event.model else ""
    word = {
        "session.needs_answer": "NEEDS ANSWER",
        "session.blocked": "BLOCKED",
        "session.your_turn": "YOUR TURN",
        "account.limit_hit": "LIMIT HIT",
        "account.limit_reset": "LIMIT RESET",
        "resume.armed": "ARMED",
        "resume.fired": "RESUMED",
        "resume.failed": "RESUME FAILED",
        "dispatch.succeeded": "LAUNCHED",
        "dispatch.failed": "DISPATCH FAILED",
        "worktree.free": "FREE",
        "session.died": "STOPPED",
    }.get(event.type, event.type)
    return f"{event.glyph} {word}{tag}{model}"


def compose(event, privacy="structural", server="orchestra", badge=None):
    """The APNs payload plus the transport headers, as one dict.

    `payload` is the JSON body; `headers` is what `push.post` needs
    (`push_type`, `priority`, `expiration`, `collapse_id`). They are returned
    together because getting one right and the other wrong is a silent failure
    — a P1 payload sent at priority 5 is delayed for power exactly when it
    matters.

    IDENTIFIERS ONLY unless `privacy == "detail"`. The prose (`event.detail` —
    the transcript line, the mission words) is fetched by the phone's NSE over
    the tailnet, so it never transits Apple. `structural` is the default and
    puts the glyph, the worktree, the status and the account on the lock screen
    — enough to act, nothing to leak.
    """
    spec = EVENT_TYPES.get(event.type, {"level": "P2", "collapse": False})
    level = spec["level"]
    # P3 is quiet: priority 5 lets iOS bundle it for power. P1/P2 are immediate
    # (10). A 10 on a `background` push is rejected, but every push here is an
    # `alert`, so 10 is always legal.
    priority = 5 if level == "P3" else 10

    interruption = {"P1": "time-sensitive", "P2": "active",
                    "P3": "passive"}.get(level, "active")

    alert = {"title": _title(event), "subtitle": _subtitle(event)}
    if privacy == "detail" and event.detail:
        alert["body"] = event.detail

    aps = {"alert": alert,
           "interruption-level": interruption,
           "thread-id": f"{server}|{event.worktree or '—'}",
           "mutable-content": 1,           # let the NSE enrich the body
           "content-available": 1}         # refresh the app's cache for free
    if badge is not None:
        aps["badge"] = int(badge)
    if level == "P3":
        aps.pop("sound", None)             # quiet: no sound
    else:
        aps["sound"] = "default"
    # The reply affordance, decided on the wire (see REPLY_CATEGORY). Only an
    # answerable event WITH a session to answer gets the field — an answerable
    # event with no sid has nowhere to send the text, so it opens the app
    # instead of showing a dead reply box.
    aps["category"] = (REPLY_CATEGORY
                       if event.type in ANSWERABLE_EVENTS and event.session_id
                       else INFO_CATEGORY)

    payload = {"aps": aps,
               "ev": event.type,
               "event_id": event.id,
               "dedupe_key": event.dedupe_key,
               "at": event.at,
               "wt": event.worktree,
               "sid": event.session_id,
               "level": level}
    if event.counts:
        payload["counts"] = event.counts

    headers = {"push_type": "alert", "priority": priority,
               # absolute epoch, never a duration: a P1 lives an hour, a P3 ten
               # minutes. `int(at + ttl)`; a bare duration means "expired in
               # 1970" and Apple makes one attempt with no store-and-forward.
               "expiration": int(event.at + (3600 if level == "P1" else 600)),
               # a discrete fact NEVER collapses (it would delete history); only
               # a self-superseding state does, and none of ours is marked so.
               "collapse_id": event.dedupe_key if spec.get("collapse") else None}
    return {"payload": payload, "headers": headers, "level": level}


# ------------------------------------------------------------------ notifier

@dataclass
class _Pending:
    """A condition seen but not yet pushed — held for its dwell."""
    event: object
    since: float


class Budget:
    """A per-level hourly ceiling. Every notification product that ships
    without one is muted at the OS level within a week — at which point the P1s
    the user actually wants are silently gone too (ARCHITECTURE.md §6.3).

    Overflow is not dropped: it is already in the `EventLog`, so the phone sees
    it on the next foreground. What the budget drops is the PUSH, not the fact.
    """

    def __init__(self, per_hour=None):
        self.per_hour = per_hour or {k: v for k, v in
                                     {"P1": 12, "P2": 6, "P3": 6}.items()}
        self._sent = {}            # level -> [timestamps within the window]

    def allow(self, level, now):
        window = [t for t in self._sent.get(level, []) if now - t < 3600]
        self._sent[level] = window
        if len(window) >= self.per_hour.get(level, 6):
            return False
        window.append(now)
        return True


class Notifier:
    """The quality-control layer: what actually gets pushed, and once.

    It is fed one projection at a time by whoever owns the sweep — `observe`
    diffs against the last projection, writes every edge to the `EventLog`
    (lossless), and returns the events that SHOULD push after four gates, in
    this order because each depends on the last:

      1. PREFERENCE — the device turned this type off. (Cheapest, and it makes
         the rest moot.)
      2. DWELL / FLAP — the condition has not held long enough. A status that
         blinks needs_input→working→needs within the dwell never reaches push
         twice, and a your-turn the agent takes back never reaches it at all.
      3. COALESCE — three sessions crossing into needs_input in one sweep are
         ONE notification carrying a count, not three lock-screen lines.
      4. BUDGET / QUIET — the hourly ceiling, and the device's night. Both drop
         the push and keep the fact.

    DEDUP — "never twice for one condition" — is not a gate here; it is
    STRUCTURAL, and that is the stronger guarantee. A condition that HOLDS
    produces exactly one edge (`derive` skips an unchanged status), so a
    session parked at a question over fifty sweeps derives one event, not
    fifty. A condition RE-ENTERED is a genuinely new episode — a question
    answered and re-asked — and its session key carries a generation counter
    that makes it a distinct fact that SHOULD push again. A growing "already
    fired" set was the first design and it was quietly wrong in the one
    direction that matters: it would have permanently suppressed the SECOND
    time an account hit its limit or a worktree was freed, because those keys
    carry no generation and recur verbatim. Edge-triggering dedups the hold;
    the generation counter distinguishes the re-ask; a suppression set does
    neither job correctly and breaks the recurrence case. It is gone.

    A resolving edge (a session leaving needs_input) CANCELS a still-dwelling
    arm for that condition — that is the flap suppression, and it is why the
    notifier holds `_pending` rather than emitting on the raw edge.
    """

    def __init__(self, sink=None, log=None, prefs=None, budget=None,
                 backoff=None, server="orchestra"):
        from . import push
        self.sink = sink
        self.log = log or EventLog()
        self.prefs = prefs or Preferences()
        self.budget = budget or Budget()
        self.backoff = backoff or push.Backoff()
        self.server = server
        self._prev = EMPTY_PROJECTION
        self._pending = {}         # dedupe_base -> _Pending, held for its dwell
        self._gens = {}            # dedupe_base -> generation counter
        self._lock = threading.Lock()
        self.pushed = 0
        self.suppressed = 0
        # the last `Response` from an ACTUAL wire send (None when `_deliver`
        # short-circuited before the wire, or the sink was a no-op). The Service
        # reads it to record `note_push` on every attempt and to prune a token
        # Apple says is gone — outcomes the return bool (pushed / not) hides.
        self.last_response = None

    # -- the entry point ----------------------------------------------------

    def observe(self, projection, now=None, device_token=None, counts=None):
        """One sweep. Returns the events that were PUSHED (already sent if a
        sink and token were given). Every derived event is logged regardless."""
        now = time.time() if now is None else now
        with self._lock:
            events = derive(self._prev, projection, now=now, gens=self._gens)
            cur_sessions = projection["sessions"]
            self._prev = projection
            # 1. record everything, lossless, before any suppression.
            for e in events:
                e.counts = counts or {}
            self.log.append(events)
            # 2. push decisions.
            to_push = self._select(events, cur_sessions, now)
            sent = []
            for e in to_push:
                if self._deliver(e, device_token, now):
                    sent.append(e)
            return sent

    # -- the four gates -----------------------------------------------------

    def _select(self, events, cur_sessions, now):
        # Resolving edges first: an arm still dwelling for a session that no
        # longer holds the SPECIFIC status the arm asserts is cancelled, so a
        # flap fires nothing. Checking merely "left every open status" was wrong:
        # a `blocked` arm survived the dialog turning into a QUESTION
        # (blocked → needs_input, still an open status), and then fired a second,
        # mislabelled P1 for a condition that had already pushed as needs_answer.
        for base in list(self._pending):
            sid = _sid_of(base)
            if sid is None:
                continue
            expected = _EXPECTED_STATUS.get(base.split("|", 1)[0])
            if expected is not None and \
                    cur_sessions.get(sid, {}).get("status") != expected:
                del self._pending[base]
        # A death arm is cancelled if the session came back alive: a sweep that
        # briefly missed a session must not summon the user to a crash that did
        # not happen. (`_sid_of` deliberately does NOT match `session.died`, so
        # the loop above never cancels a death arm for the session being ABSENT
        # — which is exactly the condition it is dwelling on.)
        for base in list(self._pending):
            if base.startswith("session.died|"):
                sid = base.split("|", 1)[1]
                s = cur_sessions.get(sid)
                if s is not None and s.get("status") in _ALIVE:
                    del self._pending[base]

        for e in events:
            spec = EVENT_TYPES.get(e.type)
            if spec is None:
                continue                          # unknown → silence (METHOD §7)
            if not self.prefs.wants(e.type):      # gate 1: preference
                self.suppressed += 1
                continue
            base = _base_of(e.dedupe_key)
            if spec["dwell_s"] > 0:               # gate 2: dwell / flap
                self._pending[base] = _Pending(e, now)
            else:
                self._pending.setdefault(base, _Pending(e, now))

        ready = []
        for base, p in list(self._pending.items()):
            spec = EVENT_TYPES[p.event.type]
            if now - p.since < spec["dwell_s"]:
                continue                          # still dwelling
            # One edge in, one push out — dedup is the edge-triggering, not a
            # set. See the class docstring for why a suppression set is gone.
            del self._pending[base]
            ready.append(p.event)

        return self._coalesce(ready, now)         # gate 3

    def _coalesce(self, events, now):
        """Three agents needing you is ONE notification, not three.

        Grouped by (type, level): a burst of the same kind collapses to the
        FIRST event carrying a `+N more` count in its detail and its payload.
        A single event passes through untouched — coalescing a burst of one is
        just the event. Different types never merge: a needs-answer and a
        limit-hit are different actions and must stay two lines.
        """
        by_kind = {}  # gate 3: coalesce
        for e in events:
            by_kind.setdefault((e.type, e.level), []).append(e)
        out = []
        for (type_, _), group in by_kind.items():
            head = group[0]
            if len(group) > 1:
                head.counts = dict(head.counts or {})
                head.counts["coalesced"] = len(group)
                extra = len(group) - 1
                head.detail = ((head.detail or _title(head))
                               + f" · +{extra} more like this")
                self.suppressed += extra
            out.append(head)
        return out

    def _deliver(self, event, device_token, now, environment=None):
        """Gate 5 (budget/quiet) then the wire. Returns True if pushed.

        `environment` is the device's own registered APNs environment
        (production/sandbox); threaded to the sink so a sandbox build's push
        goes to the sandbox host on the FIRST try rather than eating a
        BadDeviceToken and a heal-retry every single time.
        """
        self.last_response = None
        if quiet_now(self.prefs, now):            # quiet hours
            level = EVENT_TYPES[event.type]["level"]
            if not (level == "P1" and self.prefs.quiet_allow_p1):
                self.suppressed += 1
                return False
        if now < self.prefs.muted_until:          # snooze / hard mute
            self.suppressed += 1
            return False
        if not self.budget.allow(EVENT_TYPES[event.type]["level"], now):
            self.suppressed += 1
            return False
        if (self.sink is None or device_token is None
                or getattr(self.sink, "name", None) == "none"):
            # the pipeline runs to completion with no (working) sink — the event
            # is logged, the decision is made, only the last hop is a no-op. This
            # is the state the user is in until they create a key, and it must be
            # indistinguishable from working to everything above. A NoopSink is
            # this state, so it is EXEMPT from backoff and failure accounting: its
            # Response(0) is retriable, and letting it feed the shared Backoff
            # would silently throttle a fleet that has push perfectly configured
            # the moment a key lands, and would skew pushed/suppressed.
            self.pushed += 1
            return True
        if self.backoff.blocked(now):
            self.suppressed += 1
            return False
        badge = (event.counts or {}).get("needs_input", 0) + \
                (event.counts or {}).get("blocked", 0)
        wire = compose(event, privacy=self.prefs.rules.get("_privacy",
                       "structural"), server=self.server, badge=badge or None)
        r = self.sink.send(device_token, wire["payload"],
                           environment=environment, **wire["headers"])
        self.last_response = r
        if r.retriable:
            self.backoff.note(r, now)
        elif r.ok:
            self.backoff.ok()
        self.pushed += 1
        return r.ok


def _base_of(dedupe_key):
    """Everything but the trailing generation counter — the CONDITION's
    identity across re-entries."""
    parts = dedupe_key.rsplit("|", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return dedupe_key


def _sid_of(base):
    """The session id inside a session-scoped dedupe base, or None."""
    m = re.match(r"session\.(?:needs_answer|blocked|your_turn)\|([^|]+)", base)
    return m.group(1) if m else None


# ------------------------------------------------------------- the live service

EVENTS_LOG = config.HERE / "events.log.json"


def prefs_from_device(push):
    """A device's stored push object (auth) -> `Preferences`. Everything the
    device set via `/api/v1/devices/self/settings`, with the type defaults for
    anything it left unsaid."""
    push = push or {}
    q = push.get("quiet_hours") or {}
    rules = dict(push.get("rules") or {})
    if push.get("privacy"):
        rules["_privacy"] = push["privacy"]
    return Preferences(
        quiet_from=q.get("from") if q.get("enabled") else None,
        quiet_to=q.get("to") if q.get("enabled") else None,
        quiet_allow_p1=bool(q.get("allow_p1")),
        tz_offset_min=int(push.get("tz_offset_min") or 0),
        rules=rules,
        muted_until=float(push.get("muted_until") or 0.0),
        nudge_min=int(push.get("nudge_min") or 15))


class Service:
    """The one live pipeline: the observer's snapshots in, push out, per device.

    World-level work happens ONCE per sweep — derive, the durable log, the
    dwell/flap/coalesce QC — because the world is the same for every phone. Then
    the ready events fan out per device, and only the device-specific gates
    (that device's preferences, its quiet hours in its own zone, its budget,
    its APNs token) run per device. That split is why three phones do not
    triple the git work or the log, and why turning your_turn on for one phone
    does not turn it on for another.

    The sink is held LONG-LIVED and rebuilt only when the credentials in config
    change, so the moment the user drops a real `.p8` into place the very next
    event is a real push with no restart — and until then every phone gets a
    `NoopSink` that runs the whole pipeline and reports `no key`, which is
    exactly the state this project ships in. Rebuilding it every sweep, as an
    earlier cut did, threw away the sink's `healed_environment`, `last` and JWT
    warmth on every tick — a device on the wrong environment then paid two curl
    round trips forever. One shared `Backoff` lives here too, not per device: a
    429/503 is Apple's word about the SERVICE, and backing off one device while
    the others keep hammering earns a longer ban (push.Backoff's own docstring).
    """

    def __init__(self, log_path=None, server="orchestra"):
        self.log = EventLog(path=log_path)
        self.server = server
        self._prev = EMPTY_PROJECTION
        self._gens = {}
        self._baselined = False   # first observe after start re-baselines silently
        self._per_device = {}     # devid -> Notifier (holds that device's QC)
        self._sink = None         # long-lived; rebuilt only when creds change
        self._sink_creds = None
        self._backoff = None      # one shared Backoff, injected into every device
        self._lock = threading.Lock()
        self.cursor = 0           # last observer version consumed

    def _notifier_for(self, devid, push):
        n = self._per_device.get(devid)
        if n is None:
            n = Notifier(sink=None, log=self.log, server=self.server)
            # the durable log and the world-derive state are SHARED, so this
            # per-device notifier only owns the device's own QC — its dwell
            # bookkeeping and budget. The sink and the Backoff are the Service's,
            # injected each sweep (a 429 is the service's word, not a device's).
            # It never derives or logs; the Service does that once. We drive it
            # through `_deliver`-shaped calls below rather than `observe`.
            self._per_device[devid] = n
        n.prefs = prefs_from_device(push)
        return n

    def _sink_for(self, pushmod):
        """The long-lived sink, rebuilt only when the config credentials change.

        A fresh sink every sweep would discard `healed_environment`, `last` and
        the warm JWT; keying the rebuild on `Credentials.from_config()` (a frozen
        dataclass, so `==` is by value) means the drop-in of a real `.p8` swaps
        the NoopSink for an APNsSink on the very next sweep, and nothing else
        does. A rebuild also resets the shared Backoff — a hold earned against
        the old backend says nothing about the new one."""
        creds = pushmod.Credentials.from_config()
        if self._sink is None or creds != self._sink_creds:
            self._sink = pushmod.sink(creds)
            self._sink_creds = creds
            self._backoff = pushmod.Backoff()
        return self._sink

    def observe(self, projection, now=None, counts=None):
        """One sweep. Derive + log once; deliver per push device. Returns the
        number of pushes actually sent (or would-be-sent under a NoopSink)."""
        from . import auth, push as pushmod
        now = time.time() if now is None else now
        with self._lock:
            # The FIRST observe after construction is a silent baseline: derive
            # to advance the generation counters and seed `_prev`, but append
            # nothing and push nothing. Without it, every process restart diffs
            # empty→world and re-fires a P1 for every standing question, a P2 for
            # every exhausted account, and — worst — a `resume.failed` for every
            # failed schedule still on disk (they persist 24 h), all carrying the
            # SAME dedupe keys as the pre-restart originals. The persisted log
            # already holds those events; re-emitting them re-buzzes the phone for
            # questions it was already told about (ARCHITECTURE.md §6.5's "re-
            # baseline without emitting", here for restart rather than wake).
            events = derive(self._prev, projection, now=now, gens=self._gens)
            self._prev = projection
            if not self._baselined:
                self._baselined = True
                return 0
            for e in events:
                e.counts = counts or {}
            self.log.append(events)

            # world-level derive/log done once (above). The per-device gates —
            # preference, dwell/flap/coalesce, quiet/budget/mute — run per device
            # against that device's own Notifier.
            sent = 0
            devices = auth.push_devices()
            if not devices:
                return 0
            sink = self._sink_for(pushmod)     # long-lived; shared Backoff too
            cur_sessions = projection["sessions"]
            for d in devices:
                n = self._notifier_for(d["id"], d.get("push"))
                n.sink = sink
                n.backoff = self._backoff      # one hold for the whole fleet
                environment = (d.get("push") or {}).get("environment")
                if sink.name == "apns":
                    # per-device attribution of a heal: clear before this
                    # device's sends so a value left by the previous device is
                    # not persisted onto this one.
                    sink.healed_environment = None
                # reuse the device notifier's gates by handing it the already-
                # derived events; its own `_select` applies this device's
                # preference + dwell, `_coalesce`, then `_deliver` applies its
                # quiet/budget/mute and sends to its token. COPIES, not the
                # originals: `_coalesce` rewrites `detail`/`counts` on the head
                # event, and the originals are what the durable log holds — one
                # device's coalescing must not rewrite the logged record or the
                # next device's view of it.
                copies = [replace(e) for e in events]
                ready = n._select(copies, cur_sessions, now)
                token = (d.get("push") or {}).get("token")
                dead = False
                for e in ready:
                    if n._deliver(e, token, now, environment=environment):
                        sent += 1
                    if sink.name != "apns":
                        continue
                    r = n.last_response       # the ACTUAL wire result, or None
                    if r is None:
                        continue
                    # record EVERY attempt, success or failure — a push that
                    # silently stopped after a restore is otherwise
                    # indistinguishable from a quiet fleet (note_push's docstring).
                    auth.note_push(d["id"], r.status, now)
                    # Apple says this token is gone (410 / BadDeviceToken): forget
                    # it, or it costs a POST every notification and a strike with
                    # Apple for pushing to a dead token. Stop sending to it now.
                    if r.gone:
                        auth.forget_push(d["id"])
                        dead = True
                        break
                # a 400→other-host heal means this device is registered against
                # the wrong environment: persist the correction so every future
                # push does not pay the same double round trip (push.APNsSink).
                if not dead and sink.name == "apns" and sink.healed_environment:
                    auth.set_push(d["id"], {"environment": sink.healed_environment})
            return sent

    def pump(self, observer_mod, timeout=25.0):
        """Block until the observer publishes a version past our cursor, then
        feed that snapshot through. One iteration of the daemon loop, exposed so
        a test can drive it deterministically without the thread."""
        obs = observer_mod
        snap = obs.snapshot() if hasattr(obs, "snapshot") else None
        if snap is None:
            return 0
        if snap.v <= self.cursor:
            return 0
        self.cursor = snap.v
        proj = project(snap, resumes=_safe_resumes(), accounts=_safe_accounts(),
                       dispatch_jobs=_safe_dispatch())
        return self.observe(proj, now=time.time(), counts=snap.counts)


def _safe_resumes():
    try:
        from . import resume
        return resume.resume_public()
    except Exception:
        return {}


def _safe_accounts():
    try:
        from . import limits
        return limits.limits_by_account()
    except Exception:
        return {}


def _safe_dispatch():
    """The dispatch jobs, snapshotted under their lock. `dispatch.succeeded` /
    `dispatch.failed` (P1, default on) can only fire if this source is threaded
    into the projection — without it a dispatched mission that fails to launch,
    the exact fire-and-forget the user walked away from, notifies nobody."""
    try:
        from . import dispatch
        with dispatch._jobs_lock:
            return {jid: dict(j) for jid, j in dispatch._jobs.items()}
    except Exception:
        return {}


_service = None
_service_lock = threading.Lock()


def service():
    """The process-wide `Service`, built once. Reached as `notify.service()` so
    tests can install their own by setting `notify._service`."""
    global _service
    with _service_lock:
        if _service is None:
            import getpass
            try:
                server = getpass.getuser()
            except Exception:
                server = "orchestra"
            _service = Service(log_path=EVENTS_LOG, server=server)
        return _service


def push_loop(observer_mod):
    """The daemon: consume every snapshot the observer publishes, forever.

    A consumer of the publish point, exactly like the SSE stream — it waits on
    a new version, feeds it through, repeats. It never drives the sweep and
    never blocks it; a slow Apple or a wedged curl delays only this thread's
    next iteration, and the observer keeps publishing.
    """
    svc = service()
    while True:
        try:
            snap = observer_mod.wait_for(svc.cursor, timeout=30.0)
            if snap is None:
                # No new snapshot version this interval — but the pipeline still
                # has to run on a CADENCE, not only on a version bump. A dwell-
                # held push (blocked 40 s, your_turn 20 s, limit_hit 20 s) is
                # armed on one sweep and RELEASED by a later one, and the resume /
                # account / dispatch sources change without ever bumping the
                # snapshot version. Re-observe the current snapshot (same
                # version) so those pending dwells fire and those edges derive,
                # within 30 s, instead of starving until unrelated board activity.
                snap = observer_mod.snapshot()
                if snap is None:
                    continue
            else:
                svc.cursor = snap.v
            proj = project(snap, resumes=_safe_resumes(),
                           accounts=_safe_accounts(),
                           dispatch_jobs=_safe_dispatch())
            svc.observe(proj, now=time.time(), counts=snap.counts)
        except Exception:
            # never let this thread die: a bad snapshot must cost one iteration,
            # not the whole push pipeline. The next publish re-derives.
            time.sleep(1.0)


def send_test(devid=None, now=None):
    """Send one test push, end to end, and return the transport result.

    The `--send-test-push` path and the `/api/v1/push/test` route both land
    here. It composes a real notification, signs a real JWT, and does the real
    HTTP/2 POST — the ONLY thing it cannot do without a key is get a 200 back,
    and it says exactly that. This is what lets the user verify the whole
    pipeline the moment they have a `.p8`, and diagnose it precisely if they do
    not: a 403 names the bad credential, a 0 names the unreachable host.
    """
    from . import auth, push as pushmod
    now = time.time() if now is None else now
    devices = auth.push_devices()
    if devid:
        devices = [d for d in devices if d["id"] == devid]
    if not devices:
        return {"ok": False, "error": "no_push_device",
                "message": "no paired device has registered a push token — "
                           "pair a phone and let it register first"}
    d = devices[0]
    token = (d.get("push") or {}).get("token")
    env = (d.get("push") or {}).get("environment")
    sink = pushmod.sink()
    ev = Event(id="test", at=now, type="dispatch.succeeded", level="P3",
               dedupe_key=f"push.test|{int(now)}", worktree="orchestra",
               detail="test push from orchestra — the pipeline is wired")
    wire = compose(ev, privacy="detail", server=service().server)
    r = sink.send(token, wire["payload"], environment=env, **wire["headers"])
    auth.note_push(d["id"], r.status, now)
    if r.gone:
        auth.forget_push(d["id"])       # a dead token never becomes live again
    health = sink.health() if hasattr(sink, "health") else {}
    return {"ok": r.ok, "backend": sink.name, "status": r.status,
            "apns_id": r.apns_id, "reason": r.reason,
            "environment": r.environment or env,
            "message": r.summary(), "health": health}
