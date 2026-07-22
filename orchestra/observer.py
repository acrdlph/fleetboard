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
query or a bounded tail of a transcript. Nothing is written, nothing is typed,
and no state another module owns is reaped — under a perpetual sweep any such
write becomes a scheduled background action nobody asked for.

`_cache` holds the last snapshot for `STATE_TTL_S` seconds when nothing is
sweeping, and for as long as a running sweep promises to refresh it
(`Observer.republish_s`) when something is, so a board polling every couple of
seconds doesn't re-shell `git` twice a second. It is mutated
in place and never rebound: the act layer parks a `_cache["t"] = 0.0` in it so
a button reverts on the very next poll instead of four seconds later, and the
tests poke `_cache["state"]` through the facade — same object either way.
Patch `observer.cached_state`, never the facade copy.

`demo_state` is fictional data with the exact shape of `collect_state`, for
screenshots. `cached_state` is the one entry point the server calls.

`Observer` is the publish point (ENGINE.md §2.5): one perpetual thread that
sweeps on its own cadence and publishes an immutable, versioned `Snapshot`.
`GitCadence` is what makes that thread affordable: git is 79 % of a sweep's
CPU and cannot be stat-memoised (`dirty` is the working tree), so it runs on
its own slower clock and the sweep reuses the last answer — honestly, because
`freshness["git"]` publishes how old it is, and a `nudge()` pulls it forward.
The transcript scan is the opposite case and gets the opposite treatment: it
CAN be stat-memoised, so it is (`transcripts.StatMemo`), and so are the two
per-pid lookups behind the process probe, keyed on the process's generation
(`procs.ProcMemo`). The sweep's `cold` flag is what bypasses every one of those
once a minute to check they were telling the truth.
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

from . import config, gitrepo, procs, transcripts, status, limits, watcher

STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}


# ---------------------------------------------------------------- collectors

def collect_state(fresh=None, git=None, cold=False):
    """Compose the board. `fresh` is an optional out-parameter.

    Pass a dict and the collector stamps it `kind -> wall clock of that kind's
    last SUCCESSFUL probe` (ENGINE.md §3.3): one `generated_at` cannot say that
    git is 47s stale because a fetch wedged. Left None it costs nothing and this
    function behaves exactly as it did before there was an Observer — which is
    what keeps `python3 tests/characterize.py` byte-identical across this change.

    `git` is the other seam, and the same shape of promise: left None the git
    fan-out runs in full on this thread, exactly as it always has. The sweep
    passes `GitCadence.resolve` instead — `roots -> ({root: info}, probed_at)`
    — so git runs on its own slower clock and the sweep reuses the last answer
    in between. `probed_at` is what lands in `fresh["git"]`, so the freshness
    map reports when git ACTUALLY ran rather than when this function did.

    `cold` is the reconcile sweep (§4.3 #1): it bypasses every memo — the
    transcript stat memo, and the per-generation cwd/environment memos in
    `procs` — so every transcript is re-read from the file and every pid is
    re-probed, and each disagreement with what a memo would have served is
    counted into `drift`. It costs a full uncached collect once a minute and it
    is what makes the memos honest.
    """
    def stamp(kind):
        if fresh is not None:
            fresh[kind] = time.time()

    now = time.time()
    worktrees = gitrepo.discover_worktrees()
    stamp("worktrees")
    # `all_procs`, not `procs`: a local of that name would shadow the module.
    all_procs = procs.claude_processes(cold=cold)
    stamp("procs")
    sessions = transcripts.scan_sessions(worktrees, all_procs, now, cold=cold)
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
                # `resets_at` only — a countdown here would be frozen at the
                # cclimits fetch (300 s cache) and would also make this card
                # differ on every publish. ENGINE.md §3.4; the board subtracts.
                lim = {"worst": al["worst"], "group": al["group"],
                       "resets_at": al["resets_at"]}
            elif al:
                # a model-scoped cap only strands a session running THAT model
                hit = next((sx for sx in al.get("scoped_exhausted", [])
                            if (sx["label"] or "").lower() in smodel), None)
                if hit:
                    lim = {"worst": hit["label"], "group": hit["group"],
                           "resets_at": hit["resets_at"]}
            if lim:
                s["status"] = "limit"
                s["limit"] = lim
            elif limit_re.search(s["last_assistant"] or ""):
                # the CLI wrote its limit notice into the transcript —
                # trust it even when the cclimits cache is cold/stale
                s["status"] = "limit"
                s["limit"] = {"worst": None, "group": None, "resets_at": None}

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
    roots = [w["git"] for w in worktrees]
    if git is None:
        git_by_root = gitrepo.git_info_many(roots)
        stamp("git")
    else:
        git_by_root, git_at = git(roots)
        if fresh is not None and git_at:
            # NOT `now`: the honest clock is when git last ran, which under a
            # cadence is up to GIT_S ago. Stamping `now` here would claim a
            # freshness this data does not have — §3.3's whole point.
            fresh["git"] = git_at

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
        # agent, the button reads ✕ close. A card with no live procs simply
        # does not render the flag, so it never offers to close an agent that
        # no longer exists — the same visible behaviour as before, reached by
        # reading instead of writing. Reaping the entry belongs to finish
        # (ENGINE.md §2.5): a perpetual sweep that popped it here would reap
        # closeout flags on a schedule nobody requested.
        ts = finish._closeouts.get(c["name"])
        if ts and c["live_procs"]:
            c["closeout_sent"] = ts

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

