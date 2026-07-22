# ENGINE — the settled component architecture

**Date:** 2026-07-21 · **Status:** settled. Later implementation tasks are written against this
document. Where it contradicts an earlier ADR, this document wins and an ADR is raised to record
the reversal (see §11).

> **`orchestra.py:NNN` citations throughout are historical.**
> [ADR 0010](adr/0010-split-into-a-package.md) split that file into the `orchestra/` package;
> the line numbers were true at the 2,302-line commit this was written against. Grep for the
> symbol name. §2.1, §2.8, §9 and §10 carry inline notes where the split actually reversed a
> decision rather than merely moved a line.

It decides four things and does not reopen them:

1. **Two components, not three.** `OBSERVER` (knows) and `SERVER` (tells and does), sitting on a
   shared stateless `PROBES` floor. `ACTUATOR` is a **gateway function**, not a component.
2. **Stateless per sweep stays the source of truth.** The engine becomes *continuous*, not
   *accumulating*. Byte-offset incremental parsing is rejected on measurement.
3. **Per-target leases, never a global mutation mutex.**
4. **One process, background threads, supervised by launchd.** (Originally *"one process, one
   file"* — the *one file* clause was reversed by [ADR 0010](adr/0010-split-into-a-package.md)
   and the code is now the `orchestra/` package. Everything else here is unaffected: it was
   always an argument about process count, not file count.)

---

## 1. Why this changes at all

### 1.1 The forcing argument

`cached_state()` (orchestra.py:996–1003) computes state **only when a client asks for it**:

```python
def cached_state():
    if DEMO:
        return demo_state()
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()
        _cache["t"] = now
    return _cache["state"]
```

State exists because a browser asked. Close the browser and nothing computes, nothing is
compared to anything, and no change is detected — ever.

A notification's entire job is to reach you **when you are not looking at the board**. Under a
lazy model that is not a latency problem to be tuned down; it is an impossibility. There is no
value of `STATE_TTL_S` that makes a push fire for a user with no tab open. **Therefore observation
must become continuous and client-independent.** That is the one premise in the whole programme
that survived every attack unscratched.

One honest correction to how that argument has been stated. It does **not** follow that
orchestra's architecture forbids continuous observation today. `resume_loop()` (orchestra.py:2166,
started at :2295) already runs forever with zero clients attached, wakes every
`RESUME_POLL_S = 5.0`, and *actuates terminals*. The distance from here to "push is possible" is
one more `threading.Thread(target=..., daemon=True)`. So continuity costs a thread, not a
decomposition. The decomposition has to earn itself on the *other* four things push exposes:

| exposed by push | today |
|---|---|
| one freshness class for five input classes | `STATE_TTL_S = 4.0` governs git (changes hourly), procs (seconds) and cclimits (300 s TTL) alike |
| no home for a **network** signal source | `_limits["data"]` is warmed only by a browser hitting `/api/limits` (:2208); the comment at :702–703 says so. With zero clients the ⛔ LIMIT HIT status never applies, so the flagship notification can never fire |
| no versioned publish point | there is nothing to diff, so nothing to notify from and nothing for a phone to resume against |
| a snapshot carrying actuator state | `_cache["t"] = 0.0` poked by hand from an HTTP request thread at :1476 and :1499 because the ✓/✕ button label is serialised inside the state blob |

Those four are what the split below actually buys. "Therefore three layers" was not an argument;
these are.

### 1.2 The lag, measured — and why it is the *second* argument

```
collect_state()          1641 ms   (9 worktrees, 5 live claude processes)
  git_info x9            1277 ms   78%   ← five git processes per worktree, 45 spawns
  scan_sessions           335 ms   20%   ← re-tails 128 KB of every transcript, every time
  claude_processes        112 ms    7%
  discover_worktrees        1 ms

STATE_TTL_S = 4.0 (orchestra.py:61) + setInterval(tick, 5000) (index.html:1169)
  → ~10.6 s worst case from a real change to pixels
```

Re-measured on a busier day the same collect took **4083 ms**, so the number is a floor, not a
ceiling. And `CFG["working_s"] = 90` (orchestra.py:51), consumed at `classify_session`:563, holds
`● WORKING` for up to **90 s** after a session stops writing — 8.5× the entire pipeline. That is
hysteresis, not latency, and no amount of faster collection touches it.

**Be honest about what this justifies.** The lag alone does not justify a re-architecture. Four
edits fix most of it and none of them is architectural:

| fix | measured effect |
|---|---|
| `git_info` across worktrees through a `ThreadPoolExecutor` | 4029 → 710 ms (5 spawns unchanged) |
| …and collapsed from 5 git calls to 2 | 710 → 491 ms |
| move `sub_dir.rglob` (:601–609) *after* the `age > window_s` check (:619) | stops ~17,960 subagent `stat()`s per collection to keep ~31 in-window sessions |
| `working_s` 90 → 25 | removes the dominant staleness term |

What makes the architecture worth doing is the **combination**: the iOS client is coming, and the
wire contract it binds to is decided by this work. Refactoring the engine is always cheap —
1,634 lines of tests, and (since ADR 0010) a package whose seams the import graph enforces.
Refactoring an App Store binary is not. So the test for "does
this land now" is not *is it elegant* but **would shipping iOS before it force iOS to be rebuilt**.
Four things pass that test (§8), and all four are cheap. Everything else in this document is
internal and could, in principle, be deferred — it is landed anyway because the migration order
in §9 makes each step individually shippable and individually valuable.

---

## 2. The decomposition

### 2.1 The rule

> **A component owns a perpetual thread of control and the mutable state that thread maintains.
> Everything else is a library, a store, or a function.**

This is the only criterion on the table that is falsifiable, so it is the one used. Applied:

