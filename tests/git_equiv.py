"""Golden-equivalence checker for git_info — old vs new, field by field.

Step 1 of ENGINE.md §9 replaces five `git` spawns per worktree with two. Every
way that can go wrong is SILENT: the board renders a confidently wrong value
rather than raising. Three known traps, all reproduced before writing a line:

  * detached HEAD — porcelain v2 emits the bare string "(detached)" and carries
    no sha, so a naive port renders "(detached)" where v1 rendered
    "detached@f89402a";
  * no upstream — the "# branch.ab" line is ABSENT entirely, not "+0 -0", so a
    parser that assumes it exists breaks on every untracked branch;
  * orientation — "# branch.ab +A -B" is ahead-then-behind, while v1's
    `rev-list --left-right --count @{u}...HEAD` puts the UPSTREAM on the left,
    i.e. behind-then-ahead. Swap them and every count on the board inverts.

So equivalence is not asserted, it is measured: this loads the previous
implementation straight out of git history and runs both against the same real
worktrees, diffing every field. Production code stays clean — nothing is kept
around "for comparison".

    python3 tests/git_equiv.py              # diff old vs new on real worktrees
    python3 tests/git_equiv.py --rev HEAD~1 # compare against a specific rev
    python3 tests/git_equiv.py --bench      # timings for both, serial + parallel

Unit tests for the three traps live in tests/test_orchestra.py against temp
repos; this tool is the wide net over whatever the machine actually has.
"""

import argparse
import importlib.util
import pathlib
import subprocess
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
FIELDS = ("branch", "commit", "dirty", "ahead", "behind")


def load_current():
    sys.path.insert(0, str(ROOT))
    import orchestra
    # load_config() parses sys.argv, so hide this tool's own flags from it —
    # otherwise argparse rejects --rev/--bench as unknown server options
    argv, sys.argv = sys.argv, sys.argv[:1]
    try:
        orchestra.load_config()
    finally:
        sys.argv = argv
    return orchestra


def load_old_gitrepo(rev, orchestra):
    """Materialise the gitrepo module as of `rev`, wired to the live deps.

    The old module is executed in its own namespace with `shell` and `config`
    injected from the current package, so the only thing that differs between
    the two runs is git_info itself — not the subprocess helper underneath it.
    """
    src = subprocess.run(["git", "show", f"{rev}:orchestra/gitrepo.py"],
                         cwd=ROOT, capture_output=True, text=True)
    if src.returncode != 0:
        raise SystemExit(f"cannot read gitrepo.py at {rev}: {src.stderr.strip()}")
    mod = types.ModuleType("gitrepo_old")
    mod.__dict__["shell"] = orchestra.shell
    mod.__dict__["config"] = orchestra.config
    body = "\n".join(l for l in src.stdout.splitlines()
                     if not l.startswith("from . import"))
    exec(compile(body, f"<gitrepo@{rev}>", "exec"), mod.__dict__)
    return mod


def compare(rev="HEAD"):
    orchestra = load_current()
    old = load_old_gitrepo(rev, orchestra)
    wts = orchestra.discover_worktrees()
    if not wts:
        raise SystemExit("no worktrees discovered — check orchestra.config.json")

    mismatches, checked, volatile = [], 0, []
    print(f"comparing git_info: {rev} vs working tree, over {len(wts)} worktrees\n")
    for w in wts:
        root = w.get("git_root") or w["path"]
        # The fleet is LIVE — other agents commit and stage while this runs, so a
        # naive one-shot A/B manufactures phantom diffs (observed: a worktree
        # reporting branch='?' once and its real branch a moment later). Confirm
        # any disagreement is reproducible, and interleave the order so a field
        # that is genuinely drifting shows up as volatile rather than as a
        # one-sided regression.
        a, b = old.git_info(root), orchestra.git_info(root)
        diff = [f for f in FIELDS if a.get(f) != b.get(f)]
        if diff:
            b2, a2 = orchestra.git_info(root), old.git_info(root)
            stable = [f for f in diff if a2.get(f) != b2.get(f)]
            churned = [f for f in diff if f not in stable]
            if churned:
                volatile.append((w["name"], churned))
            diff = stable
            a, b = a2, b2
        checked += 1
        flag = "  " if not diff else "!!"
        print(f"{flag} {w['name']:<26} branch={b.get('branch')!r:<34} "
              f"dirty={b.get('dirty')} ahead={b.get('ahead')} behind={b.get('behind')}")
        for f in diff:
            print(f"     {f}: old={a.get(f)!r}  new={b.get(f)!r}")
            mismatches.append((w["name"], f, a.get(f), b.get(f)))
    for name, fields in volatile:
        print(f"   ~ {name}: {', '.join(fields)} changed under us mid-run "
              f"(live worktree) — not counted as a mismatch")

    print()
    detached = sum(1 for w in wts
                   if str(orchestra.git_info(w.get("git_root") or w["path"])
                          .get("branch") or "").startswith("detached@"))
    noupstream = sum(1 for w in wts
                     if orchestra.git_info(w.get("git_root") or w["path"])
                     .get("ahead") is None)
    print(f"coverage: {checked} worktrees · {detached} detached · "
          f"{noupstream} without an upstream")
    if not detached:
        print("  ⚠ no detached HEAD in this sample — the (detached) trap is NOT "
              "exercised here; rely on the unit test for it")
    if not noupstream:
        print("  ⚠ every worktree has an upstream — the missing-branch.ab trap "
              "is NOT exercised here; rely on the unit test for it")

    if mismatches:
        print(f"\nFAIL — {len(mismatches)} field mismatch(es)")
        return 1
    print("\nOK — every field identical on every worktree")
    return 0


def bench(rev="HEAD", rounds=3):
    orchestra = load_current()
    old = load_old_gitrepo(rev, orchestra)
    wts = orchestra.discover_worktrees()
    roots = [w.get("git_root") or w["path"] for w in wts]

    def serial(fn):
        t = time.time()
        for r in roots:
            fn(r)
        return (time.time() - t) * 1000

    print(f"{len(roots)} worktrees, {rounds} rounds, median ms\n")
    for name, fn in ((f"old ({rev})", old.git_info), ("new (working tree)", orchestra.git_info)):
        runs = sorted(serial(fn) for _ in range(rounds))
        print(f"  {name:<22} {runs[len(runs)//2]:7.0f}")
    t = time.time()
    for _ in range(rounds):
        orchestra.collect_state()
    print(f"  {'collect_state':<22} {(time.time()-t)*1000/rounds:7.0f}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rev", default="HEAD")
    ap.add_argument("--bench", action="store_true")
    a = ap.parse_args()
    sys.exit(bench(a.rev) if a.bench else compare(a.rev))