# Cadences, §2.5.
#
# What a sweep costs, in the only unit that matters. The first number written
# here was a WALL duty cycle — sweep_ms over the cadence — and it understates
# the truth by ~2x, because the sweep is a parallel fan-out: 600 ms of wall
# time spends 1600 ms of CPU, and battery is charged in CPU-seconds. `ps -o
# time` hides it too, since most of those ms are billed to the `git`/`ps`
# children and never appear against the server process (measured: 8 %
# parent-only against 56 % actual). Measured with getrusage(SELF)+(CHILDREN)
# over this fleet — 9 worktrees, 709 transcripts on disk, ~47 in window:
#
#   stage                      wall      CPU    share
#   git_info_many             253 ms   1264 ms   79 %      <- the whole problem
#   scan_sessions             206 ms    205 ms   13 %
#   claude_processes          114 ms    114 ms    7 %
#   collect_state (full)      600 ms   1586 ms
#
#   1.59 CPU-s every sweep  ->  IDLE_S 3.0 = 53 % of one core, CONTINUOUSLY
#                               IDLE_S 1.0 (the document's value) = 159 %
#
# So §2.5's prescribed 1.0 was not merely aggressive, it was unreachable: more
# than a full core, forever, with nobody watching. GIT_S is what makes it
# reachable. Git is 79 % of that CPU and the least volatile thing on the card,
# so it moves to its own slower clock and the sweep reuses the last answer in
# between. After: 0.31 CPU-s on a sweep that skips git, 1.65 on one that does
# not. Measured over 60 s of the real loop, not arithmetic:
#
#   IDLE_S  GIT_S   measured                     git_probes in 60 s
#     3.0     off     52 % of one core                 20        <- before
#     3.0     15      19 %                              4        <- shipped
#     2.0     15      27 %                              4
#     1.0     15      43 %                              4
#
# 2.7x off the shipped cadence, and the document's 1.0 goes from impossible to
# merely expensive. IDLE_S stays 3.0: what it buys is notification latency, and
# 3 s is already far below the threshold for "did the board notice". Tightening
# it is now an argument somebody can win, which it was not before — but it is
# their argument, with their own measurement, not a free rider on this one.
#
# THEN the transcript scan stopped re-parsing files that had not changed
# (transcripts.py, the stat memo). Same fleet, same instrument, and this time
# the A/B is the memo turned off in the same process rather than a different
# build, so the git fan-out — which still dominates and whose probe count
# wobbles by one between runs — is held equal:
#
#   IDLE_S  memo    measured over 120 s      sweeps  git_probes
#     3.0    off      22 % of one core         41        9
#     3.0    on       18 %                     41        9        <- shipped
#     1.0    off      46 %  (over 60 s)        61        5
#     1.0    on       32 %                     61        5
#     2.0    on       23 %                     31        5
#
# A sweep that skips git went 0.31 -> 0.19 CPU-s (six consecutive: 185/189/181/
# 197/219/205 ms), because scan_sessions went 196 -> 76 CPU-ms at a 98 %/96 %
# hit rate. The board-wide number moves less than that ratio suggests, and the
# table says why: 9 git fan-outs are ~15 of those 21.75 CPU-s. Git is still the
# bill. What is left of a git-free sweep is now ps+lsof (0.11) ahead of the
# scan (0.076), so the process probe is the next step, not this one.
#
# THEN the process probe stopped asking the kernel things that cannot change
# (procs.py, the per-generation memo). Same instrument, same in-process A/B:
#
#   IDLE_S  memo    measured over 120 s      sweeps  git_probes
#     3.0    off      16 % of one core         40        8
#     3.0    on       15 %                     40        8        <- shipped
#     1.0    off      30 %                    120        8
#     1.0    on       26 %                    120        8
#
# A git-free sweep went 0.19 -> 0.153 CPU-s (six consecutive: 153/156/152/151/
# 155/152 ms against 189/196/224/207/203/205), because `claude_processes` went
# 112 -> 73 CPU-ms: lsof and `ps eww` stop running at all on a fleet whose pids
# have not changed. What remains is one `ps` over the whole table, 68 of those
# 73 ms, and procs.py explains at length why it cannot be narrowed without
# breaking discovery.
#
# That is the smallest win of the three and the table says so plainly: 1 point
# at IDLE_S 3.0, 4 at 1.0. Git is still ~15 of the 18 CPU-s. The remaining
# sweep is now ONE `ps` (0.068) and the transcript scan (0.076) and nothing
# else worth naming — there is no fourth cheap win here, and the next honest
# argument is about GIT_S or about IDLE_S, with its own measurement.
#
# All five cadences below are DEFAULTS for config keys of the same name, not
# constants (`config.CFG["idle_s"]` &c). This one is the reason: the tables
# above are a trade whose right answer depends on whose laptop it is and
# whether it is plugged in, and it was a number you had to edit code to change.
#
# The knob table shipped for README.md, on the finished build — same fleet,
# same instrument, 120 s per row, one knob moved and the rest at their
# defaults. It is not the A/B tables above and does not supersede them: those
# isolate what each optimisation bought, this prices the settings a user can
# actually turn.
#
#   knob            value    CPU    sweeps  git_probes
#   idle_s           5.0     16 %      24        8
#   idle_s           3.0     17 %      40        8      <- default
#   idle_s           2.0     19 %      60        8
#   idle_s           1.0     28 %     119        8
#   git_s            5.0     29 %      40       20
#   git_s           60.0      9 %      40        2
#   reconcile_s      off     16 %      40        8
#
# Two things in there are worth more than the numbers themselves.
#
# FIRST: `idle_s` is not the expensive knob, `git_s` is. Tripling the sweep
# rate (3.0 -> 1.0) costs 11 points; tripling the GIT rate (15 -> 5) costs 12
# on its own, at an unchanged sweep rate. That is the memos' doing — a sweep
# that skips git is now ~0.15 CPU-s — and it is why 5.0 and 3.0 are one point
# apart. Anyone arguing about `idle_s` is arguing about the cheaper half.
#
# SECOND: the numbers this replaces were WRONG, and wrong in the direction that
# flatters an argument. The brief for this change quoted "1.0 costs 43 % and
# 3.0 costs 15 %" — 43 % is the row measured BEFORE the transcript memo and the
# process memo landed (it is in the first table above, where it was correct).
# Carried forward unmeasured, it overstates today's cost of `idle_s` 1.0 by
# 15 points and would have told a user that the aggressive setting is
# unaffordable when it is merely dearer. Re-measured because ADR 0011 says a
# performance claim without a measurement is not a claim — including, and
# especially, a claim inherited from this file's own earlier tables.
#
# THEN THE SWEEP STOPPED BEING THE MECHANISM. `watcher.py` holds a bounded set
# of kqueue fds over the project dirs and the in-window transcripts and calls
# `nudge()` when one of them moves, so the timer is a SAFETY NET rather than
# how anything is noticed (ADR 0012). That changes what `idle_s` buys. It no
# longer sets notification latency — measured, ten writes to a real transcript
# on the real fleet, median write -> nudge 53 ms and write -> published version
# 212 ms (max 468). It sets the worst case for the three things kqueue CANNOT
# see: a process being BORN (there is a filter for process death, none for
# birth), an append to a transcript already outside the 48 h window, and a
# subagent file more than one directory deep.
#
# NOTHING HAPPENING ANYWHERE. Same instrument, same fleet, 120 s per row, and
# A/B INTERLEAVED three times — this machine's load average wanders between 8
# and 50 and a straight sequence of rows measures the load, not the loop:
#
#   watch  idle_s   CPU of one core   sweeps  git_probes
#   off      3.0     13.3 / 13.9 / 15.2 %      40     8    <- the old default
#   on      30.0      5.8 /  5.3 /  5.8 %       4     4    <- shipped
#   on      60.0      3.1 %                     2     2
#
# 14 % -> 6 %, and the three timer-only rows are worth reading as a group: 13.3
# was taken at load 47, where the loop could only manage 13 sweeps instead of
# 40. That row FLATTERS the thing being replaced — under load the old cadence
# cannot even reach itself. 15.2 % at load 17, with the full 40 sweeps, is the
# honest one, and it matches the 17 % this file measured before the watcher.
#
# WITH ONE AGENT ACTUALLY WORKING, which is the regime that picks the number,
# because idle it barely matters:
#
#   idle_s   CPU    sweeps   of which event-driven
#     30.0    7.3 %     8            6              <- shipped
#     15.0    9.6 %    15           11
#      5.0   15.0 %    35           17
#
# Read the last column downward. Going 30 -> 5 buys 27 extra sweeps and 8
# points, and every one of those sweeps found something the events had already
# reported. That is the whole argument for 30.0 over anything tighter: below it
# the timer is re-discovering what it was just told. Above it, 60.0 saves 3
# points for twice the blind spot on the un-watchable three, and a `claude`
# started in a quiet worktree being invisible for a minute is the wrong side of
# "did the board notice me".
#
# `hot_s` is NOT the floor the watcher uses. It exists so a burst of MUTATIONS
# cannot spin the loop and is sized for a handful of them; a transcript being
# appended to continuously would nudge every 0.15 s forever, which at ~0.15
# CPU-s per git-free sweep is ~100 % of one core — worse than the timer it
# replaced. Event-driven nudges get their own rate limit, `watch_min_interval_s`
# (1.0 s), and the reasoning lives in watcher.py beside it.
IDLE_S = 30.0       # cadence with no evidence of change; config key "idle_s"

