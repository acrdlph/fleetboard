"""orchestra.gitrepo — the git layer: which worktrees exist, and where
every branch really is.

Two jobs that both start with a directory listing. `discover_worktrees` walks
the configured roots and returns the dirs that are git checkouts (a bare
`<worktree>/.git` or the `<worktree>/repo/.git` layout); `git_info` asks git
for one worktree's branch, tip, dirt and ahead/behind. `branch_topology` is
the expensive one behind the map view — fork point, tip and drift for every
branch, grouped by origin URL — so it sits behind a 30 s cache.

`munge` and `match_worktree` are here rather than with the transcript code
because both are pure functions on a *path*: Claude Code names a project dir
after the cwd it was started in, and mapping that name back to a worktree is
git-side knowledge. The longest-prefix rule in `match_worktree` is the reason
'myapp' never swallows 'myapp-audit'.

Everything reads; nothing here writes to a repo. The only git write commands
the board ever runs live in the finish path.
"""

import concurrent.futures
import re
import time
from pathlib import Path

from . import config, shell


# ---------------------------------------------------------------- worktrees

def munge(path):
    """Claude Code's project-dir name for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def match_worktree(proj_name, wt_prefixes):
    """Map a munged project-dir name to a worktree path by the LONGEST matching
    prefix, so 'myapp' doesn't swallow 'myapp-audit'. Returns None if none match.
    `wt_prefixes` is {worktree_path: munged_prefix}."""
    best = None
    for path, pref in wt_prefixes.items():
        if proj_name == pref or proj_name.startswith(pref + "-"):
            if best is None or len(pref) > len(wt_prefixes[best]):
                best = path
    return best


def discover_worktrees():
    pat = re.compile(config.CFG["pattern"], re.I) if config.CFG["pattern"] else None
    wts, seen = [], set()
    for root in config.CFG["roots"]:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for p in sorted(root.iterdir(), key=lambda x: x.name.lower()):
            if not p.is_dir() or (pat and not pat.search(p.name)) or str(p) in seen:
                continue
            git_root = None
            if (p / ".git").exists():
                git_root = p
            elif (p / "repo" / ".git").exists():  # <worktree>/repo layout
                git_root = p / "repo"
            if git_root:
                seen.add(str(p))
                wts.append({"name": p.name, "path": str(p), "git": str(git_root)})
    return wts


def git_info(git_root):
    """Branch, last commit, dirty count and ahead/behind for one worktree.

    Two `git` spawns, not five: `status --porcelain=v2 --branch` carries the
    branch, the upstream, ahead/behind and the dirty list in a single pass,
    replacing `branch --show-current`, `status --porcelain` and
    `rev-list --left-right --count`. `log -1` stays separate — v2 reports the
    commit oid but not its timestamp or subject.

    Three things about that format bite silently, all reproduced before this
    was written:

    * A detached HEAD reports the bare string `(detached)` with no sha. The
      label CANNOT be rebuilt by slicing `branch.oid`, because git's abbrev
      length is per-repository — measured 8 characters in one worktree of this
      fleet and 9 in the other eight. So detached heads spend one extra spawn
      on `rev-parse --short`, which is both exact and rare.
    * `# branch.ab` is ABSENT when there is no upstream — not `+0 -0`. Left
      unset, ahead/behind stay None, which is what the caller expects.
    * `# branch.ab +A -B` is ahead-then-behind, the OPPOSITE order to
      `rev-list --left-right --count @{u}...HEAD`, which puts the upstream
      first. Reading them positionally inverts every count on the board.
    """
    info = {"branch": None, "commit": None, "dirty": 0, "ahead": None, "behind": None}
    branch = None
    rc, out = shell.run(["git", "status", "--porcelain=v2", "--branch"], cwd=git_root)
    if rc == 0:
        dirty = 0
        for line in out.splitlines():
            if line.startswith("# branch.head "):
                branch = line[len("# branch.head "):].strip()
            elif line.startswith("# branch.ab "):
                ab = line[len("# branch.ab "):].split()
                if len(ab) == 2:
                    # +ahead -behind — note the order, it is not rev-list's
                    info["ahead"] = int(ab[0].lstrip("+"))
                    info["behind"] = int(ab[1].lstrip("-"))
            elif line and not line.startswith("#"):
                dirty += 1          # 1/2/u/? entries; one line per path, as v1
        info["dirty"] = dirty
    if branch and branch != "(detached)":
        info["branch"] = branch
    else:
        rc, head = shell.run(["git", "rev-parse", "--short", "HEAD"], cwd=git_root)
        info["branch"] = f"detached@{head}" if rc == 0 else "?"
    rc, log = shell.run(["git", "log", "-1", "--format=%h%x00%ct%x00%s"], cwd=git_root)
    if rc == 0 and log:
        h, ct, s = (log.split("\x00") + ["", "", ""])[:3]
        info["commit"] = {"hash": h, "ts": int(ct or 0), "subject": s}
    return info


GIT_WORKERS = 16      # fan-out for git_info_many; see the note there


def git_info_many(roots):
    """`git_info` for every worktree at once — {root: info}.

    A board with nine worktrees spends ~880 ms here, and almost none of it is
    ours: each call is a handful of short-lived `git` processes, so the cost is
    spawn plus disk, and the interpreter is blocked on `wait()` for nearly all
    of it. That makes concurrency the single biggest win available on this
    path — bigger than cutting the number of spawns, which only helps once the
    waiting is already overlapped.

    Fanning out per worktree (not per git command) keeps `git_info` itself an
    ordinary serial function: readable, unit-testable, and still the thing the
    tests monkeypatch. Duplicate roots are collapsed so a `<dir>/repo` layout
    pointing several worktrees at one git dir does not pay twice.
    """
    uniq = list(dict.fromkeys(roots))
    if not uniq:
        return {}
    if len(uniq) == 1:
        return {uniq[0]: git_info(uniq[0])}
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(GIT_WORKERS, len(uniq)),
            thread_name_prefix="gitinfo") as ex:
        # git_info is resolved from module globals per call, so a test that
        # patches gitrepo.git_info still takes effect through this path
        return dict(zip(uniq, ex.map(lambda r: git_info(r), uniq)))


# ----------------------------------------------------------- branch topology

TOPO_TTL_S = 30.0
_topo = {"t": 0.0, "data": None}


def _base_ref(git_root):
    """The trunk ref this repo's branches are measured against."""
    rc, out = shell.run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=git_root)
    if rc == 0 and out.startswith("refs/remotes/"):
        return out[len("refs/remotes/"):]
    for cand in ("origin/main", "origin/master", "main", "master"):
        rc, _ = shell.run(["git", "rev-parse", "-q", "--verify", cand], cwd=git_root)
        if rc == 0:
            return cand
    return None


