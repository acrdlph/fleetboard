"""orchestra.identity — the address a mutation is actually delivered to.

Watching may be addressed by pid; ACTING may not. A pid is a slot the kernel
reuses, not a name. The chat drawer captures one when it opens and the user
sends minutes later; if the original agent exited in between, the OS is free to
have handed that number to a different `claude` — one running
`--dangerously-skip-permissions`, which will act on whatever is typed at it.
That is not a hypothetical: it is ADR 0008, and it was live in `/api/send`
until this module existed.

So every mutation names a DURABLE identity — a session id, a worktree, a tmux
pane, a working directory — and this module re-resolves that name against a
process table read AT THE INSTANT THE MUTATION ACTS. The pid rides along as a
hint and is checked against the answer; it is never the answer. An identity
that no longer resolves is REFUSED, with an `error` code the client can branch
on, never quietly retargeted at whoever holds the number now.

Two shapes of address, because two shapes of click exist:

* BY SESSION (`sid`) — the strongest, and what the chat drawer uses. The sid is
  the transcript's own name and outlives every process that ever served it.
  Resolution re-runs the exact pairing that built the board
  (`transcripts.scan_sessions` -> `procs.pair_sessions_with_procs`) over
  processes read just now, and answers with whichever process owns that session
  AT THIS MOMENT. A pid hint that disagrees with that answer is precisely the
  recycle case, and it is refused.
* BY PLACE (`worktree` / `cwd` / `tmux`) — what is left when the target is a
  terminal rather than a conversation: the second entry in the drawer's target
  picker, a loose process on a card, a row in the "other processes" strip. The
  pid must still be a live `claude` AND still be in the place the client named.
  A recycled pid almost never is.

`account`, `tty` and `tmux` may ride along on either shape as corroborators.
They cost nothing — every one of them is already on the card the click came
from — and each narrows the window further: to survive them a recycled pid
would have to have landed in the same pane, on the same tty, under the same
account.

WHY THE BOARD SNAPSHOT IS NOT CONSULTED. `Snapshot` says it of itself: it is
advisory, "never a mutation precondition". It can be a whole sweep old, and one
sweep is long enough for a pid to die and come back as somebody else. Every
answer here comes from a fresh `procs.claude_processes()`, which is ~70 CPU-ms
warm (procs.py's memo table) — a price paid per click, not per sweep.

Nothing here mutates. It sits above the observe layer (gitrepo, procs,
transcripts) and below the act layer, and `terminal` is its only caller inside
the package — which is what makes it unskippable: there is no way to type at an
agent that does not pass through here.
"""

import os
import time

from . import gitrepo, procs, transcripts


# The two refusals, as codes rather than prose, so a client can tell "your
# handle is stale, re-read the board" from "you never gave me a handle".
GONE = "identity_gone"
UNADDRESSED = "unaddressed"

# The addresses that are durable enough to act on. `account`, `tty` and `pid`
# are deliberately absent: an account names many agents, a tty is reused by
# whatever runs in that window next, and a pid is the bug.
ADDRESSES = ("sid", "worktree", "cwd", "tmux")


def _refuse(code, message):
    return {"ok": False, "error": code, "message": message}


def _short(sid):
    return (sid or "")[:8]


def _under(cwd, path):
    return bool(cwd) and (cwd == path or cwd.startswith(path.rstrip(os.sep) + os.sep))


def _worktree_named(name):
    """The worktree a client named, by name or by path. None if it is gone."""
    return next((w for w in gitrepo.discover_worktrees()
                 if name in (w["name"], w["path"])), None)


def _owner_of(sid, live, now=None):
    """(worktree, session) for `sid`, recomputed from disk and from `live`.

    The whole point is that this runs the SAME join the board ran — scan the
    transcripts, pair each session with a process by account then by freshness
    — rather than a second, simpler rule that would drift from it. What the
    card showed and what a mutation acts on must be produced by one function.
    """
    wts = gitrepo.discover_worktrees()
    by_path = {w["path"]: w for w in wts}
    for path, sessions in transcripts.scan_sessions(
            wts, live, time.time() if now is None else now).items():
        for s in sessions:
            if s.get("sid") == sid:
                return by_path.get(path), s
    return None, None


