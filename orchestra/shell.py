"""orchestra.shell — the one place the board shells out.

Every subprocess orchestra starts goes through `run`: git, ps, lsof,
osascript, tmux, cclimits. It never raises. A missing binary, a timeout, a
crash — all come back as `(1, "")`, so a collector that shells out for extra
context can lose that context without taking the board down with it. Callers
read the return code when they care and ignore it when they don't.

Reach it as `shell.run(...)`, never `from .shell import run`: the tests stand
in for git and tmux by patching `shell.run`, and a name imported at import
time would keep pointing at the real subprocess.
"""

import subprocess


def run(cmd, cwd=None, timeout=6):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception:
        return 1, ""
