"""Characterization harness — pins observable behaviour across a refactor.

The unit suite monkeypatches module globals (`fb.CFG`, `fb.git_info`, `fb._cache`, …) at 67
sites. That coupling is exactly what a module split breaks, and it breaks it *silently*: once
`collect_state` lives in `observer.py` and imports `git_info` by name, patching
`orchestra.git_info` stops having any effect and the test keeps passing while testing nothing.

So the unit suite cannot be its own safety net for this change. This harness is the net instead:

  * it patches NOTHING — it only calls public functions with explicit arguments,
  * it locates the code through `load_orchestra()`, which resolves either the single-file
    layout or the package layout, so the SAME harness runs on both sides of the split,
  * it records a golden JSON and compares against it, so "behaviour is identical" is a
    byte-comparison rather than an opinion.

Mutation-testing this harness: clear __pycache__ first. A same-size edit
(`+= 1` -> `+= 2`) within the same second leaves mtime and size unchanged, so
Python happily reuses the stale .pyc and the mutation appears to be "caught"
when it never ran at all. Observed here; it cost a confused half hour.

Usage:
    python3 tests/characterize.py --record     # before the refactor
    python3 tests/characterize.py              # after — must be byte-identical
    python3 -m unittest tests.test_characterization   # same check, as a test

Because it asserts on the public surface only, it also proves the package facade is complete:
if the split forgets to re-export something, loading fails here first.
"""

import argparse
import hashlib
import importlib.util
import itertools
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "golden" / "characterization.json"


def load_orchestra():
    """Import the app under either layout.

    A package today (`orchestra/__init__.py`); a single `orchestra.py` before ADR 0010.
    Everything this harness touches must be reachable from the top-level name in both,
    which is the facade contract. The single-file branch is kept so the harness still runs
    against a pre-split commit — that is how "identical on both sides" was proved.
    """
    pkg_init = ROOT / "orchestra" / "__init__.py"
    single = ROOT / "orchestra.py"
    if pkg_init.exists():
        sys.path.insert(0, str(ROOT))
        import orchestra as mod
        return mod, "package"
    if single.exists():
        spec = importlib.util.spec_from_file_location("orchestra", single)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["orchestra"] = mod
        spec.loader.exec_module(mod)
        return mod, "single-file"
    raise SystemExit("no orchestra.py and no orchestra/ package found")


# ------------------------------------------------------------------ input space

def _classify_inputs():
    # `turn_ended` joined the product when it stopped being dead code (the CLI's
    # own end-of-turn marker, read positionally). It doubles the space to 2,880
    # because it interacts with EVERY other axis: it must lose to delegated
    # work, to a live shell and to an unresolved tool_use, and it must never
    # revive a session with no process. Those are exactly the compositions a
    # later refactor can break silently.
    ages = [0, 1, 5, 19, 20, 59, 60, 89, 90, 91, 200, 3600]
    pends = [[], ["AskUserQuestion"], ["Bash"], ["Read", "Bash"], ["AskUserQuestion", "Bash"]]
    for age, alive, pend, deleg, skip, shells, ended in itertools.product(
            ages, [True, False], pends, [0, 1, 3], [True, False], [0, 1], [False, True]):
        yield dict(age_s=age, alive=alive, pending_tools=pend, delegated=deleg,
                   skip_perms=skip, working_s=90, shells=shells, turn_ended=ended)


def _quiet_inputs():
    """`quiet_s` — the last branch of the ladder, and the only clock in it that
    now carries a measured number rather than `working_s`.

    A separate product rather than a `quiet_s` axis on the one above, because
    the interesting question is narrow and the existing 2,880 cases would
    double for nothing: does the quiet timer decide ONLY the silence that
    nothing else explains, and does it leave the other two jobs of the old
    single number — the approval grace and the orphan grace — where they were?
    So `working_s` is pinned at 90 while `quiet_s` moves, and the ages straddle
    both. `turn_ended` is fixed False here: it returns above this branch and is
    already pinned across the full product above.
    """
    ages = [0, 24, 25, 44, 45, 46, 89, 90, 91, 200]
    for age, quiet, alive, pend, deleg, shells, skip in itertools.product(
            ages, [None, 25, 45], [True, False], [[], ["Bash"]], [0, 1],
            [0, 1], [False, True]):
        yield dict(age_s=age, alive=alive, pending_tools=pend, delegated=deleg,
                   skip_perms=skip, working_s=90, shells=shells, quiet_s=quiet)


