# FRESHNESS — making orchestra's state true, fast, and pushable

**Status:** settled (design). **Date:** 2026-07-21.
**Outranked by:** [`VERIFIED-FACTS.md`](VERIFIED-FACTS.md). If this document contradicts a
measurement there, this document is wrong.

> `orchestra.py:NNN` citations throughout are historical —
> [ADR 0010](adr/0010-split-into-a-package.md) split that file into the `orchestra/` package.
> Grep for the symbol name. The one substantive change: the suite no longer loads a file by
> path, so the passages about `tests/` importing `orchestra.py` via `importlib` now read
> `import orchestra`.

This is the specification for the state layer that both the web board and the coming iOS client
read. It covers one thing: making what they display **fresh** — low-latency, accurate, and cheap
enough to push. The API contract, auth, and the phone's UX live in their own documents.

The answer is **layered**, and that is not a hedge. Four independent teams designed four
architectures — a fast poller, a background collector, a kqueue watcher, and an agent-side hook
channel — and each one is right about a different part of the problem. Any single one shipped
alone leaves a fault the others fix. The layers below are ordered so that **each is
independently shippable, independently valuable, and does not require the next one to be worth
doing.** Stop after any layer and the board is better than it was.

---

## 1. The problem, quantified

### 1.1 What it costs to look

Measured on the dev machine (Darwin 25.2, python 3.14.5, git 2.50.1 Apple Git-155), 9 worktrees,
5 live `claude` processes, 8 Claude homes, load average 26:

```
collect_state()                 1641 ms
  git_info x9                   1277 ms   78%   40 git subprocesses, strictly serial
  scan_sessions                  335 ms   20%   of which 202 ms is an 18,029-file rglob
  claude_processes               112 ms    7%   ps + lsof + ps eww + tmux list-panes
  discover_worktrees               1 ms
```

Stacked on top of that, from a real change to pixels on the board:

```
  up to  4000 ms   STATE_TTL_S = 4.0            (orchestra.py:61)
       + 1641 ms   collect_state(), inline, in the request
  up to  5000 ms   setInterval(tick, 5000)      (index.html:1169)
  ========================
      ~10,600 ms   worst case
```

Two structural facts make it worse than the arithmetic:

- **`STATE_TTL_S = 4.0` is shorter than the 5 s poll.** A single tab's cache is *always* expired
  at poll time. The cache saves one client nothing; it only ever helps when clients interleave —
  which is exactly the case that triggers a herd.
- **`cached_state()` has no lock** under `ThreadingHTTPServer`. Three concurrent cold requests
  measured **4.658 s / 4.691 s / 4.718 s** — the 1.6 s collect became 4.7 s because three copies
  of it contended for the same git subprocesses. Adding the phone to an open browser tab does
  not amortise the scan. It multiplies it.

### 1.2 What it costs to be wrong

Separately, and larger than everything above combined:

```python
# orchestra.py:557 — classify_session, first branch
if age_s < working_s:            # CFG["working_s"] = 90
    return "working", False
```

That branch is evaluated **before every piece of hard evidence**. Consequences, measured:

| | today |
|---|---|
| `● WORKING` after an agent stops | held up to **90 s** |
| `▲ NEEDS ANSWER` after the question is written to disk | median **90 s** late |
| `AskUserQuestion`s never displayed at all (human answered inside the window) | **24 of 74 — 32 %** |
| `○ ENDED` / `◇ FREE` after `/exit` | 90 + 4 + 5 + 1.6 ≈ **100.7 s** |

And the clock the window runs on is a lying clock. `age_s` is `now - max(st_mtime, sub_mtime)`.
But `last-prompt`, `ai-title`, `mode`, `permission-mode`, `file-history-snapshot` and
`bridge-session` records carry **no `timestamp`** and are written by the CLI on UI events —
typing into the composer, a file changing, a title being generated. Worst skew observed on this
machine:

```
mtime age    1,779 s      last timestamped entry   219,803 s     (2.5 days)
```

A session whose last genuine activity was two and a half days ago can be the *freshest* session
in its worktree, report `● WORKING`, and steal the live-process pairing from the agent that is
actually running.

Meanwhile the fact that would settle it is already on disk **and already parsed**:
`{"type":"system","subtype":"turn_duration",…}` is the CLI's explicit end-of-turn marker.
`parse_session_tail()` (orchestra.py:507–512) reads that record today and extracts two fields from
it — and never records **that it was last**. Sampled across the 71 in-window transcripts on this
machine, `system/turn_duration` is the terminal non-sidechain entry in **40 of them (56 %)**.
Those sessions are provably not mid-turn, and the classifier guesses from mtime for every one.

### 1.3 The framing: latency, hysteresis, ambiguity

This is the most important paragraph in the document. "Status is laggy" is three different
problems, and every one of them has a different fix. Conflating them is the main way this work
goes wrong.

| class | meaning | example | fixed by |
|---|---|---|---|
| **latency** | the truth changed; we have not looked yet | 4 s cache + 5 s poll + 1.6 s collect | Layers 1–4: cheap collector, background loop, events, push |
| **hysteresis** | we looked; the rule deliberately holds the old value | `age_s < 90 ⇒ working`, evaluated first | Layer 0 + §4: read the evidence that is already on disk, and reorder the ladder |
| **ambiguity** | the signal genuinely cannot distinguish two states | "awaiting a permission prompt" vs "tool still running" — both are an unresolved `tool_use` | Layer 5: a better source (hooks), or process-level evidence (tmux pane) |

**Faster polling does nothing for the second two.** A 100 ms poll of a 90 s window still shows
`● WORKING` for 90 seconds. Equally, reading `turn_duration` perfectly does nothing for the 4 s
cache. The layers below are assigned to classes explicitly, and each layer states which class it
attacks:

| layer | attacks |
|---|---|
| 0 — reorder the classifier | **hysteresis** (the single largest term) |
| 1 — cheap collector | **latency** |
| 2 — background loop + version | **latency**, and makes push possible at all |
| 3 — event-driven invalidation | **latency** (detection), and supplies the honest clock |
| 4 — SSE push + deltas | **latency** (delivery), payload, fan-out |
| 5 — hooks at the source | **ambiguity**, then latency again as a bonus |

---

## 2. Target

### 2.1 Per-signal latency budgets

"Freshness" is not one number. Different facts change at different rates and deserve different
budgets. These are the contracts each layer is measured against.

| signal class | example | changes when | detection budget | staleness tolerance |
|---|---|---|---|---|
| **turn boundary** | `turn_duration`, `AskUserQuestion` written | agent finishes / asks | **≤ 150 ms** | 0 — this *is* the status |
| **transcript append** | assistant text, `tool_result` | agent writes | ≤ 150 ms | 0 |
| **process exit** | `/exit`, crash, terminal closed | process dies | **≤ 1 s** | 0 — gates `○ ENDED` and `◇ FREE` |
| **process appearance** | a new agent launched | exec | ≤ 2 s | 2 s |
| **per-pid immutables** | `CLAUDE_CONFIG_DIR`, host app, tmux pane, cwd | **never**, for a live pid | once per pid | ∞ by construction |
| **git refs** | branch, HEAD oid, ahead/behind | commit, checkout, fetch, push | ≤ 1 s (watched) | 2 s |
| **git dirt** | `dirty` count | any of ~8,000 working-tree files | ≤ 2 s agent-caused, ≤ 10 s human editor | 10 s, **hard-gated to 10 s for dispatch** |
| **topology** | fork points, branch graph | any ref | ≥ 30 s | 30 s |
| **usage limits** | `cclimits` | network refetch | 300 s | 300 s |

### 2.2 End-to-end goals

| path | today | target | after which layer |
|---|---|---|---|
| change → board pixels (typical) | ~6,200 ms | **≤ 350 ms** | 2 |
| change → board pixels (typical) | | **≤ 150 ms** | 3 |
| change → board pixels (p95) | 10,600 ms | **≤ 400 ms** | 3 |
| change → board pixels, **hidden tab** | up to 66,000 ms (browsers clamp `setInterval` to ≥1/min) | **≤ 400 ms** | 2 (long-poll) |
| turn ends → `◆ YOUR TURN` | 100,700 ms | **≤ 400 ms** | 0 + 3 |
| question written → `▲ NEEDS ANSWER` | 90,000 ms median, 32 % never | **≤ 400 ms, always** | 0 + 3 |
| change → phone, app foregrounded, tailnet | — | **≤ 400 ms** direct WireGuard, ≤ 600 ms DERP-relayed | 4 |
| change → **push emitted** (our half) | impossible without a foreground tab | **≤ 200 ms** | 3 + 4 |
| change → phone lock screen | — | 0.6–3 s, **dominated by APNs, not ours** | 4 |
| server CPU, one client watching | ~42 % of a core, multiplied per client | **≤ 8 % of a core, flat in client count** | 2 + 3 |
| server CPU, idle fleet, no client | 0 (nothing runs — which is why push is impossible) | **≤ 1 %** | 2 |

The phone numbers are stated with the tailnet leg separated out because that leg is not ours:
direct WireGuard on-LAN is ~2–10 ms, NAT-traversed WAN ~20–60 ms, DERP-relayed ~60–150 ms. APNs
delivery has no published SLA. **We own the ~200 ms up to the push being emitted. We do not own
what Apple does next, and this document will not pretend otherwise.**

---

## 3. The design, in layers

### Layer 0 — reorder the classifier (ship this first, this afternoon)

**Class: hysteresis. ~20 lines. No new machinery, no transport change, no watcher, no threads.**

This is the single highest-value, lowest-risk change in the whole programme, and it is
deliberately placed before all the architecture. It removes the 90 s term — larger than every
cache TTL and poll interval in the system combined — while every other layer is still unwritten.

Two changes:

1. **The decay window moves from the first branch to the last.** Hard evidence is read before any
   clock.
2. **`turn_duration`-as-last-entry is read as `turn_ended`.** The record is already parsed; it is
   one boolean out of code that already runs.

```python
def classify_session(age_s, alive, pending_tools, delegated, skip_perms,
                     working_s, shells=0, *, turn_ended=False,
                     evidence_age=None, procs_known=True,
                     thinking_s=None, block_grace_s=None, orphan_grace_s=10):
    """Base session status from observable signals.

    ORDER IS THE CONTRACT: nothing may be decided by a clock before the evidence
    on disk has been read. The positional signature is unchanged, and with the
    keyword defaults this function is behaviour-identical to the old one EXCEPT
    that AskUserQuestion and turn_duration are no longer suppressed by the window.
    All ten existing tests in TestClassifySession pass unmodified.
    """
    pend = pending_tools or []
    age = age_s if evidence_age is None else evidence_age
    thinking_s = working_s if thinking_s is None else thinking_s
    block_grace_s = working_s if block_grace_s is None else block_grace_s

    if not procs_known:                      # lsof/ps failed wholesale:
        return "unknown", False              # never claim ENDED, never claim FREE

    if alive and "AskUserQuestion" in pend:  # the question is ON DISK
        return "needs_input", False
    if alive and turn_ended and not delegated and not pend:
        return "waiting", False              # PROVABLY not mid-turn
    if alive and delegated:                  # its own workflows / bg agents
        return "working", False
    if alive and shells:                     # live Bash-tool wrapper shell
        return "working", True
    if alive and pend:
        # "awaiting approval" and "tool still running" are the same bytes on
        # disk. Under --dangerously-skip-permissions there is nothing to
        # approve. Otherwise, hold WORKING until the silence exceeds genuine
        # tool-run silence (block_grace_s = the measured p99 of 4,107 mid-turn
        # gaps = 61.2 s) before calling it BLOCKED.
        if skip_perms or age < block_grace_s:
            return "working", True
        return "blocked", False
    if not alive:
        # A fresh write with no observed process is "we have not seen the
        # process yet" (a just-exec'd agent, or a lagging proc-table read) —
        # NOT "ended". This rule was implicit in the old first branch; it is
        # now named, bounded, and testable.
        return ("working", False) if age < orphan_grace_s else ("ended", False)
    if age < thinking_s:
        return "working", False              # decay, LAST
    return "waiting", False
```