| candidate | perpetual thread? | owns mutable state? | verdict |
|---|---|---|---|
| collection loop | yes | the world model | **OBSERVER** |
| HTTP accept loop + publisher | yes | subscribers, intents, leases | **SERVER** |
| `run` / `git_info` / `claude_processes` / transcript readers / cclimits fetch / tmux + osascript effectors | no | no | **PROBES — a library** |
| `send_to_process` / `start_finish` / `_park_on_trunk` / `focus_process` / `_run_dispatch` | no (borrows the caller's) | no | **SERVER methods** |
| `_jobs` / `_closeouts` / `_resumes` | no | yes, but request-created and client-rendered | **a SERVER store (`IntentStore`)** |
| `resume_loop` | yes | none of its own | **an internal client of the SERVER** |

```
                          ┌──────────────────────────────────────────┐
   world  ───probe───▶    │                PROBES                    │
   (git, ps, lsof,        │  pure · stateless · no cache · reentrant  │
    tmux, transcripts,    │  callable from any thread at any time     │
    cclimits network)     └───────────┬──────────────────┬───────────┘
                                      │                  │
                          reads only  │                  │  reads AND writes
                                      ▼                  ▼
                     ┌──────────────────────┐   ┌─────────────────────────────┐
                     │      OBSERVER        │   │           SERVER            │
                     │ 1 sweep thread       │   │ accept loop + publisher     │
                     │ owns: Snapshot(v),   │──▶│ owns: IntentStore, Leases,  │
                     │  memo, hook edges,   │   │  Subscribers, HTTP, SSE,    │
                     │  limits cache        │   │  APNs, act() gateway        │
                     │ NEVER mutates world  │◀──│ nudge() / hook() only       │
                     └──────────────────────┘   └─────────────────────────────┘
                                                        │        │
                                                        ▼        ▼
                                                   browser SSE   APNs → iPhone
```

The arrow from OBSERVER to SERVER is `snapshot()` / `wait_for()`. The arrow back is `nudge()` and
`hook()` — evidence, never commands. **OBSERVER must never reference a SERVER symbol.**

> **Updated by [ADR 0010](adr/0010-split-into-a-package.md) — shipped.** This paragraph used to
> read *"Both live in `orchestra.py` at module scope (§2.7); the direction is enforced by a grep
> test, not by the import system."* They no longer do. `observer.py` and `server.py` are separate
> modules and the import system enforces the direction: `server` imports `observer`, never the
> reverse. The one back-edge that exists — `observer.collect_state` READING `finish._closeouts`
> (it reaped it until step 2 shipped; §2.5's deletion is done) — is a **deliberate, commented
> function-local import**, which is exactly the kind of thing a grep test could not have caught.
> The rule is unchanged; its enforcement got stronger.

### 2.2 Why ENGINE / BROKER / ACTUATOR was rejected

The three-layer proposal cut on verbs — *knows / tells / does*. The code does not split there.

**The ACTUATOR is a false seam.** It has no independent trigger: the complete inbound mutation
edge is six POST routes plus `/api/focus` in `do_GET` (:2199) and one timer (`resume_loop`:2166).
A thing whose entire inbound edge is the request path *is* the request path. Its return value is
the product — `start_finish` (:1446–1537) has nine distinct return shapes and `index.html` renders
those strings verbatim; `start_dispatch` can return `needs_decision` with `can_opus` / `opus_left`,
which is a **dialog asked mid-actuation**. As a return value that is a return value; over a typed
command queue it is a two-phase conversation with correlation ids and a timeout policy.

And the dependency the boundary exists to police does not exist. Every mutating path already
re-probes: `focus_process`:1259, `send_to_process`:1347, `start_finish`:1456/1463/1464/1466/1468,
`_run_dispatch`:1749/1752, `_limit_active_until`:1990 (forces a network refetch), `fire_resume`:2152.
Exactly two read the snapshot and **both are bugs** (§2.6). You do not need a component to forbid a
call. You need a rule with teeth — and a component boundary would not even have provided one,
since nothing stops you putting the snapshot inside the command.

Two of the three-layer model's specific rules are rejected outright:

- **"SERIALIZED — at most one mutation in flight" is fatal.** `_tmux_resume` holds for up to
  ~12 minutes (`RESUME_READY_S = 420.0` at :2022/:2091, three retries each preceded by
  `_wait_composer_idle(name, 90.0)` at :2094). One 3 a.m. auto-resume would freeze every chat,
  finish and dispatch on every worktree. Replaced by per-target leases (§2.5).
- **Asynchrony is a property of an operation, not of a layer.** A tmux `send-keys` is ~40 ms;
  `_run_dispatch` is ~17 s; `_tmux_resume` is up to 12 minutes. Only the last two want a job/poll
  pattern, and the code already has it, targeted, for dispatch (`_jobs`:1626,
  `dispatch_status`:1851). Promoting asynchrony to a layer turns a 40 ms write into a job id.

### 2.3 Why the fact-store factoring was rejected

The store-centric alternative — many independent producers writing addressed facts on their own
cadences into one versioned store — is the more interesting design and three of its payload rules
are adopted wholesale (§3.4, §6.2, §3.3). It is rejected as the **structure** for one concrete
reason:

`pair_sessions_with_procs` (orchestra.py:186–199) pairs sessions to processes by exact
`CLAUDE_CONFIG_DIR` match first and then falls back to **freshness order**; its docstring states
the precondition that `sessions` must be freshest-first. The handoff-succession pass (:735–745)
then compares ages *across* sessions to decide which limit-hit session has been superseded. Both
require the process table and the session table to describe **the same instant**. Today they do,
because `collect_state` gathers them in one pass. Under independent producer cadences
(`proc/*` at 2 s, `sess/*` at 1 s) the derivation sees skew that has never existed in this
codebase: a proc tombstone whose successor session has not landed, a fresh session with no proc.
That injects a new bug class into the most heuristic, least-testable code in the file.

Its other advantages are not exclusive to it: an OBSERVER that owns a rate-limited limits poller
closes the cclimits hole just as completely (§7.5), and a pure `compose(snapshot ⊕ intents)`
deletes the `_closeouts.pop` violation identically (§2.4).

**The consistency rule that falls out, and it is binding:**

> Processes and transcripts are gathered in **one pass**, always, because they are correlated by a
> heuristic. git and cclimits may carry their own refresh cadence *within* a sweep, because
> nothing correlates them to anything.

### 2.4 PROBES — the floor

No thread, no module-level mutable state, no cache. Every return value is true at the instant of
the call and stale thereafter. Both components sit on it; neither calls the other through it.

```python
Rc = tuple[int, str]

def run(cmd: list[str], cwd: str | None = None, timeout: float = 6.0) -> Rc: ...

# world shape
def discover_worktrees() -> list[dict]: ...
def git_info(git_root: str) -> dict:
    """ONE `git status --porcelain=v2 --branch` (branch + upstream + ahead/behind +
    dirty in a single 19 ms call) + ONE `git log -1` for info["commit"], which
    porcelain v2 does not carry. Replaces the five spawns at :143,147,149,153,156.

    MANDATORY: porcelain v2 emits `# branch.head (detached)` and does NOT carry the
    `detached@<sha>` label built today at :146-148. Map (detached) ->
    f"detached@{branch_oid[:9]}". Three of the user's nine worktrees are detached
    right now; without this a third of the board renders branch: null."""

def claude_processes() -> list[dict]: ...
def proc_identity(pid: int) -> dict | None:
    """(pid, lstart, cwd, cmd) or None. The anti-recycle probe. Called immediately
    before every write to a terminal. macOS recycles pids; never satisfied from
    any cache, ever."""

# transcripts — parameterised, because the four projections need different windows
def read_window(fp: Path, nbytes: int, *, from_end: bool = True) -> str: ...
def parse_session_tail(fp: Path, nbytes: int = 128 << 10) -> dict: ...
def find_last_user(fp: Path, nbytes: int = 1 << 20) -> str: ...      # scanned BACKWARDS
def session_topic(fp: Path, nbytes: int = 16 << 10) -> str: ...      # the HEAD of the file
def read_chat(account: str, sid: str, limit: int = 40) -> dict: ...  # 512 KB tail

# limits — network. The CACHE lives in the OBSERVER, not here.
def fetch_limits(*, refresh: bool = False, timeout: float = 90.0) -> dict: ...

# effectors
def tmux_send(sock: str | None, target: str, text: str, *, literal: bool) -> bool: ...
def tmux_capture(sock: str | None, target: str) -> str: ...
def osa_send(host: str, tty: str, text: str) -> bool: ...
def osa_focus(host: str, tty: str) -> bool: ...
```

Making `fetch_limits` pure is what ends the three-writer problem on `_limits` — today
`cached_limits` writes it from a GET (:1029), `set_reserve` reaches in and rewrites
`acc["reserve_blocked"]` in place from a POST (:1134–1141), and `_limit_active_until` forces a
refetch from inside `fire_resume` (:1990). After this: the OBSERVER owns the cache, `set_reserve`
writes `CFG` only, and `reserve_blocked` is recomputed at compose time.

**`read_chat` lives here and is called synchronously on the request thread.** The claim that it
feeds actuation is false: `read_chat` (:1539) renders transcript text into the drawer, the reply
comes from a `<textarea>` and goes to `/api/send`, and the two paths never touch. Routing a
read-only, idempotent, parallel-safe 512 KB tail read through a serialised gateway would be a
pure serialisation bug. It is also the case that independently kills incremental parsing: three
of the four transcript projections need bytes written before any engine attached, and
`session_topic` needs the *beginning* of the file.

### 2.5 OBSERVER

**Owns:** the sweep thread, the immutable versioned `Snapshot`, the stat-keyed parse memo, hook
edges with TTL, the cclimits cache, the drift counter.

**Must never:** mutate the world (no `send-keys`, no `git switch`, no AppleScript, no file
writes); reference a SERVER symbol; mutate anything a mutation path owns — in particular the
`_closeouts.pop` at `collect_state`:776–781 is **deleted**, because under an always-running loop
it would silently reap closeout flags on a schedule nobody requested.

```python
class Observer:
    """Owns the ONLY perpetual read loop. Publishes immutable, monotonically
    versioned snapshots. Never mutates the world. Never imports server."""

    def __init__(self, cfg: dict, *,
                 idle_s: float = 1.0,        # cadence with no evidence of change
                 hot_s: float = 0.15,        # floor between sweeps after a nudge
                 git_s: float = 5.0,         # git re-probe cadence WITHIN a sweep
                 limits_s: float = 300.0,    # cclimits poll, see §7.5
                 reconcile_s: float = 60.0,  # unconditional cold sweep, memo bypassed
                 max_stale_s: float = 8.0):  # never serve older than this
        ...

    # lifecycle
    def start(self) -> None: ...
    def stop(self, timeout: float = 5.0) -> None: ...

    # READ API — the only surface the SERVER may touch
    def snapshot(self) -> Snapshot:
        """The most recent completed sweep.

        ADVISORY. Safe to render, to diff, to notify from. NEVER a mutation
        precondition — a mutation validates against probes.* at the instant it
        acts. Raises AdvisoryReadInMutation if called inside act()."""

    def wait_for(self, after: int, timeout: float) -> Snapshot | None:
        """Block until version > after. The publisher thread's whole life."""

    def limits(self) -> dict: ...
    def stats(self) -> dict:      # {version, sweep_ms, drift, cold_at, memo_hits, ...}

    # WRITE API — evidence, never commands
    def nudge(self, reason: str) -> None:
        """Something changed. Moves the next sweep to now + hot_s. Never blocks,
        never fails, never a source of truth."""

    def hook(self, sid: str, event: str, at: float) -> None:
        """A Claude Code hook edge. Lowers latency; expires after HOOK_TTL_S;
        sweep inference wins after that. A dropped hook costs latency, never truth."""
```

**Threading model:** exactly one perpetual thread (`observer-sweep`), plus a `ThreadPoolExecutor(8)`
it owns for fanning `git_info` across worktrees inside a sweep, plus one optional watcher thread
if kqueue is ever built (§10). One `threading.Condition` guarding `_snap` / `_version`. Readers
take no lock: `snapshot()` is one attribute read of a frozen object.

**Why the GIL is not a problem here, measured:**

| phase | wall | cpu | GIL-held |
|---|---|---|---|
| `claude_processes` | 122 ms | 9 ms | 7 % |
| `scan_sessions` | 265 ms | 259 ms | **98 %** |
| `git_info` ×9 | 1076 ms | 68 ms | 6 % |
| `collect_state` total | 1408 ms | ~340 ms | 24 % |

A 50 ms-tick stand-in for an SSE writer, measured against a live `scan_sessions`, was late by
**1.3 ms at p50 and nothing at p95**. Even four concurrent full collects only reached 22.8 ms at
p99. 76 % of the collect is `subprocess` wait with the GIL released. Do not spend design budget
here.

### 2.6 SERVER

**Owns:** the HTTP accept loop, the publisher thread, `Subscribers`, `IntentStore`, `Leases`, the
`act()` gateway, APNs fan-out, and the scheduler thread (today's `resume_loop`, unchanged in
semantics).

**Must never:** read `observer.snapshot()` as a mutation precondition; touch the OBSERVER's
internals; hold a global mutation lock.

#### The gateway

```python
@dataclass(frozen=True)
class Result:
    ok: bool
    message: str
    mode: str | None = None          # start_finish's nine modes, unchanged
    intent: str | None = None        # intent id, when one was opened
    extra: dict = field(default_factory=dict)   # needs_decision, can_opus, ...

def act(*, kind: str, targets: Sequence[str], payload: dict,
        fn: Callable[[Intent], Result], actor: str = "http",
        idem: str | None = None, lock_wait_s: float = 2.0) -> Result:
    """The single actuation gateway. EVERY mutating route and the scheduler enter
    here and nowhere else.

    1. idempotency — key = idem or sha1(kind|targets|digest(payload)) bucketed to
       30 s. A terminal Result on that key is REPLAYED, not re-executed. Two tabs,
       or the phone and the browser, double-firing ✓ finish yield ONE closeout
       brief and two identical answers. (A mutex would serialise the double-fire
       and still execute it twice.)
    2. leases — per-target, acquired in sorted order, bounded wait, then fail fast
       NAMING THE HOLDER.
    3. guard — fn re-validates against probes.* inside `mutating()`, which makes
       observer.snapshot() raise. No snapshot may gate a mutation.
    4. write-ahead — the intent is persisted at phase 'running' and the audit line
       appended BEFORE the effect.
    5. settle — record the Result, append the outcome, observer.nudge(kind)."""
```

Every existing actuator body is reused **unchanged** inside `fn`. That is the punchline: the
ACTUATOR's entire justification — serialisation, idempotency, audit, guarded preconditions — is
satisfied by one ~40-line function. A gateway function is not a component.

#### Per-target leases

```python
def target_key(*, tmux: str | None = None, sock: str | None = None,
               tty: str | None = None, worktree: str | None = None) -> str:
    """Stable identity of the thing that can actually be corrupted:
         pane:<sock>/<target>   ·   tty:/dev/ttys004   ·   wt:voyager-cli
    Composite operations hold several, always acquired in sorted order."""

class Leases:
    @contextlib.contextmanager
    def hold(self, keys: Sequence[str], wait_s: float, what: str):
        """Fail fast with the holder named. For a human pressing a button,
        'a resume is typing at this agent (started 40s ago)' beats a 12-minute
        hang. The held set is published on the card so the UI greys the control
        and says why."""
```

The hazard being defended is narrow and real: `deliver_text` (:1709–1724) presses Enter up to
three times while polling `capture-pane`, and `_wait_composer_idle` (:2025–2037) requires *idle on
two consecutive looks*. A second writer **on the same pane** invalidates both proofs. Different
panes are independent, so different panes take different locks.

#### Preconditions — how the seam does not leak

The rule, stated once:

> **A mutation never takes a precondition from the snapshot. Preconditions come from a
> synchronous probe at the instant of the mutation.**

Enforced mechanically, six lines:

```python
_ACT = threading.local()

@contextlib.contextmanager
def mutating(target: str):
    prev, _ACT.on, _ACT.target = getattr(_ACT, "on", False), True, target
    try:
        yield
    finally:
        _ACT.on = prev

# in Observer.snapshot():
if _ACT_GUARD.in_mutation():
    raise AdvisoryReadInMutation(
        "snapshot() read inside a mutation; call probes.* instead")
```

Six lines, and the existing 1,634 lines of stdlib `unittest` turn the contract into a failing test
rather than a design document. It is *stronger* than a queue boundary, which does nothing to stop
you putting the snapshot inside the command. Its one weakness is honest and recorded in §10: the
thread-local does not follow a thread you spawn inside an actuation; the dispatch worker sets it
explicitly and a grep test is the backstop.

The two live violations it catches:

- **`fire_resume`:2127–2134** reads `cached_state()`, sees `status != "limit"` because
  `_limits["data"]` was never warmed with no browser open, and **silently marks an armed 3 a.m.
  resume `done` without firing it.** Only the regex fallback at :728 saves it today.
- **`_pick_defaults`:1609** picks the dispatch worktree from a snapshot up to 4 s stale, so two
  dispatches 200 ms apart both take the same free worktree.

#### Identity-addressed mutation

`/api/send` (:2263) today takes a raw `pid` with no `sid`; `send_to_process`:1347–1349 validates
only that *some* claude owns that pid; `index.html`:683 captures the pid into the drawer and POSTs
it minutes later. PID reuse types your instruction into a different agent. `fire_resume`'s own
docstring names this exact failure — *"unattended, a 'continue' typed at the wrong agent is an
injected instruction"* (:2113–2116) — and fixes it for the resume path only.

```python
def act_send(*, sid: str, account: str, text: str,
             actor: str = "http", idem: str | None = None) -> Result:
    """Type `text` at the agent owning `sid`. Resolves sid -> live process by
    PROBING NOW, then verifies (pid, lstart) via probes.proc_identity immediately
    before the write. A client-supplied pid is accepted only as a hint and is
    rejected unless it matches the resolution. PIDs are per-sweep DISPLAY data and
    are never a mutation handle."""
```

Same treatment for `/api/focus` (:2199).

### 2.7 The hard cases, resolved

**Finish — where the two-step state machine lives.** Its state is in four places with four
owners today: `_closeouts[wt]` (:1498, server memory), `c["closeout_sent"]` (:779, injected into
the *observer's* snapshot and garbage-collected there), `window._armFinish` (index.html:730, a 6 s
double-click window that exists **only in the browser**), and the ground truth
(`merge-base --is-ancestor` + `status --porcelain`, re-probed at click time, :1463–1467). Split by
freshness contract:

| piece | home | why |
|---|---|---|
| marker (`phase: armed \| brief_sent \| closing`, `sent_at`, `armed_until`) | `IntentStore`, **durable** | it is a saga step, and `_resumes` already proves the persistence pattern (:1874) |
| guard (`landed and not porcelain`) | PROBES, re-evaluated at **every** transition | `start_finish`:1463–1467 already does this; it is the single most correct thing in the file. Generalise it, do not replace it |
| button label | composed by the publisher from `snapshot ⊕ intents` | never in the snapshot, so both `_cache["t"] = 0.0` pokes (:1476, :1499) **delete themselves** and the button flips in the same HTTP round trip |
| reap ("the flag dies with the terminal") | publisher-thread janitor reading the advisory snapshot | reaping is not a world mutation, so an advisory read is legal. Leaving it in `collect_state`:776–781 makes an always-running collector reap on a schedule nobody asked for |
| the 6 s arm | `intent.armed_until`, **server-side** | non-negotiable before a second client exists: two clients desynchronise instantly, and an APNs action button has nowhere to hold a browser timer |

`start_finish`:1489–1492's *"can't close yet — the brief went to the agent 3m ago"* is already a
saga-state report written as an error return, because no layer owned saga state. It becomes a
phase query.

**Dispatch job state.** `_jobs` (:1626, in-memory, LRU-20), `_closeouts` (:1405, in-memory, reaped
by the collector) and `_resumes` (:1877, persisted to `resume.schedule.json`) merge into one
`IntentStore` with **one explicit durability policy per kind**:

```
durable  (atomic tmp + os.replace, reloaded at boot) : resume, finish
volatile (memory, LRU 64)                            : dispatch, send, focus
```

Dispatch stays asynchronous with a job id because it is 17 s, not because it is a mutation. The
write-ahead ordering is load-bearing and fixes the worst defect in the system today:
`_run_dispatch` creates the tmux session at :1798 but appends to `DISPATCH_LOG` at :1827, after
`sleep(6)` + `sleep(3)` + `deliver_text`. `start.sh`:8 sends SIGTERM, **there is no signal handler
anywhere in orchestra.py**, and every worker is `daemon=True` — so a plain `./start.sh` mid-dispatch
orphans a tmux session running `claude --dangerously-skip-permissions` with a half-delivered brief
and no audit row. Journal `phase: creating` with the tmux name *before* the side effect.

**`read_chat`.** PROBES, request thread, synchronous. See §2.4.

### 2.8 Deployment

**One process, two components, background threads, under launchd `KeepAlive` +
`RunAtLoad` — not `nohup` (start.sh:10).** (Said *"one file"* until ADR 0010; process count is
what this section argues, and that is unchanged.)

Process count and supervision are orthogonal axes and only the first was ever argued. The gap
"no client attached" is closed by a background thread, in-process. The gap "no process running" is
closed by supervision, and `nohup` provides none: crash once and push is gone until the user
notices. A LaunchAgent wrapping the existing process gives KeepAlive, RunAtLoad, log rotation and
a clean SIGTERM contract while leaving the design untouched. A `StartCalendarInterval` LaunchDaemon
can additionally **wake the machine**, so a scheduled resume fires at its reset time rather than at
lid-open — a plist feature applied to one process, not an argument for splitting anything.

Thread inventory:

| thread | owner | perpetual | touches |
|---|---|---|---|
| `observer-sweep` | OBSERVER | yes | probes, `_snap`, memo, hooks, limits cache |
| `observer-git-pool` (8) | OBSERVER | pooled | `probes.git_info` only |
| HTTP accept + per-request workers | SERVER | accept yes | `act()`, `compose()`, `observer.snapshot()` |
| `publisher` | SERVER | yes | `observer.wait_for`, `compose`, `Subscribers`, APNs, intent reap |
| `scheduler` (today's `resume_loop`) | SERVER | yes | `act_resume` via the gateway, nothing else |
| dispatch worker | SERVER | finite | probes, its own `Intent` |

---

## 3. The state model

### 3.1 Structures

```python
@dataclass(frozen=True)
class Snapshot:
    v:          int      # monotonic. Bumps ONLY when the composed view changes.
    at:         float    # wall clock of the sweep that produced it
    cards:      dict     # worktree name -> card  (today's collect_state shape)
    other_procs: list
    counts:     dict
    freshness:  dict     # kind -> wall clock of that kind's last SUCCESSFUL probe
    drift:      int      # cumulative reconcile disagreements, §4.3
    sweep_ms:   float
```

```python
@dataclass
class Intent:
    id:         str      # idempotency key
    kind:       str      # finish | dispatch | resume | send | focus | reserve
    target:     str      # STABLE identity: "wt:voyager-cli" / "sid:<uuid>"
    phase:      str      # armed | running | brief_sent | closing | done | failed | interrupted
    created_at: float
    expires_at: float | None
    payload:    dict
    result:     dict | None
```

### 3.2 Versioning

`v` bumps when the **composed view** differs, not when a probe returns. A sweep that finds nothing
new refreshes `freshness` and publishes nothing. This is what makes the notifier and the delta
stream honest — a version bump means something a client cares about actually changed.

```python
def _publish(self, cards, counts, other, now):
    prev = self._snap
    if prev and cards == prev.cards and counts == prev.counts and other == prev.other_procs:
        self._snap = replace(prev, freshness=dict(self._fresh))   # no version bump
        return
    self._version += 1
    changed = [k for k, c in cards.items() if prev is None or prev.cards.get(k) != c]
    changed += [k for k in (prev.cards if prev else {}) if k not in cards]
    self._hist.append((self._version, tuple(changed)))            # deque(maxlen=512)
    self._snap = Snapshot(self._version, now, cards, other, counts,
                          dict(self._fresh), self._drift, self._sweep_ms)
    with self._cv:
        self._cv.notify_all()
```

**Diff the composed cards by equality, not by a fact-key → card-key dependency map.** A dependency
map is a second source of truth that drifts from the composition the first time someone edits the
pairing heuristic at :186. Nine dict comparisons per publish costs nothing measurable and cannot be
wrong.

### 3.3 Per-field freshness

One `generated_at` cannot say that git is 47 s stale because a `git fetch` wedged. Every snapshot
carries:

```json
"freshness": {"worktrees": 1753100411.2, "procs": 1753100411.2,
              "transcripts": 1753100411.2, "git": 1753100388.6,
              "limits": 1753100122.9, "hooks": 1753100409.8}
```

The board renders a subtle staleness marker per field group when a kind is more than `3 ×` its own
cadence behind. A design whose components can say how old their data is beats one that cannot.

### 3.4 Time-invariance — a hard schema rule

**No field derived from `now()` may appear in the wire payload.** `age_s` (:627) comes off the
wire; `last_write_at` (an absolute float, already computed at :618 and thrown away by `int(age)` at
:627) replaces it. Likewise `commit.ts` not "3h ago", `resets_at` not `resets_in`.

Two independent reasons, both load-bearing:

1. A now-derived field makes every card differ on every publish. The equality diff degenerates,
   deltas become full snapshots, and the notifier fires on nothing.
2. The client can then animate *"wrote 2.3s ago"* from `Date.now()` at frame rate with **zero
   round-trips**. A number that only moves every 5 s reads as frozen. That is a component of the
   felt lag that no server-side change can fix.

During migration `age_s` ships **alongside** `last_write_at` for one release so `index.html`:465 /
:1021 and every existing test keep working; it is removed in step 6.

### 3.5 A delta for a client at version N

```python
def delta_since(self, n: int) -> dict:
    snap = self._snap
    if n <= 0 or not self._hist or n < self._hist[0][0] - 1:
        return {"type": "snapshot", "v": snap.v, "at": snap.at,
                "cards": snap.cards, "counts": snap.counts,
                "other_procs": snap.other_procs, "freshness": snap.freshness}
    keys = set()
    for ver, ks in self._hist:          # deque(maxlen=512)
        if ver > n:
            keys.update(ks)
    return {"type": "delta", "v": snap.v, "base": n, "at": snap.at,
            "cards": {k: snap.cards.get(k) for k in keys},   # None = card removed
            "counts": snap.counts, "freshness": snap.freshness}
```

512-version ring; an unknown or too-old `n` gets a full snapshot. That is the entire resync path.

**Deltas are not shipped for bandwidth.** The payload is 32,381 bytes on loopback and
`reconcileGrid` (index.html:525–545) already does keyed per-card DOM diffing. They exist because
three consumers need exactly this: SSE resume-after-reconnect via `Last-Event-ID`, iOS over a
tailnet after a background suspension, and the notifier's transition detection. They cost ~15 lines
because the per-card change list already exists for other reasons. If they turn out worthless,
deleting them removes 15 lines and no concept — which is why the envelope in §5.3 carries a `type`
field from day one.

---

## 4. Statefulness and its dangers

The strongest attack on the original proposal was this: `collect_state()` is a **pure function of
the world at time `now`**. Nothing carries forward, so **there is no state in which orchestra can be
persistently wrong** — every bug has a lifetime of one collection. `scan_sessions`:619–620
(`if age > window_s: continue`) is free garbage collection; the `pending` dict at :503 is rebuilt
from zero, so a lost `tool_result` cannot strand a session. That is a real engineering asset and it
is not being spent.

### 4.1 The structural rule

> **Statelessness stays the source of truth. The engine answers from the most recent completed
> sweep, never from memory. Retained state may only (a) memoise a pure function on a
> self-invalidating key, (b) schedule the next sweep earlier, (c) carry a hook edge with a TTL, or
> (d) record an intent a human expressed.**

Every failure mode below then degrades to **latency**, bounded by the sweep interval — never to
wrongness.

### 4.2 Byte-offset incremental parsing is rejected

ADR 0006 asserted that a stateful engine that "was watching when the write happened" knows
strictly more than a stateless one. Measured, on this machine, that is false where it matters:

| claim | measurement |
|---|---|
| "parse only new bytes instead of re-tailing 128 KB" | all four parse functions total **97.3 ms of a 1,690 ms collect (5.8 %)** |
| the tail being replaced | **0.9 ms** for a 128 KB read |
| a full parse of the largest transcript (103.8 MB, 2,881 lines) | **178 ms** |
| "precise last-write timestamps vs coarse mtime predicates" | `scan_sessions`:618 **already** computes `age = now - max(mtime, sub_mtime)` at float precision; :627 truncates it with `int()`. The coarseness is the `working_s = 90` *threshold*, not the resolution |
| the real cost inside `scan_sessions` | discovery: `sub_dir.rglob` stats **17,960 subagent files per collection** to keep ~31 in-window sessions — and an offset-keeping engine still has to do it |

And the precedent is bad: `_proven_in_transcript`:2040–2065 is the codebase's **only** offset reader
and it does `f.seek(offset); f.read()` with no inode check and no `size >= offset` check. A short
read returns empty, the function returns False, and `_tmux_resume`:2092–2104 concludes the message
never landed — **and re-sends up to 3 times.** An agent does the work three times, unattended, at
3× usage. Do not generalise a pattern you have one instance of and it is broken.

**Replacement, which recovers ~98 % of the 97 ms for free:** a stat-keyed memo.

```python
class StatMemo:
    """Pure-function cache, not a model. The key contains everything that can
    change the answer, so it is self-invalidating and cannot go stale."""
    def key(self, fp: Path, st: os.stat_result) -> tuple:
        return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
```

Only **one** transcript changes in any given 5 s window, so the memo hits on essentially everything.
`session_topic` (:429–447) is a genuinely pure function of the first 16 KB, which never change, so
it memoises on `(dev, ino)` alone and never expires within an uptime.

### 4.3 Mandatory mitigations

| # | mitigation | cadence | cost |
|---|---|---|---|
| 1 | **cold reconcile** — full `discover_worktrees`, full `git_info` per worktree, full process table, **memo bypassed** so every transcript is re-parsed from the file | every **60 s** | ~0.9 s measured → **1.5 % duty cycle** |
| 2 | **rolling memo audit** — 1/6 of in-window transcripts re-parsed with the memo bypassed and compared | every sweep | ~5 files × 2 ms |
| 3 | **hard max staleness** — never serve a snapshot older than `max_stale_s = 8.0` regardless of nudges | continuous | free |
| 4 | **drift counter** — a reconcile that disagrees with the memo-served view increments `Snapshot.drift`, which is on the wire and on `/api/state` | continuous | free |
| 5 | **PIDs are never cached across a sweep**, and every write re-verifies `(pid, lstart)` | per mutation | one `ps`, single-digit ms |

Undetected drift is the whole risk, so mitigation 4 is not decoration. `drift` appearing on the
board is the signal that something in this section is wrong.

### 4.4 Transcript truncation, rotation, in-place rewrite, compaction

Measured facts about this corpus (2,461 top-level transcripts, 18,740 total `.jsonl`):

- **Transcripts are append-only.** A 25 s live kqueue watch on three active transcripts saw
  `fflags = 0x6` (WRITE|EXTEND) only, sizes strictly increasing, inodes stable.
- **Compaction does not truncate or rotate.** A `user` entry with `isCompactSummary: true` was
  appended *in place* at byte 5,487,135 of a 7,280,567-byte file.
- **Orphaned `tool_use` ids do not accumulate.** Cumulative parse from byte 0 vs. the 128 KB tail
  across 40 recent transcripts: **0 divergence** in pending-tool count. The "session pinned to
  ■ BLOCKED forever" fear is not supported by this corpus.

Because offsets are rejected, the only places any of this can bite are two, and both are handled:

```python
# 1. _proven_in_transcript — the ONLY offset reader. Fix today, independent of
#    everything else in this document.
def _proven_in_transcript(fp, mark, text, timeout_s=20.0):
    """mark is (st_dev, st_ino, offset), captured immediately before the send."""
    dev, ino, offset = mark
    ...
    st = os.stat(fp)
    if (st.st_dev, st.st_ino) != (dev, ino) or st.st_size < offset:
        offset = 0                 # rotated, replaced, or shorter than we left it
    with open(fp, "rb") as f:
        f.seek(offset)
        chunk = f.read()
```

```python
# 2. Compaction poisons "the last thing you told it". The 22,877-char compact
#    summary is a `user` entry beginning "This session is being continued from a
#    previous conversation that ran out of context..." and _real_prompt (:418-426)
#    ACCEPTS it, because _MACHINE_TEXT (:413) does not match it. Add:
_MACHINE_TEXT = re.compile(r"...|^This session is being continued from a previous "
                           r"conversation that ran out of context", re.I)
# and skip any entry carrying isCompactSummary: true outright.
```

Also worth recording: across the entire corpus there are **zero** `"type":"summary"` entries, so
`session_topic`:436's summary branch is dead code and the README's claim that topics come from the
compaction summary is already false. Re-check before relying on it.

**What happens to a byte offset when a transcript is compacted:** nothing — compaction appends. The
answer matters only for `_proven_in_transcript`, whose mark is seconds old, and the `(dev, ino,
size)` guard above covers the general case regardless.

### 4.5 Sleep / wake

Measured on this machine: `time.monotonic()` is `mach_absolute_time()` and **includes sleep** —
644,530.1 s monotonic vs 644,524.9 s wall-since-boot over 179 hours, i.e. 5.2 s of NTP drift, not
hours of missing sleep. Timers fire on wake. So the sleep hazard is not "the clock lied"; it is
"nothing ran for nine hours and everything we hold is from yesterday".

```python
def _detect_wake(self, now: float) -> bool:
    """A sweep that should have happened 1s ago happened 9 hours ago."""
    gap = now - self._last_sweep_wall
    if self._last_sweep_wall and gap > max(30.0, 3 * self._reconcile_s):
        self._memo.clear()                  # everything is suspect
        self._hooks.clear()                 # edges from before the lid closed
        self._limits.expire()
        self._force_cold = True
        self._quiet_push_until = now + self._reconcile_s   # ONE full reconcile
        return True
    return False
```

Two consequences that are not optional:

- **Push is suppressed for one full reconcile cycle after wake and after start.** Otherwise opening
  the lid delivers forty notifications about transitions that happened at 3 a.m.
- **Intents survive.** A resume armed for 3 a.m. must still fire. `resume_loop` compares `due_at`
  against wall-clock `time.time()` and its schedules persist to `resume.schedule.json` — that is
  already correct across both sleep and restart and it is the most sleep-robust code in the file.
  **Do not redesign it.**

If a kqueue watcher is ever built (§10), it is re-bound from scratch on wake. Since its only output
is `nudge()`, a stale or orphaned watch costs nothing but a beat of latency.

### 4.6 Missed-event detection

There is no event we depend on, by construction — that is what §4.1 buys. The detectors that exist
are therefore about *quality*, not correctness:

- `Snapshot.drift` (§4.3) — reconcile vs. memo disagreement.
- `freshness[kind]` — a producer that stopped producing is visible on the board, not silent.
- `hook_expired_total` in `observer.stats()` — hooks arriving and then expiring without a
  corresponding inferred transition means the hook installation is lying, or a sweep is missing
  something.

### 4.7 Memory bounds over multi-day uptime

| structure | bound |
|---|---|
| `StatMemo` | LRU 4,096 entries, keyed `(dev, ino, size, mtime_ns)`; cleared on wake |
| `session_topic` memo | LRU 2,048 keyed `(dev, ino)` — pure over the first 16 KB, never expires within an uptime |
| hook edges | dict pruned every sweep by `HOOK_TTL_S = 90` |
| `Snapshot` history ring | `deque(maxlen=512)` of `(version, changed_keys)` — versions only, not snapshots |
| intents, volatile | LRU 64 per kind |
| intents, durable | reaped 24 h after terminal phase (today's `resume_loop` prune at :2180 already does exactly this) |
| SSE subscribers | hard cap `MAX_SUBSCRIBERS = 32`, rejected beyond with 503 (ADR 0005) |
| push dedup set | LRU 512 `(target, transition)` pairs, persisted (§10) |

The failure this prevents is concrete: an unbounded per-session map grows one entry per agent-start
forever and the visible symptom is the header at `collect_state`:796–801 saying "12 working" when 3
are. That number is what the user reads to decide whether to start another agent.

---

## 5. The serving layer

### 5.1 Resolved: SSE on the existing `ThreadingHTTPServer`. No rewrite.

This was the gating risk and it is retired by measurement, twice, independently.

```
12 SSE clients open                    → 14 threads alive (1/client + main + accept)
broadcast → first-client latency       → 0.45–0.68 ms
normal GET while 12 streams held open  → 21.2 ms (not starved)
after 12 rude disconnects              → 0 subscribers, 2 threads   ← fully reclaimed
```

Pushed harder against a real `ThreadingHTTPServer`:

| concurrent SSE streams | threads | RSS | p95 event latency | unrelated `GET /ping` |
|---|---|---|---|---|
| 3 | 6 | 21.7 MB | 0.3 ms | 21 ms |
| 50 | 53 | 21.7 MB | 0.3 ms | 12 ms |
| 200 | 203 | 31.7 MB | 0.4 ms | 13 ms |
| **500** | **503** | **49.3 MB** | **1.1 ms** | **19 ms** |
| 800 | 809 | 93.4 MB | 4.2 ms | 5005 ms ← breaks |

**Supported concurrency: 500 long-lived streams; we cap at 32.** The requirement is a browser in a
few tabs plus a phone. `asyncio` or a `selectors` rewrite would buy a ceiling 100× above
requirement at the cost of rewriting `Handler` (:2192–2284) and every synchronous actuator. Rejected
outright.

The daemon-thread leak that was expected does not exist: `socketserver._Threads.append`
early-returns on daemon threads and `ThreadingHTTPServer` sets `daemon_threads = True`.

### 5.2 The exact changes required

```python
class Handler(BaseHTTPRequestHandler):
    # PINNED. SSE over HTTP/1.1 without chunked framing or `Connection: close`
    # hangs EventSource forever waiting on a Content-Length that never arrives.
    # BaseHTTPRequestHandler defaults to 1.0; do not "modernise" this.
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        if self.path.startswith("/api/events"):
            return self._sse()      # EARLY RETURN — do_GET (:2193-2245) funnels every
                                    # route into `body = ...` then unconditionally
                                    # sets Content-Length at :2241
        ...

class Srv(ThreadingHTTPServer):
    daemon_threads = True           # already the default; stated so it is not "fixed"
    request_queue_size = 256        # socketserver's default of 5 is never overridden
                                    # at :2296; measured connect timeouts under burst
    def handle_error(self, request, client_address):
        # ADR 0005: a dropped SSE client raises ConnectionResetError and
        # socketserver prints a full traceback. Every tailnet blip would spam stderr.
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            super().handle_error(request, client_address)
```

Streaming was verified against the **real** `Handler` subclassed from `orchestra.py`: headers
`HTTP/1.0 200 OK` + `Content-Type: text/event-stream`, then events delivered at t+0.004 s, +0.310,
+0.613, +0.919, +1.229 — genuinely incremental. A concurrent `GET /api/state` returned 4,485 bytes
in 70 ms while the stream was open. Total structural change: **one early return, ~14 lines.**

### 5.3 The frame envelope

```
id: 41207
event: state
data: {"type":"delta","v":41207,"base":41203,"at":1753100411.2,
       "cards":{"voyager-cli":{...}},"counts":{...},"freshness":{...}}

: keepalive          ← every 25 s on cv.wait(timeout=25) expiry
```

`type` is present from the first release even though the first release only ever sends
`"snapshot"`. That single field is the entire hedge that lets deltas arrive later with no version
negotiation.

### 5.4 The real ceiling is the browser, not the server

Browsers cap **6 connections per origin**. One `EventSource` per tab plus `dispatchPoll` at
index.html:1084 (1 s) starves POSTs at 3 tabs and deadlocks at 6. The server survives 500 streams;
the browser dies at 6 tabs.

**Client contract: one `EventSource` per browser**, held in a `SharedWorker` and fanned to tabs via
`BroadcastChannel`, with per-tab conditional polling as the fallback when `SharedWorker` is
unavailable. This is in the design, not to be discovered in production.

### 5.5 What `/api/state` becomes

A dict read of `compose(observer.snapshot(), intents)`. That kills the thundering herd in
`cached_state` (:996–1003, no lock) where N tabs expiring together each fire a full collection —
measured **3430 ms for 4 concurrent collectors vs 1408 ms for one**, i.e. self-inflicted load that
worsens the very lag being complained about.

---

## 6. The status model, revisited

### 6.1 What precise write timestamps actually buy

Not resolution — `scan_sessions`:618 already has float precision and :627 throws it away. What they
buy is that a **threshold crossing becomes an event you can schedule instead of a condition you
discover on a poll**.

```python
def _next_edge(self, now: float) -> float:
    """Earliest wall time at which the composed view could change with NO new
    input. Status boundaries are functions of absolute stamps, so they are exactly
    predictable — the engine FIRES the transition instead of noticing it late."""
    t = now + self._idle_s
    for s in self._sessions:
        if s["status"] == "working" and not s["tool_running"]:
            t = min(t, s["last_write_at"] + CFG["quiet_s"])
    for sid, (ev, at) in self._hooks.items():
        t = min(t, at + HOOK_TTL_S)
    for it in self._intents_armed:
        t = min(t, it.armed_until)
    return t
```

The sweep thread's wait is bounded by `_next_edge`. ● WORKING → ◆ YOUR TURN publishes **at** the
crossing, to the millisecond, at zero cost.

### 6.2 Splitting the 90 seconds

`CFG["working_s"] = 90` is doing three different jobs and they need three different fixes:

| what the 90 s covers | removable? | fix |
|---|---|---|
| **poll granularity** — up to 10.6 s before anyone even looks | **yes, entirely** | continuous sweep + `_next_edge` + SSE |
| **parse / heuristic coarseness** — `int(age)` truncation, one-second sweep grid | **yes** | absolute `last_write_at`, client-side animation |
| **genuine silence while an agent thinks** — a real gap between transcript writes with nothing wrong | **NO** | must be covered by *explanation*, not by *waiting* |

The third is the one that matters and it is why `working_s` cannot simply be set to 5. The
resolution is that **most long silences are already explained by a signal we have**, and the
timeout only has to cover the unexplained remainder:

```python
QUIET_S = 25.0          # unexplained silence before a live agent is "your turn"
HOOK_TTL_S = 90.0
FLICKER_DWELL_S = 3.0
```

A live session does **not** fall out of `working` on the quiet timer at all when any of these hold —
this is `classify_session`:557–579 restated, and five of its six branches never consulted `age_s`
in the first place:

- a hook says `PreToolUse` / `Notification` / anything but `Stop` and has not expired
- `pending_tools` is non-empty (a tool is running — silence is expected and correct)
- `delegated` is non-zero (waiting on its own workflows or background agents)
- `shells` is non-zero (a backgrounded Bash leaves the transcript idle until it exits)

So `QUIET_S` covers only: a live agent, no pending tool, no delegation, no shell, no hook, and no
transcript write. That genuinely is "it has gone quiet", and 25 s is generous for it.

### 6.3 The anti-flicker rule, concretely

Flicker is worse than lag. Three rules, all cheap:

**(a) Asymmetric hysteresis.** Escalation is immediate; de-escalation waits.

```python
LOUDER = {"needs_input": 0, "limit": 1, "blocked": 2, "working": 3,
          "waiting": 4, "ended": 5}     # lower number = louder

def settle(prev, proposed, now, since):
    if prev is None or LOUDER[proposed] < LOUDER[prev]:
        return proposed, now                     # escalate instantly, always
    if now - since < FLICKER_DWELL_S:
        return prev, since                       # de-escalation must dwell
    return proposed, now
```

Any transition toward *more* attention (working → needs_input, waiting → blocked, anything →
limit) publishes on the sweep that sees it. Any transition toward *less* attention must hold for
`FLICKER_DWELL_S = 3.0` before it publishes.

**(b) A single write resets the quiet clock.** `last_write_at` is `max(mtime, sub_mtime)` including
subagent directories, so a session whose main transcript is idle while a workflow writes stays
`working`. This is existing behaviour (:601–618) and it is preserved exactly.

**(c) An expiring hook never changes the displayed status by itself.** When a hook edge expires,
inference resumes — but the result is passed through `settle()` like anything else, so a hook
expiry can only produce a visible change 3 s later and only if inference genuinely disagrees. This
matters because a hook expiry is a publish nobody asked for.

**Landing order is a constraint, not a preference:** `working_s` 90 → `QUIET_S` 25 lands **after**
the continuous sweep, `_next_edge` and hooks exist. Tightening the threshold while the *discovery*
of the crossing is itself a 5 s poll produces exactly the flicker this section forbids.

---

## 7. Signal sources, ranked

```
hooks (observed)  >  process table  >  precise file writes  >  mtime heuristics  >  tmux capture-pane
```

| rank | source | ingested by | confidence | freshness contract |
|---|---|---|---|---|
| 1 | **Claude Code hooks** | `POST /api/hook` → `observer.hook(sid, event, at)` | 100 | edge, **hard 90 s TTL** |
| 2 | **process table** | `probes.claude_processes()` in every sweep | 90 | ≤ sweep interval |
| 3 | **precise file writes** | `stat()` of transcript + subagent dirs, absolute `last_write_at` | 80 | ≤ sweep interval |
| 4 | **mtime heuristics** | `QUIET_S` threshold via `_next_edge` | 60 | derived |
| 5 | **tmux `capture-pane`** | only inside actuation (`composer_idle`:1697, `_wait_composer_idle`:2025) | 40 | on demand, never on the observe path |

### 7.1 Hooks

Claude Code hooks carry `session_id` and `transcript_path` — exactly the key `scan_sessions`
already uses (`fp.stem`, :626). One route, one dict:

```python
def ingest_hook(payload: dict) -> dict:
    sid = payload.get("session_id")
    if not sid:
        return {"ok": False, "error": "no session_id"}
    observer.hook(sid, payload.get("hook_event_name", "?"), time.time())
    return {"ok": True}
```

`Stop` collapses working → waiting instantly. `Notification` makes ▲ NEEDS ANSWER **ground truth**
instead of sniffing `AskUserQuestion` out of `pending_tools` (:565), which is the weakest signal on
the board. This directly retires three pieces of guesswork: the `AskUserQuestion` sniff (:565),
`skip_perms` parsed off the command line (:653–654), and `shell_children`'s ancestry walk
(:296–315).

The route lives on the **SERVER**. One HTTP listener. The three-layer proposal had the ENGINE
"hosting an inbound hooks endpoint" while the BROKER "owns HTTP" — two listeners or a
boundary reach-through, in the first paragraph of a spec about hard interfaces.

### 7.2 The TTL is not optional

A hook is **edge-triggered**. One dropped because the server was restarting would otherwise leave a
session pinned to a status nothing ever corrects — the exact failure the whole architecture exists
to prevent, since no push would fire for it either. `HOOK_TTL_S = 90` turns hooks into a latency
reduction that can never become wrongness. That single rule is what makes hooks safe to ship.

### 7.3 Reconciliation when sources disagree

```python
def status_for(sess, hooks, now):
    ev = hooks.get(sess["sid"])
    if ev and now - ev.at < HOOK_TTL_S:
        st = HOOK_STATUS.get(ev.event)          # Stop->waiting, Notification->needs_input, ...
        if st and sess["alive"]:                # rank 2 VETOES rank 1: a hook cannot
            return st, "observed"               # claim a dead process is waiting
    return classify_session(...), "inferred"    # today's function, unchanged
```

Two rules:

- **A higher-ranked source wins on the same question.** A hook beats inference about *what the
  agent is doing*.
- **A lower-ranked source vetoes on a question the higher one cannot answer.** The process table
  beats a hook about *whether the agent exists*. A hook from a process that has since exited yields
  ○ ENDED, not the hook's status.

`card["status_src"]` (`"observed"` | `"inferred"`) ships on the wire so the board can be honest.

### 7.4 Degradation

| absent | effect |
|---|---|
| hooks (an agent the user started themselves) | falls to rank 2–4 — exactly today's behaviour. No session is ever *worse* than today, and the board must not present unhooked sessions as untrustworthy in a confusing way |
| process table (`ps` / `lsof` slow or wedged) | `freshness["procs"]` goes stale and the board says so; statuses hold their last value through `settle()` |
| transcripts unreadable | session drops out of the window, exactly as today (:619–620) |
| tmux | actuation falls back to AppleScript for Terminal.app / iTerm2, unchanged |

**Adoption is the hard part and it is out of scope here.** Agents dispatched *by* orchestra can be
configured automatically; agents the user starts independently cannot. The installation flow must
not hijack the user's own `settings.json` hooks. ADR 0007 fixes the direction; the mechanism is an
open question (§11).

### 7.5 cclimits — the fifth input class

Neither file, nor process, nor git. The OBSERVER owns a rate-limited **network** poller because
`collect_state`:704 needs `limits_by_account()` to emit ⛔ LIMIT HIT at all, and `_limits["data"]`
is warmed today only by a browser hitting `/api/limits` (:2208). Run the engine with zero clients —
the entire point — and the limit-hit push never fires.

```python
LIMITS_NEAR_S = 300.0     # any account within reserve of a cap, or any resume armed
LIMITS_IDLE_S = 900.0     # nothing is close to anything
```

`set_reserve` (:1110–1142) writes `CFG` only; `reserve_blocked` is recomputed at compose time.

---

## 8. What this gives the iOS client

The phone consumes **the same publish point, the same envelope and the same delta protocol as the
browser**. There is no phone-specific state path. That is the whole point of doing this before the
app exists.

```
                        Observer.wait_for(v)
                                │
                         compose(snapshot ⊕ intents)   ← one composition, all consumers
                                │
             ┌──────────────────┼───────────────────┐
             ▼                  ▼                   ▼
       browser SSE         iOS SSE / poll      notifier → APNs
       (SharedWorker)      (Last-Event-ID)     (dedup, quiet window)
```

Four contract items are irreversible in the "would iOS be rebuilt" sense, and all four land before
any client code is written:

| contract | why it cannot be deferred |
|---|---|
| **absolute `last_write_at`, no `age_s`** | a server-computed relative age forces a round-trip just to look alive, and makes every card differ on every publish. Both clients animate locally. |
| **`/api/send` and `/api/focus` take `{sid, account}`, never `pid`** | a phone that can type into a stranger's shell is not a bug you patch later. `send_to_process`:1347 confirms only that *some* claude owns the pid |
| **the `{type, v, at}` frame envelope** | deltas, resync via `Last-Event-ID`, and any future frame type slot in with no version negotiation |
| **the finish arm server-side** | an APNs action button has nowhere to hold a 6 s browser timer, and two clients desynchronise instantly |

Everything else in this document is internal and invisible from outside the process.

The notifier is ~15 lines and needs nothing added to the design:

```python
def notifier(observer, stop):
    v, prev = observer.snapshot().v, dict(observer.snapshot().cards)
    while not stop.is_set():
        snap = observer.wait_for(v, timeout=30.0)
        if snap is None:
            continue
        if time.time() < observer.quiet_push_until:      # §4.5
            v, prev = snap.v, dict(snap.cards); continue
        for name, card in snap.cards.items():
            was = (prev.get(name) or {}).get("status")
            now_ = card.get("status")
            if was != now_ and now_ in ("needs_input", "limit", "blocked"):
                push(name, now_, card)                   # dedup LRU, §4.7
        v, prev = snap.v, dict(snap.cards)
```

APNs itself stays stdlib: `openssl dgst -sha256 -sign key.p8` for ES256 (with the mandatory
DER → raw `r||s` 64-byte conversion, which is the sharp edge) and `curl --http2` via the linked-in
nghttp2 1.67.1. Both binaries ship with macOS. No package.

---

## 9. Migration

Every step is independently shippable, independently valuable and independently revertable. Every
step keeps the stdlib `unittest` suite green.

> **The "one file" constraint is OBSOLETE — superseded by
> [ADR 0010](adr/0010-split-into-a-package.md), shipped.** This section used to add: *"which
> constrains the design: **all functions stay at module scope in `orchestra.py`**, because the
> suite loads the file by path via `importlib.util.spec_from_file_location` and monkeypatches
> module globals … Splitting into `probes.py` / `observer.py` / `server.py` would break that
> patch point in ~40 setUps in the same commit that moves 2,300 lines of behaviour — deleting
> the safety net and doing the dangerous thing simultaneously."*
>
> The objection was real about the hazard and wrong about the remedy, and ADR 0010 answers it
> with two measures taken **before** any code moved:
>
> 1. `tests/characterize.py` — 1,589 cases across 13 functions, byte-compared against a recorded
>    golden, monkeypatching **nothing**. The split could not disarm it, and it resolves either
>    layout, so it doubles as proof the facade is complete. It was verified to actually fail by
>    reintroducing a known regression.
> 2. **Import modules, not names** — every cross-module reference goes through the module object
>    (`from . import gitrepo` … `gitrepo.git_info(…)`), which keeps attribute lookup late and so
>    keeps every patch point alive. The 67 patch sites migrated to their canonical module
>    (`fb.shell.run`, `fb.procs.claude_processes`, `fb.config.DEMO`, …), one module per commit.
>
> The steps below are otherwise unaffected — they were always about behaviour, not file layout.
> Their `orchestra.py:NNN` citations are historical; grep for the symbol instead.

Baseline before starting: 142 tests, 15.4 s, one pre-existing environmental failure
(`test_send_keys_reaches_the_shell`, tmux).

### Step 0 — three live bugs, today, no architecture (behaviour-changing)

| fix | why |
|---|---|
| `_proven_in_transcript`:2046–2048 gets `(st_dev, st_ino)` + `size >= offset` | a short read makes `_tmux_resume`:2092–2104 re-send up to 3 times, unattended, at 3× usage |
| `fire_resume`:2127–2134 guards on a probe, not `cached_state()` | an armed 3 a.m. resume currently marks itself `done` without firing |
| `_MACHINE_TEXT`:413 matches the compact-summary preamble | the board shows a machine-written summary as "the last thing you told it" |

**Observable:** nothing, until the bugs would have bitten. **Tests:** three new unit tests.
**Rollback:** revert; they are independent one-function edits.

### Step 1 — the git storm (pure refactor, behaviour-identical)

**This is step 1 because the user should feel the board get faster before any architecture lands.**

1. **First**, write a golden-equivalence test that diffs new-vs-old `git_info` field-for-field
   across the user's real worktrees. This is what caught the detached-HEAD regression: porcelain v2
   emits `# branch.head (detached)` and carries no `detached@<sha>` label, and three of nine
   worktrees are detached right now — a third of the board would have silently rendered
   `branch: null`.
2. `ThreadPoolExecutor(16)` across worktrees, `git_info` body untouched: **4029 → 710 ms**.
3. Collapse 5 spawns to 2 with the `(detached)` → `detached@<branch.oid[:9]>` mapping:
   **710 → 491 ms**, verified 0/9 field mismatches.
4. Move `sub_dir.rglob` (:601–609) **after** the `age > window_s` check (:619).

**Concurrency is the win; the flag collapse is the garnish** — serial-2 only reaches 669–2929 ms.
Any plan that leads with the flag collapse is mis-attributing its own numbers.

**Observable:** the board visibly quickens; `/api/state` returns in ~250–500 ms instead of
1.6–4.1 s. **Tests:** the golden test, plus the existing suite unchanged. **Rollback:** revert the
commit; nothing else depends on it.

### Step 2 — the publish point (mostly refactor; one behaviour change)

`Observer` with one sweep thread publishing versioned immutable snapshots. `cached_state()` becomes
a dict read that **falls back to a synchronous `collect_state()` when the sweep thread is not
running**, so tests that set `fb._cache["state"] = None` keep working untouched.

Ships in the same step: `last_write_at` on the wire **alongside** `age_s`, and index.html animating
the age locally from `Date.now()`.

**Observable:** `/api/state` is O(1); N tabs no longer trigger N concurrent collections; "wrote 2.3s
ago" ticks smoothly instead of jumping every 5 s. **Tests:** all 142 green plus new tests for
version monotonicity, `freshness`, and the sync fallback. **Rollback:** don't start the thread —
`cached_state()` degrades to exactly today.

**This is the keystone.** It is the interface iOS consumes, and every deferred design converges on
it, which is what makes a later split a transport change rather than a redesign.

### Step 3 — SSE (additive)

`/api/events` off the publish point: one early return in `do_GET` before the Content-Length tail
(~14 lines), `request_queue_size = 256`, `handle_error` override, `protocol_version` pinned with a
comment. Client: one `EventSource` per browser via `SharedWorker` + `BroadcastChannel`; the 5 s poll
stays as the fallback for one release.

**Observable:** board updates land in well under a second. **Tests:** a subscriber-lifecycle test
(connect, receive, rude disconnect, threads reclaimed) and a cap test. **Rollback:** stop serving
the route; the poll is still there.

### Step 4 — the `act()` gateway (behaviour-changing)

Per-target `Leases`, idempotency keys, `mutating()` guard + `AdvisoryReadInMutation`,
identity-addressed `/api/send` and `/api/focus`, `_pick_defaults` re-probing instead of reading the
snapshot.

**Observable:** two tabs pressing ✓ finish produce one closeout brief; a 7-minute resume no longer
blocks a chat message to a different agent; a stale pid in an open drawer is rejected instead of
obeyed. **Tests:** concurrent double-fire, lease contention naming the holder, guard raises,
pid-mismatch rejection. **Rollback:** the gateway is a wrapper — bypassing it restores today's
behaviour per route.

### Step 5 — `IntentStore` and the finish saga (behaviour-changing)

`_jobs` / `_closeouts` / `_resumes` merge; durable kinds journal atomically; `closeout_sent` is
composed at serve time next to where `resumes` already rides along (:2196); both `_cache["t"] = 0.0`
pokes (:1476, :1499) and the `_closeouts.pop` in `collect_state` (:776–781) are deleted; the arm
moves out of `window._armFinish` (index.html:730) into `intent.armed_until`.

**Observable:** the ✓/✕ button flips in the same round trip and agrees across tabs; finish state
survives a restart. **Tests:** one existing test rewritten
(`test_closeout_flag_rides_the_card_and_dies_with_the_terminal`), plus journal round-trip and
arm-expiry tests. **Rollback:** keep the in-memory dicts as the store; only the composition changes.

### Step 6 — hooks and the status model (behaviour-changing)

`POST /api/hook` → `observer.hook()` with `HOOK_TTL_S = 90`, consulted ahead of the quiet branch;
`_next_edge()`; `settle()` anti-flicker; **then** `working_s` 90 → `QUIET_S` 25. `age_s` leaves the
wire.

**Observable:** ● WORKING stops lying; ▲ NEEDS ANSWER becomes observed for hooked agents. **Tests:**
hook ingest, TTL expiry falls back to inference, escalate-immediately / de-escalate-after-dwell,
`_next_edge` fires at the crossing. **Rollback:** stop consulting hooks and restore the constant —
two lines.

### Step 7 — the limits poller (additive)

OBSERVER-owned, 300 s / 900 s cadence; `set_reserve` writes `CFG` only.

**Observable:** ⛔ LIMIT HIT appears without anyone having opened the limits tab. **Tests:**
zero-client limit annotation; `set_reserve` no longer mutates `_limits`. **Rollback:** disable the
poller; `/api/limits` still warms it as today.

### Step 8 — supervision and durability (behaviour-changing, no UI)

SIGTERM/SIGINT handler (none exists anywhere in the file); write-ahead dispatch journal row before
`create_tmux` at :1798; launchd plist with `KeepAlive` + `RunAtLoad` replacing `nohup` (start.sh:10).

**Observable:** a plain `./start.sh` mid-dispatch no longer orphans an agent without an audit row;
the server comes back by itself after a crash. **Tests:** journal-before-effect ordering; graceful
shutdown settles a running intent. **Rollback:** the plist is a file; `nohup` still works.

### Step 9 — APNs and the iOS client

Notifier off the same versioned stream, push dedup persisted, then the app. Nothing in the contract
moves.

---

## 10. What we are not building

| not building | why |
|---|---|
| **ENGINE / BROKER / ACTUATOR** | cut on verbs; ACTUATOR fails every clause of the component rule (§2.2). Its two headline rules — global serialisation and actuator-must-not-read-engine — are respectively fatal and already satisfied |
| **A fact store with independent producers** | breaks the one-pass consistency `pair_sessions_with_procs`:186–199 and the succession pass at :735–745 require (§2.3). Its three best payload ideas are adopted |
| ~~**Splitting into `probes.py` / `observer.py` / `server.py`**~~ **— REVERSED, and built.** [ADR 0010](adr/0010-split-into-a-package.md) | the original entry read: *"breaks the module-global monkeypatch idiom all 142 tests are built on, for a boundary that would still be a convention."* Both halves were answered rather than waited out: `tests/characterize.py` (1,589 cases, patches nothing) replaced the monkeypatch idiom as the safety net, and module-object imports (`from . import gitrepo`, never `from .gitrepo import git_info`) kept every patch point alive at its canonical module. The boundary is now the import graph, not a convention |
| **Byte-offset incremental parsing** | 5.8 % of the collect; the 128 KB tail it replaces costs 0.9 ms; the stat-keyed memo recovers ~98 % for free; and it forfeits the file's best property — no bug outlives one sweep (§4.2) |
| **kqueue watches** | a full stat sweep over 532 transcripts is **7.8 ms** and exactly **one** file changes in a 5 s window. A directory watch says the directory changed, not which entry; subagent `.jsonl` are *created* (~982/day, peak 4,123) in nested dirs, so per-file watches cannot see them. **If ever built: nudge-only, hard-capped at 256 fds, never a source of truth.** Note the fd correction below |
| **Per-client delta computation as a protocol** | 32,381-byte payload on loopback; `reconcileGrid` already diffs per card. Kept only as a byproduct of per-card versioning, behind the `type` field |
| **Broker auth, rate limiting, per-device topic filters, token rotation** | inventing requirements for a single-user loopback tool. A Tailscale ACL plus one shared-secret header is the whole story; idempotency is already in `act()` |
| **`adopt_orphans()` on restart** | deciding whether a half-delivered brief landed depends on `_proven_in_transcript`, which must be fixed and trusted first. Step 8's write-ahead row at minimum leaves an audit trail naming the orphan |
| **A `launchd` daemon split of the OBSERVER** | supervision, not process count, was the real want (§2.8) |

**Correction to `VERIFIED-FACTS.md`, to be folded in:** the fd ceiling for a watch set is **not**
`ulimit -n` (1,048,576). It is `kern.maxfilesperproc = 61,440` with `kern.maxfiles = 122,880`
**system-wide**. Registering all 18,740 transcripts drove `kern.num_files` from 17,349 to 36,120 —
30 % of the per-process cap and 15 % of the global table. A dashboard must never be able to stop
other applications opening files. The original statement ("any design that rejects kqueue on
fd-exhaustion grounds is wrong") is true at `ulimit` scale and false at the real one; kqueue is
deferred for the measurement reasons above regardless, and capped at 256 fds if it is ever built.

**Known weaknesses of what we *are* building**, stated so nobody rediscovers them as surprises:

- The `mutating()` thread-local does not follow a thread spawned inside an actuation. The dispatch
  worker sets it explicitly; the backstop for everything else is a grep test, not the type system.
- ~~Two components in one file makes the import-direction rule a convention.~~ **No longer a
  weakness** — ADR 0010 split the file, so `server` imports `observer` and the import system
  enforces the direction. Zero dependencies survived intact; only the *one file* clause was
  traded.
- Per-target leases that fail fast can tell a user "busy, try again". For a 40 ms tmux write that is
  worse than a 50 ms queue wait. Bounded at 2 s with the holder named; the right policy is a UX
  judgement to settle by watching the user hit it (§11).

---

## 11. Open questions

Each with a recommendation. None blocks step 1.

**1. `QUIET_S` — is 25 s right?**
It is a guess. The honest way to set it is to measure the distribution of *unexplained* inter-write
gaps (live agent, no pending tool, no delegation, no shell) and put `QUIET_S` at p99.
**Recommendation:** ship step 2 with an `observer.stats()` histogram of unexplained gaps, run it for
a week, then set the constant from data. Until then 25 s with `settle()`'s 3 s dwell.

**2. Lease contention policy — fail fast or short queue?**
Currently: 2 s bounded wait, then fail naming the holder. **Recommendation:** keep fail-fast, and
revisit only if the user actually hits it. A 12-minute silent wait is unarguably worse; a "busy"
toast is merely mildly annoying.

**3. cclimits cadence at 3 a.m.**
300 s when anything is near a cap or a resume is armed, 900 s otherwise. Too aggressive spends the
user's cclimits calls for nobody; too lazy makes the limit-hit push up to 15 minutes late — the
exact notification the architecture exists to deliver. **Recommendation:** ship the two-tier
cadence, add a `limits_polls_today` counter, and tune once there is a week of data.

**4. Hook installation mechanism.**
Agents orchestra dispatches can be configured automatically. Agents the user starts cannot, and the
flow must not hijack their own `settings.json` hooks. **Recommendation:** a `orchestra hooks install`
command that writes an *additive* hook entry pointing at a small shim, plus a board banner offering
it per unhooked account. Needs the user's call on how invasive that is allowed to be.

**5. Does `working_s` stay in `orchestra.config.json`?**
It is being replaced by `QUIET_S` with different semantics. **Recommendation:** accept `working_s`
as a deprecated alias for one release, log once when it is set, then remove.

**6. ADRs to raise before step 2.**
This document reverses part of ADR 0006 (statefulness) and refines 0005 and 0007.
**Recommendation:** raise three — *0008: two components, actuation is a gateway*; *0009: stateless
per sweep remains the source of truth, incremental offsets rejected*; *0010: mutations are
identity-addressed*. Mark ADR 0006 "amended by 0009" rather than editing it; the history is the
value.

**7. Does the board surface `drift` and `freshness`?**
They are on the wire either way. **Recommendation:** yes, but quietly — a small staleness marker
per field group and a `drift > 0` indicator in the footer. If they are invisible, nobody will ever
notice the engine has gone wrong, which defeats §4.3.