def _settle_inputs():
    """Anti-flicker: escalate now, de-escalate after a dwell. `since` is the
    clock of the last ADOPTION, so the elapsed values straddle the boundary
    exactly, and `unknown` is in the list because it is a status `LOUDER` does
    not rank — a wholesale ps/lsof failure produces it and it must never be
    held back."""
    sts = ["needs_input", "limit", "blocked", "working", "waiting", "ended", "unknown"]
    now = 1000.0
    for prev, proposed, elapsed in itertools.product(
            [None] + sts, sts, [0.0, 2.999, 3.0, 100.0]):
        yield dict(prev=prev, proposed=proposed, now=now,
                   since=now - elapsed, dwell_s=3.0)


def _closeout_inputs():
    statuses = [None, "working", "needs_input", "blocked", "waiting", "ended", "unknown"]
    for st, anyw, sent, now in itertools.product(
            statuses, [True, False], [0, 940, 1000], [1000]):
        yield dict(paired_status=st, any_working=anyw, sent=sent, now=now)


TEXTS = [
    "", "   ", "hello world",
    "\x1b[31mred\x1b[0m text",
    "<command-name>/effort</command-name>",
    "Caveat: The messages below were generated by the user while running local commands.",
    "This session is being continued from a previous conversation that ran out of context.",
    "The user opened the file /tmp/x.py in the IDE.",
    "<system-reminder>noise</system-reminder>",
    "line one\nline two\n\n\nline three",
    "  leading and trailing  ",
    "a" * 400,
    "🚀 unicode ⌁ glyphs ▲ ⛔",
]

PATHS = [
    "/Users/a/code/myapp", "/Users/a/code/myapp-audit", "/Users/a/code/other",
    "/Users/a/code", "/", "/Users/a/code/myapp/sub", "relative/path",
]


def _safe(mod, name, *args, **kw):
    """Call a public function, recording either its value or its failure signature.

    A refactor must preserve how a function fails, not merely how it succeeds.
    """
    fn = getattr(mod, name, None)
    if fn is None:
        return {"__missing__": name}
    try:
        return fn(*args, **kw)
    except Exception as exc:                      # noqa: BLE001 — recording is the point
        return {"__raised__": type(exc).__name__, "__msg__": str(exc)[:200]}


# ------------------------------------------------------------------ the snapshot