Deployed constants, once §4's evidence clock exists: `thinking_s = 20`, `block_grace_s = 60`,
`orphan_grace_s = 10`. Until then the defaults leave them at `working_s`, so Layer 0 can ship
with the old clock and lose nothing.

**Expected after Layer 0:** `▲ NEEDS ANSWER` and `◆ YOUR TURN` become correct on the *first*
collect that sees them, i.e. within the existing 10.6 s latency stack instead of 90 s behind it.
**Risk:** low; the ladder is pure and unit-tested. **Test:** §8.1.

---

### Layer 1 — make the collector cheap

**Class: latency. ~200 lines. No architectural change. Ships alone.**

Nobody has actually tried making the collector fast. Every cause of the 1641 ms is mundane.

#### 1a. Resolve past the Xcode shim, once

`/usr/bin/git` on macOS is Apple's `xcrun` trampoline. Measured, medians of 7:

| command | `/usr/bin/git` | `/Library/Developer/CommandLineTools/usr/bin/git` |
|---|---|---|
| `status --porcelain=v2 --branch` | 49.6 ms | **36.6 ms** |
| `branch --show-current` | 16.1 ms | **5.9 ms** |
| `log -1 --format=%h` | 20.1 ms | **9.7 ms** |
| `/usr/bin/true` (spawn floor) | 2.7 ms | — |

~13 ms of pure shim tax on every one of the 40 invocations per collect. `shutil.which("git")`
returns `/usr/bin/git` and `realpath` does not follow it — the trampoline is not a symlink, so
candidates must be probed explicitly. On Linux `shutil.which` already returns the real binary and
this is a no-op.

```python
def _resolve_git():
    """~13 ms of xcrun shim tax on every git invocation; 40 invocations per
    collect. Resolve once at startup and VERIFY the candidate runs."""
    cands = []
    if os.environ.get("ORCHESTRA_GIT"):
        cands.append(os.environ["ORCHESTRA_GIT"])
    if sys.platform == "darwin":
        cands += ["/Library/Developer/CommandLineTools/usr/bin/git",
                  "/Applications/Xcode.app/Contents/Developer/usr/bin/git"]
    w = shutil.which("git")
    if w:
        cands.append(w)
    cands.append("git")
    for c in cands:
        try:
            p = subprocess.run([c, "--version"], capture_output=True,
                               text=True, timeout=5)
            if p.returncode == 0 and p.stdout.startswith("git version"):
                return c
        except Exception:
            continue
    return "git"

GIT = _resolve_git()
```

#### 1b. One git process per worktree instead of four, and a timeout that is not a lie

`git --no-optional-locks status --porcelain=v2 --branch` returns branch, oid, upstream,
ahead/behind and the dirty entries in one call. Verified byte-for-byte against `git_info` on all
9 worktrees.

```
# branch.oid 479b1dc202cbb999028f557f808d604fe2ff4aac
# branch.head main                 <- or literally "(detached)"
# branch.upstream origin/main      <- absent when there is no upstream
# branch.ab +0 -0                  <- absent when there is no upstream
? docs/mobile/                     <- one entry line per dirty path
```

**The trap:** `# branch.ab +A -B` is `+ahead -behind`. The v1 call it replaces is
`git rev-list --left-right --count @{u}...HEAD`, where `parts[0]` is the **upstream** side —
i.e. *behind* — and `parts[1]` is *ahead* (orchestra.py:156–160 assigns them in that order). A
naive port silently swaps the two numbers on every card. Get this backwards and nothing errors.

`--no-optional-locks` is mandatory for two reasons, not one. It stops `git status` taking
`<gitdir>/index.lock` out from under a live agent — and it stops `git status` writing the
refreshed index back into the gitdir, which under Layer 3 is a **self-inflicted event loop**:
plain `git status --porcelain` produced 10 gitdir write events over 5 runs; the
`--no-optional-locks --porcelain=v2` form produced 2.

```python
class GitTimeout(Exception): pass
class GitUnavailable(Exception): pass


def run(cmd, cwd=None, timeout=6, strict=False):
    """Signature unchanged for every existing caller. With strict=True a timeout
    RAISES instead of masquerading as (1, "") — today a hung `git status`
    renders as dirty=0 -> availability FREE -> a _pick_defaults dispatch
    target, which is silent corruption, not merely staleness."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except subprocess.TimeoutExpired:
        if strict:
            raise GitTimeout(" ".join(map(str, cmd)))
        return 1, ""
    except Exception:
        if strict:
            raise GitUnavailable(" ".join(map(str, cmd)))
        return 1, ""


_commit_memo, _commit_lock = {}, threading.Lock()


def _commit_for(git_root, oid):
    """git objects are immutable: an oid's {hash, ts, subject} never changes,
    so `git log -1` runs only on an actual new commit — 9 spawns per collect
    becomes ~0."""
    with _commit_lock:
        hit = _commit_memo.get(oid)
    if hit is not None:
        return hit
    rc, log = run([GIT, "log", "-1", "--format=%h%x00%ct%x00%s", oid],
                  cwd=git_root, timeout=6, strict=True)
    if rc != 0 or not log:
        return None
    h, ct, s = (log.split("\x00") + ["", "", ""])[:3]
    c = {"hash": h, "ts": int(ct or 0), "subject": s}
    with _commit_lock:
        if len(_commit_memo) > 4096:
            _commit_memo.clear()
        _commit_memo[oid] = c
    return c


def git_info_v2(git_root):
    """Reproduces git_info's five fields with their exact meanings and types."""
    rc, out = run([GIT, "--no-optional-locks", "status",
                   "--porcelain=v2", "--branch"],
                  cwd=git_root, timeout=6, strict=True)
    if rc != 0:
        raise GitUnavailable(git_root)

    info = {"branch": None, "commit": None, "dirty": 0,
            "ahead": None, "behind": None, "stale": False, "ok": True}
    oid = head = None
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            oid = line[13:].strip()
        elif line.startswith("# branch.head "):
            head = line[14:].strip()
        elif line.startswith("# branch.ab "):
            parts = line[12:].split()              # ["+3", "-1"]
            if len(parts) == 2:
                try:
                    info["ahead"] = int(parts[0])       # "+3" -> 3   AHEAD
                    info["behind"] = -int(parts[1])     # "-1" -> 1   BEHIND
                except ValueError:                      # v1 had these SWAPPED
                    pass
        elif line[:2] in ("1 ", "2 ", "u ", "? "):
            info["dirty"] += 1

    if oid and oid != "(initial)":
        info["commit"] = _commit_for(git_root, oid)
    if head and head != "(detached)":
        info["branch"] = head
    elif info["commit"]:
        # %h obeys the same core.abbrev rules as `rev-parse --short HEAD`, so
        # this reproduces today's string byte for byte rather than guessing oid[:7]
        info["branch"] = "detached@" + info["commit"]["hash"]
    else:
        info["branch"] = "?"
    return info
```

#### 1c. Parallel, with carry-forward instead of silent corruption

Measured across all 13 git worktrees under `~/Downloads`:

```
shim  serial 644.8 ms      shim  parallel 227.7 ms
real  serial 385.1 ms      real  parallel 171.7 ms
```

```python
_git_pool = concurrent.futures.ThreadPoolExecutor(max_workers=8,
                                                  thread_name_prefix="git")
_git_last, _git_last_lock = {}, threading.Lock()


def collect_git(worktrees, deadline_s=4.0):
    """A worktree that times out carries FORWARD its last good values with
    stale=True. It must NEVER degrade to dirty=0 — that reads as availability
    'free' and makes it a dispatch target."""
    roots = list({w["git"] for w in worktrees})
    futs = {_git_pool.submit(_git_task, r): r for r in roots}
    concurrent.futures.wait(futs, timeout=deadline_s)   # stragglers keep running
    out = {}
    for f, r in futs.items():
        try:
            out[r] = f.result(timeout=0)
        except Exception:
            with _git_last_lock:
                prev = _git_last.get(r)
            out[r] = ({**prev, "stale": True, "ok": False} if prev else
                      {"branch": "?", "commit": None, "dirty": None,
                       "ahead": None, "behind": None, "stale": True, "ok": False})
    return out
```

`dirty is None` and `stale is True` both flow into `card_availability`, which returns
`"unknown"`, never `"free"`. **Unknown surfaces as unknown.**

#### 1d. Stop walking 18,029 files to keep 44

Measured:

```
transcripts found + stat'd        698 files    47.9 ms
FULL subagent rglob + stat     18,029 files   192.3 ms   (170 dirs)   <- 70% of scan_sessions
HOT  subagent rglob + stat      2,204 files    23.2 ms   (28 dirs)
cheap stat of all 170 sub-dirs                  2.8 ms
```

**88 % of the deep walk serves transcripts that line 619 discards milliseconds later.** But the
naive fix — pre-gating on the transcript's own mtime — breaks the invariant that a session whose
main transcript is stale but whose *subagents* are live must still count. And gating on
`sub_dir.stat().st_mtime` has a real hole: a directory's mtime changes when an entry is
created/removed/renamed, **not** when a file already inside it is appended to.

So: three lanes, and the correctness guarantee comes from the slow one.

```python
DEEP_EVERY_S = 60.0

class TranscriptCache:
    """Per-transcript memo keyed on (dev, ino, size, mtime_ns). Only files that
    GREW are re-tailed. A file that shrank or changed inode is dropped whole —
    truncation or replacement must never serve a stale `topic`, which is read
    from the first 16 KB and is immutable ONLY under append-only semantics."""

    def probe(self, fp, st):
        key = (st.st_dev, st.st_ino)
        with self.lock:
            c = self.e.get(str(fp))
        if (c and c["key"] == key and c["size"] == st.st_size
                and c["mtime_ns"] == st.st_mtime_ns):
            return c, False
        fresh = {"key": key, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                 "sub_mtime": (c or {}).get("sub_mtime", 0.0),
                 "sub_dir_mtime": (c or {}).get("sub_dir_mtime"),
                 "subagent_said": (c or {}).get("subagent_said")}
        if c and c["key"] == key and st.st_size >= c["size"]:
            fresh["topic"] = c.get("topic")        # append-only: head is frozen
        return fresh, True
```

and in `scan_sessions`, replacing the unconditional `rglob`:

```python
            plausible = (now - st.st_mtime <= window_s
                         or (sub_dir_mtime and now - sub_dir_mtime <= window_s)
                         or now - ent["sub_mtime"] <= window_s)
            # `deep` (every DEEP_EVERY_S) is the correctness argument; this gate
            # is an accelerator. A subagent APPENDING to an existing nested file
            # bumps neither the transcript nor the dir mtime, so worst-case
            # staleness for that one pattern is DEEP_EVERY_S — and the session
            # is never DROPPED, only its sub_mtime is late.
            if has_sub and (deep or (plausible and
                            (changed or sub_dir_mtime != ent["sub_dir_mtime"]))):
                ...rglob...
```

This makes the hot path **O(in-window sessions)** and the reconcile **O(all transcripts) once a
minute** — 192 ms / 60 s = 0.3 % duty. That is the honest reading of the invariant: the hot path
does not grow with history; the reconcile does, and it is off every request path.

#### 1e. Per-pid immutables, keyed on `(pid, start_time)`

Of `claude_processes()`'s four subprocesses, three compute values that **cannot change** for a
live pid: `CLAUDE_CONFIG_DIR` is fixed at exec, the host terminal is fixed at spawn, the tmux
pane is fixed. Measured: `ps -axo` 58.2 ms, `lsof -a -d cwd` 35.9 ms, `ps eww` 19.2 ms,
`tmux list-panes` 13.0 ms.

