"""orchestra.observer — one board-shaped snapshot of everything being watched.

`collect_state` is the join: worktrees from `gitrepo`, live processes from
`procs`, sessions from `transcripts`, then policy on top. First the limit
join — an agent parked at the prompt on an exhausted account isn't "your
turn", it's out of juice — read from the cclimits cache, or from the CLI's own
limit notice in the transcript when that cache is cold. Then handoff
awareness: a stranded session whose worktree has a FRESHER live one is
annotated `handed_to` and stops counting as attention. Then each card gets an
availability, the cards get a severity sort, and the counts strip is tallied.

Watching is read-only and touches nothing: every read here is a `git`/`ps`
query or a bounded tail of a transcript. Nothing is written, nothing is typed.

`_cache` holds the last snapshot for `STATE_TTL_S` seconds, so a board polling
every couple of seconds doesn't re-shell `git` twice a second. It is mutated
in place and never rebound: the act layer parks a `_cache["t"] = 0.0` in it so
a button reverts on the very next poll instead of four seconds later, and the
tests poke `_cache["state"]` through the facade — same object either way.
Patch `observer.cached_state`, never the facade copy.

`demo_state` is fictional data with the exact shape of `collect_state`, for
screenshots. `cached_state` is the one entry point the server calls.

`Observer` is the publish point (ENGINE.md §2.5): one perpetual thread that
sweeps on its own cadence and publishes an immutable, versioned `Snapshot`.
It exists because observation today happens only when a client asks — with no
browser attached nothing computes, nothing is detected, and no notification
can ever fire. That is structural, not a tuning problem.

Nothing starts it on import. `python3 -m orchestra` starts it; a test that
imports the package gets exactly today's lazy behaviour, and so does a
deployment that simply never calls `start_observer()` — that is the rollback.
"""

import getpass
import os
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace

from . import config, gitrepo, procs, transcripts, status, limits

STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}


# ---------------------------------------------------------------- collectors