def build(mod):
    snap = {}

    snap["classify_session"] = [
        {"in": kw, "out": _safe(mod, "classify_session", **kw)} for kw in _classify_inputs()
    ]
    snap["classify_quiet"] = [
        {"in": kw, "out": _safe(mod, "classify_session", **kw)} for kw in _quiet_inputs()
    ]
    snap["settle"] = [
        {"in": kw, "out": _safe(mod, "settle", **kw)} for kw in _settle_inputs()
    ]
    snap["closeout_step"] = [
        {"in": kw, "out": _safe(mod, "closeout_step", **kw)} for kw in _closeout_inputs()
    ]

    snap["munge"] = [{"in": p, "out": _safe(mod, "munge", p)} for p in PATHS]

    prefixes = {p: _safe(mod, "munge", p) for p in PATHS if not p.startswith("relative")}
    snap["match_worktree"] = [
        {"in": name, "out": _safe(mod, "match_worktree", name, prefixes)}
        for name in list(prefixes.values()) + ["-Users-a-code-myapp-extra", "unrelated", ""]
    ]

    snap["_clean"] = [{"in": t, "out": _safe(mod, "_clean", t)} for t in TEXTS]
    snap["_real_prompt"] = [{"in": t, "out": _safe(mod, "_real_prompt", t)} for t in TEXTS]
    snap["_osa_escape"] = [
        {"in": t, "out": _safe(mod, "_osa_escape", t)}
        for t in ['plain', 'has "quotes"', "back\\slash", "new\nline", "", "⌁ unicode"]
    ]

    snap["account_label"] = [
        {"in": h, "out": _safe(mod, "account_label", pathlib.Path(h))}
        for h in ["/Users/a/.claude", "/Users/a/.claude-work", "/Users/a/.claude-spare",
                  "/Users/a/.claude-", "/Users/a/other"]
    ]

    # per-model headroom + reserve: the policy the account picker duplicated and drifted from
    accounts = [
        {"label": "main", "plan": "max", "headroom_percent": 62,
         "limits": [{"name": "session", "used_percent": 21},
                    {"name": "weekly", "used_percent": 38}]},
        {"label": "work", "plan": "max", "headroom_percent": 0,
         "limits": [{"name": "session", "used_percent": 100},
                    {"name": "weekly", "used_percent": 91}]},
        {"label": "spare", "plan": "pro", "headroom_percent": 88,
         "limits": [{"name": "session", "used_percent": 4},
                    {"name": "weekly", "used_percent": 12}]},
    ]
    snap["_model_remaining"] = [
        {"in": {"account": a["label"], "model": m},
         "out": _safe(mod, "_model_remaining", a, m)}
        for a in accounts for m in [None, "opus", "sonnet", "haiku", "fable"]
    ]
    snap["account_reserve"] = [
        {"in": lbl, "out": _safe(mod, "account_reserve", lbl)}
        for lbl in ["main", "work", "spare", "unknown"]
    ]
    snap["_limit_active_until"] = [
        {"in": lim, "out": _safe(mod, "_limit_active_until", lim)}
        for lim in [{}, {"resets_at": 0}, {"resets_at": 1784650000},
                    {"name": "weekly", "resets_at": 1784650000, "used_percent": 100}]
    ]

    snap["card_availability"] = [
        {"in": {"sessions": s, "procs": p},
         "out": _safe(mod, "card_availability", s, p)}
        for s in ([], [{"status": "working"}], [{"status": "ended"}],
                  [{"status": "waiting"}, {"status": "ended"}],
                  [{"status": "needs_input"}], [{"status": "limit"}])
        for p in ([], [{"pid": 1}], [{"pid": 1}, {"pid": 2}])
    ]

    snap["session_topic"] = [
        {"in": t, "out": _safe(mod, "session_topic", t)} for t in TEXTS
    ]

    snap["state_payload"] = _state_payload(mod)
    snap["limits_payload"] = _limits_payload(mod)

    return snap


# ---------------------------------------------------------- the wire payload

VOLATILE = {
    # wall-clock and machine readings that legitimately differ between runs.
    # Normalised rather than dropped, so a field VANISHING is still caught.
    "generated_at", "at", "cpu", "etime", "age_s", "last_write_at",
    "resets_in", "resets_at", "resets_in_seconds", "due_at", "created_at",
    "fired_at", "fetched_at", "closeout_sent", "ts", "ts_epoch", "sweep_ms",
    "freshness", "pid", "uptime_s",
}