Memoise on **`(pid, start_time)`**, not bare `pid`. A pid recycled between one collect and the
next must not inherit another agent's worktree attribution — that is a dispatch-safety bug, not a
display bug. `etime` from the same `ps` is the monotonic guard: if `etime` regressed or `cmd`
changed, it is a different process.

Two further rules the memo buys:

- **Sticky cwd.** `wt_claims` records every worktree a pid's cwd has *ever* been under. An agent
  that `cd`s to `/tmp` no longer disappears from `wt_procs` and no longer manufactures `◇ FREE`
  on a worktree it is actively using.
- **`lsof` failure is no longer catastrophic.** Today one `lsof` timeout empties every `cwd`, and
  the whole board reports `○ ENDED` with every worktree `◇ FREE`. With a per-pid memo, a pid we
  already resolved keeps its cwd; a pid we could not resolve gets `cwd: None, cwd_ok: False` and
  publishes `liveness: "unknown"`.

**Linux is untouched.** `_pid_cwds` uses `os.readlink("/proc/<pid>/cwd")` (orchestra.py:171) and
`_pid_config_dirs` reads `/proc/<pid>/environ` (orchestra.py:227) — both subprocess-free and
sub-millisecond, both still selected by `sys.platform.startswith("linux")` exactly as today. The
memo simply has no work to do there.

#### 1f. Projected

| stage | measured now | after Layer 1 | how |
|---|---|---|---|
| `discover_worktrees` | 0.9 ms | 0.9 ms | unchanged |
| `claude_processes` | 126 ms | **58 ms** | one `ps`; lsof/`ps eww`/tmux memoised per `(pid, start)` |
| transcript discovery + stat | 48 ms | 48 ms | unavoidable; 698 files |
| subagent walk | 192 ms | **26 ms** | hot lane + dir probe; full reconcile every 60 s |
| tail parse ×44 | 47 ms | **~5 ms** | only files that grew |
| `session_topic` ×44, `find_last_user` ×13 | ~38 ms | **~0 ms** | memoised; invalidated on truncation/replacement |
| `git_info` ×N | 1277 ms | **172 ms** | real binary, one call, parallel — measured at **13** worktrees |
| assembly / limits / severity | ~5 ms | ~5 ms | unchanged |
| **serial total** | **1641 ms** | **~314 ms** | |
| **git overlapped** (git has zero data dependency) | — | **~230 ms** | pool runs while `ps`/transcripts proceed |
| **hot cycle, git on its own lane** | — | **~142 ms** | git every 4th cycle |

**7.1× on the full collect, 11× on the hot cycle.** Still behind a 4 s cache and a 5 s poll —
Layer 1 alone takes the ~10.6 s worst case to ~9.3 s. It is worth shipping anyway, because
everything above depends on the collector being cheap, and because it fixes two silent-corruption
bugs (`dirty=0` on timeout, `lsof`-fails-so-everything-is-FREE) on its own.

**Risk:** low. Every change is mechanical and differentially testable against the current
implementation. **Test:** §8.2.

---

### Layer 2 — collection off the request path

**Class: latency. ~110 lines. Requires Layer 1 to be worth it.**

`STATE_TTL_S = 4.0` and `setInterval(tick, 5000)` are not policy. They are **symptoms of the
cost** — two sleeps whose only job is to stop a 1.6 s git storm from running every 5 s. Layer 1
kills the cost; the sleeps become unjustifiable.

Exactly one thread ever calls `collect_state()`. Single-flight is **structural, not a mutex** —
a mutex still serialises N cold collects; this runs zero.

```python
class Collector(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="collector")
        self.cv = threading.Condition()
        self.snapshot, self.seq = None, 0
        self.poked = False
        self.demand_t = 0.0
        self.idle_streak = 0

    def poke(self):
        """Every mutation calls this. Replaces `_cache["t"] = 0.0` (orchestra.py
        :1476, :1499) and extends the same guarantee to _run_dispatch (1736),
        /api/send (2263), set_reserve (1110) and _park_on_trunk (1408), which
        have NO invalidation today."""
        with self.cv:
            self.poked = True
            self.cv.notify_all()

    def demand(self):
        self.demand_t = time.time()

    def period(self):
        idle = time.time() - self.demand_t
        if idle < 30:
            self.idle_streak = 0
            return CFG["tick_s"]              # 0.5 — someone is watching
        self.idle_streak += 1
        if self.idle_streak < 2:              # hysteresis: no boundary flap
            return CFG["tick_s"]
        return 2.0 if idle < 900 else 10.0    # push still wants edges at 2 s

    def wait_for(self, since, timeout):
        """Long-poll: park until the snapshot is newer than `since`."""
        end = time.time() + timeout
        with self.cv:
            while self.snapshot is not None and self.seq <= since:
                left = end - time.time()
                if left <= 0:
                    break
                self.cv.wait(left)
            return self.snapshot, self.seq

    def publish(self, snap):
        with self.cv:
            self.snapshot = snap      # atomic reference swap; a published
            self.seq += 1             # snapshot is NEVER mutated in place
            self.cv.notify_all()
```

Three properties fall out, and each closes a documented hole:

- **Snapshots cannot tear.** A snapshot is torn iff it contains two facts that were never
  simultaneously true. Records are immutable once published; assembly grabs one consistent
  generation of every input and then computes with zero I/O; every cross-tier derivation
  (session→proc pairing, `alive`, `status`, `availability`, `severity`, `counts`,
  `free_worktrees`) is recomputed at assembly and never cached. It is structurally impossible to
  publish a session bound to a pid the same snapshot knows is dead.
- **Every published state carries per-signal evidence ages**, so a consumer can tell "no live
  process" from "process table not read in 40 s":

  ```python
  snap["evidence"] = {"procs_ts": …, "cwd_ts": …, "sessions_ts": …,
                      "git_ts": …, "limits_ts": …, "deep_ts": …}
  snap["collector"] = {"ok": True, "collect_ms": 142, "period_s": 0.5,
                       "deep": False, "git_fresh": True}
  ```
  Per-worktree, `git.stale` marks carried-forward values. Per-session, `liveness` is
  `"observed"` or `"unknown"`.
- **`⛔ LIMIT HIT` becomes reachable on a cold server.** The collector refreshes `_limits` on its
  own slow lane every `LIMITS_TTL_S`. Today `_limits["data"]` is populated **only** by the
  browser's `primeLimits()`, so a limit-stranded agent is structurally invisible to any client
  that does not poll `/api/limits` — including the phone. `limits_by_account()` stays pure over
  `_limits["data"]`, and the *request* path still never fetches.

`/api/state` keeps its exact JSON shape (including the `resumes` ride-along) and gains long-poll:

```python
if self.path.startswith("/api/state"):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
    since = int((q.get("since") or ["-1"])[0] or -1)
    wait = min(float((q.get("wait") or ["0"])[0] or 0), 30.0)   # always a real 200
    COLLECTOR.demand()
    if wait > 0:
        snap, seq = COLLECTOR.wait_for(since, wait)
    else:
        snap, seq = cached_state(), COLLECTOR.seq
    body = json.dumps({**snap, "seq": seq, "resumes": resume_public()}).encode()
    if len(body) > 4096 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
        body = gzip.compress(body, 6)          # 35 KB -> ~6 KB, matters on cellular
```

`wait` is clamped to 30 s so a parked request always returns a real 200 and never a 504 —
`tick()`'s existing error path must not be spuriously triggered. `/api/state` with no query
string behaves exactly as today.

**A fallback matters for the test suite.** `tests/` imports `orchestra.py` via `importlib` and
never runs `main()`, so no collector thread exists there. `cached_state()` keeps its old
TTL+lock path behind `if COLLECTOR is None or not COLLECTOR.is_alive():`, and the seven
`fb._cache["state"] = None` sites in `test_integration.py` keep working unchanged.

**Expected after Layer 2:** detect mean 250 ms / worst 500 ms, collect ~142 ms typical, serve
~5 ms from a pre-built snapshot, client wait ~0 (the request is already parked). **Browser total
~330 ms typical, ~700 ms worst** — from 10,600 ms. Hidden tabs are fixed too: a parked request is
not a timer, so browsers do not clamp it. Server CPU becomes flat in client count.

**Risk:** medium — thread lifecycle, and the interaction with the test suite's import-without-main
pattern. **Test:** §8.3.

---

### Layer 3 — event-driven invalidation

**Class: latency (detection). ~250 lines. Optional accelerator; Layer 2 is complete without it.**

Layer 2 asks "did anything change?" twice a second. Layer 3 is *told*, in a fraction of a
millisecond, and — more importantly — is told **at the instant the bytes land**, which is what
supplies §4's honest clock.

Measured on this machine, not recalled:

```
kqueue file append -> event          0.17 ms p50, 0.23 ms max (30 cross-thread appends)
kqueue dir child-create -> event     3.4 ms
kqueue dir child MODIFY              NO EVENT AT ALL          <- the one real limitation
EVFILT_PROC | NOTE_EXIT on non-child, same-uid claude pids    40/40 armed, no privileges
363-watch registration               7.7 ms
```

**Correcting a claim that gets made against this design:** there is no fd crisis.
`kern.maxfilesperproc = 61,440`, `RLIMIT_NOFILE` soft = **1,048,576**, and the in-window watch
set is **366 objects** (295 project dirs + 71 transcripts), not thousands. 2,000 `O_EVTONLY` fds
open in 161 ms. Any argument that rejects kqueue on fd grounds is wrong.
`os.O_EVTONLY` **is** exposed by Python (32768, since 3.10); the `getattr(os, "O_EVTONLY", 0x8000)`
fallback is belt-and-braces, not a workaround. `select.KQ_FILTER_USER` and `KQ_NOTE_TRIGGER` are
genuinely **absent** from Python's `select`, so cross-thread wakeup uses an `os.pipe()`
registered with `KQ_FILTER_READ`.

#### The watch plan

| # | object | count now | fires when | triggers |
|---|---|---|---|---|
| 1 | each `CFG["roots"]` dir | 1 | worktree dir created/removed | `discover_worktrees()` (0.9 ms) + reconcile |
| 2 | each `<home>/projects/` | 8 | new project dir | rescan that home |
| 3 | each matched `<home>/projects/<munged>/` | 295 | a new `*.jsonl` appears | re-`glob` that one dir |
| 4 | each **in-window** transcript | 71 | bytes appended / truncated / replaced | `cursor.poll()` on that one file |
| 5 | each in-window `<sid>/` subagent dir | 28 | subagent transcript created | mark subagent activity |
| 6 | each distinct `--git-common-dir` + `refs/heads`, `refs/remotes/origin` | 3 × 3 | commit / fetch / push / checkout | git recompute for every worktree sharing it |
| 7 | each worktree root (top level only) | 13 | top-level file created/deleted | git recompute (dirty) |
| 8 | each live `claude` pid | 5 | **process exit** | drop it instantly; recompute that worktree |

Deliberately **not** watched: the ~8,000 files inside each worktree, and the ~18,000 subagent
transcripts belonging to out-of-window sessions.

Seven of the nine worktrees on this machine share one `--git-common-dir`
(`/Users/achill/Downloads/ConfidAI/.git`), so the git watch set collapses to 3 distinct gitdirs.

#### The honest hole: `git status` dirty

kqueue fires on a directory when an entry is added, removed or renamed. It does **not** fire when
a file already inside it is modified in place. An agent's `Edit` rewriting `src/foo.ts` touches
nothing we watch. Three triggers cover it, in descending precision:

1. **The transcript event is the proxy.** An agent that edits a file writes a `tool_result`
   within milliseconds. That event fires, and the dispatcher adds that session's worktree to
   `_dirty_git`. **Agent-caused dirt is detected in ~40 ms** — the case the board exists for.