# What `idle_s` was before events, and what it goes back to the moment there
# are none: Linux (no stdlib inotify binding), a kqueue that failed to start, a
# watch thread that died. Read from the watcher's `running` on every wait, not
# latched at startup, so a watcher that dies at hour nine costs three seconds of
# latency rather than thirty. Degradation is automatic and logged once.
#
# A CEILING, not a substitute — `min(idle_s, idle_blind_s)`. Replacing `idle_s`
# outright would mean `Observer(idle_s=0.01)` silently ran at 3.0, i.e. an
# explicit argument losing to a default, which is the one rule every other
# cadence in this file keeps (`_cadence`, and the test named for it). Blind, the
# loop may never wait LONGER than this; it may certainly wait less.
IDLE_BLIND_S = 3.0  # ceiling on the wait with no watcher; key "idle_blind_s"
HOT_S = 0.15        # floor between sweeps after a MUTATION nudge; key "hot_s"

# git's own clock, §2.5's `git_s`. The document says 5.0 and calls it a
# "re-probe cadence WITHIN a sweep", implying a memo; there cannot be one.
# branch/commit/ahead-behind could be keyed on `.git/HEAD` and the refs, but
# `dirty` depends on the WORKING TREE — any file the user or an agent saves —
# and no cheap stat detects that. (`.git/index` mtime does not: it moves when
# git writes the index, and `--no-optional-locks` exists precisely to stop git
# rewriting it when we look.) So this is a CADENCE, not a skip-if-unchanged
# memo: git is re-probed on a slower clock and the last answer is reused in
# between, which is honest only because `freshness["git"]` publishes its age.
#
# 15.0, not 5.0. At 5.0, git runs on ~2 sweeps in 3 and the loop still costs
# ~40 % of a core; at 15.0 it is 19 %. What the extra 10 s delays is a branch
# name, an ahead/behind count and a dirty count — none of which is why anybody
# watches this board, and all of which are dated on the card by
# `freshness["git"]`. Every event that MAKES git move (finish parking a
# worktree on the trunk, a dispatch cutting a branch) already calls `nudge()`,
# and a nudge pulls git forward, so the 15 s is only ever paid for edits nobody
# told us about. Worst case on the board is git_s + idle_s + the fan-out
# itself, ~18 s at the defaults.
GIT_S = 15.0        # minimum interval between git fan-outs; config key "git_s"

