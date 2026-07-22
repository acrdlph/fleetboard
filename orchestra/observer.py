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
"""

import getpass
import os
import re
import time

from . import config, gitrepo, procs, transcripts, status, limits

STATE_TTL_S = 4.0              # cache collector output between requests
_cache = {"t": 0.0, "state": None}


# ---------------------------------------------------------------- collectors

def collect_state():
    now = time.time()
    worktrees = gitrepo.discover_worktrees()
    # `all_procs`, not `procs`: a local of that name would shadow the module.
    all_procs = procs.claude_processes()
    sessions = transcripts.scan_sessions(worktrees, all_procs, now)

    # An agent parked at the prompt on an exhausted account isn't "your turn" —
    # it's out of juice. Joined from the cclimits cache (populated lazily by
    # /api/limits; never fetched on the state path).
    acct_limits = limits.limits_by_account()
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




# --------------------------------------------------------------- demo state

def demo_state():
    """Fictional data with the exact shape of collect_state(), for screenshots."""
    now = time.time()

    seq = [0]

    def sess(status, acct, model, age, topic, said, subdir=None, pend=None, sid=None):
        seq[0] += 1
        return {"id": "demo0000", "sid": sid or f"demo-{seq[0]}",
                "account": acct, "status": status, "age_s": age,
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
    if config.DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()
        _cache["t"] = now
    return _cache["state"]