2. **The gitdir and refs watches** catch add / commit / checkout / fetch / merge exactly.
3. **A staggered slow lane** for human edits in an editor: any worktree with a live `claude`
   process gets a git refresh at most every `GIT_MAX_S = 10 s` if nothing triggered one. Thirteen
   worktrees staggered over 10 s is one 44 ms call every 0.8 s ≈ 5 % of a core, against today's
   1277 ms every 4 s ≈ 32 %.

Editors that save atomically (write-temp + rename — vim, VS Code by default) *do* fire the
worktree-root watch and land in tier 1. So `dirty` is event-exact for agent activity, ≤10 s for
in-place human editor saves, and `stale`/`ok` are published per worktree. That is a conscious
trade, stated as one. `dirty` participates in **no** status decision; the one place it feeds a
*decision* — `_pick_defaults` sorting free worktrees by `dirty` — is hard-gated: any candidate
whose `dirty` was observed more than 10 s ago forces a synchronous single-worktree probe (~40 ms)
before it can be dispatched into.

#### The dispatcher, and the bug not to ship

```python
DEBOUNCE_S   = 0.100      # coalesce the turn-end write cluster
GIT_MIN_S    = 0.400      # per-worktree floor on git re-runs
PROC_MIN_S   = 1.000      # per-`ps` floor
RECONCILE_S  = 1.000      # watch-set self-heal + stat sweep  (see below)
AUDIT_S      = 60.0       # deep drift check

def _apply_loop(self):
    while not self._stop.is_set():
        now = time.time()
        if now < self._deadline:
            # WAIT OUT THE REMAINING DEBOUNCE. Waiting a fixed 0.25 s here with
            # the event already cleared turns a claimed 60 ms design into a
            # measured 255 ms one. This line is the whole difference.
            self._wakeup.wait(max(0.005, self._deadline - now))
            continue
        ...drain dirty sets, recompute only what changed, publish...
        self._wakeup.wait(0.25)
        self._wakeup.clear()
```

`DEBOUNCE_S = 0.100` is a **correctness** device, not a performance one. At a turn boundary
Claude Code writes `assistant` → `system/turn_duration` → `last-prompt` → `ai-title` →
`permission-mode` within ~50 ms. Publishing each would flash the board through
`working → waiting → working` and could ring the bell on an intermediate view. One coalesced
publish per burst is both cheaper and more correct.

#### The safety net, which is what makes this shippable

`RECONCILE_S = 1.0`: every second, diff the desired watch plan against the live watch set, and
`os.stat` the whole set to catch anything that moved without an event. Measured: **0.55 ms p50 /
0.78 ms max** to sweep the real 366-object set; 1.98 ms for a 996-object set. At a 1 s interval
that is **0.055 % of one core**.

**If every watch silently failed, the system degrades to a 1 s poll — five times better than
Layer 2's hot cadence and never worse than today.** That single property is why this layer is
safe to ship: its worst case is better than its predecessor's best case.

A second, orthogonal detector runs alongside: a **liveness canary** — if the watcher reports zero
events for 120 s while `ps` shows live `claude` processes writing, self-demote to polling, set
`signals.sessions.healthy = false`, and surface `source: "poll1s"` in the API. Degradation is
visible, never silent.

| situation | detection | response |
|---|---|---|
| transcript replaced by rename | `NOTE_DELETE` on our fd | unwatch → re-watch by path; cursor sees a new inode and re-parses from 0 |
| transcript truncated in place | `WRITE` with `st_size < off` | cursor re-parses from 0; `session_topic` memo invalidated on the same condition |
| new session appears | project-dir `WRITE` | re-`glob` that dir, attach cursor, watch |
| fd exhaustion / `ENOSPC` | `watch()` returns False | path enters `DEGRADED`, covered by the 1 s sweep, one warning logged |
| inotify queue overflow | `IN_Q_OVERFLOW` | full resync: reseed every cursor from its 128 KB tail (53 ms) |
| we simply missed something | — | `RECONCILE_S` 1 s sweep; `AUDIT_S` 60 s full git + `ps` comparison, logged |

#### Linux, honestly

Python's stdlib has **no inotify binding** (`importlib.util.find_spec("inotify")` → `None`).
`ctypes` *is* stdlib, so a ctypes wrapper does not break zero-dependency in the packaging sense —
but it reaches past the Python ABI to the libc ABI: three symbols (`inotify_init1`,
`inotify_add_watch`, `inotify_rm_watch`) and one 16-byte struct (`struct.Struct("iIII")`). That
is a different *kind* of risk from `import json` and is stated as one.

Linux is better than macOS in two respects and worse in none:

- inotify events **carry the filename**, so the fd→path table kqueue forces on us is unnecessary.
- a directory watch fires **`IN_MODIFY` for content changes to files inside it**, so one watch per
  project dir covers every transcript in it. The watch count drops from 366 to ~300.

Process exit uses **`os.pidfd_open`**, which is plain stdlib on CPython ≥ 3.9 / Linux ≥ 5.3 — a
pidfd becomes readable in `selectors` exactly when the process exits. Probe with
`hasattr(os, "pidfd_open")`. Do **not** reach for `ctypes` + `syscall(434, …)`: the syscall
number is architecture-specific and the stdlib call already exists. Fallback for older kernels is
`os.kill(pid, 0)` on the 1 s lane.

Limits: `max_user_watches` is 8,192 on old kernels and **65,536–524,288 on current distros**; we
need ~300. `ENOSPC` puts the path in `DEGRADED`.

The whole ctypes path is wrapped in `try/except`, logs once, and returns `PollWatcher`. Set
`ORCHESTRA_WATCH=poll` on any platform to force it. **That is also what CI runs**, so the
event-driven code path is testable without a Linux box.

**Expected after Layer 3:**

| term | ms |
|---|---|
| write → kevent | 0.2 |
| `DEBOUNCE_S` coalesce | 100 |
| `cursor.poll()` on the one changed file | 0.5 |
| card re-assembly + severity + counts (pure python, zero syscalls) | 3 |
| serialize + long-poll wake + loopback | 2 |
| browser rAF + `render()` | 16 |
| **total** | **~122 ms p50, ~250 ms p95** |

Process exit — the `○ ENDED` / `◇ FREE` transition that costs 100.7 s today — lands in ~6 ms plus
the debounce.

**Risk:** the highest of any layer, and the failure mode is silent (a missed re-watch after an
atomic rename leaves an fd on a dead inode, and a session goes dark in a way that looks exactly
like healthy quiet). The 1 s reconcile and the liveness canary exist entirely to make that
failure loud. **Test:** §8.4.

---

### Layer 4 — push instead of poll

**Class: latency (delivery), payload, fan-out. ~130 lines server, ~90 client.**

With Layer 2's long-poll the client is already parked, so Layer 4 does **not** buy much raw
latency for a single browser tab. What it buys is real anyway:

- **Payload.** `/api/state` is 34,976 B, of which `worktrees` is 94.5 %. Per session,
  `last_assistant` is 258 B, `topic` 147 B, `cwd` 92 B — prose that rarely changes — while the
  fields that actually change each tick are `age_s` (2 B) and `status` (9 B). ~35 KB every 5 s to
  transmit ~11 B of genuine delta per session. A delta frame is ~200 B–2 KB. On cellular this is
  the difference between a usable phone client and a battery complaint.
- **Fan-out.** One publish writes to N queues instead of N clients each re-requesting.
- **The push pipeline.** Server-side edge detection has to exist for APNs anyway (§6); SSE is
  where it is delivered to anything that is watching.

Verified on a real `ThreadingHTTPServer` with 12 concurrent subscribers: 14 threads alive,
0.45–0.68 ms broadcast latency, an ordinary GET served in 21.2 ms while all 12 streams were held
open, and **0 subscribers / 2 threads after 12 rude disconnects** — fully reclaimed. Two
mitigations are mandatory: override `handle_error()` (a dropped SSE client otherwise prints a
traceback per tailnet blip) and impose a hard subscriber cap.

`protocol_version` is unset, so `BaseHTTPRequestHandler` speaks HTTP/1.0 and the stream is
terminated by close — which forbids `Content-Length` and is exactly what SSE wants. The route
must early-return before `do_GET`'s unconditional `Content-Length` tail (orchestra.py:2240–2245).
`wbufsize = 0` is confirmed, so `flush()` is belt-and-braces.

#### The frame format

```jsonc
{
  "v": 1,
  "seq": 1284,                       // monotonic; a gap forces a snapshot refetch
  "generated_at": 1753113600.412,    // absolute epoch, always
  "complete": false,                 // true only for a full snapshot frame
  "order": ["api", "web", "docs"],   // server owns severity(); the client NEVER sorts
  "counts": {"working": 3, "needs_input": 1, "blocked": 0,
             "waiting": 2, "limit": 0, "ended": 4},
  "free_worktrees": ["docs"],
  "worktrees": [ { /* full card, only for worktrees that changed */ } ],
  "resumes": { /* ride-along, unchanged */ },
  "signals": {
    "procs": {"at": 1753113600.1, "ok": true, "source": "ps"},
    "sessions": {"at": 1753113600.4, "ok": true, "source": "kqueue"},
    "git": {"api": {"at": 1753113598.9, "ok": true, "stale": false}}
  },
  "transitions": [
    {"kind": "attention", "wt": "api", "sid": "9b8ef2d1-…",
     "from": "working", "to": "needs_input", "at": 1753113600.38}
  ],
  "digest": "3f9c…"                  // every 30th frame only; see below
}
```

Rules, each of which exists because something breaks without it:

- **`order`, `counts`, `free_worktrees`, `generated_at` ship on EVERY frame** (166 B total). The
  client contains no triage sort and does not know the handed-off exclusion rule
  (orchestra.py:799–800). `severity()` must never be reimplemented in JS — `index.html` already
  flags its one existing fork (`ruleBasedAutoPick`, L935: *"mirror the backend rule exactly"*)
  and this design adds no second.
- **`transitions` is server-computed.** The bell stops being `attn > lastAttn` arithmetic on the
  client. That is what makes the false-bell class structurally impossible and what makes the
  edge push-able from a server with no browser attached.
- **`digest` every 30th frame** is a hash of the canonical per-session status tuple
  `(sid, status, why, evidence_at, pid_certain)`. On mismatch the client resyncs. Silent
  delta/full divergence is the one failure mode with no observable symptom otherwise, so it gets
  a detector rather than an argument that the code is right.
- **Backpressure:** `queue.Queue(maxsize=64)`, non-blocking put. On overflow, drain the queue and
  replace it with a single `{"resync": true}` marker — a slow phone gets a coherent snapshot, not
  an incoherent partial history.
- **`mode=digest`** projection for the phone: `sid`, `status`, `why`, `evidence_at`,
  `pid_certain`, plus the globals. ~1 KB instead of 35 KB.

#### Client transport

```js
let seq = 0, renderPending = false;

const es = new EventSource("/api/stream");
es.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.resync || (seq && m.seq !== seq + 1)) return resync();      // gap -> snapshot
  seq = m.seq;
  if (lastState && m.generated_at < lastState.generated_at) return; // never regress
  applyDelta(lastState, m);          // patches lastState ONLY — never the DOM
  for (const t of m.transitions || []) maybeBell(t);
  scheduleRender();
};
es.onerror = () => { $("sync").classList.add("err"); resync(); };

function scheduleRender() {          // at most one render per animation frame
  if (renderPending) return;
  renderPending = true;
  requestAnimationFrame(() => { renderPending = false; render(lastState);
                                refreshMissionPickers(); });
}
```

Long-poll stays as the non-EventSource path and as the resync path — for `curl`, for the phone
when a stream is not warranted, and for anyone beyond `MAX_STREAMS`.

**Expected after Layer 4:** browser unchanged from Layer 3 (~122 ms p50) but at ~2 KB instead of
35 KB per update; phone foregrounded 130–270 ms; push emitted within ~120 ms of the byte landing.

**Risk:** medium, concentrated entirely in the client. §5 is the containment. **Test:** §8.5.

---

### Layer 5 — better signals at the source