# A cold sweep bypasses every memo and cadence (§4.3 #1) and counts what they
# would have got wrong as `drift`. Several terms now, and they mean opposite
# things, which is why `stats()` breaks them all out rather than shipping only
# the sum:
#
# * `git_drift` is not a lie. The cadence never claimed to be current and
#   `freshness["git"]` says how old it is; a disagreement is the measured COST
#   of running git on a slower clock, counted because a number nobody can see
#   is a number nobody can argue with. Non-zero is normal.
# * `scan_drift` / `tree_drift` ARE lies. The transcript stat memo claims the
#   bytes have not changed. Non-zero means the key is wrong — the one failure
#   mode that is otherwise completely silent, since a stale parse looks exactly
#   like a quiet agent. Non-zero is a bug.
# * `cwd_drift` / `env_drift` are lies too, and the worst kind: the
#   per-generation memo claims a pid is the same process it was, and a wrong
#   answer there is not a display bug — it is the act layer typing into the
#   wrong agent (ADR 0008). Non-zero is a bug.
#
# The cold scan costs 260 CPU-ms against a warm 76 on this fleet, and the cold
# process probe 116 against a warm 73, once a minute: well under 1 % duty for
# the only thing that can catch a memo lying.
RECONCILE_S = 60.0  # config key "reconcile_s"

