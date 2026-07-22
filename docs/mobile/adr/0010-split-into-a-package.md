# ADR 0010 — Split `orchestra.py` into a stdlib-only package

**Date:** 2026-07-21 · **Status:** Accepted — **shipped**, in 17 commits, one module per commit.

## Context

The README's opening claim is *"an agent harness with zero dependencies — one python3 stdlib
file."* That was a genuine virtue at 800 lines. At 2,300 it is a liability, and the work queued
behind it — the observer loop, SSE, the delta protocol, auth and pairing, the APNs pipeline,
hooks ingestion — roughly doubles the file again.

`ENGINE.md` §9 argued against splitting, on the grounds that the unit suite loads the file by
path and monkeypatches module globals across 67 sites, so a split would "delete the safety net
and do the dangerous thing simultaneously."

That objection is **real but not decisive**. It is an argument about *technique and sequencing*,
not about whether. Measured coupling:

```
fb.CFG 35 · fb.DEMO 21 · fb._resumes 21 · fb._limits 16 · fb._closeouts 13
fb.fire_resume 12 · fb.schedule_resume 11 · fb.run 10 · fb.munge 10 · fb.classify_session 10 …
                                                        67 patch sites across 2 test files
```

The genuine hazard is subtler than "tests break": once `collect_state` lives in `observer.py`
and does `from .gitrepo import git_info`, patching `orchestra.git_info` **silently stops having
any effect** and the test keeps passing while testing nothing. A test that fails is a
nuisance; a test that lies is a trap.

## Decision

**Split into a package**, and defeat the objection with two measures taken *before* any code
moves:

1. **An independent safety net.** `tests/characterize.py` pins 1,589 cases across 13 functions
   by calling public functions with explicit arguments and byte-comparing a recorded golden. It
   monkeypatches nothing, so the split cannot disarm it. It resolves either layout via
   `load_orchestra()`, so the same harness runs on both sides and doubles as proof the facade is
   complete. It was verified to actually fail by reintroducing a known regression.
2. **Import modules, not names.** Every cross-module reference goes through the module object —
   `from . import gitrepo` … `gitrepo.git_info(...)` — never `from .gitrepo import git_info`.
   This preserves monkeypatching at the canonical location, so tests keep their patch points
   rather than losing them.

The layout follows the component model in `ENGINE.md`, so the file tree teaches the
architecture:

```
orchestra/
  __init__.py     facade — re-exports the public surface
  __main__.py     entry point (python3 -m orchestra)
  config.py       CFG, load_config
  shell.py        run()
                  ── observe: the part that knows
  gitrepo.py      munge, discover_worktrees, git_info, branch topology
  procs.py        claude_processes, pid→cwd, tmux pane map, session↔proc pairing
  transcripts.py  claude_homes, account_label, tail parsing, text cleaning, scan_sessions
  status.py       classify_session, closeout_step, card_availability   (pure policy)
  observer.py     collect_state, cached_state, _cache, demo_state
  limits.py       cclimits, reserve, per-model headroom
                  ── act: the part that does
  terminal.py     focus_process, send_to_process, AppleScript/tmux actuation
  chat.py         read_chat
  finish.py       the closeout saga, _closeouts
  dispatch.py     dispatch jobs, _jobs
  resume.py       scheduling, _resumes
                  ── serve: the part that tells
  server.py       Handler, routing
```

### As built — two deliberate deviations from the tree above

The tree, taken literally, contains two import cycles. Both were broken the same way: **move the
shared helper down**, to the module that already sits below both ends of the cycle.

| symbol | ADR says | shipped in | why |
|---|---|---|---|
| `account_label` | `transcripts.py` | **`config.py`** | `procs.claude_processes` calls it, and `transcripts.scan_sessions` calls `procs.pair_sessions_with_procs` — so `transcripts` owning it makes procs↔transcripts mutual. It is a four-line pure string function on a home-dir name with zero dependencies, called from six modules. It is a shared leaf helper, not transcript logic. Direction becomes `config → procs → transcripts`, which is the order this ADR intended. |
| `closeout_shell` | `finish.py` (implied by "the closeout saga") | **`dispatch.py`** | `finish.start_finish` calls `dispatch.start_dispatch`, and `dispatch._run_dispatch` was the only caller of `closeout_shell` — mutual. It is a pure f-string builder for the tmux command a dispatch runs, takes the brief as a parameter and touches none of the `CLOSEOUT_*` constants, so it carries nothing with it. Direction becomes `finish → dispatch`, one way. |