**Class: ambiguity. ~350 lines. Opportunistic; degrades to Layers 0–4 with no visible seam.**

Everything above is orchestra reverse-engineering agent state from mtimes and process tables. Two
statuses cannot be recovered that way at any speed, and the README already concedes it:

- **`■ BLOCKED` vs a long tool run.** Both are an unresolved `tool_use`. Permission prompts are
  not written to the transcript at all.
- **`◆ YOUR TURN`.** Pure fallthrough — "alive and nothing else matched".

Claude Code will simply tell us. Verified against the running binary
(`~/.local/share/claude/versions/2.1.216`) and a live headless agent:

- 30 hook events exist in the binary's `hook_event_name` enum; we need nine.
- A live capture of `claude -p` produced 12 hook invocations. The `Stop` payload carries
  `session_id`, `transcript_path`, `cwd`, `permission_mode`, `stop_hook_active`,
  `last_assistant_message`, `background_tasks`, `session_crons`.
- `claude --help`: `--settings <file-or-json>  Path to a settings JSON file … to load
  **additional** settings from` — it is **additive**.

One 400-byte `Stop` payload replaces, exactly: `lsof` (31.6 ms), `ps eww` (20.0 ms), the 128 KB
tail parse (47 ms), the per-worktree `skip_perms` guess (which is wrong-grained —
`all(procs)` for the worktree, so one `--dangerously-skip-permissions` agent beside one
interactive agent makes both read as never-blocked), the `pair_sessions_with_procs` cardinality
guess, and the stale-`delegated` bug — because `background_tasks` arrives attached to **this**
turn rather than scraped from whichever `turn_duration` survived the 128 KB tail.

#### Adoption: exactly one tier, and it needs no consent

**Tier 0 — orchestra-dispatched agents. Zero config, 100 % coverage, day one.**

```python
shell_cmd = (f"CLAUDE_CONFIG_DIR={shlex.quote(str(home))} "
             f"exec claude --dangerously-skip-permissions{model_flag}"
             f"{signal_flag()}")     # -> " --settings ~/.orchestra/hooks.json"
```

`--settings` is additive, so the user's own `settings.json`, hooks and `statusLine` are never
opened, never parsed, never rewritten. Every agent orchestra launches is hooked from its first
millisecond, and the `~30 s` blindness the UI currently apologises for in prose
(`index.html:1132`) collapses to one frame after `SessionStart`.

**Tier 1 — writing into the user's `~/.claude/settings.json` is explicitly NOT part of this
design.** That file on this machine holds 16 top-level keys including `permissions`,
`enabledPlugins`, `env`, `statusLine` → `gsd-statusline.js`, and two pre-existing hooks; and
`claude --help` states that *settings files that fail validation are silently ignored*. A bug
there silently bricks the user's configuration across eight homes. The value does not justify the
blast radius when Tier 0 already covers the fleet the board exists to watch. If it is ever
revisited it needs its own ADR, an atomic write, a sentinel-scoped merge, a backup, and a
self-check that pipes a synthetic payload through the real script and demands a 204.

#### The transport, and its real tail

```sh
#!/bin/sh
# orchestra-signal v1. Never write to stdout (the CLI parses hook stdout as
# control JSON and a stray token can BLOCK the turn). Never exit non-zero.
LC_ALL=C; export LC_ALL      # ${#b} must be BYTES: verified 6 vs 9 for multibyte
b=$(cat) || exit 0; [ -n "$b" ] || exit 0
...POST to 127.0.0.1:4242/api/agent/event?pid=$PPID via /dev/tcp, else curl,
   else append to ~/.orchestra/spool.jsonl...
```

Measured over 40 runs: **p50 8.7 ms, p90 10.2 ms — and max 446.6 ms at load average 6.7.** The
tail is `fork`/`exec` scheduler-bound, not network-bound, and the dev box routinely sits at load
26. So the honest claim is "usually ~10 ms, occasionally half a second", not "27 ms". That is
still excellent, and it is why hooks are an **upgrade layered on top of** Layer 3's evidence
rather than a replacement for it: if the hook is late, kqueue already saw the write.

Three caveats stated up front:

- `$PPID` inside the hook command is the `claude` process **only** because the shell `exec`s a
  single simple command. Add a redirect, a pipe, or a second statement and `$PPID` becomes the
  wrapper shell. Any pid learned this way is re-validated against the live `ps` table
  (`cmd == "claude" or cmd.startswith("claude ")`) before it is ever used.
- `claude --bare` is documented as *"Minimal mode: skip hooks"*. An agent launched that way
  silently reverts to inference. The design must treat hook absence as normal, not as an error.
- `PermissionRequest` and `Notification` could not be triggered headlessly (`claude -p`
  auto-denies rather than prompting). Both names are in the binary's enum and the notification
  literals (`agent_needs_input`, `worker_permission_prompt`, `idle_prompt`, `agent_completed`)
  are present, but their payload shapes are unverified. **Nothing load-bearing depends on them:**
  `■ BLOCKED` is built on `PreToolUse`-without-`PostToolUse` plus per-session `permission_mode`,
  both proven.

#### Ingest, and the safety rule

`POST /api/agent/event` reads ≤64 KB, `json.loads`, folds one event into one dict, publishes.
No disk, no subprocess, and — deliberately — **no call to `collect_state()`**: a `Stop` cannot
change a git branch.

**This handler has no reachable path to `send_to_process`, `_run_dispatch`, `deliver_text` or
`start_finish`.** A hook event can change what the board *says*; it can never make orchestra
*act*. That is the "only an explicit user request may act on an agent" invariant, enforced
structurally.

#### The reconciliation rank

```
hooks (observed) > process table > precise file writes (kqueue) > entry timestamps > mtime
```

A session with a live hook lease publishes `confidence: "observed"` and `why: "hook:Stop"`. When
no hook event has arrived for `HOOK_LEASE_S = 90` s — and `Stop`/`PostToolUse` are frequent
enough that 90 s of total silence means the wire broke, not that the agent went quiet — the
session drops back to `confidence: "inferred"` and the Layer 0–3 ladder. **Zero hooks installed
is exactly Layers 0–4, which is a complete system.**

#### The thing only hooks can do

`UserPromptSubmit` fires ~10 ms after the CLI accepts text typed at an agent. That makes an
**action's own success observable**, which is a new capability rather than a latency improvement.
`▶ resume` (index.html:764–775) today has no refresh, no disable and no pending state: the button
stays armed and re-clickable while the row still reads `⛔ LIMIT HIT` — a double-send hazard that
types `continue` twice into one terminal. With hooks it disables on click and re-enables on the
observed `UserPromptSubmit` for that `sid`, or a 3 s timeout marks it failed. Without hooks, the
timeout path alone is still a strict improvement and ships in Layer 4.

**Risk:** the payload schema is undocumented and unversioned inside a binary that ships weekly.
Mitigated by the rank above: a renamed key degrades a session to inference and logs once. It must
never degrade to a *wrong* status. **Test:** §8.6.

---

## 4. The status model, revisited

### 4.1 What the 90 s window was actually covering

`working_s = 90` is a p99.5 cover on **genuine mid-turn write silence**. Claude Code appends to
a transcript only at completion boundaries: an assistant message is serialised after streaming
finishes, a `tool_result` after the tool returns. During model thinking, during a long single
tool call, and for the whole life of a backgrounded `Bash`, **zero bytes are written**. That
silence is real. No watcher, no matter how fast, can see into it.

Measured across 48 h of real transcripts, 4,107 mid-turn inter-entry gaps:

```
p50 1.10 s   p75 4.07 s   p90 9.96 s   p95 18.92 s   p99 61.22 s   p99.9 181.8 s   max 480.3 s
>5 s 20.65 %   >10 s 9.96 %   >30 s 2.19 %   >60 s 1.05 %   >90 s 0.54 %
```

**A naive 5 s window converts one in five mid-turn intervals into a false `▲`/`■`/`◆`, rings the
bell, and re-sorts the board. That is strictly worse than the current complaint.** This is the
single most important constraint in this section.

So the window decomposes into two parts, and only one of them is removable:

| part | size | removable? | why |
|---|---|---|---|
| poll granularity cover | ~10 s | **yes** | 4 s cache + 5 s poll + 1.6 s collect. Layers 1–4 delete it. |
| genuine silent-thinking cover | ~80 s | **no** | the agent really is producing no bytes |

And crucially, the window is applied to **100 %** of gaps when it is only *needed* for some.
Re-deriving the whole gap distribution split by **the evidence available at the start of each
gap** — exactly what an evidence-first classifier knows (n = 31,696):

| bucket | share | gap distribution | needs a timer? |
|---|---|---|---|
| **A** — unresolved `tool_use` | 26.7 % | p50 0.18 s, p99 128 s, max 4,749 s | **No.** Positive evidence holds `working` indefinitely — correct even at 4,749 s, where today's 90 s window is already wrong. |
| **B** — `turn_duration` seen, 0 pending | 2.3 % | p50 59 s, p90 887 s | **No.** Provably the user's turn. Report it immediately; today it is 90 s late. |
| **C** — thinking (neither) | 71.0 % | p50 2.37 s, p95 33 s, p99 163 s | **Yes** — and this is the *only* place a window survives. |

In bucket C nothing is being suppressed: `pending` is empty, no turn marker has been written, and
the only competing verdict is `waiting`. The window's entire job there is *"how long do we say
WORKING before falling back to YOUR TURN."*

### 4.2 The clock

`age_s` stops being `now - st_mtime`. It becomes the **newest `timestamp` on a real
(`assistant` / `user` / `tool_result` / `system`) entry**, or — under Layer 3 — the wall clock at
which the kernel woke us for a write, whichever is later. That second clock cannot be forged,
because we were woken by the write itself.

`mode`, `permission-mode`, `last-prompt`, `ai-title`, `file-history-snapshot` and
`bridge-session` return `False` from the cursor's `_apply` and cannot advance liveness. The
2.5-day session with a 1,779 s mtime becomes impossible to report as `● WORKING` under any code
path.

Two more bugs die in the same parser, and both are structural rather than window-related:

- **Sticky `pending_tools`.** An interrupted turn writes a plain `user` entry
  `[Request interrupted by user]` and **no `tool_result`** (35 occurrences in 7 days, zero with a
  matching `tool_result`). Today the `tool_use` is never popped, so the session reads `■ BLOCKED`
  until it scrolls out of the 128 KB tail — indefinitely. Expire a `tool_use` on: a matching
  `tool_result`, the interrupt marker, a later `turn_duration`, or a later real user prompt.
- **Stale `delegated`.** `parse_session_tail` keeps the *last* `turn_duration` in the tail, which
  describes the **previous** turn if the current one has not ended. A session that ended turn N
  awaiting one workflow and then began turn N+1 carries `delegated=1` forever and is pinned
  `● WORKING`. `turn_over` and `delegated` are reset to `False`/`0` by any later
  `assistant` / `tool_result` / human prompt — derived from *position in the stream*, not from
  "the last one anywhere in the tail".

### 4.3 The anti-flicker rule

Under polling, a wrong de-escalation costs 4 + 5 + 1.6 ≈ 9 s to reverse, so you are forced to be
conservative and pick 90. Under Layers 2–4 the correction lands ~120 ms after the agent's next
byte. **That asymmetry is what licenses a short window — but only for the display, never for the
notification.** Measured, for bucket C:

| `thinking_s` | transient false "YOUR TURN" | median time to correction | would ring the bell (>45 s settle) |
|---|---|---|---|
| 10 s | 17.11 % | 9.8 s | 2.61 % |
| **20 s** | **8.49 %** | **16.3 s** | **2.10 %** |
| 45 s | 3.36 % | 34.1 s | 1.46 % |
| 90 s | 1.46 % | 131.4 s | 1.00 % |

Five rules. All five are required; any four of them flicker.