# The ceiling on one wait, and READ THE SIGN OF THE INEQUALITY: the loop waits
# `min(idle_s, max_stale_s)` and sweeps when that expires, so a `max_stale_s`
# BELOW `idle_s` silently becomes the cadence and `idle_s` stops meaning
# anything. It was 8.0 against an `idle_s` of 3.0, where it never bound. Raising
# `idle_s` to 30.0 for the watcher would have left it as the real cadence — the
# whole idle-CPU win, quietly cancelled by a constant three screens away, with
# every sweep counter still looking plausible. 45.0 is 1.5x `idle_s`; `stats()`
# publishes both so the pair can be checked against each other on a running loop.
MAX_STALE_S = 45.0  # never wait longer than this between sweeps; key "max_stale_s"
HIST = 512          # version/changed-keys ring, §3.5


def _cadence(key, default, given):
    """One cadence, resolved: explicit argument > config key > the constant.

    `given is None` and not `or given`, because 0.0 is a meaningful (if
    unwise) value for every one of these and truthiness would silently swap a
    caller's 0 for 3.0. A key present in the file but unparseable as a float
    is a config error the user wants to hear about at startup, so the
    ValueError is left to propagate rather than quietly falling back.
    """
    return float(config.CFG.get(key, default) if given is None else given)


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
# the same object — `last_write_at` beside `age_s` in `scan_sessions` — and
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


