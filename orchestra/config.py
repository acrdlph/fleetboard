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
    "git_s": 15.0,             # min seconds between git fan-outs on the sweep (ENGINE.md §2.5)
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


def account_label(home):
    name = home.name.lstrip(".")
    if name == "claude":
        return "main"
    return re.sub(r"^claude-?", "", name) or name