**R1 — Escalation is immediate; de-escalation is guarded.** Any move *toward* attention
(`working → needs_input`, `working → blocked`, `waiting → needs_input`) fires on the frame the
evidence lands. Any move *away from* activity (`working → waiting`) is subject to R2 and R3.

**R2 — Bucket C gets a `thinking` presentation, not a status flip.** The wire vocabulary does
**not** change; a new presentation field does.

```
t = 0                      last real entry
0 .. thinking_s (20 s)     status "working",  phase "active"
20 .. notify_settle_s      status "waiting",  phase "settling",  provisional: true
> 45 s                     status "waiting",  phase null,        provisional: false
```

A `provisional` session is rendered `◆ YOUR TURN` **dimmed, annotated `quiet 32s`**, and is
**excluded from `counts`**, from `attn`, from `severity()`'s attention rank, and from
`transitions`. It cannot ring the bell, cannot re-sort the board to the top, and cannot fire a
push. The user sees the truth ("the agent has gone quiet; it may be thinking") without the board
committing to it.

**R3 — `notify_settle_s = 45`.** A session must sit in `waiting` for 45 s of continuous silence
before it becomes notification-worthy. This cuts bell misfires to 2.10 % of thinking gaps.
Compare today's failure, which runs the other way and is worse: `working` suppresses
`needs_input`, so **32 % of `AskUserQuestion`s are never announced at all**.

**R4 — Minimum dwell.** A status must hold for `MIN_DWELL_S = 2` s before a *contradicting*
status may replace it — **unless** the new status is backed by hard evidence
(`AskUserQuestion`, `turn_duration`, `PermissionRequest`, process exit), which always wins
immediately. This kills sub-second oscillation without ever delaying something real.

**R5 — Coalesce the burst.** `DEBOUNCE_S = 100 ms` (§Layer 3) makes the turn-end write cluster
atomic, so the board never renders an intermediate view of it.

Optional exactness, affordable **only** because it is event-scheduled: `composer_idle()`
(orchestra.py:1697) already parses `"esc to interrupt"` out of `tmux capture-pane` — ground truth
for mid-turn. Fire it **once**, at the instant `thinking_s` expires, for tmux-hosted sessions
only. Bucket C collapses to zero false flips for every dispatched fleet agent, at a few calls per
minute rather than a per-tick cost. `tmux list-panes -a` measures 6.6–13.0 ms.

### 4.4 Every status, before and after

| status | today | after | evidence |
|---|---|---|---|
| `● WORKING` | **inferred** — mtime proxy, and mtime includes non-activity writes | **observed** in buckets A/B; **inferred, bounded, and labelled** in C | unresolved `tool_use`, live wrapper shell, pending workflows/bg agents, or `< thinking_s` since a real entry |
| `▲ NEEDS ANSWER` | observed but **suppressed 90 s**; 32 % never shown | **observed, ≤400 ms, always shown** | `AskUserQuestion` `tool_use` with no `tool_result`; or `Notification(agent_needs_input)` |
| `■ BLOCKED` | **inferred**; conflates "awaiting approval" with "tool running" | **inferred with a p99-calibrated grace**, then **observed** with hooks | unresolved `tool_use`, no `skip_perms`, silence > `block_grace_s = 60`; or `PermissionRequest` |
| `◆ YOUR TURN` | **inferred** — pure fallthrough | **observed** in bucket B; **provisional** in C | `turn_duration` with 0 pending; or `Stop{background_tasks: []}` |
| `⛔ LIMIT HIT` | half-observed; **unreachable on a cold server** | **observed, reachable without `/api/limits`** | `cclimits` on the collector's slow lane; `isApiErrorMessage` (24 in 3 days, never read today); the existing regex |
| `○ ENDED` | observed, modulo total `lsof` failure | **observed**, with `unknown` when the proc read fails | `NOTE_EXIT` / pidfd, plus `orphan_grace_s` |
| `◇ FREE` | half-observed; cwd drift manufactures FREE, and FREE gates dispatch | **conservative** | no live proc that is *or ever was* under the worktree, no `working` session, and both the proc and git signals healthy |
| `unknown` | does not exist | **new** | any signal the board depends on could not be read |

`block_grace_s = 60` is the measured p99 of mid-turn gaps (61.2 s), deliberately chosen so a
legitimate long tool run under approvals does not read `■ BLOCKED` and ring the bell. Setting it
to zero — "an unresolved `tool_use` under approvals *is* blocked" — would fire on 2.19 % of all
mid-turn gaps, audibly. Today's 90 s window suppresses that by accident; the replacement must
suppress it on purpose.

Equally, **`working` must never be unbounded.** A tri-state turn tracker
(`TURN_OPEN` / `TURN_ENDED` / `TURN_UNKNOWN`) is the right *signal*, but treating `TURN_OPEN` as
an unbounded licence to report `● WORKING` is a regression, not a tightening: 11 of the 71
in-window transcripts on this machine currently end on an `assistant` entry, and 9 of those have
last-real-activity ages between 15 hours and 2 days. `TURN_OPEN` suppresses de-escalation; the
honest-clock fallback still caps it.

### 4.5 What every status now ships

```jsonc
{
  "status": "waiting",
  "phase": "settling",              // "active" | "settling" | null
  "provisional": true,              // excluded from counts / attn / transitions
  "why": "turn_duration, 0 pending",
  "confidence": "observed",         // "observed" | "inferred"
  "evidence_at": 1753113568.2,      // ABSOLUTE epoch; the client owns elapsed time
  "evidence_source": "transcript",  // "hook" | "proc" | "transcript" | "mtime"
  "liveness": "observed"            // "observed" | "unknown"
}
```

`why` is not decoration. It is what converts *"a status must never claim more certainty than the
evidence supports"* from an aspiration into a testable assertion, and it is the surface that lets
a wrong status be diagnosed instead of argued about.

---

## 5. Render invariants