class GitCadence:
    """git on its own clock: `roots -> ({root: info}, oldest probe clock)`.

    Four rules, and every one of them is the difference between a slower sweep
    and a board that lies:

    * DUE. A full fan-out runs when `every_s` has passed since the last one.
      Between them every root is served from `_info`.
    * NEW. A root with no cached answer is probed the moment it appears, on the
      spot, off-clock — a card that shows up with no branch, no commit and
      `dirty 0` is worse than one whose dirty count is 12 s old. A root that
      disappears is dropped, so a dead worktree cannot drag the freshness clock
      backwards forever.
    * FORCED. `force()` makes the next resolve a full fan-out. `Observer.nudge`
      calls it, so anything that moves git — finish parking a worktree on the
      trunk, a dispatch cutting a branch — is on the board next sweep instead of
      up to `every_s` later.
    * COLD. `cold=True` bypasses the clock entirely (§4.3 #1) and compares the
      fresh answer against what the cache would have served. Disagreements land
      in `drift`.

    The returned clock is the OLDEST probe backing any root on the board, not
    `now`, so `freshness["git"]` reads as "no card's git data is older than
    this". It only advances when git actually ran.
    """

    def __init__(self, every_s=None):
        self.every_s = _cadence("git_s", GIT_S, every_s)
        self._info = {}          # root -> the last git_info for it
        self._at = {}            # root -> wall clock of the probe that produced it
        self._full_at = 0.0      # last full fan-out
        self._forced = True      # the first resolve always probes
        self.probes = 0          # fan-outs run
        self.reuses = 0          # sweeps that shelled out to git not at all
        self.drift = 0           # cold-sweep disagreements with the cache

    def force(self, reason=""):
        """Evidence that git moved. The next resolve re-probes, whatever the clock."""
        self._forced = True

    def resolve(self, roots, cold=False):
        now = time.time()
        uniq = list(dict.fromkeys(roots))
        due = cold or self._forced or (now - self._full_at) >= self.every_s
        want = uniq if due else [r for r in uniq if r not in self._info]
        if want:
            got = gitrepo.git_info_many(want)
            self.probes += 1
            if cold:
                # what the cache WOULD have served, against the truth
                self.drift += sum(1 for r, info in got.items()
                                  if r in self._info and self._info[r] != info)
            for r, info in got.items():
                self._info[r], self._at[r] = info, now
        else:
            self.reuses += 1
        if due:
            self._full_at, self._forced = now, False
        for gone in [r for r in self._info if r not in set(uniq)]:
            self._info.pop(gone, None)
            self._at.pop(gone, None)
        return ({r: self._info[r] for r in uniq if r in self._info},
                min((self._at[r] for r in uniq if r in self._at), default=None))

    def stats(self):
        return {"git_s": self.every_s, "git_probes": self.probes,
                "git_reuses": self.reuses, "git_drift": self.drift}