**The one true cycle, and the one late import.** `observer.collect_state` reads `finish._closeouts`
(it *reaped* it until the sweep thread landed — under a perpetual loop that write became a
scheduled reap nobody requested, so pruning moved to `finish`, ENGINE.md §2.5);
`finish.start_finish` invalidates `observer._cache`. This ADR pins `_closeouts`→finish and
`_cache`→observer, so neither could simply move. It is broken at *import* time only: `finish`
imports `observer` at module level (act depends on observe — the correct direction), and
`collect_state` reaches `finish` through a single, commented, function-local import. Python
resolves it from `sys.modules` at call time for free. To keep every intermediate commit
self-consistent, `finish.py` was extracted in two commits — state and pure helpers first (a leaf,
depending only on `shell`), then `start_finish` after `observer` and `dispatch` existed. The result
is a strict import-time DAG at every single commit and exactly one call-time back-edge.

**`HERE` moved up, not down.** `Path(__file__).resolve().parent` became `.parent.parent` in the
first commit. The package lives one level under the repo root, so without that change
`orchestra.config.json`, `dispatch.log.jsonl`, `resume.schedule.json` and the four served HTML
assets would all have silently relocated into `orchestra/` — losing every armed resume schedule and
404-ing the board on `/`. No test covered it; it was verified by hand.

**Four scalars are deliberately absent from the facade** — `DEMO`, `CONFIG_PATH`, `RESUME_STATE`,
`DISPATCH_LOG`. They are rebound at runtime, so a facade re-export would be a stale snapshot and
`orchestra.DEMO = True` would be a patch that lies — the exact failure mode this ADR exists to
prevent. Reach them at `orchestra.config.DEMO` and friends. Everything else is re-exported,
including the mutable containers, which the facade binds as the *same object* so in-place test
mutation keeps working untouched.

## Consequences

- **Zero dependencies is preserved** — the value that actually matters. Still stdlib only.
- **"One file" is deliberately traded.** The README must be updated to stop claiming it; keeping
  the claim while shipping a package would be the worst outcome.
- The entry point becomes `python3 -m orchestra`. `start.sh`, the README and CI need updating.
  A stray `orchestra.py` must **not** be left beside the `orchestra/` package — the shadowing is
  ambiguous and confusing.
- Each piece of mutable state gets exactly one owning module: `_cache`→observer,
  `_topo`→gitrepo, `_limits`→limits, `_closeouts`→finish, `_jobs`→dispatch, `_resumes`→resume.
  Today they are seven globals in one namespace with no enforced ownership.
- The 67 patch sites must migrate to canonical modules. Mechanical, but it is the risky part,
  and it is why the characterization harness exists.
- A later extraction of the observer into a separate daemon becomes a transport change rather
  than a redesign — the seam will already be drawn.

## Alternatives rejected

| option | why rejected |
|---|---|
| **Keep one file** (`ENGINE.md` §9) | Correct about the hazard, wrong about the remedy. The hazard is answered by an independent net plus module-object imports, both cheap. Deferring the split only means splitting more code later. |
| **Split later, after the observer lands** | The observer is precisely the code that most needs a home. Writing it into a 2,300-line file and moving it afterwards is strictly more work. |
| **Split and migrate tests in one commit** | This is the thing `ENGINE.md` rightly feared. Avoided by sequencing: net first, then mechanical moves verified one module at a time. |
| **Dependency injection instead of module-object imports** | Cleaner in the abstract, but it rewrites every call site and every test at once — the same big-bang risk in a different costume. Module-object imports get the testability now; DI stays available later. |
