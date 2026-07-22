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