def resolve(pid=None, sid=None, account=None, worktree=None, cwd=None,
            tmux=None, tty=None, live=None):
    """(process, None) or (None, refusal) — who this mutation is really for.

    `pid` is a HINT. It is checked against the durable address and never
    substituted for one: a request carrying nothing but a pid is refused with
    `UNADDRESSED`, which is the backward-compatibility story for the legacy
    wire form — it stays callable, and it stops guessing.

    `live` lets a caller that has just read the process table pass it in rather
    than pay for a second `ps`; leave it None and one is read here, which is
    what "at the instant it acts" means.
    """
    try:
        pid = int(pid) if pid else None
    except (TypeError, ValueError):
        pid = None
    sid = (sid or "").strip() or None
    worktree = (worktree or "").strip() or None
    cwd = (cwd or "").strip() or None
    tmux = (tmux or "").strip() or None
    tty = (tty or "").strip() or None
    account = (account or "").strip() or None

    if not any((sid, worktree, cwd, tmux)):
        return None, _refuse(UNADDRESSED,
            f"refusing to act on pid {pid} alone — pids are recycled, so a bare "
            "pid can name a different agent by the time the click lands "
            "(ADR 0008). Reload the board and try again.")

    if live is None:
        live = procs.claude_processes()
    by_pid = {p["pid"]: p for p in live}

    if sid:
        wt, s = _owner_of(sid, live)
        if s is None:
            return None, _refuse(GONE,
                f"that agent is gone — session {_short(sid)} is no longer on the board")
        if account and s.get("account") != account:
            return None, _refuse(GONE,
                f"that agent is gone — session {_short(sid)} reads as "
                f"[{s.get('account')}] now, not [{account}]")
        if worktree and wt and worktree not in (wt["name"], wt["path"]):
            return None, _refuse(GONE,
                f"that agent is gone — session {_short(sid)} is in "
                f"{wt['name']}, not {worktree}")
        own = s.get("pid")
        if not own:
            return None, _refuse(GONE,
                f"that agent is gone — session {_short(sid)} has no live "
                "terminal any more")
        if pid and pid != own:
            # THE BUG, refused. The client held pid N; the session it named is
            # served by a different process now. Whoever holds N is somebody
            # else, and typing at them is the misdirected keystroke.
            return None, _refuse(GONE,
                f"that agent is gone — pid {pid} is not session "
                f"{_short(sid)}'s terminal any more (it is {own})")
        target = by_pid.get(own)
        if target is None:
            return None, _refuse(GONE,
                f"that agent is gone — session {_short(sid)} lost its process "
                "mid-request")
    else:
        # Addressed by place. There is no conversation to re-pair, so the pid
        # is load-bearing — but only inside the place the client named, which
        # is what a recycled pid will not satisfy.
        if not pid:
            return None, _refuse(UNADDRESSED,
                "no pid to act on — a place names a terminal only together "
                "with the process the board showed in it")
        target = by_pid.get(pid)
        if target is None:
            return None, _refuse(GONE, f"that agent is gone — pid {pid} is no "
                                       "longer a live claude process")
        if worktree:
            wt = _worktree_named(worktree)
            if wt is None:
                return None, _refuse(GONE,
                    f"that agent is gone — worktree '{worktree}' is no longer "
                    "on the board")
            if not _under(target.get("cwd"), wt["path"]):
                return None, _refuse(GONE,
                    f"that agent is gone — pid {pid} is not running in "
                    f"{wt['name']} any more")
        if cwd and target.get("cwd") != cwd:
            return None, _refuse(GONE,
                f"that agent is gone — pid {pid} runs in "
                f"{target.get('cwd') or 'an unknown directory'} now, not {cwd}")

    # Corroborators. Cheap, already on the wire, and each one is another thing a
    # recycled pid would have had to reproduce exactly.
    if tmux and (target.get("tmux_target") or None) != tmux:
        return None, _refuse(GONE,
            f"that agent is gone — pid {target['pid']} is not in tmux pane "
            f"{tmux} any more")
    if tty and (target.get("tty") or None) != tty:
        return None, _refuse(GONE,
            f"that agent is gone — pid {target['pid']} is not on {tty} any more")
    if account and not sid and target.get("account") and \
            target["account"] != account:
        return None, _refuse(GONE,
            f"that agent is gone — pid {target['pid']} belongs to "
            f"[{target['account']}] now, not [{account}]")
    return target, None