def branch_topology():
    """Where every branch really is: fork point from trunk, tip, drift."""
    now = time.time()
    groups = {}
    for w in discover_worktrees():
        g = w["git"]
        rc, origin = shell.run(["git", "remote", "get-url", "origin"], cwd=g)
        key = origin if rc == 0 and origin else "local:" + w["path"]
        base = _base_ref(g)
        if not base:
            continue
        rc, mb = shell.run(["git", "merge-base", "HEAD", base], cwd=g)
        if rc != 0 or not mb:
            continue

        def ts(ref):
            rc2, out2 = shell.run(["git", "show", "-s", "--format=%ct", ref], cwd=g)
            try:
                return int(out2.strip().splitlines()[-1])
            except (ValueError, IndexError):
                return None

        fork_ts, tip_ts, base_ts = ts(mb), ts("HEAD"), ts(base)
        if not (fork_ts and tip_ts):
            continue
        _, ah = shell.run(["git", "rev-list", "--count", f"{mb}..HEAD"], cwd=g)
        _, bh = shell.run(["git", "rev-list", "--count", f"{mb}..{base}"], cwd=g)
        _, cts = shell.run(["git", "log", "--format=%ct", "-40", f"{mb}..HEAD"], cwd=g)
        _, last = shell.run(["git", "log", "-1", "--format=%h%x00%s"], cwd=g)
        h, subj = (last.split("\x00") + ["", ""])[:2]
        _, br = shell.run(["git", "branch", "--show-current"], cwd=g)
        _, dirty = shell.run(["git", "status", "--porcelain"], cwd=g)
        grp = groups.setdefault(key, {
            "repo": re.sub(r"\.git$", "", key.rsplit("/", 1)[-1]),
            "base": base, "trunk_ts": 0, "trunk_commits": [], "_root": g,
            "branches": []})
        if base_ts and base_ts > grp["trunk_ts"]:
            # separate clones fetch at different times — the freshest
            # origin/<main> wins as this repo's trunk tip
            grp["trunk_ts"], grp["_root"] = base_ts, g
        grp["branches"].append({
            "worktree": w["name"], "branch": br or "?",
            "fork_ts": min(fork_ts, tip_ts), "tip_ts": tip_ts,
            "ahead": int(ah or 0), "behind": int(bh or 0),
            "dirty": len([l for l in dirty.splitlines() if l.strip()]),
            "hash": h, "subject": subj,
            "commits": [int(x) for x in cts.split()][:40] if cts else [],
        })
    for grp in groups.values():
        _, tct = shell.run(["git", "log", "--format=%ct", "-40", grp["base"]],
                           cwd=grp.pop("_root"))
        grp["trunk_commits"] = [int(x) for x in tct.split()][:40] if tct else []
    return {"generated_at": now, "groups": list(groups.values())}


def demo_topology():
    now = time.time()
    H = 3600

    def spread(t0, t1, n):
        return [int(t0 + (t1 - t0) * i / max(1, n - 1)) for i in range(n)]

    def br(wt, branch, fork_h, tip_h, ahead, behind, dirty, subj):
        return {"worktree": wt, "branch": branch, "fork_ts": int(now - fork_h * H),
                "tip_ts": int(now - tip_h * H), "ahead": ahead, "behind": behind,
                "dirty": dirty, "hash": "a1b2c3d", "subject": subj,
                "commits": spread(now - fork_h * H + 600, now - tip_h * H, min(ahead, 20))}

    return {"generated_at": now, "groups": [{
        "repo": "orbital", "base": "origin/main", "trunk_ts": int(now - 0.4 * H),
        "trunk_commits": spread(now - 70 * H, now - 0.4 * H, 24),
        "branches": [
            br("orbital-api", "feat/webhook-retries", 68, 0.35, 14, 6, 12,
               "feat(webhooks): exponential backoff with jitter"),
            br("orbital-web", "fix/checkout-race", 34, 0.6, 6, 2, 3,
               "fix(cart): serialize checkout mutations"),
            br("kepler-worker", "perf/batch-inserts", 9, 0.05, 9, 0, 7,
               "perf(db): batch event inserts"),
            br("lander-docs", "docs/quickstart", 46, 26, 3, 11, 2,
               "docs: rewrite quickstart around the new init flow"),
            br("voyager-cli", "main", 48, 48, 0, 9, 0, "chore: release v0.4.1"),
        ]}]}


def cached_topology():
    if config.DEMO:
        return demo_topology()
    now = time.time()
    if _topo["data"] is None or now - _topo["t"] > TOPO_TTL_S:
        _topo["data"] = branch_topology()
        _topo["t"] = now
    return _topo["data"]