class Observer:
    """Owns the ONLY perpetual read loop. Never mutates the world.

    Threading model (§2.5): exactly one thread, `observer-sweep`, reusing the
    ThreadPoolExecutor fan-out already inside `collect_state`. One Condition
    guards `_snap`/`_version`. Readers take no lock — `snapshot()` is a single
    attribute read of a frozen object.
    """

    def __init__(self, *, idle_s=None, hot_s=None, git_s=None,
                 reconcile_s=None, max_stale_s=None, idle_blind_s=None,
                 watch=None, watcher_factory=None):
        # §2.5 also lists limits_s. It is not implemented and not accepted: it
        # would start polling cclimits from the sweep, which is step 7. A
        # parameter that silently does nothing is worse than an absent one.
        #
        # Every cadence left None reads its config key, so all five are
        # settings on disk rather than constants you have to edit code to
        # change; an explicit argument still wins, which is how the tests drive
        # the loop at cadences no user would choose. The read happens HERE and
        # not at each use, so a live config edit takes effect at the next
        # `Observer(...)` and never mid-loop — a cadence that changed under a
        # running `_loop` would make `sweeps`-per-second unattributable to any
        # setting, and this module's whole argument is made of measurements.
        self.idle_s = _cadence("idle_s", IDLE_S, idle_s)
        self.idle_blind_s = _cadence("idle_blind_s", IDLE_BLIND_S, idle_blind_s)
        self.hot_s = _cadence("hot_s", HOT_S, hot_s)
        self.reconcile_s = _cadence("reconcile_s", RECONCILE_S, reconcile_s)
        self.max_stale_s = _cadence("max_stale_s", MAX_STALE_S, max_stale_s)
        self._git = GitCadence(git_s)
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
        # The watcher is built here and started in `start()`, so an Observer
        # that is only ever `publish()`ed into — which is most of the suite —
        # opens no file descriptors at all.
        self.watch = bool(config.CFG.get("watch", True) if watch is None else watch)
        self._watcher = None
        if self.watch:
            make = watcher_factory or watcher.Watcher
            self._watcher = make(lambda reason: self.nudge(reason, git=False),
                                 pids=self._live_pids)

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    @property
    def watching(self):
        """Whether events are currently driving the loop. Read on every wait,
        never latched: a watcher that dies mid-uptime must cost three seconds of
        latency, not thirty."""
        return bool(self._watcher is not None and self._watcher.running)

    @property
    def effective_idle_s(self):
        """What the loop actually waits, which is none of the three settings on
        its own: `idle_s` while events drive it, capped by `idle_blind_s` when
        they do not, and capped again by `max_stale_s` in the wait itself."""
        return self.idle_s if self.watching else min(self.idle_s, self.idle_blind_s)

    @property
    def republish_s(self):
        """How long the request path may trust `_cache` while this thread runs.

        The cache is refreshed at the END of a sweep, so the interval between
        refreshes is the cadence PLUS a sweep — not the cadence. Both terms are
        measured rather than assumed: `effective_idle_s` already accounts for a
        dead watcher, and `_sweep_ms` is the last sweep's own duration, which on
        a loaded box is seconds and not milliseconds.
        """
        return (min(self.effective_idle_s, self.max_stale_s)
                + self._sweep_ms / 1000.0 + STATE_TTL_S)

    def _live_pids(self):
        """The claude pids of the last snapshot, for `EVFILT_PROC`/`NOTE_EXIT`.

        Costs no fd (the ident is the pid), so watching every one of them is
        free. There is no kqueue filter for process BIRTH, which is why this
        reads the snapshot rather than being a source of it — a pid that has
        appeared since the last sweep is found by the next sweep, not here.
        """
        snap = self._snap
        if snap is None:
            return ()
        pids = {p["pid"] for c in snap.cards.values() for p in c.get("live_procs", [])}
        pids |= {p["pid"] for p in snap.other_procs}
        return tuple(sorted(pids))

    def start(self):
        with self._cv:
            if self.running:
                return self
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="observer-sweep", daemon=True)
            self._thread.start()
        if self._watcher is not None:
            # after the sweep thread: a watcher whose first nudge lands before
            # there is a loop to wake is a wasted nudge, not a crash, but there
            # is no reason to arrange one
            self._watcher.start()
        return self

    def stop(self, timeout=5.0):
        if self._watcher is not None:
            self._watcher.stop(timeout)
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
            nxt = started + self.effective_idle_s
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
        state = collect_state(
            fresh=fresh, cold=cold,
            git=lambda roots: self._git.resolve(roots, cold=cold))
        ms = (time.perf_counter() - t0) * 1000.0
        self._sweeps += 1
        # Every memo's and cadence's disagreement with the cold recompute,
        # summed (§4.3 #4). Three terms: the git cadence, which never claimed
        # to be current and whose drift is a measured cost; the transcript stat
        # memo; and the per-generation cwd/environment memos in `procs`. The
        # last two DID claim to be current — their drift is a lie and any
        # non-zero value is a bug. `stats()` breaks them out separately.
        self._drift = (self._git.drift + transcripts.memo_drift()
                       + procs.proc_memo_drift())
        if cold:
            self._cold_at = started
        # Compare-and-swap, never a blind write. A mutation that parked
        # `_cache["t"] = 0.0` while this sweep was in flight means the state we
        # just collected predates it; dropping our write leaves the next request
        # to collect synchronously — exactly what happens today.
        if _cache["t"] == t_before and _cache["state"] is s_before:
            _cache["state"], _cache["t"] = state, started
        snap = self.publish(state, fresh=fresh, sweep_ms=ms)
        if self._watcher is not None:
            # AFTER the publish, never before: `_live_pids` reads the snapshot,
            # so a rearm here arms the exit watches for the fleet this sweep
            # just found rather than the previous one's. It is also the only
            # thing that keeps the watch set current at `idle_s` 30 — the
            # watcher's own rebuild clock is the fallback, not the mechanism.
            self._watcher.rearm("sweep")
        return snap

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
                                     drift=self._drift, sweep_ms=self._sweep_ms)
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
                # all five cadences, so "did my config take?" is answerable
                # from the running loop and not only from the file on disk
                "idle_s": self.idle_s, "hot_s": self.hot_s,
                "reconcile_s": self.reconcile_s, "max_stale_s": self.max_stale_s,
                "idle_blind_s": self.idle_blind_s,
                # what the loop is ACTUALLY waiting, which is neither of the two
                # above on its own: the blind cadence when there is no watcher,
                # and `max_stale_s` if somebody set that below `idle_s`
                "idle_effective_s": min(self.effective_idle_s, self.max_stale_s),
                **self._git.stats(), **transcripts.memo_stats(),
                **procs.proc_memo_stats(),
                **(self._watcher.stats() if self._watcher is not None
                   else {"watching": False})}

    # ----------------------------------------------------------- write API

    def nudge(self, reason="", git=True):
        """Evidence, never a command: something changed, sweep sooner.

        Never blocks, never fails, never a source of truth — a dropped nudge
        costs latency and nothing else.

        Sooner AND fuller, for a MUTATION: the nudge also pulls git off its
        cadence. Every such caller is a completed mutation (finish/exit parks a
        worktree back on the trunk, finish/brief and finish/nudge type at an
        agent that is about to commit, a dispatch cuts a branch), and a board
        that re-sweeps in 150 ms only to serve 15 s-old branch data has answered
        the wrong half of the question. git is forced unconditionally across
        mutations rather than per-reason: guessing which of them move the working
        tree is a second source of truth that goes stale the first time somebody
        adds a route.

        `git=False` is the WATCHER's nudge, and the distinction is not cosmetic.
        A transcript write is not a mutation, it is an agent working, and it
        arrives as often as an agent types. Forcing a fan-out on each one would
        run git at the event rate — the exact cost `GIT_S` exists to bound, and
        by the measured table beside it the most expensive thing this loop can
        do. Watched evidence therefore buys a sooner sweep and nothing more;
        git keeps its own 15 s clock, which is what it was designed for and
        which `freshness["git"]` still dates on the card.
        """
        try:
            self._nudge_at = time.time()
            self._nudge_reason = reason
            self._nudges += 1
            if git:
                self._git.force(reason)
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