`index.html` encodes rules bought with real bugs — commits `0bba570` (*"the tick refreshes data
under controls, never the controls themselves"*), `479b1dc` (*"the board never re-sorts under
your cursor"*), `2614afb`, `ff59315`. Every one is restated here as a testable requirement, with
how the push path preserves it.

| # | invariant | how the push path holds it | test |
|---|---|---|---|
| I1 | **`render(lastState)` is the only writer of `#tiles`, `#grid`, `#other`.** | `applyDelta` patches `lastState` and calls `scheduleRender()`. No delta code touches card DOM. | stub `render`, drive 50 deltas, assert `grid.innerHTML` never mutated outside a `render` call |
| I2 | **`render` is replayable from `lastState` alone.** Two consecutive calls produce byte-identical DOM and ring no bell the second time. Six sites depend on it (L597, L602, L646, L733, L734, L1164). | **strengthened**: `maybeBell` moves *out* of `render` and into `applyDelta`, driven by the server's `transitions` array and consumed once. Replay can no longer ring at all — today that is true only by accident. | `render(s); a=html; render(s); assert html===a` and zero oscillators |
| I3 | **The tick swaps data under a control, never the control.** `setOptions` keeps all three guards: signature bail, `document.activeElement` bail, selection restore limited to surviving options. | untouched; `refreshMissionPickers()` moves from the poll to the merge path | focus `#mWt`, apply 50 deltas, assert element identity `===`, `activeElement`, and `.value` unchanged |
| I4 | **A card is not replaced while a click is in progress on it.** | **NOT satisfied today** — `gridHover` holds structure only, and L556–557 rewrites content regardless. Fixed here: `pointerdown` on a `.card button` sets `pointerHold`; `reconcileGrid` queues that one card's rewrite until `pointerup`/`pointercancel`. ~6 lines. | `pointerdown` on `.finish`, apply a delta changing that card, `pointerup`+`click`, assert the handler ran exactly once |
| I5 | **The board never re-sorts under the pointer.** | untouched. Deltas merge into `lastState` during hover; `structureHeld` replays on `pointerleave`. The hold operates on `lastState`, not on the transport. | set `gridHover`, apply an order-reversing delta, assert child order unchanged and `#holdNote` reads `⌗ re-sort held`; `pointerleave` → order matches |
| I6 | **A card that moved in the last 600 ms swallows the first click and names itself — and never latches.** `_movedAt` refreshes only on a genuine >4 px move. | rAF coalescing plus stable card strings (I9) mean a static layout stops stamping `_movedAt` | render at 30 Hz with a static layout for 2 s; assert a `.finish` click reaches its handler |
| I7 | **State that must never be missed lives outside the diffed string.** `classList.toggle("attention", …)` runs unconditionally after the html-cache check. | new per-card state (`stale`, `provisional`, `inferred`, `unknown`) follows the same shape: a class toggle after the diff, never a token inside the html | force `el._html` to equal the new html; assert `classList` still tracks `availability` |
| I8 | **A rebuilt `.resume` button is stamped with its countdown synchronously, in the same frame.** | unchanged; L513 still ends every render | render with `resets_at = now+3600`; assert synchronously that the button is `disabled` and reads `▶ resume in …` |
| I9 | **The server ships absolute timestamps; the client owns elapsed time.** | `evidence_at`, `resets_at`, `commit.ts`, `closeout_sent`, `generated_at` are all absolute epochs. `rel()` runs against `Date.now()/1000` on the existing 1 s ticker (L784). | freeze the transport, advance a fake clock 30 s, assert every "… ago" string advanced by 30 s |
| I10 | **The 1 s ticker mutates attributes on existing nodes only** — `textContent`, `disabled`, `classList`, `dataset`; never `innerHTML`. | unchanged | spy on the `innerHTML` setter for one ticker pass; assert zero calls |
| I11 | **Every delta carries the globals the client cannot derive** — `order`, `counts`, `free_worktrees`, `generated_at`. No second copy of `severity()` in JS. | shipped on every frame, 166 B | assert `[...grid.children].map(el=>el.dataset.wt)` always equals the server's `order`; grep for a second severity ranking |
| I12 | **The bell rings at most once per coherent full-state view.** | server owns the edge; `transitions` is consumed once; a frame without `complete` or with `resync` never updates `lastAttn` | apply a delta sequence that transiently omits then restores a `needs_input` session; assert zero oscillators |
| I13 | **Resync on any gap.** Monotonic `seq`; refetch on non-contiguous seq or `onerror`; reject any state whose `generated_at` regresses. | **new**; neither guard exists today | deliver seq 1,2,4 → assert a snapshot fetch; deliver an older `generated_at` → assert `lastState` unchanged |
| I14 | **Freshness is reported honestly.** | `syncText` becomes `rel(Date.now()/1000 - lastState.generated_at)`, driven by the 15 s keepalive. Today L640 stamps client wall-clock and can overstate freshness by a full `STATE_TTL_S`. | serve `generated_at = now-8`; assert the sync line reads 8 s, not the clock |
| I15 | **Every state-changing action has a deterministic confirmation path.** | `▶ resume` disables on click; re-enables on the observed state change or a 3 s timeout that marks it failed. Actions call `COLLECTOR.poke()`. | click `▶ resume now`; assert disabled before the fetch settles, and a second synthetic click issues no second `/api/send` |
| I16 | **Optimistic values are visually distinct from confirmed ones.** | the chat echo gets `.msg.you.pending`, cleared on confirmation, marked failed rather than silently vanishing | make `/api/send` reject; assert the bubble carries a failure marker and survives the next `loadChatMsgs` |
| I17 | **At most one render per animation frame.** `stampMoves` does N `getBoundingClientRect()`; `flipReorder` 2N more. | rAF dirty flag | deliver 100 deltas synchronously; assert `render` ran at most once |
| I18 | **The card key is unique.** `discover_worktrees` dedupes on path, not name, so two roots each containing `api/` collapse to one card and collide in `REG`. | the snapshot ships a stable `key` (the worktree path); `dataset.wt` becomes the key | serve two worktrees named `api` from different roots; assert `grid.children.length === 2` |
| I19 | **`REG` must not outlive its sessions.** L419–421 accumulates and never prunes, so `resumeBody()` can POST a long-expired `resets_at`. | rebuilt from `lastState` on each render | render A with session S, then B without S; assert `REG['wt|S']` is undefined |
| I20 | **Job pollers stay request/response.** `pollDispatch` (1 s) and `pollFinishJob` (1.5 s) are job-scoped and are not folded into the board stream. | untouched | kill the state stream mid-dispatch; assert `.proglive` lines still advance |
| I21 | **Zero dependencies, zero external runtime fetches on the data path.** | `fetch` + `EventSource`, both built in | grep for new `src="http` / `import` beyond the existing font link |

**One non-obvious consequence worth naming.** `rel(s.age_s)` is baked into the card HTML string,
and `rel()` returns `Ns` below 60 s — so **every card holding a session younger than 60 s (that
is, every `● WORKING` card) produces a different HTML string on every tick and gets a full
`innerHTML` rewrite.** The keyed diff degrades to zero exactly where it is needed most. Moving
elapsed time onto the 1 s ticker (I9) makes card strings stable across time, so a card rewrites
only when something semantic changed: **rewrites drop from ~12/min to ~2/min per working card
while update frequency rises 10×.** The strongest objection to a higher-frequency design — "more
rewrites eat more clicks" — is not merely mitigated; the net rewrite count goes *down*.

---

## 6. What the phone gets

**The event stream specified in §Layer 4 is the same stream that feeds APNs.** That is the point
of deriving it here rather than in the push document: one edge detector, three consumers.

```
                      ┌─ SSE mode=full     → browser board
Collector.publish() ──┼─ SSE mode=digest   → iOS app, foregrounded, over the tailnet
  (transitions[])     └─ push dispatcher   → APNs → locked phone
```

| concern | how this design serves it |
|---|---|
| **Edges exist without a browser** | Today `attn` is computed client-side (`index.html:425`) inside a `setInterval` that browsers clamp to ≥1/min when hidden. The board you are *not* looking at is the one that fails to notify you. Layer 2 makes observation continuous and client-independent; Layer 4's `transitions` array is computed in the collector thread. This is the structural precondition for push, not an optimisation. |
| **Payload** | `mode=digest` ships `sid`, `status`, `why`, `evidence_at`, `pid_certain` plus the globals — ~1 KB against 35 KB. Deltas are ~120–200 B. gzip on the long-poll path. |
| **Battery** | A parked long-poll or an SSE stream costs one idle socket. The alternative — a 5 s poll of a 35 KB endpoint over WireGuard — is 420 KB/min to transmit ~11 B of change per session. |
| **Push volume** | R3 (`notify_settle_s = 45`) and `provisional` gate what is notification-worthy. A transient thinking-gap de-escalation never reaches APNs. Measured misfire rate: 2.10 % of thinking gaps, against today's 32 % of questions never announced. |
| **Push payload** | `transitions` entries are already the right shape: `{kind, wt, sid, from, to, at}`. `kind ∈ {attention, limit, ended}` maps to alert category; `at` is absolute so the phone can render "asked 40 s ago" correctly even if the push was delayed. |
| **Cold-start correctness** | `⛔ LIMIT HIT` is reachable without an inbound `/api/limits` (Layer 2), so a limit-stranded agent is visible to a phone that has never opened the limits view. |
| **Resync after suspension** | iOS suspends the app and kills the socket. On foreground: `GET /api/state?since=<lastSeq>` returns a full snapshot with `complete: true`; `seq` and the `generated_at` regression guard make the merge safe. `digest` catches silent divergence. |
| **Degraded honesty** | `signals.*.ok`, `signals.*.source`, `confidence`, `liveness: "unknown"` and per-worktree `git.stale` all cross the wire, so the phone can say "process table not read in 40 s" instead of confidently showing `○ ENDED`. |
| **What we do not own** | Server → APNs over a warm HTTP/2 connection is ~50–250 ms. Apple → device has no published SLA and degrades under Low Power Mode. Budget: **≤200 ms is ours, 0.6–3 s end to end.** The phone is a notifier; the board is the real-time surface. |

---

## 7. Migration

Each step is shippable alone, valuable alone, and revertible alone.

| step | layer | what | net lines | win |
|---|---|---|---|---|
| **1** | 0 | reorder `classify_session`; read `turn_duration` as `turn_ended` | ~20 | **the 90 s term dies.** `▲ NEEDS ANSWER` stops being 90 s late and stops being invisible 32 % of the time. No new machinery. |
| **2** | 1a–1c | `_resolve_git`, `run(strict=)`, `git_info_v2` + oid memo, `collect_git` parallel + carry-forward | ~115 | 1641 → ~570 ms; the `dirty=0`-on-timeout corruption is fixed |
| **3** | 1d–1e | `TranscriptCache` three-lane walk; `ProcFacts` on `(pid, start)` with sticky `wt_claims` | ~110 | ~570 → ~314 ms; the `lsof`-fails-so-everything-is-FREE catastrophe is fixed; cwd drift no longer manufactures FREE |
| **4** | 2 | `Collector` thread, `poke()` at the four missing mutation sites, `/api/state?since=&wait=`, gzip, `evidence` block | ~140 | **10.6 s → ~330 ms.** Herd structurally impossible. Hidden tabs fixed. Push becomes possible. |
| **5** | 4 (client half) | absolute timestamps on the 1 s ticker, rAF coalescing, `seq`/`generated_at` guards, `pointerdown` hold, `REG` pruned, resume confirmation | ~90 js | card rewrites down 6×; three latent client bugs closed |
| **6** | 4 (server half) | `/api/stream` SSE, delta frames, server-side `transitions`, `digest`, `mode=digest` | ~130 | ~35 KB/5 s → ~2 KB/change; bell moves server-side; the push pipeline exists |
| **7** | 3 | `KqueueWatcher` + `PollWatcher` behind `ORCHESTRA_WATCH`, `TranscriptCursor`, 1 s reconcile, liveness canary | ~250 | ~330 → **~122 ms**; server CPU 8 % → ~1 %; the honest write clock |
| **8** | 4 §4.3 | `thinking_s = 20`, `notify_settle_s = 45`, `provisional`, `MIN_DWELL_S`, the `phase` presentation | ~60 | the board stops lying in *both* directions |
| **9** | 5 | `--settings` on dispatch, `/api/agent/event`, the signal overlay, `HOOK_LEASE_S` | ~350 | `■ BLOCKED` / `◆ YOUR TURN` become observed for every dispatched agent; actions become confirmable |
| **10** | 3 (Linux) | `InotifyWatcher` via ctypes + `os.pidfd_open`, behind a runtime probe | ~120 | parity on Linux; `ORCHESTRA_WATCH=poll` remains the floor |

Steps 1–3 are roughly a day and need no architectural change at all. Step 4 is where the user's
complaint is actually answered. Steps 7–9 are where the board stops guessing.

`branch_topology()` (2587 ms, 109 subprocesses, three of which literally duplicate `git_info`'s
work on a different 30 s clock) is **not** covered here. It moves onto the same collector's slow
lane and gets the same `_resolve_git` + memo treatment; that is its own change.

---

## 8. Testing

Everything below fits `tests/` as it stands: stdlib `unittest`, zero dependencies, `orchestra.py`
loaded by path via `importlib`, module globals snapshotted and restored by `ConfigGuard`.

### 8.1 Layer 0 — the classifier ladder

The ten existing `TestClassifySession` cases must pass **unmodified**. That is the acceptance
criterion, and it is not free: `test_recent_is_working_regardless_of_liveness` asserts
`classify_session(5, False, [], 0, False, 90) == "working"`, which encodes a real rule — *a fresh
write with no observed process means "we have not seen the process yet", not "ended"*. The
reordered ladder must name that rule (`orphan_grace_s`) rather than inheriting it accidentally
from the old first branch. Do not assume the reorder is free; run the ten.

New cases:

```python
def test_question_beats_the_window(self):
    # the invariant the old first branch violated
    st, _ = fb.classify_session(0, True, ["AskUserQuestion"], 0, False, 90)
    self.assertEqual(st, "needs_input")

def test_turn_ended_is_not_working(self):
    st, _ = fb.classify_session(0, True, [], 0, False, 90, turn_ended=True)
    self.assertEqual(st, "waiting")

def test_turn_ended_with_pending_workflow_is_still_working(self):
    st, _ = fb.classify_session(0, True, [], 1, False, 90, turn_ended=True)
    self.assertEqual(st, "working")

def test_proc_read_failure_is_unknown_not_ended(self):
    st, _ = fb.classify_session(9999, False, [], 0, False, 90, procs_known=False)
    self.assertEqual(st, "unknown")

def test_long_tool_run_under_approvals_is_not_blocked_before_the_grace(self):
    st, _ = fb.classify_session(30, True, ["Bash"], 0, False, 90, block_grace_s=60)
    self.assertEqual(st, "working")
```

**Gap replay** — the test that stops a regression to a naive short window:

```python
GAPS = json.loads((FIXTURES / "midturn_gaps.json").read_text())   # n=4107, measured

def test_no_needs_you_transition_inside_a_midturn_gap(self):
    """Replay the measured mid-turn gap distribution. A mid-turn gap is,
    by construction, a gap with an unresolved tool_use or an open turn — the
    classifier must never emit needs_input/blocked/waiting inside one."""
    bad = 0
    for gap in GAPS:
        for t in (0, gap * 0.25, gap * 0.5, gap * 0.9, gap):
            st, _ = fb.classify_session(t, True, ["Bash"], 0, True, 90,
                                        block_grace_s=60)
            if st != "working":
                bad += 1
    self.assertEqual(bad, 0)
```

`tests/fixtures/midturn_gaps.json` is generated once from real transcripts and committed. It is
the empirical contract that any future window change must satisfy.

### 8.2 Layer 1 — differential and corruption

```python
@unittest.skipUnless(HAVE_GIT, "git required")
def test_git_info_v2_matches_git_info_field_for_field(self):
    """Across a clean repo, a dirty repo, a detached HEAD, a repo with no
    upstream, and a repo with only an initial commit."""
    for repo in self.repos:
        self.assertEqual(fb.git_info(repo), _strip_new_keys(fb.git_info_v2(repo)))

def test_ahead_behind_are_not_swapped(self):
    """# branch.ab is '+ahead -behind'; the v1 rev-list --left-right call it
    replaces put BEHIND first. Getting this backwards is silent."""
    repo = self.repo_ahead_2_behind_1
    self.assertEqual(fb.git_info_v2(repo)["ahead"], 2)
    self.assertEqual(fb.git_info_v2(repo)["behind"], 1)

def test_git_timeout_is_unknown_not_clean(self):
    with mock_run_raising(fb.GitTimeout):
        g = fb.collect_git([{"git": self.repo}])[self.repo]
    self.assertIsNone(g["dirty"])
    self.assertTrue(g["stale"])
    self.assertNotEqual(fb.card_availability([], False, git=g), "free")

def test_truncated_transcript_recomputes_topic(self):
    """session_topic reads the first 16KB and is memoised. Truncation or
    replacement must invalidate it; appending must not."""
```

**Spawn-count budgets instead of wall-clock.** Wall-clock in CI is noise; subprocess counts are
deterministic and are what actually moved.

```python
def test_collect_state_spawn_budget(self):
    with count_run_calls() as calls:
        fb.collect_state()
    git_calls = [c for c in calls if c[0][0].endswith("git")]
    self.assertLessEqual(len(git_calls), len(self.worktrees) + 1)   # was 4-5x
    self.assertLessEqual(len([c for c in calls if c[0][0] == "lsof"]), 1)
```

**Stat-count budget**, which is how the O(in-window) invariant is enforced:

```python
def test_scan_is_o_of_in_window_sessions(self):
    """Fixture: 3 in-window transcripts, 200 out-of-window ones each with a
    populated subagent dir. The hot lane must not stat the out-of-window
    subagent trees."""
    with count_stats() as n:
        fb.scan_sessions(self.worktrees, [], time.time())
    self.assertLess(n, 400)          # today this fixture costs >20,000
```

### 8.3 Layer 2 — single-flight, ordering, fallback

```python
def test_twenty_concurrent_requests_collect_once(self):
    with count_collects() as n:
        threads = [threading.Thread(target=lambda: urlopen(f"{base}/api/state"))
                   for _ in range(20)]
        [t.start() for t in threads]; [t.join() for t in threads]
    self.assertEqual(n, 1)

def test_binding_is_stable_across_collects(self):
    """Two collects with no FS or process change must produce identical
    sid -> pid assignments; a one-byte UNTIMESTAMPED append to a dormant
    transcript must not re-deal pids in that worktree."""

def test_long_poll_returns_200_not_504(self):
    t0 = time.time()
    r = urlopen(f"{base}/api/state?since=999999&wait=1")
    self.assertEqual(r.status, 200)
    self.assertGreaterEqual(time.time() - t0, 0.9)

def test_state_shape_is_unchanged(self):
    """Key-for-key against a golden snapshot, plus the resumes ride-along.
    New keys are additive only."""

def test_cached_state_falls_back_without_a_collector(self):
    """tests/ imports orchestra.py without main(), so no thread exists."""
    self.assertIsNone(fb.COLLECTOR)
    self.assertIn("worktrees", fb.cached_state())
```

### 8.4 Layer 3 — deterministic event testing

Event-driven code is tested by **never using the real clock and never using the real watcher.**
Two injectable seams:

```python
class FakeWatcher(fb._WatchBase):
    """Records the desired watch set; fires events on command. The apply loop
    is driven synchronously by the test — no sleeps, no races."""
    def watch(self, path, tag): self.plan[str(path)] = tag; return True
    def fire(self, tag, key, flags=0): self.on_batch([(tag, key, flags)])

class FakeClock:
    def __init__(self, t=1_000_000.0): self.t = t
    def time(self): return self.t
    def advance(self, dt): self.t += dt
```

```python
def test_append_publishes_within_one_debounce(self):
    store = fb.LiveStore(watcher=self.fw, clock=self.clock)
    append_entry(self.transcript, assistant_text("hi"))
    self.fw.fire("transcript", str(self.transcript))
    self.clock.advance(fb.DEBOUNCE_S)
    store.tick()                                   # one synchronous pass
    self.assertEqual(store.seq, 1)

def test_debounce_coalesces_the_turn_end_cluster(self):
    for e in (assistant_text("done"), turn_duration(0, 0),
              last_prompt(), ai_title(), permission_mode()):
        append_entry(self.transcript, e); self.fw.fire("transcript", str(self.transcript))
    self.clock.advance(fb.DEBOUNCE_S); store.tick()
    self.assertEqual(store.seq, 1)                 # ONE publish, not five

def test_untimestamped_records_do_not_advance_liveness(self):
    """mtime 1s, last real entry 2 days: must NOT be working."""
    seed_transcript(self.transcript, last_real_entry_age=172_800)
    append_entry(self.transcript, last_prompt())   # bumps mtime, no timestamp
    self.fw.fire("transcript", str(self.transcript)); store.tick()
    self.assertNotEqual(status_of(store, self.sid), "working")

def test_rotation_reparses_from_zero(self):
    os.replace(self.transcript, self.transcript.with_suffix(".old"))
    write_fresh(self.transcript)
    self.fw.fire("transcript", str(self.transcript)); store.tick()
    self.assertEqual(cursor(store).off, self.transcript.stat().st_size)

def test_missed_events_are_repaired_by_reconcile(self):
    """The safety-net contract: if the watcher fires NOTHING, the 1s stat
    sweep must still converge on the truth."""
    append_entry(self.transcript, assistant_text("silent"))
    self.clock.advance(fb.RECONCILE_S); store.tick()
    self.assertEqual(store.seq, 1)

def test_interrupt_expires_pending_tools(self):
    """tool_use -> '[Request interrupted by user]' -> new user prompt must
    not classify as blocked."""

def test_stale_delegated_does_not_pin_working(self):
    """turn_duration(pendingWorkflowCount=1) followed by a new user prompt
    and assistant activity must not report working via that stale count."""
```

`ORCHESTRA_WATCH=poll` is what CI sets, so the `PollWatcher` path is the tested one on every
platform and the kqueue/inotify backends are exercised only in a `skipUnless` block on a real
machine.

### 8.5 Layer 4 / §5 — render invariants

Each of I1–I21 gets the test named in its row. They run headless against a minimal DOM shim in
`tests/test_render.py` (a few hundred lines of stdlib: a `document` stub with `innerHTML`
setters that record writes, `classList`, `dataset`, and a fake `requestAnimationFrame`). This is
the one piece of new test infrastructure the programme needs, and it pays for itself at I4 alone.

### 8.6 Layer 5 — hooks

```python
def test_hook_event_never_acts(self):
    """A hook payload must not be able to reach send_to_process, _run_dispatch,
    deliver_text or start_finish."""
    with forbid(fb, "send_to_process", "_run_dispatch", "deliver_text", "start_finish"):
        fb.apply_signal(stop_payload(), pid=1234)

def test_unknown_event_is_ignored_not_guessed(self): ...
def test_hook_lease_expiry_returns_to_inference(self): ...
def test_pid_from_hook_is_revalidated_against_ps(self): ...
def test_no_hooks_installed_is_layer_0_to_4_exactly(self): ...
```

### 8.7 Latency regression in CI

Wall-clock assertions are flaky. Assert **work counts**, which are deterministic, and record
wall-clock as an artefact rather than a gate:

| gate | budget |
|---|---|
| git subprocesses per `collect_state` | ≤ `n_worktrees + 1` |
| `lsof` / `ps eww` invocations per collect with no new pid | 0 |
| `os.stat` calls in `scan_sessions` on the 203-transcript fixture | < 400 |
| `collect_state` calls under 20 concurrent requests | 1 |
| publishes for one turn-end write cluster | 1 |
| bytes in a single-session delta frame | < 3,000 |
| `render` calls per 100 synchronous deltas | ≤ 1 |

Plus one wall-clock **smoke** check with a wide bound (`collect_state() < 1.0 s` on the fixture
tree), which catches an accidental reintroduction of a serial git storm without failing on a busy
runner.

---

## 9. Risks and open questions

**Risks, ranked by how badly they fail.**

1. **Silent watch death (Layer 3).** A missed re-watch after an atomic rename leaves an fd on a
   dead inode and a session goes dark — indistinguishable from healthy quiet. *Mitigation:* the
   1 s reconcile stat-sweep (0.55 ms), the 120 s liveness canary, the 60 s deep audit, and
   `signals.sessions.source` surfaced in the API so degradation is visible. **The worst case is a
   1 s poll, which is better than Layer 2's best case.** This is the property that makes the
   layer shippable.
2. **Stateful drift (Layer 2+).** Stateless re-derivation is self-healing; an accumulating store
   can diverge and stay wrong. *Mitigation:* `AUDIT_S = 60` full re-derivation compared against
   the incremental store with any delta logged; the `digest` frame; laptop sleep/wake detected by
   a monotonic-vs-wall-clock jump forcing a full resync.
3. **The status model over-corrects and the board flickers.** *Mitigation:* §4.3's five rules,
   the committed gap-replay fixture, and `provisional` keeping every de-escalation out of
   `counts`, the bell and push until it has settled 45 s.
4. **Hook payload schema drift (Layer 5).** Undocumented, unversioned, ships weekly.
   *Mitigation:* the reconciliation rank — a renamed key degrades a session to inference and logs
   once. It must never degrade to a *wrong* status. Hooks are never the only source for any
   status.
5. **Thread lifecycle and the test suite.** `tests/` imports `orchestra.py` without `main()`.
   *Mitigation:* the collector is started only from `main()`; `cached_state()` keeps its
   TTL fallback; the seven `_cache["state"] = None` sites keep working.
6. **`ctypes` on Linux couples us to the libc ABI.** *Mitigation:* three symbols, one struct,
   stable since 2005, wrapped in `try/except`, with `ORCHESTRA_WATCH=poll` one env var away — and
   the poll floor is 1.98 ms for a 996-object sweep. The ctypes path is optional, not
   load-bearing.
7. **SSE thread-per-client.** Verified reclaimed after rude disconnects, but not unbounded.
   *Mitigation:* hard `MAX_STREAMS` cap with a 503 that falls back to long-poll, which is a
   first-class path rather than a consolation.

**Open questions.**

- **`thinking_s = 20` is a judgement call**, not a measurement. It should be configurable and
  instrumented: log every de-escalation and whether the agent resumed within 60 s, and re-derive
  the table in §4.3 from the user's own fleet after a week.
- **Does the tmux `capture-pane` probe at `thinking_s` expiry earn its complexity?** It collapses
  bucket C to zero for dispatched agents at ~13 ms per crossing. It is specified but not
  scheduled; decide after §4.3 has a week of real data.
- **`branch_topology()` has not been designed here.** 2587 ms, 109 subprocesses, three of which
  duplicate `git_info` on a different clock. It needs the same treatment and probably belongs on
  the collector's 30 s lane.
- **Should `provisional` be visible on the phone at all**, or should the phone only ever see
  settled statuses? Leaning toward: visible in the app, never in a push.
- **Multi-machine.** Everything here assumes one host. A second Mac in the fleet changes the
  aggregation story and is out of scope until the phone ships.

---

## Appendix — corrections to claims made during design

Recorded so they are not repeated. Every line was checked on this machine.

| claim | correction |
|---|---|
| "kqueue needs one fd per file and would blow the 256-fd default" | `kern.maxfilesperproc = 61,440`, `RLIMIT_NOFILE` soft = **1,048,576**; the watch set is **366 objects**. 2,000 `O_EVTONLY` fds opened in 161 ms. |
| "inotify's 8,192-watch default limit" | 8,192 on old kernels; **65,536–524,288 on current distros**. We need ~300. |
| "`os.O_EVTONLY` is not exposed by Python" | It is — **32768**, since 3.10. Keep the `getattr` fallback anyway. |
| "use `ctypes` + `syscall(434, …)` for `pidfd_open`" | **`os.pidfd_open` is stdlib** on CPython ≥3.9. The syscall number is architecture-specific; do not hand-roll it. |
| "kqueue delivers in ~5 ms" | **0.17 ms p50, 0.23 ms max** over 30 cross-thread appends. |
| "event → publish in 60 ms" | Only if the debounce wait is `wait(deadline - now)`. A fixed `wait(0.25)` with the event already cleared measures **255 ms**. One line; 4× the headline number. |
| "unbounded `working` while the turn is open is stricter than 90 s" | Backwards. 11 of 71 in-window transcripts end on an `assistant` entry, 9 of them 15 h–2 days old. `TURN_OPEN` suppresses de-escalation; it does not license unbounded `WORKING`. |
| "an unresolved `tool_use` under approvals is `BLOCKED`, immediately" | Fires on 2.19 % of all mid-turn gaps, audibly. `block_grace_s = 60` (the measured p99) is required. |
| "hook latency max 29.3 ms" | p50 8.7 ms, p90 10.2 ms, **max 446.6 ms at load 6.7**. The tail is fork/exec, and this box runs at load 26. |
| "hooks give 100 % coverage" | 7 of 8 Claude homes have no `hooks` key; all 6 running agents predate any install; `claude --bare` skips hooks entirely. Tier 0 (`--settings` on dispatch) is the only coverage claim this design makes. |
| "`# branch.ab` maps straight onto the old fields" | `+A -B` is **ahead, behind**. The v1 `rev-list --left-right --count` call put **behind first**. Silent two-number swap. |
| "the classifier reorder passes all existing tests for free" | It does — verified, 10/10 plus 5 new cases — but only because `orphan_grace_s` explicitly preserves the rule `test_recent_is_working_regardless_of_liveness` encodes. Run the ten; do not assume. |