def _normalise(obj, path="", subs=()):
    """Replace volatile leaf values with a type marker, keeping structure.

    The point of this snapshot is the SHAPE of the wire payload — which keys
    exist, nested how, holding what type — not the readings of the moment. A
    renamed or dropped field must fail; a clock tick must not.
    """
    if isinstance(obj, dict):
        return {k: (f"<{type(v).__name__}>" if k in VOLATILE and not isinstance(v, (dict, list))
                    else _normalise(v, f"{path}.{k}", subs))
                for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [_normalise(x, path + "[]", subs) for x in obj]
    if isinstance(obj, str):
        # The fixture lives under a randomly-named temp dir whose path is
        # embedded throughout the payload (card paths, git roots, session cwds,
        # munged project dirs). Without this the snapshot differs on every run
        # and the check is useless — it was, until this was added.
        for needle, token in subs:
            if needle in obj:
                obj = obj.replace(needle, token)
        return obj
    return obj


def _fixture_fleet(mod, tmp):
    """A tiny but real fleet on disk: two git worktrees and a Claude home.

    Deliberately NOT demo mode. `cached_state()` under DEMO returns
    `demo_state()` — a separate hand-written fixture that never touches the
    compose path — so snapshotting it pins nothing about the code a restructure
    actually moves. (Verified: a field added inside collect_state's card loop
    went completely undetected against the demo payload.)

    Nothing is monkeypatched here. The fixture is selected purely through
    config — roots, pattern and homes — so the real discover_worktrees →
    git_info → scan_sessions → collect_state path runs end to end. Live probes
    (`ps`/`lsof`) still run for real, but no claude process has its cwd inside a
    temp dir, so every card lands with an empty live_procs deterministically.
    """
    import subprocess
    root = tmp / "code"
    root.mkdir(parents=True)
    for name, branch in (("alpha", "main"), ("beta", "feat/x"), ("gamma", "fix/y")):
        d = root / name
        d.mkdir()
        env = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
               "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
        run = lambda *a, _d=d, _e=env: subprocess.run(
            ["git", "-C", str(_d), *a], check=True, capture_output=True,
            text=True, env={**os.environ, **_e})
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t.t")
        run("config", "user.name", "t")
        (d / "f").write_text("1\n")
        run("add", "-A")
        run("commit", "-q", "-m", "seed")
        if branch != "main":
            run("checkout", "-q", "-b", branch)
            (d / "g").write_text("2\n")
            run("add", "-A")
            run("commit", "-q", "-m", "work")
        if name == "beta":
            (d / "dirty1").write_text("x\n")
            (d / "dirty2").write_text("y\n")

    home = tmp / ".claude"
    # gamma's transcript is backdated: in-window but long idle, so it lands on a
    # different status to alpha/beta and the severity sort has something to sort.
    # beta's turn is CLOSED (a `turn_duration` after its last word) and alpha's
    # is not, so the payload pins the `turn_ended` wire key both ways — present
    # only when observed — and pins that the marker alone never resurrects a
    # session with no live process.
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for name in ("alpha", "beta", "gamma"):
        cwd = root / name
        proj = home / "projects" / mod.munge(str(cwd))
        proj.mkdir(parents=True)
        (proj / f"sess-{name}.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "cwd": str(cwd), "gitBranch": "main",
             "message": {"content": f"build the {name} feature"}},
            {"type": "assistant", "cwd": str(cwd),
             "message": {"model": "claude-opus-4-8",
                         "content": [{"type": "text", "text": f"working on {name}"}]}},
        ] + ([
            # beta also left a background task outstanding, dated NOW so the
            # `delegated_s` shelf life is live rather than expired. Without a
            # dated launch here `pending_bg_tools` would be pinned as the
            # constant 0 and the wire key would be blind to the count behind
            # it — the same blindness `_limits_payload` exists to fix.
            {"type": "assistant", "cwd": str(cwd), "timestamp": now_iso,
             "message": {"model": "claude-opus-4-8", "content": [
                 {"type": "tool_use", "id": "toolu_char1", "name": "Bash",
                  "input": {"command": "sleep 900", "run_in_background": True}}]}},
            {"type": "user", "cwd": str(cwd), "timestamp": now_iso,
             "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "toolu_char1",
                  "content": "Command running in background with ID: bchar1. "
                             "You will be notified when it completes."}]}},
            {"type": "system", "subtype": "turn_duration", "cwd": str(cwd),
             "durationMs": 1000, "pendingWorkflowCount": 0,
             "pendingBackgroundAgentCount": 0}] if name == "beta" else [])) + "\n")
        if name == "gamma":
            old = 1784600000.0          # fixed, well inside the 48h window
            os.utime(proj / f"sess-{name}.jsonl", (old, old))
    return root, home