def nudge(reason="", git=True):
    """Module-level convenience: a no-op when no sweep thread is running, so
    every mutation path can call it unconditionally. Every caller of THIS is a
    mutation, hence `git=True` — the watcher holds its Observer directly and
    passes `git=False`."""
    if _observer is not None:
        _observer.nudge(reason, git=git)


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
                             "resets_at": now + 7560}),
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

    With the sweep thread running this is O(1): the request path trusts what
    the sweep published for as long as the sweep promises to publish again
    (`republish_s`), so N tabs no longer trigger N concurrent collections. With
    no sweep thread — the rollback, and every test run — the branch below is
    STATE_TTL_S and the whole function, byte for byte what it was before.

    The branch is also the freshness guarantee after a mutation: the act layer
    parks `_cache["t"] = 0.0`, and that still forces one synchronous collect on
    the very next request rather than making the user wait out a sweep. It
    passes no `git=`, so that collect probes git in full — the cadence is a
    background economy, and the one moment a user has just acted is not where
    to spend it. The nudge that accompanies every such mutation re-arms the
    sweep's own cadence, so the two agree rather than race.
    """
    if config.DEMO:
        return demo_state()
    now = time.time()
    # STATE_TTL_S is the NO-THREAD bound and nothing else. It was also the
    # right bound for the thread while `idle_s` was 3.0 — the sweep refreshed
    # `_cache` faster than 4.0 s expired, which is what the docstring above
    # claims. `idle_s` 3.0 -> 30.0 (ADR 0012) silently ended that: the cache
    # then expired 4 s into every 30 s cycle and every request in the remaining
    # 26 s ran a full synchronous collect on the request thread. Measured with
    # the shipped config: 13 of 30 one-second polls collected, and /api/state
    # on the nine-worktree fleet took 8-17 s per request instead of 0.7 ms.
    # Same shape as the `max_stale_s` 8.0 -> 45.0 correction in that ADR — a
    # ceiling left below a raised cadence quietly becomes the cadence — but on
    # the request path rather than in the loop.
    #
    # So while the thread is running, the bound is the thread's own promise
    # (`republish_s`). Fall past it and the sweep is late or wedged, and a
    # synchronous collect is exactly the right answer; the parked
    # `_cache["t"] = 0.0` still lands miles outside it, so a mutation still
    # forces a collect on the very next request.
    obs = _observer
    ttl = obs.republish_s if (obs is not None and obs.running) else STATE_TTL_S
    if _cache["state"] is None or now - _cache["t"] > ttl:
        fresh = {}
        state = collect_state(fresh=fresh)
        _cache["state"], _cache["t"] = state, now
        if _observer is not None:
            # a collect is a collect: publish it, or the version and the
            # freshness map would silently miss everything a mutation caused.
            _observer.publish(state, fresh=fresh)
    return _cache["state"]
