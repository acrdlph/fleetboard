"""orchestra.config — where the board's settings come from, and what they are.

Precedence: CLI flags > orchestra.config.json (next to the package, else cwd)
> the defaults in CFG below. `load_config` is called once, from
`orchestra.__main__`, before anything else runs.

CFG is a mutable dict and is mutated in place, never rebound — every reader
holds the same object. DEMO and CONFIG_PATH are the opposite: plain scalars
that get REBOUND at runtime, so every reader must reach them as
`config.DEMO` / `config.CONFIG_PATH`. A `from .config import DEMO` anywhere
would freeze a copy and silently disable demo mode.

`account_label` lives here rather than with the transcript code (ADR 0010's
prose puts it there) because it is a four-line pure string function on a
home-dir name with no dependencies, called from six modules — a shared leaf.
Keeping it here is what makes config → procs → transcripts acyclic.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent.parent  # package lives one level under the repo root

CFG = {
    "host": "127.0.0.1",       # keep loopback: the board serves transcript text
    "port": 4242,
    "roots": [str(Path.cwd())],  # dirs whose git-repo children are watched
    "pattern": "",             # optional regex filter on worktree dir names
    "homes": [],               # Claude home dirs; [] = auto-discover ~/.claude*
    "session_window_h": 48,    # ignore transcripts idle longer than this
    "working_s": 90,           # transcript written within this => working
    # `quiet_s` is what is left of the 90 s lie. `working_s` used to decide the
    # end of a turn on its own; the CLI's own end-of-turn marker now decides it
    # for 84 % of in-window sessions by OBSERVATION, and this timer covers only
    # the residual — a live agent with no pending tool, no delegated workflow
    # or background agent, no live shell and no marker. `working_s` keeps its
    # other two jobs (the approval grace, the orphan grace).
    #
    # A "misfire" is a genuine thinking pause long enough to be mistaken for
    # the end of a turn: the board says ◆ YOUR TURN, summons you, and the agent
    # carries on. Measured over 14,006 such gaps in the 79 in-window
    # transcripts (p50 2.2 s, p95 27.7 s, p99 72.7 s):
    #
    #     quiet_s   misfire   1 in     of which recover < 120 s
    #        10     16.23 %      6            15.94 %
    #        20      7.78 %     13             7.50 %
    #        25      5.80 %     17             5.53 %     <- ENGINE.md's proposal
    #        45      2.71 %     37             2.53 %     <- shipped
    #        60      1.54 %     65             1.37 %
    #        90      0.61 %    165             0.51 %     <- today
    #
    # Almost every misfire recovers within two minutes, so the misfire rate IS
    # the flicker rate — this is the WORKING → YOUR TURN → WORKING oscillation,
    # not a harmless late correction. 45 sits between the p95 and p99 of the
    # pauses it must tolerate, halves today's staleness, and costs 2.1 % more
    # misfires than 90 on the one session in six that reaches it — a fleet-wide
    # 0.34 pp. 25 would double the flicker (5.80 %) to buy 20 s on a fallback
    # path; the return per second of added lateness falls off a cliff after 45
    # (25→45 buys 0.155 pp/s, 45→60 buys 0.078, 60→90 buys 0.031).
    "quiet_s": 45,             # unexplained silence before a live agent is idle
    # How long a background launch that has not reported back still counts as
    # delegated work (transcripts.parse_session_tail / scan_sessions). It is a
    # SHELF LIFE, not a timeout on the task: the task may well run longer, this
    # is how long its launch alone is allowed to stand as the explanation for a
    # quiet session.
    #
    # It has to exist because 3.8 % of background launches never report at all
    # — 76 of 2,000 in ~/.claude* (Workflow 37, Bash 32, Agent 7), killed or
    # lost to a restart — and an unbounded outstanding set would pin those
    # sessions at ● WORKING for the whole 48 h window.
    #
    # Chosen off both sides of the trade, measured by replaying every
    # end-of-turn claim in the corpus (908 of them, 720 transcripts, the same
    # 128 KB tail the board reads). "Uncaught" is the board saying the turn
    # ended and the agent then speaking again with no human prompt — the
    # flicker; "held" is the opposite error, a turn that really had ended kept
    # at WORKING by a launch that was never coming back:
    #
    #     bound      uncaught misfire   held wrongly
    #      none        4.41 %            2  (0.22 %)
    #     3600 s       4.41 %            2  (0.22 %)
    #     1200 s       4.41 %            2  (0.22 %)
    #      900 s       4.41 %            1  (0.11 %)
    #      600 s       4.41 %            0  (0.00 %)   <- shipped
    #      450 s       4.41 %            0  (0.00 %)
    #      300 s       4.63 %            0  (0.00 %)
    #      120 s       4.63 %            0  (0.00 %)
    #
    # The benefit saturates at 450 s and the cost starts at 750 s, so 600 is
    # the top of the flat, free stretch — every longer bound buys nothing and
    # starts paying. That it is well below the p90 of launch→notification
    # latency (1,928 s; p50 278 s, p95 3,010 s) is not a contradiction: a task
    # still running after ten minutes is one whose session has other evidence
    # of work — an unresolved tool_use, a live shell, or a non-zero
    # pendingWorkflowCount — and every branch of those sits ABOVE this one.
    # Measured: of the 908 claims, no misfire had an outstanding launch older
    # than 450 s (p50 16 s, p90 692 s, p95 2,069 s).
    "delegated_s": 600,        # …and how long an unanswered launch explains it
    # `block_grace_s` — how long an unresolved tool_use is "a tool still
    # running" (● WORKING) before it becomes ■ BLOCKED, i.e. "the thing it is
    # waiting for is YOU". It inherited working_s because Layer 0 kept it
    # conservative; this is the first time it has been measured.
    #
    # Very little reaches it. `AskUserQuestion` is answered one branch above
    # (needs_input) and delegated work one above that — and, the big one, a
    # RUNNING Bash is answered by the `shells` branch, because the Bash tool
    # wraps every command in a live zsh child. A Bash awaiting APPROVAL has no
    # wrapper shell yet, and that asymmetry is what makes this branch worth
    # having at all. Under --dangerously-skip-permissions it is dead code:
    # 416 of 737 transcripts (56.4 %) never ran in a mode that can ask, and on
    # the board's own working set it is 78 of 96 (81.2 %).
    #
    # Measured by replaying every transcript and taking the SILENCE BETWEEN
    # CONSECUTIVE WRITES while a tool_use was unresolved — which is what `age`
    # actually is; a parallel tool call or a sidechain write resets it, so the
    # raw tool_use→tool_result span would have been the wrong distribution.
    # Split into the two populations this number trades:
    #
    #   flicker — the silence ended because the TOOL finished, so ■ BLOCKED
    #             is a false summons: 18,741 silences, p95 6.6 s, p99 63.0 s
    #             (in the sessions that CAN be asked: p95 10.6 s, p99 117.3 s)
    #   catch   — the silence ended because a HUMAN did: the tool_use resolves
    #             with "The user doesn't want to proceed…" / "User has
    #             approved your plan", or the tool is ExitPlanMode. 293 of
    #             them, and they are the entire reason ■ BLOCKED exists.
    #
    #      V     flicker   caught   lateness removed vs 90 s
    #     10 s   3.031 %   23.9 %   3,333 s — over 414 extra false alarms
    #     20 s   1.942 %   18.4 %   2,726 s — over 210
    #     30 s   1.574 %   15.7 %   2,225 s — over 141
    #     45 s   1.254 %   13.3 %   1,596 s — over  81
    #     60 s   1.030 %   11.9 %   1,025 s — over  39   <- shipped
    #     90 s   0.822 %   10.9 %       0 s — over   0   <- inherited
    #    120 s   0.619 %    9.6 %
    #
    # 60 is where quiet_s's rule puts it: between the p95 and the p99 of the
    # genuine tool-run silence it must tolerate, in the population where the
    # branch is live. It takes 29 s off every real approval prompt for 39
    # extra false alarms in 18,741 opportunities (+0.21 pp), and on the
    # board's own working set it is FREE — 5 false silences at 60 and 5 at 90
    # (0.216 %), 90 s of lateness removed. Below 60 the trade only gets worse:
    # 26 s of promptness bought per extra false summons at 60, 20 s at 45,
    # 13 s at 20. A false ■ BLOCKED is flicker, never danger — BLOCKED makes a
    # card "attention", never "free", so it cannot reach dispatch.
    "block_grace_s": 60,       # unresolved tool_use: running, then it is you
    # `orphan_grace_s` — how long a FRESH transcript write with NO observed
    # process still reads ● WORKING before it becomes ○ ENDED. It STAYS at 90,
    # and what follows is the measurement that says so rather than the
    # inheritance that used to.
    #
    # What it claims to cover is the lag between a process EXISTING and
    # orchestra SEEING it. Driven at 40 ms resolution, 9 launches: the process
    # is in `procs.claude_processes()` with a resolved cwd 0.14–0.32 s after
    # exec, and the transcript's first byte does not land until 2.25–3.33 s.
    # A just-exec'd agent is therefore visible 2.1–3.0 s BEFORE it can appear
    # on the board at all, and needs no grace whatsoever. The probe does not
    # miss either: 0 unresolved cwds in 3,000 lsof calls, 0 short reads in 200
    # whole-table `claude_processes()` calls, and over 816 live board ticks
    # (27.2 min) 7,966 process observations without a single missing cwd and
    # 0 pairing flaps.
    #
    # So the measurable half of the question wants ~0, and the number's real
    # job is the half with no observed events: a probe that comes back empty.
    # Counterfactual over those same 816 ticks — 4,663 observations of a
    # session that WAS paired with a live pid, asking what ONE blind probe
    # would have published (a live session's transcript is 51 s old at a
    # random moment, p50; p75 405 s):
    #
    #      V      live sessions published ○ ENDED   worktrees gone FREE
    #     10 s               73.4 %             1,317 of 2,448  (5.5x)
    #     30 s               57.7 %               750 of 2,448  (3.1x)
    #     45 s               52.0 %               540 of 2,448  (2.3x)
    #     90 s               42.3 %               239 of 2,448  (today)
    #
    # ENDED feeds card availability feeds worktree-FREE feeds dispatch, so
    # that last column is "how many worktrees one dropped `ps` offers to a
    # second agent". There is no distribution to place a percentile in — the
    # event has an observed rate of ZERO — so there is no MEASURED number
    # below 90, and a guess in this direction is the one that puts two agents
    # on one worktree. It stays until evidence replaces the timer:
    # `classify_session` already takes `procs_known` for exactly this failure
    # and nothing passes it. The price of leaving it is known too — 89.2 s of
    # ● WORKING after an agent is gone (driven, 6 runs: the transcript is
    # 0.82–0.87 s old at the moment the process disappears from `ps`).
    "orphan_grace_s": 90,      # a fresh write with no process: seen yet, or over
    # `subagent_grace_s` — how long after the last write anywhere under
    # `<session-id>/` the card still shows ⚙ subagents running. The last of the
    # inherited numbers, and the only one that was measurably TOO SHORT.
    #
    # It is a display hint and nothing else: `subagents_active` is read by one
    # line of index.html and feeds no status, no availability and no dispatch,
    # so neither error can put two agents on one worktree. What it can do is
    # flicker, and flicker is the thing this project spends numbers on.
    #
    # THE POPULATION IS NOT THE CONVERSATION'S. One `agent-*.jsonl` under the
    # tree is one subagent's whole life, so its first and last timestamps
    # bracket a span in which that subagent is definitely running. Measured
    # over ~/.claude* — 8 homes, 148 trees, 18,145 runs (32 multi-hour files
    # dropped: a resumed file, not one turn), 605,784 writes:
    #
    #                        p50     p95      p99
    #     tree writes        0.2 s   5.9 s   27.0 s
    #     main transcript    1.0 s  43.1 s  407.9 s
    #
    # An order of magnitude denser at the tail. A workflow agent writing
    # steadily looks nothing like a conversation, which is the whole reason
    # this could not go on borrowing `working_s`.
    #
    # But the gap distribution is the WRONG one to place a percentile in here,
    # and that is the interesting part. `quiet_s` gets one draw per chance to
    # misfire, because one misfire is one false summons. A subagent run is p50
    # 23 writes long (p90 67), so 0.18 % of silences reaching 90 s is 1 RUN IN
    # 15 whose ⚙ blinks off and back on mid-flight. The event that costs
    # something is a run going dark AT ALL, so the distribution to choose from
    # is the LONGEST silence inside one run: p50 13.4 s, p75 35.1, p90 71.3,
    # p95 104.0, p99 195.2, p99.5 296.6, max 1200.
    #
    #      V    runs that blink   lit with nothing running   pp per added s
    #     60        13.30 %              7.7 %                  0.394
    #     90         6.57 %              9.4 %                  0.224   <- was
    #    120         3.68 %             10.9 %                  0.096
    #    150         1.83 %             12.2 %                  0.062
    #    180         1.17 %             13.3 %                  0.022   <- now
    #    240         0.71 %             15.4 %                  0.008
    #    300         0.50 %             17.3 %                  0.004
    #
    # 180 is where `quiet_s`'s own rule puts it — between the p95 and the p99
    # of the pause it must tolerate — and it is where the return per second of
    # added lateness falls off a cliff (90→120 buys 0.096 pp/s, 120→150 0.062,
    # 150→180 0.022, 180→240 0.008). It takes the flicker from 1 run in 15 to 1
    # in 86 and pays for it with 3.9 pp more of an indicator lit after the work
    # stopped — over-count on a hint, which is the harmless direction.
    # On the board's OWN worktrees the case is stronger still: 123 runs, p95
    # 252 s, 35.77 % blinking at 90 against 14.63 % at 180.
    "subagent_grace_s": 180,   # last write under <session-id>/: still running
    # De-escalation dwell (ENGINE.md §6.3(a)): a status must stand this long
    # before it may quieten. Escalation toward more attention never waits. It
    # does NOT stack on `quiet_s` — see `status.settle`.
    "flicker_dwell_s": 3.0,
    "max_sessions": 6,         # per worktree card
    "exclude_accounts": [],    # account labels never AUTO-picked for dispatch
    "reserve_percent": {},     # {label: pct} buffer kept free before AUTO-pick treats account as full ("*" = default)
    # The sweep's cadences (ENGINE.md §2.5). They are keys and not constants
    # because they trade notification latency against battery, and the right
    # trade belongs to whoever owns the laptop. Every default here is the
    # measured value shipped in observer.py, which is where the measurements
    # and the reasoning live — read the tables beside IDLE_S/GIT_S before
    # changing one, and note that `git_s` moves the bill more than `idle_s`
    # does. Observer(idle_s=…) still wins over the file: the tests drive the
    # loop at cadences no user would choose.
    #
    # `idle_s` no longer sets notification latency — the watcher below does,
    # at ~53 ms measured. It sets how long the things kqueue CANNOT see wait:
    # a `claude` being born (there is no filter for process birth), an append
    # to a transcript already outside the 48 h window, a subagent file more
    # than one directory deep. Measured on a nine-worktree fleet with nothing
    # happening: the old timer-only 3.0 cost 13.9 % of one core; the watcher
    # with 30.0 costs 5.8 %.
    "idle_s": 30.0,            # seconds between sweeps with no evidence of change
    "idle_blind_s": 3.0,       # …and with no watcher: Linux, or a kqueue that died
    "hot_s": 0.15,             # floor between sweeps after a MUTATION nudge
    "git_s": 15.0,             # min seconds between git fan-outs on the sweep
    "reconcile_s": 60.0,       # cold sweep: bypass every memo, count the disagreements
    "max_stale_s": 45.0,       # never wait longer than this between sweeps (>= idle_s,
                               # or it silently BECOMES the cadence — see observer.py)
    # The watcher (ENGINE.md §10's deferred kqueue, built: watcher.py). Events
    # are what let `idle_s` go from 3.0 to 30.0, so turning this off without
    # also lowering `idle_s` leaves a board that notices things half a minute
    # late — which is why the loop reads `idle_blind_s` whenever the watcher is
    # not actually running, rather than trusting the setting.
    "watch": True,             # react to transcript writes instead of polling for them
    "watch_max_fds": 2048,     # hard cap; over it the excess degrades to the timer
    "watch_debounce_s": 0.05,  # quiet period that ends a burst — 50 lines, one nudge
    "watch_min_interval_s": 1.0,   # min seconds between event nudges (the rate limit)
    "watch_max_window_s": 2.0,     # never defer a nudge longer than this
    "watch_rebuild_s": 30.0,   # re-enumerate the watch set on this clock too
    # The event stream (`GET /api/events`, ADR 0005). Thread-per-client is what
    # that ADR measured and accepted, so a subscriber is a THREAD held for the
    # subscriber's whole lifetime — a resource, not a statistic, and the reason
    # this is a cap and not a comment.
    #
    # ADR 0005 measured where the box breaks, against a real
    # ThreadingHTTPServer but not against this loop:
    #
    #     streams   threads   RSS      p95 event   unrelated GET /ping
    #        50        53     21.7 MB    0.3 ms     12 ms
    #       500       503     49.3 MB    1.1 ms     19 ms   <- supported ceiling
    #       800       809     93.4 MB    4.2 ms   5005 ms   <- the box breaks
    #
    # Re-measured here, against THIS handler, bump -> bytes at every client:
    #
    #     streams   threads   p50       p95       GET /api/state   idle CPU
    #         1         3     0.051 ms  0.088 ms   0.6 ms          0.024 %
    #        12        14     0.719 ms  0.851 ms   0.6 ms          0.069 %
    #        32        34     1.303 ms  1.725 ms   0.7 ms          0.213 %
    #
    # (threads = one per stream + main + accept; idle CPU is a share of one
    # core with nothing happening at all.) The requirement is a browser in a
    # few tabs plus a phone, and the BROWSER gives out long before the server
    # does: 6 connections per origin means one EventSource per tab starves
    # POSTs at 3 tabs (ENGINE.md §5.4). So 32 sits an order of magnitude above
    # the need and an order below the measured ceiling, and costs a fifth of a
    # percent of a core to hold open. Over it the stream is REFUSED with a 503
    # naming the cap; the alternative is degrading every subscriber already
    # connected, which is the one outcome nobody can diagnose from outside.
    "sse_max_subscribers": 32,
    # How long a stream may go silent before a `: keepalive` comment frame.
    # Nothing on this side needs it — the server is happy to hold an idle
    # socket for hours. It exists for what sits BETWEEN: a NAT or a proxy reaps
    # an idle connection without telling either end, and EventSource cannot
    # distinguish that from a fleet where nothing is happening, which on this
    # board is the normal case. 25 s is under the shortest common NAT idle
    # timeout (30 s) and costs three bytes.
    "sse_keepalive_s": 25.0,
    # How often a SILENT stream checks that its client is still there. It is
    # not a second keepalive and writes nothing: it is one `select` plus one
    # `MSG_PEEK` on a socket that already exists.
    #
    # It has to exist because a dead peer is otherwise only discovered by
    # WRITING to it, and on a quiet fleet the next write is the keepalive —
    # which is the normal case, not an edge one. Measured, 12 rude RSTs, time
    # until every slot came back:
    #
    #     discovered by the next write only   > 20,000 ms  (the probe's own cap;
    #                                                       the true bound is
    #                                                       sse_keepalive_s)
    #     discovered by this check at 1.0 s        198 ms
    #
    # And the window is the CAP's problem, not a cosmetic one: EventSource
    # retries about every 3 s, so one flapping client parks ~8 dead slots
    # inside a 25 s keepalive and four of them park all 32 — the cap would
    # then refuse live clients on behalf of clients that had already left.
    #
    # What closing it costs, 32 idle streams, 8 s windows, share of one core:
    #
    #     liveness 1.0 s   0.213 %
    #     liveness off     0.197 %
    #
    # i.e. inside the run-to-run noise of the measurement (at 12 streams the
    # ordering reverses, 0.069 % against 0.082 %), because the check is one
    # `select` and one `MSG_PEEK` per stream per second and writes nothing.
    "sse_liveness_s": 1.0,
    # Authentication (auth.py). One knob, and it is the one that is dangerous
    # to touch: `auth_trust_loopback` False makes the local browser present a
    # token like anything else, which is what you want the day something else
    # on this machine proxies to the board — `tailscale serve` makes every
    # request arrive from 127.0.0.1, and this switch is the difference between
    # that being a bypass and being a login. It defaults True because the board
    # is a loopback program and a process that can open that socket can already
    # read every transcript on disk without asking this server at all.
    "auth_trust_loopback": True,
    "resume_delay_s": 60,      # auto-resume fires this long after the limit reset
    "resume_message": "continue",  # what auto-resume types at the stalled agent
}

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
    # One flag for one knob. `idle_s` is the only cadence a user has a reason
    # to change from the command line — it is the battery/latency dial — so it
    # gets the `--window-h` treatment. The other four (hot_s, git_s,
    # reconcile_s, max_stale_s) stay file-only, like `working_s`: they are
    # tuning for someone who has already read observer.py, and a flag each
    # would be five ways to misconfigure the loop for one that is used.
    ap.add_argument("--idle-s", type=float, metavar="S",
                    help="seconds between SAFETY-NET sweeps (default 30.0, "
                         "~6%% of one core idle). Transcript writes arrive as "
                         "events in ~50ms; this is the worst case for what "
                         "kqueue cannot see, and 3.0 on a platform with no "
                         "watcher")
    ap.add_argument("--config", metavar="FILE", help="path to a orchestra.config.json")
    # Device administration. Flags rather than a route, because the admin
    # surface of API.md §2.5 is the one thing a stolen phone must never be able
    # to reach — a device that could revoke devices could revoke the Mac's
    # ability to revoke IT. On this machine, at a shell, is a boundary the
    # network cannot cross. Each one prints and exits; none of them starts a
    # server. The flags are parsed here so `--help` lists them, and acted on in
    # __main__, because this module is below `auth` in the import graph.
    ap.add_argument("--add-device", metavar="LABEL",
                    help="mint a token for one device and print it ONCE, then exit")
    ap.add_argument("--list-devices", action="store_true",
                    help="list registered devices (no tokens — they are stored hashed)")
    ap.add_argument("--revoke-device", metavar="ID",
                    help="revoke a device by id; its token stops working immediately")
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
    # `is not None`, not truthiness: `--idle-s 0` is a spin loop and must reach
    # the loop as the mistake it is, not be silently ignored as a default.
    if args.idle_s is not None: CFG["idle_s"] = args.idle_s
    return args


def account_label(home):
    name = home.name.lstrip(".")
    if name == "claude":
        return "main"
    return re.sub(r"^claude-?", "", name) or name