def collect_state(fresh=None):
    """Compose the board. `fresh` is an optional out-parameter.

    Pass a dict and the collector stamps it `kind -> wall clock of that kind's
    last SUCCESSFUL probe` (ENGINE.md §3.3): one `generated_at` cannot say that
    git is 47s stale because a fetch wedged. Left None it costs nothing and this
    function behaves exactly as it did before there was an Observer — which is
    what keeps `python3 tests/characterize.py` byte-identical across this change.
    """
    def stamp(kind):
        if fresh is not None:
            fresh[kind] = time.time()

    now = time.time()
    worktrees = gitrepo.discover_worktrees()
    stamp("worktrees")
    # `all_procs`, not `procs`: a local of that name would shadow the module.
    all_procs = procs.claude_processes()
    stamp("procs")
    sessions = transcripts.scan_sessions(worktrees, all_procs, now)
    stamp("transcripts")

    # An agent parked at the prompt on an exhausted account isn't "your turn" —
    # it's out of juice. Joined from the cclimits cache (populated lazily by
    # /api/limits; never fetched on the state path).
    acct_limits = limits.limits_by_account()
    if fresh is not None and limits._limits.get("data"):
        # the cclimits CACHE clock, not `now`. Nothing on the state path
        # probes cclimits (by design — it shells out and costs seconds), so
        # stamping `now` here would claim a freshness this data does not have.
        # This is the one kind that is genuinely hours behind the others.
        fresh["limits"] = limits._limits["t"]
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

    # one fan-out for every worktree's git state, rather than one blocking call
    # per card — this path is dominated by waiting on `git`, not by our own work
    git_by_root = gitrepo.git_info_many([w["git"] for w in worktrees])
    stamp("git")

    cards = []
    for w in worktrees:
        ss = sessions.get(w["path"], [])
        live = [p for p in all_procs if p.get("cwd") and
                (p["cwd"] == w["path"] or p["cwd"].startswith(w["path"] + "/"))]
        cards.append({
            **w,
            "git": git_by_root.get(w["git"]) or gitrepo.git_info(w["git"]),
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

    from . import finish   # late by design: finish imports observer at module
                           # level for the cache-invalidation seam. ADR 0010,
                           # 'cycles'. Keep this import function-local.
    for c in cards:
        c["availability"] = status.card_availability(
            _attention_statuses(c["sessions"]), bool(c["live_procs"]))
        # two-step finish: while a closeout brief is with this card's live
        # agent, the button reads ✕ close. The flag dies with the terminal,
        # so a card never offers to close an agent that no longer exists.
        ts = finish._closeouts.get(c["name"])
        if ts:
            if c["live_procs"]:
                c["closeout_sent"] = ts
            else:
                finish._closeouts.pop(c["name"], None)

    matched = {p["pid"] for c in cards for p in c["live_procs"]}
    other = [p for p in all_procs if p["pid"] not in matched]

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



# ------------------------------------------------------- the publish point

# Cadences, §2.5. IDLE_S deviates from the document's 1.0 and the reason is
# measured: §2.5's 1.0 assumes the within-sweep git memo it paces with git_s,
# which does not exist yet, so every sweep is a full collect — 600 ms against
# the live fleet here (14 worktrees, 41 sessions). At 1.0 s that is a ~38 %
# duty cycle, forever, on battery, with nobody watching. At 3.0 s it is ~17 %,
# barely more than the ~12 % an open tab already costs at today's 5 s poll,
# and it keeps `_cache` warm inside STATE_TTL_S so /api/state never collects.
# Tighten it when the memo lands and a sweep stops costing 600 ms.
IDLE_S = 3.0        # cadence with no evidence of change
HOT_S = 0.15        # floor between sweeps after a nudge
# A cold sweep bypasses the parse memo (§4.3) and counts the disagreements as
# drift. There is no memo yet, so a cold sweep is identical to a warm one and
# `drift` is honestly always 0 — the clock and the counter are here so step 4
# has somewhere to land, not because they do anything today.
RECONCILE_S = 60.0
MAX_STALE_S = 8.0   # never wait longer than this between sweeps
HIST = 512          # version/changed-keys ring, §3.5


@dataclass(frozen=True)
class Snapshot:
    """One completed sweep, immutable and versioned (ENGINE.md §3.1).

    ADVISORY. Safe to render, to diff, to notify from. Never a mutation
    precondition — a mutation validates against the world at the instant it acts.

    `cards` is name -> card, in the board's severity order (dicts preserve
    insertion order). It is the view the version is diffed against; the wire
    payload still travels as `cached_state()`'s list, so a duplicate worktree
    name — which the rest of the app already treats as impossible, `_closeouts`
    and `/api/finish` both key on it — costs delta precision, never a card.
    """
    v: int
    at: float
    cards: dict
    other_procs: list
    counts: dict
    freshness: dict = field(default_factory=dict)
    drift: int = 0
    sweep_ms: float = 0.0


# The stopwatches. Three card fields move on their own with nothing in the
# world having changed: `age_s` is `int(now - last_write_at)` and ticks once a
# second, `etime` is the same shape off `ps`, and `cpu` is a resampled
# percentage. All three ride the wire — the board draws them — but none may
# decide a version. Left in the diff, `v` would tick once a second forever on
# any box with a live agent, deltas would degenerate to full snapshots, and the
# notifier would fire on nothing. §3.2 says diff the composed view; this says
# the composed view is the card minus its stopwatches.
#
# Nothing is lost by removing them: every one has an ABSOLUTE twin already in
# the same object — `last_write_at` beside `age_s` (transcripts.py:308) — and
# `status`, which is what a threshold crossing actually means, keeps its vote.
_UNDIFFED_PROC_KEYS = ("cpu", "etime")
_UNDIFFED_SESSION_KEYS = ("age_s",)


def _strip(items, keys):
    return [{k: v for k, v in it.items() if k not in keys} for it in items]


def _diffable(card):
    """The card as the version sees it: identical but for the stopwatches."""
    live, sess = card.get("live_procs"), card.get("sessions")
    if not live and not sess:
        return card
    out = dict(card)
    if live:
        out["live_procs"] = _strip(live, _UNDIFFED_PROC_KEYS)
    if sess:
        out["sessions"] = _strip(sess, _UNDIFFED_SESSION_KEYS)
    return out


def _diffable_procs(plist):
    return _strip(plist, _UNDIFFED_PROC_KEYS)


class Observer:
    """Owns the ONLY perpetual read loop. Never mutates the world.

    Threading model (§2.5): exactly one thread, `observer-sweep`, reusing the
    ThreadPoolExecutor fan-out already inside `collect_state`. One Condition
    guards `_snap`/`_version`. Readers take no lock — `snapshot()` is a single
    attribute read of a frozen object.
    """

    def __init__(self, *, idle_s=IDLE_S, hot_s=HOT_S,
                 reconcile_s=RECONCILE_S, max_stale_s=MAX_STALE_S):
        # §2.5 also lists git_s and limits_s. Neither is implemented here and
        # neither is accepted: git_s paces a WITHIN-sweep memo that does not
        # exist yet, and limits_s would start polling cclimits from the sweep —
        # that is step 7. A parameter that silently does nothing is worse than
        # an absent one.
        self.idle_s, self.hot_s = idle_s, hot_s
        self.reconcile_s, self.max_stale_s = reconcile_s, max_stale_s
        self._cv = threading.Condition()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._snap = None
        self._version = 0
        self._hist = deque(maxlen=HIST)     # (version, changed card keys)
        self._dcards, self._dother = {}, []  # the published view, stopwatches out
        self._fresh = {}
        self._drift = 0                     # §4.3; no memo to disagree with yet
        self._sweep_ms = 0.0
        self._sweeps = 0
        self._publishes = 0
        self._errors = 0
        self._last_error = None
        self._cold_at = 0.0
        self._nudge_at = 0.0
        self._nudges = 0
        self._nudge_reason = None
        self._logged_error_at = 0.0

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    def start(self):
        with self._cv:
            if self.running:
                return self
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="observer-sweep", daemon=True)
            self._thread.start()
        return self

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None

    def _loop(self):
        while not self._stop.is_set():
            started = time.time()
            cold = (started - self._cold_at) >= self.reconcile_s
            try:
                self.sweep(cold=cold)
            except Exception as exc:               # noqa: BLE001 — a wedged
                self._errors += 1                  # probe must not kill the loop
                self._last_error = f"{type(exc).__name__}: {exc}"
                if started - self._logged_error_at > 60:
                    self._logged_error_at = started
                    print(f"orchestra: sweep failed — {self._last_error}",
                          file=sys.stderr)
            if self._stop.is_set():
                break
            nxt = started + self.idle_s
            if self._nudge_at > started:        # nudged while we were sweeping
                nxt = min(nxt, self._nudge_at + self.hot_s)
            self._wake.clear()
            if self._wake.wait(min(max(0.0, nxt - time.time()), self.max_stale_s)):
                # woken by a nudge: honour the hot floor so a burst of
                # mutations cannot turn the loop into a spin
                floor = started + self.hot_s - time.time()
                if floor > 0:
                    self._stop.wait(floor)

    # ----------------------------------------------------------- the sweep

    def sweep(self, cold=False):
        """One full collect, published. Also refreshes the request-path cache."""
        t_before, s_before = _cache["t"], _cache["state"]
        started = time.time()
        t0 = time.perf_counter()
        fresh = {}
        state = collect_state(fresh=fresh)
        ms = (time.perf_counter() - t0) * 1000.0
        self._sweeps += 1
        if cold:
            self._cold_at = started
        # Compare-and-swap, never a blind write. A mutation that parked
        # `_cache["t"] = 0.0` while this sweep was in flight means the state we
        # just collected predates it; dropping our write leaves the next request
        # to collect synchronously — exactly what happens today.
        if _cache["t"] == t_before and _cache["state"] is s_before:
            _cache["state"], _cache["t"] = state, started
        return self.publish(state, fresh=fresh, sweep_ms=ms)

    def publish(self, state, fresh=None, sweep_ms=None):
        """Turn one `collect_state()` result into a snapshot.

        `v` bumps ONLY when the composed view differs (§3.2). A sweep that finds
        nothing new refreshes `at` and `freshness` and publishes no new version:
        the data is still true, just not new. Diffing is by EQUALITY over the
        composed cards, never a fact-key -> card-key dependency map — a map is a
        second source of truth that drifts from the composition the first time
        somebody edits the pairing heuristic.
        """
        now = state.get("generated_at") or time.time()
        cards = {c["name"]: c for c in state.get("worktrees", [])}
        other = list(state.get("other_procs", []))
        counts = dict(state.get("counts", {}))
        with self._cv:
            prev = self._snap
            if prev is not None and now < prev.at:
                return prev      # an older collect landing late — never regress
            if fresh:
                self._fresh.update(fresh)
            if sweep_ms is not None:
                self._sweep_ms = sweep_ms
            self._publishes += 1
            new_d = {k: _diffable(c) for k, c in cards.items()}
            new_o = _diffable_procs(other)
            if (prev is not None and new_d == self._dcards
                    and counts == prev.counts and new_o == self._dother):
                # no version bump — but `at` and `freshness` are precisely the
                # fields that say "still true as of now", so they do move, and
                # the cards carry the current stopwatch readings.
                self._snap = replace(prev, at=now, cards=cards, other_procs=other,
                                     freshness=dict(self._fresh),
                                     sweep_ms=self._sweep_ms)
                return self._snap
            old_d = self._dcards if prev is not None else {}
            changed = [k for k, c in new_d.items() if old_d.get(k) != c]
            changed += [k for k in old_d if k not in new_d]
            self._version += 1
            self._hist.append((self._version, tuple(changed)))
            self._dcards, self._dother = new_d, new_o
            self._snap = Snapshot(self._version, now, cards, other, counts,
                                  dict(self._fresh), self._drift, self._sweep_ms)
            self._cv.notify_all()
            return self._snap

    # ------------------------------------------------------------ read API

    def snapshot(self):
        """The most recent completed sweep, or None before the first one."""
        return self._snap

    def wait_for(self, after, timeout=30.0):
        """Block until a version newer than `after` is published. None on timeout."""
        deadline = time.time() + timeout
        with self._cv:
            while self._version <= after:
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(left)
            return self._snap

    def delta_since(self, n):
        """What changed for a client at version `n` (§3.5).

        An unknown, too-old or ahead-of-us `n` gets a full snapshot; that is the
        entire resync path. Nothing consumes this yet — SSE is step 3 — but the
        envelope carries `type` from day one, so if deltas prove worthless
        deleting them removes lines and no concept.
        """
        snap = self._snap
        if snap is None:
            return None
        hist = tuple(self._hist)
        if n <= 0 or n > snap.v or not hist or n < hist[0][0] - 1:
            return {"type": "snapshot", "v": snap.v, "at": snap.at,
                    "cards": snap.cards, "counts": snap.counts,
                    "other_procs": snap.other_procs, "freshness": snap.freshness}
        keys = set()
        for ver, ks in hist:
            if ver > n:
                keys.update(ks)
        return {"type": "delta", "v": snap.v, "base": n, "at": snap.at,
                "cards": {k: snap.cards.get(k) for k in keys},   # None = removed
                "counts": snap.counts, "freshness": snap.freshness}

    def stats(self):
        snap = self._snap
        return {"running": self.running, "version": self._version,
                "at": snap.at if snap else None, "sweeps": self._sweeps,
                "publishes": self._publishes, "sweep_ms": round(self._sweep_ms, 1),
                "drift": self._drift, "cold_at": self._cold_at,
                "nudges": self._nudges, "errors": self._errors,
                "last_error": self._last_error,
                "freshness": dict(self._fresh),
                "idle_s": self.idle_s, "hot_s": self.hot_s}

    # ----------------------------------------------------------- write API

    def nudge(self, reason=""):
        """Evidence, never a command: something changed, sweep sooner.

        Never blocks, never fails, never a source of truth — a dropped nudge
        costs latency and nothing else.
        """
        try:
            self._nudge_at = time.time()
            self._nudge_reason = reason
            self._nudges += 1
            self._wake.set()
        except Exception:                          # noqa: BLE001
            pass


# The process-wide sweep. Rebound by `start_observer`, so it is deliberately
# NOT re-exported on the facade (ADR 0010: a facade copy of a rebound global is
# a patch that lies). Reach it as `observer._observer`.
_observer = None


def start_observer(**kw):
    """Start the perpetual sweep. Called from `python3 -m orchestra`, never on
    import — a background thread doing real subprocess work during a test run is
    its own kind of hell, and tests import this package constantly."""
    global _observer
    if _observer is None:
        _observer = Observer(**kw)
    return _observer.start()


def stop_observer(timeout=5.0):
    if _observer is not None:
        _observer.stop(timeout)


def nudge(reason=""):
    """Module-level convenience: a no-op when no sweep thread is running, so
    every mutation path can call it unconditionally."""
    if _observer is not None:
        _observer.nudge(reason)


# --------------------------------------------------------------- demo state

def demo_state():
    """Fictional data with the exact shape of collect_state(), for screenshots."""
    now = time.time()

    seq = [0]

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None, sid=None):
        seq[0] += 1
        return {"id": "demo0000", "sid": sid or f"demo-{seq[0]}",
                "account": acct, "status": status,
                "last_write_at": now - age, "age_s": age,
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
    """The one entry point the server calls.

    With the sweep thread running this is O(1): the sweep refreshes `_cache`
    faster than STATE_TTL_S expires, so N tabs no longer trigger N concurrent
    collections. With no sweep thread — the rollback, and every test run — the
    branch below is the whole function, byte for byte what it was before.

    The branch is also the freshness guarantee after a mutation: the act layer
    parks `_cache["t"] = 0.0`, and that still forces one synchronous collect on
    the very next request rather than making the user wait out a sweep.
    """
    if config.DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        fresh = {}
        state = collect_state(fresh=fresh)
        _cache["state"], _cache["t"] = state, now
        if _observer is not None:
            # a collect is a collect: publish it, or the version and the
            # freshness map would silently miss everything a mutation caused.
            _observer.publish(state, fresh=fresh)
    return _cache["state"]