def _state_payload(mod):
    """The real compose path over a controlled fleet — the wire payload's shape.

    This is the only check that notices when a restructure silently drops or
    renames a field that index.html, map.html or the coming Swift client reads.
    """
    import shutil
    import tempfile
    if not shutil.which("git"):
        return [{"in": "collect_state(fixture)", "out": {"__skipped__": "git absent"}}]
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="orchestra-char-"))
    saved = {k: mod.CFG.get(k) for k in ("roots", "pattern", "homes")}
    was_demo = getattr(mod.config, "DEMO", False)
    try:
        root, home = _fixture_fleet(mod, tmp)
        mod.config.DEMO = False
        mod.CFG["roots"] = [str(root)]
        mod.CFG["pattern"] = ""
        mod.CFG["homes"] = [str(home)]
        state = mod.collect_state()
        # Do NOT re-sort. The severity ordering is board-visible behaviour and
        # sorting it away here made the snapshot blind to it — verified by
        # mutation: inverting the severity rank went undetected until this came
        # out. Python's sort is stable and the fixture is fixed, so the order is
        # deterministic as it stands.
        state.pop("other_procs", None)      # whatever else is running on the box
        subs = ((str(tmp), "<TMP>"), (mod.munge(str(tmp)), "<MUNGED-TMP>"))
        return [{"in": "collect_state(fixture)", "out": _normalise(state, subs=subs)}]
    except Exception as exc:                        # noqa: BLE001
        return [{"in": "collect_state(fixture)",
                 "out": {"__raised__": type(exc).__name__, "__msg__": str(exc)[:200]}}]
    finally:
        mod.config.DEMO = was_demo
        for k, v in saved.items():
            mod.CFG[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _limits_payload(mod):
    """The `/api/limits` wire shape, and the account summary derived from it.

    `state_payload` above cannot see either: its fixture has no cclimits, so
    `limits._limits["data"]` stays None, no session ever reaches `status:
    "limit"`, and the whole `session.limit` sub-object is absent from the
    snapshot. That blind spot let the limit fields change shape unnoticed —
    found when removing `resets_in` from the wire left the golden byte-identical.

    Demo mode is the seam that opens it without patching anything: both
    `cached_limits` and `limits_by_account` read `demo_limits()` directly under
    `DEMO`, so a config flag — the same lever `_state_payload` uses — reaches
    the real composition. `reserve_percent` is pinned empty so the recording
    machine's own config cannot leak into the golden.
    """
    was_demo = getattr(mod.config, "DEMO", False)
    saved = mod.CFG.get("reserve_percent")
    try:
        mod.config.DEMO = True
        mod.CFG["reserve_percent"] = {}
        return [{"in": "cached_limits(demo)", "out": _normalise(_safe(mod, "cached_limits"))},
                {"in": "limits_by_account(demo)",
                 "out": _normalise(_safe(mod, "limits_by_account"))}]
    finally:
        mod.config.DEMO = was_demo
        mod.CFG["reserve_percent"] = saved


def digest(snap):
    """Per-section digests, so a diff names the function that moved rather than 'something'."""
    out = {}
    for k, v in sorted(snap.items()):
        blob = json.dumps(v, sort_keys=True, default=str).encode()
        out[k] = {"sha256": hashlib.sha256(blob).hexdigest()[:16], "cases": len(v)}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--record", action="store_true", help="write the golden file")
    ap.add_argument("--show", action="store_true", help="print per-section digests")
    args = ap.parse_args()

    mod, layout = load_orchestra()
    snap = build(mod)
    dig = digest(snap)

    if args.show:
        for k, v in dig.items():
            print(f"  {v['sha256']}  {v['cases']:5d} cases  {k}")

    total = sum(v["cases"] for v in dig.values())

    if args.record:
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps({"digests": dig, "snapshot": snap},
                                     sort_keys=True, indent=1, default=str))
        print(f"recorded {total} cases across {len(dig)} functions "
              f"({layout} layout) -> {GOLDEN.relative_to(ROOT)}")
        return 0

    if not GOLDEN.exists():
        print("no golden file; run with --record first", file=sys.stderr)
        return 2

    want = json.loads(GOLDEN.read_text())
    drift = [k for k in sorted(set(want["digests"]) | set(dig))
             if want["digests"].get(k, {}).get("sha256") != dig.get(k, {}).get("sha256")]
    if not drift:
        print(f"characterization OK — {total} cases identical ({layout} layout)")
        return 0

    print(f"BEHAVIOUR CHANGED in {len(drift)} function(s) ({layout} layout):", file=sys.stderr)
    for k in drift:
        print(f"  {k}", file=sys.stderr)
        old = {json.dumps(c, sort_keys=True, default=str)
               for c in want["snapshot"].get(k, [])}
        new = {json.dumps(c, sort_keys=True, default=str) for c in snap.get(k, [])}
        for line in sorted(new - old)[:3]:
            print(f"    now: {line[:200]}", file=sys.stderr)
        for line in sorted(old - new)[:3]:
            print(f"    was: {line[:200]}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
