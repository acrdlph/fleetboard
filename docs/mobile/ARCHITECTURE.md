# orchestra — backend & system architecture

**Status:** design, not yet built. This is the plan of record for evolving orchestra from a
loopback-only web board into a system that serves both that board and a native iOS client
over a Tailscale tailnet, without losing what makes it orchestra.

Every number tagged **[M]** was measured on the author's machine against the live fleet
(9 worktrees / 33 sessions / 8 Claude homes / 697 transcripts / 1.10 GB). Numbers tagged
**[E]** are estimates and are labelled as such. Line references are `orchestra.py` at the
commit this document was written against (2302 lines).

> **Path prefix: this document says `/api/v2/…`; the shipping contract is `/api/v1/…`.**
> `API.md` §0 settled it — the *new* surface is v1 because it is the first versioned API,
> and the pre-existing unversioned paths (`/api/state`, `/api/send`, …) are frozen legacy,
> not "v1". Read every `/api/v2/x` below as `/api/v1/x`, and `/api/health` as
> `GET /api/v1/health` + `GET /api/v1/meta` (this document's `features[]` splits across the
> two). `API.md` §0.1 is the full alias table and it wins on every name, field and shape.
> The reasoning in this document is unaffected; only the spellings are.

---

## 1. The shape of the system today

One python3 file. No dependencies. It reads three things it does not own — git worktrees,
Claude Code transcript JSONL, and the process table — and derives a board from them. When
you click, it types into the terminal the agent is running in.

```
  ┌──────────────────────── the Mac ────────────────────────┐
  │                                                          │
  │  ~/.claude*/projects/**/*.jsonl   git worktrees   ps/lsof│
  │            │                           │            │    │
  │            └───────────┬───────────────┴────────────┘    │
  │                        ▼                                 │
  │              collect_state()  ← 4 s TTL, no lock          │
  │                        │      ~36 git forks per miss      │
  │                        ▼                                 │
  │   ┌──────────────────────────────────────────────┐       │
  │   │  ThreadingHTTPServer  127.0.0.1:4242          │       │
  │   │  HTTP/1.0 · no keep-alive · no gzip · no auth │       │
  │   │  GET  /api/state /topology /limits /chat      │       │
  │   │       /dispatchlog /dispatch/status /focus    │       │
  │   │  POST /api/send /finish /dispatch /reserve    │       │
  │   │       /resume/schedule /resume/cancel         │       │
  │   └──────────────────────────────────────────────┘       │
  │        │                        │                        │
  │        │ every 5 s              │ tmux send-keys          │
  │        ▼                        ▼ osascript               │
  │   index.html                claude --dangerously-         │
  │   map.html                  skip-permissions              │
  │   limits.html               (in tmux / Terminal / iTerm)  │
  │   guide.html                                              │
  │                                                          │
  │  side state: _cache _limits _topo _closeouts _jobs        │
  │              _resumes (+ resume.schedule.json)            │
  │              dispatch.log.jsonl                           │
  └──────────────────────────────────────────────────────────┘
```

What is load-bearing and correct:

- **Watching is pure reading.** `discover_worktrees` → `git_info` → `scan_sessions` →
  `classify_session` never touches a session. The status ladder is evidence-first: a pending
  `AskUserQuestion` is read off disk before any clock is consulted (L585).
- **Acting is always an explicit click**, and always by talking to a terminal — `tmux
  send-keys`, or AppleScript into Terminal.app/iTerm2. There is no privileged channel.
- **The refresh discipline is genuinely good.** Per-card DOM keyed by worktree name, a
  re-sort hold while the pointer is on the grid, and a capture-phase click shield that
  swallows a click landing on a card that moved in the last 600 ms.
- **Everything is a plain dict.** No classes, no ORM, no schema. Tests mock by assigning
  module attributes (`fb.run = FakeGit()`), which works because every call resolves through
  the module namespace at call time. 142 tests, 8.47 s, stdlib `unittest`, no pip.

What is load-bearing and wrong, in one line each: no authentication of any kind; HTTP/1.0
with connection-close on every request; no gzip; `cached_state()` has no lock; and every
side-effecting endpoint is a naked non-idempotent POST.

---

## 2. What breaks when a phone is the client

Not "what would be nice." These are the five things that make a phone client either
impossible or actively dangerous today.

### 2.1 There is no authentication, and the asset is remote code execution

`POST /api/send` types arbitrary text into a live agent's terminal. Dispatched agents run
`claude --dangerously-skip-permissions` — there is no approval prompt. So the endpoint is
unattended RCE with the user's shell privileges, laundered through an LLM. `POST /api/dispatch`
launches a new one. `GET /api/chat` is a paginated transcript exfiltration API and
`/api/dispatchlog` returns verbatim mission prose (on this machine: production session UUIDs,
prod-DB references, pasted emails).

Binding to `100.x.y.z` hands all of that to every node on the tailnet. And the situation is
worse than "not yet secured": **`do_POST` never inspects `Content-Type`**, so a CORS *simple
request* (`text/plain`, no preflight) from any website the user visits already fires
`/api/dispatch` today, on pure loopback. There is no `Host` check either, so DNS rebinding
gets the read side. Both are live vulnerabilities independent of any mobile work.

### 2.2 The collector only runs when someone asks, and it costs a fortune when they do

`collect_state()` is reachable only through `cached_state()`, which is called only by the
`/api/state` handler, `_pick_defaults`, and `fire_resume`. **Close the browser and orchestra
computes nothing.** There is nothing to diff, so there is nothing to notify about — a
background collector is the precondition for push, not an optimisation.

And `cached_state()` has no lock:

```python
def cached_state():
    now = time.time()
    if _cache["state"] is None or now - _cache["t"] > STATE_TTL_S:
        _cache["state"] = collect_state()      # <- N clients, N times, concurrently
        _cache["t"] = now
    return _cache["state"]
```

`STATE_TTL_S = 4.0` while `index.html` polls at 5000 ms, so **every** poll misses. Cost
measured across sessions: **1.55–7.06 s** and **36–40 subprocesses** [M] — the spread is real
and tracks page-cache warmth and how many transcripts are inside the 48 h window. `git_info`
is consistently the dominant term (**1.58 s of 1.72 s** in one session, **4.62 s of 7.06 s**
in another) [M], and it is four serial `git` forks per worktree.

Adding a phone to an already-open desktop board **doubles the fork load**. Two clients that
miss the TTL in the same second each fan out independently.

### 2.3 Every poll re-sends everything, and a structural delta saves nothing

`/api/state` is **36,326 B raw / 9,202 B gzipped (3.95×), 2.18 s cold over HTTP** [M]. At the
board's 5 s cadence that is ~24 MB/hour, over HTTP/1.0 with a fresh TCP handshake per request.

The obvious fix — ship deltas — does not work as-is. Four consecutive polls diffed
structurally:

```
dt=5.2s  changed=9/9 cards  fields={'sessions': 9, 'live_procs': 5}  deltaB=32106
sess 197aab92 ['age_s']
sess 9fbf3e6e ['age_s']      ... all 33 sessions, every tick
```

**100 % of cards "change" every poll because of one field.** `scan_sessions` emits
`age_s = int(now - max(mtime, sub_mtime))` (L650, L659) — a function of *when you asked*, not
of what happened. `live_proc.cpu`/`etime` and `generated_at` do the same. Leave any of them in
and a delta protocol ships a full payload wearing a `kind:"patch"` label.

Normalised to an absolute instant and addressed by stable key, the same window:

```
dt=5.0s   ops=1  bytes=131   paths: ['active_at']
dt=11.0s  ops=0  bytes=47    paths: []
```

**131 B for a real 5-second window; 47 B when nothing happened; 36,326 B for a full poll.**

### 2.4 Every mutation is non-idempotent, and iOS retries

| endpoint | double-fire consequence | guard today |
|---|---|---|
| `POST /api/dispatch` | two agents in one worktree, two accounts burned, two branches | **none** |
| `POST /api/finish` → `mode:"dispatch"` | two headless closeout agents merging and pushing the same branch | **none** |
| `POST /api/send` | the instruction typed at the agent twice | none |
| `POST /api/finish` → `brief`/`slim` | a 600-char brief re-typed at a mid-closeout agent | sequential only (`_closeouts`, unlocked) |

tmux session names embed `%H%M%S`, so only a **sub-second** retry collides. One second later
it succeeds and doubles. And `POST /api/finish` runs `git fetch` (30 s timeout) plus
`claude_processes()` **twice** plus osascript — it can exceed **60 s**, which is exactly
`URLSession.timeoutIntervalForRequest`'s default. Client times out, user taps again, two
agents.

Addressing makes it worse: `/api/send` takes a **pid** and `send_to_process` verifies only that
*some* `claude` process holds it — not that it is the same session. A phone restored from
background with a cached board can type into a completely different agent.

### 2.5 There is no absolute time anywhere, and no way to tell "quiet" from "dead"

The payload carries `age_s` and nothing else. The cache is up to 4 s old, collection takes
1.5–7 s, and the network adds more, so `Date().addingTimeInterval(-age_s)` is wrong by an
unbounded amount. Worse, there is no liveness signal at all: a phone cannot distinguish a
calm fleet from a wedged collector from a sleeping Mac. `HEAD` and `OPTIONS` both return 501,
so there is not even a cheap probe.

Two smaller cuts that compound: **`/api/state` never reports `status == "limit"` until
`/api/limits` has been called at least once** (`collect_state` reads a cache it never fills,
L733–736), so a client that polls only state mis-triages every limit-stuck agent as
`waiting` — i.e. as "needs you"; and `_jobs` is memory-only and capped at 20, so a phone that
backgrounds mid-dispatch comes back to `{"ok": false, "error": "unknown job"}` with no way to
tell "lost" from "failed."

---

## 3. The target architecture

```
  ┌───────────────────────────── the Mac ─────────────────────────────┐
  │                                                                    │
  │  transcripts   git worktrees   ps/lsof/tmux   cclimits             │
  │      │              │               │             │                │
  │      └──────┬───────┴───────────────┴─────────────┘                │
  │             ▼                                                      │
  │   ┌──────────────────────────────────────────────────┐            │
  │   │  producer thread — the ONLY caller of collect     │            │
  │   │                                                    │           │
  │   │   probe   2.6 ms   dir mtimes + stat known files   │           │
  │   │   core   ~0.4 s    procs + sessions + classify     │           │
  │   │   git    ~0.2 s    parallel, own 15 s tick, cached │           │
  │   │                                                    │           │
  │   │   cadence: 3 s busy · 10 s calm · STOPPED idle     │           │
  │   └────────────────────┬─────────────────────────────┘            │
  │                        ▼                                           │
  │   ┌──────────────────────────────────────────────────┐            │
  │   │  state bus:  snapshot · canonical projection ·    │            │
  │   │              epoch:seq · history ring · digest    │            │
  │   └───┬──────────────┬──────────────┬────────────────┘            │
  │       │              │              │                              │
  │       ▼              ▼              ▼                              │
  │   _diff()       derive_events   read models                        │
  │       │              │              │                              │
  │  ┌────▼──────────────▼──────────────▼──────────────────────┐      │
  │  │ ThreadingHTTPServer · HTTP/1.1 keep-alive · gzip · ETag  │      │
  │  │                                                           │     │
  │  │  loopback :4242  ── plain HTTP, board token, HTML served  │     │
  │  │  tailnet  :4242  ── TLS (self-signed, SPKI-pinned),       │     │
  │  │                     bearer token + scopes, NO HTML        │     │
  │  │                                                           │     │
  │  │  GET  /api/v2/state ?since= ?wait=   snapshot + delta     │     │
  │  │  GET  /api/v2/stream                 SSE, field ops       │     │
  │  │  GET  /api/v2/events ?since=         durable event log    │     │
  │  │  POST /api/v2/*      Idempotency-Key + expect{}           │     │
  │  │  GET|POST /api/*                     frozen, adapters     │     │
  │  └──────┬──────────────────────────┬────────────────────────┘     │
  │         │                          │                               │
  │         │                    ┌─────▼──────┐                        │
  │         │                    │ _notify()  │                        │
  │         │                    │  ├ apns ── curl --http2 ─┐          │
  │         │                    │  └ ntfy ── urllib      │ │          │
  │         │                    └────────────────────────┼─┘          │
  │         │                                             │            │
  │  actuation: tmux send-keys / osascript / git          │            │
  │  serialised by worktree + agent locks                 │            │
  └───────────────────────────────────────────────────────┼────────────┘
            │                                             │
   ═════════╪═══════════ WireGuard (Tailscale) ═══════════╪═══════════
            │                                             │
      ┌─────▼──────────────────┐                    ┌─────▼──────┐
      │  iOS · SwiftUI · Swift6│                    │   APNs     │
      │  mirror → typed board  │◀───────────────────│  (or ntfy) │
      │  SSE · snapshot · push │                    └────────────┘
      └────────────────────────┘
```

### Principles

**1. One collector, one writer, one truth.** `collect_state()` runs on exactly one thread and
publishes to a bus. Nothing on a request path ever forks git. This is simultaneously the fix
for the thundering herd, the precondition for push, and a ~1000× speedup for the existing
board's poll.

**2. Absolute time on the wire, derived time on the client.** Every timestamp is an epoch
float. No `age_s`, no `resets_in_s`, no `cpu`, no `etime` in anything that gets diffed. This
single rule is what makes the delta protocol worth 277× instead of 1×.

**3. Durable identity, not positional identity.** Sessions key on the transcript UUID.
Worktrees key on `blake2b(abspath)`, not on the basename — `discover_worktrees` dedupes by
path (L127) and iterates every root, so two roots each holding a `ConfidAI` dir produce two
cards with the same name. Agents key on a tmux target or a tty salted with first-seen time,
never on a pid.

**4. Every mutation is idempotent and asserts what it expects.** `Idempotency-Key` on every
POST, reserved write-ahead before the side effect, plus an `expect` block naming the agent id
and pid. A retry is safe by construction; a stale view is refused with a readable diff.

**5. Legacy bytes are frozen.** `index.html`, `map.html`, `limits.html`, `guide.html` keep
working, byte-identically, through the entire migration. The legacy handlers contain no logic
— each is a call into the same core function v2 uses plus a pure shape adapter, so the two
can never disagree.

**6. Never lie about freshness.** Ages keep ticking while the stream is dead (they derive from
an absolute mtime, so they degrade toward "we don't know" — the safe direction). Statuses dim.
A stale board that still looks alive is the failure to design against.

### On "one file, zero dependencies"

The README's claim is *"an agent harness with zero dependencies — one python3 stdlib file."*
Two clauses, and they are not equally load-bearing.

**The clause that matters is `git clone && python3 orchestra.py`** — no pip, no venv, no wheel,
no build step. A directory of `.py` files preserves every bit of that. **The clause that costs
is "one file."** What it buys beyond the above is `curl | python3`, which nothing in the repo,
the README, or `start.sh` uses. What it costs, once this design lands (+~1,100 lines of auth,
idempotency, snapshots, ops, routing, gzip, push), is a 3,400-line module where the security
boundary is a paragraph in the middle of a file that also holds AppleScript templates — with
no linter and no type checker in CI (`py_compile` is the entire static-analysis budget).

**Decision: split into a stdlib-only package `orchestra/`, keep `orchestra.py` as a 25-line
launcher shim so `./start.sh` and every existing invocation still work. Restate the promise
precisely, and enforce it mechanically:**

> **orchestra installs nothing. It imports only the Python standard library, and it shells out
> only to binaries the host already has.**

The second clause is not new — `git`, `tmux`, `ps`, `lsof`, `osascript`, `open` and `cclimits`
are already shelled today. It is being *named* because push needs two more (`curl`, `openssl`;
§6), and because a promise broken by a subprocess is still broken. Both halves get a CI test
(§4.5).

This is a conscious trade of the *file-count* clause to preserve the *installs-nothing*
clause. It is the trade the user asked to be made consciously rather than discovered.

---

## 4. Server restructure

### 4.1 File tree

```
orchestra.py              25-line shim: sys.path insert + orchestra.cli.main()
orchestra/
  __init__.py            __version__, API_VERSION — nothing executable
  __main__.py            main()
  paths.py               ROOT, STATE_DIR, every path, migrate_stray_state()   [NEW]
  cli.py                 argparse, banner, first sync collect, threads, serve
  config.py              CFG, load_config (deep merge), save_config (locked, atomic)
  proc.py                run(), run_bin(), claude_processes, tmux helpers
  gitx.py                git_info, _gitdirs, git cache, _base_ref, topology
  sessions.py            scan_sessions, classify_session, transcript memo, sid_index
  limits.py              cached_limits (single-flight, own thread), reserve, candidates
  state.py               collect_core, collect_state, probe, producer loop, cadence
  bus.py                 snapshot ring, epoch:seq, _canon, _diff, digest, subs    [NEW]
  ids.py                 worktree_id / agent_id / handles                          [NEW]
  ops.py                 operation registry, ops.jsonl, bounded worker pool        [NEW]
  idem.py                idempotency store (write-ahead, persisted)                [NEW]
  locks.py               per-worktree / per-agent / pick / tmux-buffer locks       [NEW]
  ratelimit.py           token buckets per principal                              [NEW]
  errors.py              ApiError + envelope                                      [NEW]
  auth.py                ROUTES table, guard, devices, pairing, scopes            [NEW]
  qr.py                  QR v5-L / v6-L encoder, SVG + ANSI renderers             [NEW]
  tls.py                 cert generation, SPKI pin derivation, tailnet supervisor  [NEW]
  events.py              derive_events, event log, debounce                       [NEW]
  push/                  __init__.py (router), apns.py, ntfy.py, es256.py         [NEW]
  actuate.py             send_to_process, focus_process, start_finish, closeouts
  dispatch.py            start_dispatch, _run_dispatch, dispatch log
  resume.py              schedules (v2 file format), resume_loop
  api.py                 /api/v2 handlers
  legacy.py              /api/* adapters (frozen bodies, no logic)
  server.py              Handler (HTTP/1.1, timeouts, bounded pool, gzip, SSE)
  web/                   index.html map.html limits.html guide.html pair.html
```

### 4.2 Where state lives — and the bug the move would otherwise introduce

`HERE = Path(__file__).resolve().parent` (L41) drives `RESUME_STATE` and `DISPATCH_LOG`.
Moving those constants into `orchestra/resume.py` makes `HERE` the *package* dir. On first
upgrade `load_resumes()` finds nothing, swallows the error (`except (OSError, ValueError)`),
and **every armed auto-resume silently vanishes.** The live file today holds two `pending`
schedules armed hours out [M]. Config survives only by the accident that `load_config` falls
back to `Path.cwd()` and `start.sh` does `cd "$(dirname "$0")"`.

`paths.py` is therefore the single source of truth for every path, and it splits secrets from
runtime state:

```python
ROOT      = Path(__file__).resolve().parent.parent      # repo root, NOT the package dir
STATE_DIR = Path(os.environ.get("ORCHESTRA_STATE_DIR") or
                 Path.home() / "Library" / "Application Support" / "orchestra")

# repo root — non-secret, user-visible, already gitignored
CONFIG       = ROOT / "orchestra.config.json"
RESUME       = ROOT / "resume.schedule.json"
DISPATCH_LOG = ROOT / "dispatch.log.jsonl"
OPS_LOG      = ROOT / "ops.jsonl"
EVENTS_LOG   = ROOT / "events.jsonl"

# STATE_DIR 0700 — secrets, never in a synced folder, survives a re-clone
DEVICES   = STATE_DIR / "devices.json"        # 0600 — device token hashes
BROWSER   = STATE_DIR / "browser.token"       # 0600
TLS_KEY   = STATE_DIR / "tls/key.pem"         # 0600 — the real secret
TLS_CRT   = STATE_DIR / "tls/cert.pem"
AUDIT     = STATE_DIR / "audit.log.jsonl"     # 0600
APNS_P8   = STATE_DIR / "apns/AuthKey_*.p8"   # 0600

def migrate_stray_state():
    """The package split moves __file__ one level down. Any state file that
    landed beside the code is moved back to ROOT, loudly."""
```

Secrets leave `~/Downloads/orchestr` because that directory is commonly Dropbox/iCloud-synced
and Time-Machined. The registry being hash-only makes its leak survivable; **the TLS private
key is not hash-only** — with it an attacker stands up a listener presenting the pinned SPKI,
the phone's pinning delegate accepts it by construction, and the next dispatch delivers the
act token.

Every write goes through one helper (tmp + `os.replace` + `fsync`). `save_resumes()` is
retrofitted onto it in the same commit: it currently does a plain truncate-then-write with
`except OSError: pass`, so a crash mid-write silently loses every armed schedule.

### 4.3 The producer loop

`collect_state()` was one function doing three jobs at three natural frequencies. Split by
cost:

| tier | contents | cost [M] | when |
|---|---|---|---|
| **probe** | dir mtimes + `stat` of known in-window transcripts | **2.6 ms** | every tick |
| **core** | `claude_processes()` + `scan_sessions()` + classify + limit overlay. **No git.** | **~0.41 s** | probe moved, or every 30 s |
| **git** | `git_info` ×N, parallel, mtime-signature cached | **~0.2 s** at width 8 (from 1.58 s serial) | own 15 s tick |

The probe stats known transcripts *as well as* directory mtimes — a directory mtime catches a
new transcript but not an append to an existing one, which is the common case.

No rule in the event taxonomy reads git. `card_availability` (L690) reads only session
statuses and `has_live`; `dirty`/`branch`/`ahead`/`behind` are rendering data. So git runs on
a slower tick and **runs not at all when nobody is looking**.

```python
def state_loop():
    """The ONLY caller of collect_state(), ever."""
    _publish_once()                    # before the first wait: the bus is never None
    last_wall, last_mono = time.time(), time.monotonic()
    while True:
        period = _tick_period()
        _bus["wake"].clear()           # clear BEFORE waiting — a set() landing between
        if period is None:             # wait-return and clear was silently lost
            _bus["wake"].wait()        # nobody watching, nobody notifiable: cost ZERO
        else:
            _bus["wake"].wait(period)
        now_wall, now_mono = time.time(), time.monotonic()
        gap = (now_wall - last_wall) - (now_mono - last_mono)
        last_wall, last_mono = now_wall, now_mono
        if gap > SLEEP_GAP_S:
            _on_wake(gap)              # new epoch, clear history, suppress events 2 ticks
        try:
            _publish_once()
        except Exception as e:         # a broken tick must not kill the loop
            _bus["error"] = f"{type(e).__name__}: {e}"
```

**Every duration on the server uses `time.monotonic()`. `time.time()` only for wire values.**
The Mac sleeps. With wall clock, on wake every deadline expires at once: held streams all
time out, the idempotency TTL evaporates so a replayed action re-executes, and two post-wake
ticks seconds apart satisfy the event debounce and dump a burst of pushes about transitions
from three hours ago. This is not style; it is the difference between waking to a working
board and waking to a notification storm.

**Cadence is driven by fleet activity *or* client demand — never by subscriber count alone:**

```python
def _tick_period():
    watching   = bool(_bus["subs"]) or (mono() - _bus["last_req"] < 60)
    notifiable = bool(_push_devices)
    if not watching and not notifiable:
        return None                    # SLEEP. Block on the Event. Zero cost.
    if busy: return 3.0 if watching else 5.0
    if attn: return 5.0 if watching else 10.0
    return          10.0 if watching else 30.0
```

Two properties this protects. **A registered push device is demand** — a backgrounded phone
has no subscriber by construction (§7.4 tears the stream down), so keying cadence on
subscribers would put the push path at the slowest tick, i.e. detection latency of up to 20 s
in exactly the state the app exists to serve. And **when nobody is watching and nobody can be
notified, the producer stops completely** — today a closed browser costs literally zero, and
turning a lazy tool into a permanent daemon (74 s of CPU/hour and 1,080 git spawns/hour, for
nobody) is not an acceptable price.

| state | tick | duty cycle [E] | git spawns/hr |
|---|---|---|---|
| streaming + working | 3 s | 14 % of one core | 2,160 |
| push-only + working | 5 s | 8 % | 2,160 |
| streaming, calm | 10 s | 4 % | 240 |
| push-only, calm | 30 s | 1.4 % | 240 |
| **nobody at all** | — | **0 %** | **0** |

`cached_state()` becomes a read plus a demand stamp, with one escape hatch:

```python
def cached_state(fresh=False):
    _bus["last_req"] = time.monotonic()
    s = _bus["state"]
    if s is None:
        return collect_state()          # first tick hasn't landed
    if fresh:                           # actuation needs truth, not a snapshot
        _bus["published"].clear()
        _bus["wake"].set()
        _bus["published"].wait(1.5)
        s = _bus["state"] or s
    return s
```

`fresh=True` matters. Today `_cache["t"] = 0.0` (L1508, L1531) guarantees the *next* request
recomputes synchronously — that is what makes `✓ finish` flip to `✕ close` on the very next
poll. A bare `wake.set()` would only *ask*, so the board could show the stale state for
another 5 s: worse than today, not better. `start_finish` and `_pick_defaults` call
`fresh=True`; the plain `/api/state` GET does not.

### 4.4 The git cache — and why the naive version fails on this fleet

`git_info` is four serial subprocesses per worktree and the dominant cost. Two fixes, and one
trap.

**Parallelise.** `subprocess.run` releases the GIL for its whole duration, so a stdlib
`ThreadPoolExecutor(max_workers=8)` is real parallelism: 1.58 s → ~0.20 s [M-derived].

**Cache on a signature — but resolve the git dir properly.** **8 of the 9 worktrees on this
fleet have `.git` as a *file*** containing `gitdir: /Users/…/ConfidAI/.git/worktrees/ConfidAI2`
[M]. A naive `<path>/.git/HEAD` mtime signature reads a path that does not exist for almost
the whole fleet, returns a constant tuple forever, and the invalidation rule silently degrades
to a plain timer.

```python
def _gitdirs(git_root):
    """(gitdir, commondir). Plain repos, LINKED WORKTREES whose .git is a FILE,
    and the <worktree>/repo layout at L133. Linked worktrees keep refs in the
    COMMON dir, not in their own gitdir — there is no refs/ under .git/worktrees/X."""
```

**Never cache `dirty`.** `git status --porcelain` depends on working-tree file mtimes, not on
anything under `.git`, so no signature can invalidate it. It is also the cheapest of the four
commands and the one that answers "is this agent producing work" — and `_pick_defaults`
(L1636) sorts free worktrees by it to choose a dispatch target. So: ref-derived facts are
cached on the signature; `dirty` runs every git tick, in parallel; and a `fresh=True` publish
forces the whole thing, so a dispatch always routes on a git snapshot ≤1.5 s old (better than
today's 4 s).

### 4.5 Preserving the test suite's only seam

The suite mocks by assigning module attributes — **307 `fb.` references across 58 distinct
attributes** [M]. This works only because callers resolve through the module namespace at call
time. One hard rule, enforced by an AST test:

```python
from . import proc, gitx          # yes
rc, out = proc.run(["git", "status"], cwd=root)

from .proc import run             # NO — breaks every mock in the suite
```

The route table is late-bound (`getattr(sys.modules[__name__], name)`) for the same reason.
Two enforcement tests ship with the split:

- **`TestZeroDeps`** — walks every AST in `orchestra/` and `tests/`: imports must be in
  `sys.stdlib_module_names`; dynamic `import_module`/`__import__` with a non-constant argument
  fails the test; and every literal `argv[0]` handed to `run()` must be in a declared binary
  allowlist. The previous formulation checked imports only, which would have passed while the
  promise was broken by a subprocess.
- **`TestMockability`** — no `from <internal module> import <lowercase name>` anywhere.

`ConfigGuard.setUp/tearDown` must snapshot **every** new module-level mutable: `_bus`,
`_git_cache`, `_ops`, `_idem`, `_kver`, `_devices`, `_pairing`, `_rate`, `_push_devices`,
`_ev_hold`, `_flight`, `_tokens`. New global state omitted there leaks between tests
nondeterministically — the suite's own documented hazard, and the fastest way to make CI
flaky in a way nobody can reproduce.

### 4.6 Test-migration reality

Not "mechanical, six renames." Two categories have no mechanical translation:

- **Five sites do `fb._cache["state"] = None`** to force a fresh collect. `_cache` and
  `STATE_TTL_S` are deleted. Replacement seam: `fb.bus.publish_now()`.
- **Integration tests call `fb.collect_state()` directly.** After the split, serving goes
  through producer → publish → projections, so those tests stop exercising the production
  path. Add `TestServedMatchesCollected`.

Step 1 therefore splits into **two commits**: `1a` is pure `git mv` + import rewiring +
`sed`-level renames, verified by `git diff --stat` showing zero changed lines inside function
bodies. `1b` is everything else. A behavioural regression and a mechanical rename must not be
indistinguishable in one diff.

---

## 5. Auth, pairing and transport security

### 5.1 Threat model

| # | threat | survives the tailnet? | after this design |
|---|---|---|---|
| T1 | another node on the tailnet | yes | **closed for other people's devices** — per-device tokens, scopes, login allowlist. **Not closed for the owner's own other machines** — same `LoginName`, so `whois` passes; the only control there is that they hold no token. |
| T2 | ACL drift / `funnel` / `serve` fronting | yes | closed by the `Host` allowlist — a proxied request arrives with `Host: <node>.ts.net` and is 403'd |
| T3 | stolen **unlocked** phone | yes | **half closed, deliberately.** Acting is behind biometry; **reading is not** — the read token must work with no user present (background refresh, notification decoration) and it grants `/api/chat`. So a stolen unlocked phone exfiltrates transcripts. Mitigations: lockdown degrades reads too, dormant devices auto-revoke at 30 days, revoke is one tap. |
| T4 | another local process **as the same user** | yes | **not closed and not closable.** A process running as you reads what you read. Different local user is closed only in opt-in `board_auth: "nonce"` mode. |
| T5 | malicious website in the user's browser (CSRF today, DNS rebinding for reads) | yes — **works right now with zero network reach** | **closed.** `Authorization` on every `/api/*` makes every request non-simple → preflight → `405` on OPTIONS with no CORS headers. Plus `Host` and `Origin` allowlists and a `Content-Type: application/json` requirement. |
| T6 | log & backup leakage | yes | closed by relocation (§4.2), not by a warning |
| T7 | retry double-fire | partly | closed by write-ahead idempotency + resource locks (§5.6) |
| T8 | config as a code-execution sink (`cclimits_cmd` is executed, `resume_message` is typed at an agent) | yes | no general config-write endpoint, ever; `/api/reserve` stays `admin` and stays whitelisted to one key |
| T10 | rooted phone defeating biometry | yes | **not closed.** Client-side biometry is advisory. The controls that hold are server-side: scopes, rate limits, audit, revoke. Do not oversell Face ID in the guide. |
| T11 | pre-auth resource exhaustion (TLS handshake on the accept thread, slowloris, unbounded threads) | yes | closed: per-connection handshake on worker threads, socket timeouts, bounded pool, per-IP bucket evaluated **before** the token check |

**T5 ships first, alone, before any mobile work.** It is a live vulnerability on a pure
loopback install and its fix needs no phone, no TLS, and no pairing.

### 5.2 One route table

```python
# ROUTES is the ONLY place a path is interpreted. The authorizer and the
# dispatcher read the same row, so they cannot disagree. Keys are
# (METHOD, path-without-query), matched EXACTLY — no prefixes, no fallthrough.
ROUTES = {
  ("GET",  "/api/health"):        (health,            None,    False),
  ("POST", "/api/pair"):          (claim_pairing,     None,    False),
  ("GET",  "/api/state"):         (state_payload,     "read",  False),
  ("GET",  "/api/chat"):          (read_chat,         "read",  False),
  ("GET",  "/api/dispatchlog"):   (read_dispatch_log, "read",  False),
  ("POST", "/api/send"):          (send_to_process,   "act",   True),
  ("POST", "/api/finish"):        (start_finish,      "act",   True),
  ("POST", "/api/dispatch"):      (start_dispatch,    "act",   True),
  ("POST", "/api/reserve"):       (set_reserve,       "admin", True),
  ...
}
```

`str.startswith` routing is deleted on both verbs. This is not cosmetic: today
`"/api/dispatchlog".startswith("/api/dispatch")` is **true**, so `POST /api/dispatchlog` reaches
`start_dispatch` (L2267). A read-scoped token — the one deliberately stored *without* a
biometric gate — could launch an agent. Exact matching also means no future endpoint inherits a
neighbour's scope by prefix, and static HTML becomes a first-class row rather than a
fallthrough.

`GET /api/focus` keeps scope `act`: it is a GET with a real side effect (AppleScript, and for
tmux hosts a **brand-new Terminal window per call**). `GET /api/limits?refresh=1` is upgraded to
`act`: 90 s subprocess, real quota spend.

### 5.3 Scopes

Two tokens per device. This is what makes the stolen-phone story work: the read token must be
usable with no user present, so it cannot sit behind biometry — and if it could also act, the
biometric gate would be decorative.

| scope | endpoints | iOS storage |
|---|---|---|
| `read` | state, topology, limits (no refresh), chat, dispatchlog, dispatch/status, health; `POST /api/devices/self/apns`, `.../reissue-act` | Keychain, `kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`, access group shared with the NSE, **no ACL** |
| `act` | send, finish, dispatch, resume/*, focus, limits?refresh=1 | Keychain, `WhenUnlockedThisDeviceOnly` + `SecAccessControl([.biometryCurrentSet, .or, .devicePasscode])`, **not** in the access group |
| `admin` | reserve, all `/api/devices*` except the two `self` endpoints | **board only — phones never get admin** |

`AfterFirstUnlock` on the read token is mandatory, not an optimisation: with the default
`WhenUnlocked`, the Notification Service Extension on a locked device — the entire scenario —
cannot read it and falls through to a generic body **every single time**.

`.or .devicePasscode` is also mandatory: `SecItemAdd` with a biometry-only ACL **fails
outright** on a device with no biometry enrolled, blocking pairing entirely.

Two self-service endpoints exist at `read` scope, whitelisted to their own device's record:

- **`POST /api/devices/self/apns`** — APNs tokens rotate on reinstall, restore and some OS
  updates. Without this, push is structurally impossible for a device that never gets `admin`,
  and the failure is silent and permanent.
- **`POST /api/devices/self/reissue-act`** — returns **no token**; it queues an approval the
  desktop board confirms. `.biometryCurrentSet` invalidates on Face ID re-enrolment or adding
  an Alternate Appearance (the standard fix for glasses). Without remote re-issue, a user who
  does that at an airport has a read-only app until they physically return to their Mac.

### 5.4 Transport: TLS on the tailnet, plain HTTP on loopback

Two listeners, different policies. **Never `0.0.0.0`** — it is indistinguishable from binding
coffee-shop Wi-Fi.

```
loopback 127.0.0.1:4242   plain HTTP · serves HTML · board token · CSP
tailnet  100.x.y.z:4242   TLS, self-signed P-256, SPKI-pinned · bearer + scopes · NO HTML
         fd7a:…:4242      (both families — MagicDNS publishes A and AAAA, and iOS
                           Happy-Eyeballs tries v6 first)
```

**Why TLS at all, inside a WireGuard tunnel?** On transport confidentiality, honestly, close
to nothing — Tailscale is already end-to-end encrypted. Four real reasons:

1. **Cert pinning gives the phone a server identity independent of DNS.** MagicDNS is
   controlled by the Tailscale coordination server; pinning the SPKI means a spoofed record
   cannot redirect the `act` token to an attacker's listener. This is the actual reason.
2. **ATS.** Plain HTTP to an *IP literal* requires `NSAllowsArbitraryLoads` — a blanket
   disable. `NSExceptionDomains` keys are domain names and do not apply to IPs; and
   `NSAllowsLocalNetworking` covers unqualified hostnames, `*.local` and link-local, **not**
   RFC 6598 CGNAT `100.64.0.0/10`. Since the app must fall back to the raw tailnet IP whenever
   MagicDNS is off or slow, HTTPS is the only transport that works on both addressing paths.
   A self-signed cert accepted by a `URLSessionDelegate` server-trust challenge **is** an
   ATS-clean load; `SecTrustEvaluateWithError` is deliberately never called, because a pin is
   a strictly stronger statement than "some CA vouched for this."
3. Loopback bearer tokens over cleartext are readable by anything that can open a raw socket.
4. Defence in depth if anything ever fronts the port.

**The verified trap.** `openssl req -newkey ec -pkeyopt ec_paramgen_curve:prime256v1` on
LibreSSL 3.3.6 (the macOS system openssl) emits a cert whose SPKI carries **explicit curve
parameters** — 335 bytes, and it does **not** contain the canonical 26-byte named-curve prefix
that the Swift delegate prepends to `SecKeyCopyExternalRepresentation`'s raw point. **Pinning
would fail 100 % of the time**, presenting as an undiagnosable TLS error [M]. The key must be
generated separately:

```python
run([OPENSSL, "ecparam", "-name", "prime256v1", "-genkey", "-noout",
     "-param_enc", "named_curve", "-out", str(KEY_PATH)])
run([OPENSSL, "req", "-new", "-x509", "-key", str(KEY_PATH), "-days", "825",
     "-sha256", "-config", str(cnf), "-out", str(CRT_PATH)])
```

The pin is derived **in pure Python** — no subprocess, no temp file:

```python
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")

def cert_pin():
    der = ssl.PEM_cert_to_DER_cert(CRT_PATH.read_text())
    i = der.find(P256_SPKI_PREFIX)
    if i < 0:
        raise ValueError("not a named-curve P-256 key — delete tls/ and restart")
    spki = der[i:i + len(P256_SPKI_PREFIX) + 65]
    return base64.urlsafe_b64encode(hashlib.sha256(spki).digest()[:16]).rstrip(b"=").decode()
```

Verified byte-identical against `openssl x509 -pubkey | openssl pkey -pubin -outform der |
shasum -a 256` [M]. Doing this via `run()` would be a trap: `run()` swallows every exception
and returns `(1, "")`, so a failed regeneration would silently publish the **previous** cert's
pin — the QR looks fine, pairing succeeds, and then every connection fails a pin check the app
cannot distinguish from an attack.

**Renewal reuses the key.** Pin the SPKI, not the certificate; at 825 days, reissue from the
same key and every paired phone keeps working.

**The tailnet listener is a supervised background thread, not a boot gate.** `BackendState` is
`"Stopped"` on the author's own machine right now [M], and Tailscale also stops routinely after
sleep, network changes and re-auth. A `sys.exit` there kills the local board for a reason
`start.sh` buries in `/tmp/orchestra.log`. The supervisor retries with backoff and reports state
via `/api/health`; loopback binds unconditionally and first.

### 5.5 Pairing

```
Mac (board /pair, or --pair)              iPhone
1. "＋ pair a device"
2. code = 8 Crockford chars, TTL 120 s, single use
3. QR + manual fields rendered
                                          4. scans; TLS, pin taken from the QR
                                          5. POST /api/pair {code, label, platform}
6. validates: open? peer in 100.64/10?
   per-IP attempts < 5? compare_digest on
   the NORMALISED code?
7. mints read+act, writes registry, audits,
   pins tailnet_allow_logins if unset
                                          8. both tokens → Keychain
```

Payload, 83 bytes:

```
orc://p?h=achills-macbook-pro.tail1205d9.ts.net&c=7K3M9QP2&f=jnK0svnXpNeIqfgF5CQuaQ
```

Fixed overhead is 46 bytes, so the host budget is **60 chars at QR v5-L (106 B) and 88 at v6-L
(134 B)**. The worst realistic MagicDNS name — a 63-char label plus `.tailXXXXXX.ts.net` — is
127 bytes and fits v6. Port is omitted when 4242. The QR carries **no long-lived secret**: a
40-bit code, 120 s, single-use, 5 attempts per IP.

`qr.py` is a stdlib encoder for exactly two configurations (v5-L and v6-L, single-block, no
interleaving, ECC L, BCH(15,5) format bits, all 8 masks scored by the standard penalty rules)
— ~250 lines, the largest single block in the security work. Its unit test is the real proof:
encode → unmask → re-walk the zig-zag placement → reassemble codewords → **assert all EC
syndromes are zero**, then decode the byte-mode header and assert round-trip. That proves the
GF(256) arithmetic, the generator polynomial, the placement order, the function-pattern
reservation map and the mask are all mutually consistent. Verified passing for v5 [M].

**The manual fallback is compare, not type.** Manual entry takes host + port + the 8-char
case-insensitive Crockford code only; the app connects, computes the SPKI pin from the
presented certificate, and **displays it** for visual comparison against the Mac's screen.
Asking a user to type a 22-char case-sensitive base64url string with `-` and `_` on an iOS
keyboard inside 120 s — in exactly the situation where the camera already failed — would fail,
burn the window, and force a re-issue. Comparing 22 characters is trivial; typing them is not.
The guide says plainly: the manual path is compare-on-first-use; the QR path is the
out-of-band one.

**The file is the authority.** `--devices` / `--revoke ID` are plain file operations under an
`flock`; the guard stats `devices.json`'s mtime at most once per second and reloads. A CLI that
writes a file the running server never re-reads would print "revoked iPad" while the token kept
working — and the lost-phone story is the primary justification for the registry. `--pair`
needs a live listener, so it becomes an HTTP client of the running server.

### 5.6 Idempotency and locks

**Every v2 mutation carries `Idempotency-Key` (client UUID) and `issued_at`.** The reservation
is persisted **write-ahead, at `begin()`, before any side effect**, with a process `BOOT_ID`:

| situation | response |
|---|---|
| key unseen | execute; store `(status, body)`; `Idempotent-Replay: false` |
| `in_flight`, same boot, same fingerprint | `409 operation_in_flight`, `Retry-After: 1`. **Never blocks.** |
| `in_flight`, **different boot** | `409 operation_indeterminate`, `retriable: false` — *"the server restarted while this was running; check the fleet before retrying, a mission may already be live."* **Never re-executes.** |
| `done`, same fingerprint | the stored status and body, byte-identical, `Idempotent-Replay: true` |
| `done`, different fingerprint | `422 idempotency_key_reused` |
| `issued_at` older than 900 s | `409 expired` |

There is no `abandon()`. A handler exception settles the op and completes the key with the
failure, so the retry replays it. Recording only at completion would leave the window that
actually matters unprotected — reserve in memory → tmux session created → restart → retry sees
no record → **second agent**. `./start.sh` kills and restarts by design, so restart-mid-dispatch
is routine.

**Server-side `issued_at` expiry is not belt-and-braces.** A background `URLSession` defaults
`timeoutIntervalForResource` to **seven days** and retries across reboots and network changes.
A dispatch handed to the background daemon during a tailnet outage can land 40 minutes later
against a restarted server and re-execute: two live agents merging and pushing the same branch,
the worst outcome this product can produce.

**Resource locks, because idempotency stops a retry but not a double-tap:**

| op | lock | prevents |
|---|---|---|
| `dispatch` | `worktree:<wt_id>`, acquired **synchronously in the accept path**, held for the op's life | two agents in one worktree |
| `finish` | same, incl. the `mode:"dispatch"` headless closeout | two closeout agents merging one branch |
| `send`, `kill` | `agent:<ag_id>` | interleaved keystrokes in one pane |
| any tmux paste | one global `_tmux_buf` lock, per-op buffer name | **agent A executing agent B's instruction** |

The auto-pick case is the one that must not be missed. `POST /api/v2/dispatches` with
`worktree_id: null` is the *primary* phone flow, and if selection happens inside the worker
after the 202 there is no id to lock. Two auto-dispatches read the same snapshot, pick the same
cleanest-free worktree (the new agent takes ~30 s to register as busy), and tmux names embed
`%H%M%S`. So: a global pick lock held for microseconds, the picker subtracts already-reserved
worktrees from the free list, and the resolved `worktree_id` is in the 202 response.

The tmux buffer hazard is subtle and severe: `deliver_text` uses one global buffer name
(`orchestra-kickoff`), while per-agent locks explicitly permit concurrent sends to *different*
agents. A sets, B overwrites, A pastes B's instruction into an agent running
`--dangerously-skip-permissions`.

### 5.7 HTTP substrate

```python
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # keep-alive: the biggest latency win over WireGuard
    timeout = 30                      # MANDATORY — see below
    MAX_BODY = 256 * 1024
```

**`timeout` is not optional.** `BaseHTTPRequestHandler.timeout` and
`StreamRequestHandler.timeout` are both `None` on this machine's Python 3.14.5 [M]. Setting
`protocol_version = "HTTP/1.1"` without it means every idle keep-alive connection blocks
forever in `rfile.readline()` and permanently pins a thread — backgrounded phones, closed
laptop lids, dropped NAT flows, four board tabs at ~6 connections each. With `log_message`
overridden to `pass`, the slow leak ends in `RuntimeError: can't start new thread` with no
diagnostic trail at all.

Also, in the same change:

- A **bounded worker semaphore** (64) that 503s past the cap.
- **`411` on any `Transfer-Encoding: chunked`** — `BaseHTTPRequestHandler` cannot decode it, so
  accepting one leaves the frames in the socket and the *next* request on that connection parses
  chunk-size hex as a request line. Swift's `httpBodyStream` and `uploadTask(withStreamedRequest:)`
  both force chunked.
- **Content-Length validated before the guard, and the body drained on every denial**, with
  `Connection: close` on every non-2xx. Verified: without the drain, a denied POST with body
  `{"mission":"x"}` makes the next pooled request return
  `501 Unsupported method ('{"mission":"x"}GET')`. iOS `URLSession` pools per host, so the
  design's own 429-retry and 401-re-pair flows would return garbage.
- Negative `Content-Length` rejected (`read(-1)` currently hangs).
- `try/except` around the whole dispatcher — `POST /api/send {"pid":"abc"}` today raises an
  uncaught `ValueError`, dumps a traceback, and drops the connection.
- `do_HEAD`, `do_OPTIONS` (405, **no CORS headers** — that is what blocks T5), `do_PUT`,
  `do_DELETE`.
- gzip when `Accept-Encoding` allows, body > 1 KB, **and `Content-Type != text/event-stream`**.
- CSP + `nosniff` + `no-referrer` on HTML, loopback only. The board renders agent-authored
  transcript text via `innerHTML`; every interpolation routes through `esc()` and is correct
  today, which is exactly why it is worth locking in next to a full-privilege token.

**`--demo` never disables auth.** It is a screenshot flag; coupling an auth bypass to it means
`--demo --tailnet` serves unauthenticated keystroke injection, and it makes every guard test
vacuous. Tests get an explicit per-server `policy` override instead, and the shipped smoke tests
run **with auth on**, using a real pairing-minted token.

---

## 6. The push / event pipeline

### 6.1 Feasibility: APNs from stdlib Python — verdict, and the proof

**Verdict: yes, by shelling out. It is a conscious trade of two OS binaries, not of a pip
dependency, and it lands on the seam the test suite already mocks.**

The problem is real: Apple decommissioned the binary protocol in 2021, the provider API is
**HTTP/2-only**, `http.client` is HTTP/1.1-only, provider tokens are **ES256 JWTs over P-256**,
and `hashlib`/`hmac` are symmetric-only. Both requirements are unreachable from a pure-stdlib
import graph.

Measured on this machine:

```
$ curl --version
curl 8.7.1 (x86_64-apple-darwin25.0) libcurl/8.7.1 (LibreSSL/3.3.6) nghttp2/1.67.1

$ curl --http2 -X POST https://api.sandbox.push.apple.com/3/device/abc
  → http_version=2  http_code=403      # correct: no auth token supplied

$ openssl dgst -sha256 -sign p256.pem  → 72-byte DER SEQUENCE, first bytes 3046
```

So: `curl --http2` for the POST, `openssl dgst -sha256 -sign` for the signature, both through
`run()`/`run_bin()`. Rejected alternatives: a hand-rolled stdlib HTTP/2 client (connection
preface, frame layer, HPACK static/dynamic tables, Huffman — real protocol surface, held to the
same standard that rejected WebSockets), and `httpx`/`PyJWT` (the one thing the user asked be
traded consciously).

**The DER→JOSE conversion is ~14 lines and is the highest-value unit test in the feature.**
`openssl` emits ASN.1 `SEQUENCE { INTEGER r, INTEGER s }`; JOSE wants raw `r‖s`, 32 bytes each.
DER integers are *signed* (a `0x00` pad when the high bit is set) and *minimal-width*. Over 400
real signatures the DER length distribution was `{70: 96, 71: 194, 72: 110}` — the expected
1:2:1 [M]. **A fixed-offset parser is wrong ~75 % of the time.**

```python
def der_to_raw(der, n=32):
    if len(der) < 8 or der[0] != 0x30:
        raise ValueError("not a DER SEQUENCE")
    i, out = 2 + ((der[1] & 0x7F) if der[1] & 0x80 else 0), b""
    for _ in range(2):
        if i + 2 > len(der) or der[i] != 0x02:
            raise ValueError("expected DER INTEGER")
        ln = der[i + 1]
        if i + 2 + ln > len(der):
            raise ValueError("truncated DER INTEGER")
        v = der[i + 2:i + 2 + ln].lstrip(b"\x00")
        i += 2 + ln
        if len(v) > n:
            raise ValueError("integer wider than the curve")
        out += v.rjust(n, b"\x00")
    return out
```

Seven test vectors: both high-bit set (72 B), short r, short s, both short, r exactly 32 B, r
with two leading zeros, and **truncated buffer → `ValueError`, not `IndexError`**.

**The batching bug, found by reproduction.** Sending N pushes as one `curl -K` config with
`--next` separators looks like an obvious optimisation. It is broken:

```
auth emitted once, before the first --next:
  transfer 0 → {"auth": "bearer GLOBAL"}
  transfer 1 → {"auth": null}
  transfer 2 → {"auth": null}
```

`man curl`: *"`--next` resets all local options and only global ones survive… Global options
include `-v`, `--trace`, `--trace-ascii` and `--fail-early`."* `header`, `max-time`, `http2`
and `dump-header` are all **local**. Every push after the first went out unauthenticated →
403 `MissingProviderToken`. With every option repeated inside its own `--next` block, all three
authenticate and each captures its own `apns-id` [M]. The provider token also lives in a
mode-0600 config file, never argv — `ps` is world-readable.

**Header table, stated once because piecemeal specification is how these go wrong:**

| header | value |
|---|---|
| `apns-push-type` | `alert` / `background` / `liveactivity` — **required on iOS 13+**, a missing value is rejected outright |
| `apns-topic` | bundle id (Live Activity: `…​.push-type.liveactivity`) |
| `apns-priority` | **10** for P1/P2, **5** for P3/P4 and all `background` |
| `apns-expiration` | **`int(event.ts + ttl)` — an absolute epoch, not a duration.** Writing `900` means "expired in 1970": one attempt, no store-and-forward, silently, in exactly the offline case the setting exists for. |
| `apns-collapse-id` | ≤64 B, **only for state-superseding events** — never for discrete facts |

**JWT caching is mandatory**: Apple requires reuse for 20–60 minutes and answers regeneration
with `429 TooManyProviderTokenUpdates`. Cache 40 minutes.

**The entitlement that decides whether this works at all: Time Sensitive Notifications.**
Without it iOS silently clamps `interruption-level: time-sensitive` to `active`, which any
Focus — including Sleep Focus, the default overnight configuration — suppresses. That is
precisely the 2 a.m. blocked-agent case, failing with no server-side error. It is self-serve in
the developer portal but must be enabled and baked into the provisioning profile. **Critical
Alerts are explicitly ruled out** — a separate Apple approval with lead time, not plausibly
granted for a developer tool. Request **`.provisional`** authorization on first launch so
notifications arrive quietly with no prompt and no possibility of a permanent first-run denial
gutting the premise.

### 6.2 Fallback ladder

| tier | backend | when |
|---|---|---|
| 1 | **APNs** via `curl --http2` + `openssl` | paid Apple account, both binaries present, HTTP/2 probe passes at boot |
| 2 | **ntfy.sh** via `urllib.request` (JSON publish endpoint) | no Apple account, or either binary missing |
| 3 | in-app event feed only (`GET /api/v2/events`) | no push configured |

`_notify(events)` is one seam. It is **not** one product, and the guide must say so: under ntfy
there is no inline reply, no deep link into a card, no notification actions, no Live Activities,
no badge, no NSE. **ntfy is a degraded, no-account, text-alert-only bring-up channel.**

Two ntfy specifics. Use the **JSON publish endpoint, not headers** — every title in the copy
spec is non-ASCII (`▲ ■ ◆ ⛔ ⏱ ✓ ✗ ⌁ ●` and the `·` separator) and HTTP header values are not
UTF-8, so the header form delivers mojibake and breaks the shared glyph vocabulary on the sink
most users see first. And **force transcript text off for ntfy unconditionally**: on iOS the
ntfy app receives instant push only for ntfy.sh topics, so even a self-hosted server proxies a
wake-up through a third party.

### 6.3 Event derivation

The producer diffs consecutive canonical projections and appends **edge-triggered** events.
Push is a consumer of that log, not a special case in the collector.

| event | trigger | level | dwell | notes |
|---|---|---|---|---|
| `needs_you` | session → `needs_input` | P1 time-sensitive | 0 | no clock floor exists — `classify_session` reads a pending `AskUserQuestion` before any clock (L585) |
| `blocked` | session → `blocked` and **`skip_perms_own` false** | P1 | 40 s | see below |
| `your_turn` (evidence) | → `waiting` with a `turn_duration` record and no busy signal | P2 | 20 s | |
| `your_turn` (decay) | → `waiting` with no turn-end record | P2 | **150 s** | the structural flapper |
| `agent_died` | was working ≤300 s ago, now gone, **and** git says dirty or ahead | P2 | 30 s | |
| `limit_hit` | account exhausted, or session → `limit` **and `handed_to` absent** | P2 | 20 s | |
| `limit_reset` | account exhausted → clear | P3 | 0 | |
| `resume_fired` / `resume_failed` | schedule → done / failed | P2 / P1 | 0 | |
| `dispatch_done` / `dispatch_failed` | job done | P3 / P1 | 0 | |

Four things that decide whether this gets muted in week one:

**`handed_to` suppression is not optional.** A limit session carrying `handed_to` is excluded
from `counts` and from card severity precisely because work already continued on another
account. Alerting on `status == "limit"` without checking it fires on non-problems.

**Dwell is wall-clock seconds, never tick counts** — the tick period varies 3–30 s, so a tick
count silently changes meaning with cadence, and would be slowest exactly when nobody is
watching.

**`blocked` must be session-scoped.** `skip_perms` is computed per *worktree* across all its
processes (L685), so the rule is inert for orchestra-dispatched worktrees (every launch path
passes `--dangerously-skip-permissions`) and, inversely, one manually-attached `claude` without
the flag flips **every** pending-tool session in that worktree to `blocked` at once — a
multi-session P1 burst from an unrelated process that dwell cannot damp. A per-session
`skip_perms_own` from that session's own paired process fixes both directions.

**Nothing may outlive its truth.** An agent asks at 14:00, the phone buzzes, the user answers at
their Mac at 14:01 — and `▲ needs you` sits on the lock screen indefinitely. `apns-collapse-id`
supersedes only *undelivered* pushes. Three mechanisms, because none alone is reliable:
a **withdrawal background push** on FIRED→COOLDOWN carrying `withdraw: [dedupe_key]`;
`request.identifier = dedupe_key` so it can be targeted; and a **foreground reconcile** against
`GET /api/v2/events/open`, which is the reliable path since background pushes are throttled.

Plus a **global budget** (P1 12/hr, P2 6/hr per install, overflow rolls into a digest) and a
**mute** endpoint. Every notification product that ships without a global budget gets muted at
the OS level within a week — at which point the P1s the user actually wants are silently gone
too.

### 6.4 The limits subsystem

This is the subtlest part of the pipeline. `collect_state` reads `limits_by_account()`, which
reads a cache it never fills (L733–736), so **`status == "limit"` never appears until
`/api/limits` has been called**. So the fetch is load-bearing. But calling it every tick is
worse than useless:

- Orchestra's TTL is 300 s and cclimits' own is 60 s, so every miss is a **real network refetch
  of all 8 accounts** against a rate-limited endpoint — ~2,300 authenticated GETs/day with
  nobody watching. The feature named after limits would begin by hammering the limits endpoint.
- `cclimits` **renews expired OAuth logins in place by default**, so a background loop would
  rotate refresh tokens in `~/.claude*/.credentials.json` at 4 a.m. while live agents hold them.
  `cclimits --no-token-refresh` exists and is verified [M].
- `cached_limits`' failure path **does not update `_limits["t"]`** (L1068), so on any machine
  where cclimits is missing or failing — the default for anyone who has not installed it — the
  loop would spawn a 30 s-timeout subprocess **every tick, forever**.

So: a separate thread (the 90 s `refresh=True` subprocess must never stall the state tick),
need-driven (a known reset just passed, a session is limited, a resume is due within 15 min),
a 30 min jittered floor, exponential backoff, negative caching, and single-flight so a phone
pull-to-refresh and a desktop force-refetch cannot both spawn it.

**And the false-alert fix.** `limits_by_account()` does `if not acc.get("ok"): continue`
(L1193). On a 429 or a per-account token error cclimits still exits 0 with `ok: false`, so the
account **vanishes** from the map, the session never gets `status == "limit"`, and it stays
`waiting`. A limit-parked agent has no busy signal, so no veto fires either. The result:
polling harder to detect limits causes rate-limiting, which erases limit state, which fires
*"your turn — come do something"* at 3 a.m. for an agent that can do nothing until Thursday.
Fix: emit a row with `known: False` instead of dropping the account, propagate it to the
session, and **veto `your_turn` and `worktree_free` whenever limits are unknown or stale**. If
we cannot prove the account has headroom, we do not tell the user it is their turn.

### 6.5 Sleep — the unowned failure

Either the Mac sleeps and the feature silently does nothing — the worst failure, because the
user trusts silence and it is unfalsifiable — or the Mac never sleeps and a laptop running six
agents plus a poller is flat in hours. Power Nap does not run third-party daemons.

- **Wake detection** (§4.3): on a monotonic-vs-wall gap > 120 s, re-baseline without emitting,
  new epoch, resync all subscribers, suppress events for two ticks.
- **Power source** via `pmset -g ps`, cached 60 s: on battery, 60 s tick and probe-only unless
  something moved.
- **`last_tick_at`** in every payload and heartbeat — the only way a client tells "calm fleet"
  from "dead server."
- **`pmset -g assertions`** surfaced in `/api/health`, so the server can tell the user *why*
  pushes stopped instead of the user discovering it.
- **A client-scheduled deadman** — a push cannot come from a dead server, so "no contact in 30
  minutes" must be a local `UNTimeIntervalNotificationTrigger`, cancelled and rescheduled on
  every successful poll.
- Documented deployment for unattended use: `caffeinate -is ./start.sh`, or a `launchd`
  LaunchAgent with `KeepAlive` (there is no service manager of any kind today).

---

## 7. Realtime & sync

### 7.1 Transport: SSE, with a fallback ladder

| tier | transport | exit condition |
|---|---|---|
| **1** | **SSE** `GET /api/v2/stream` | 2 failures to receive `hello`; buffering detected (≥5 samples); 404 |
| **2** | long-poll `GET /api/v2/state?since=&wait=25` | 404, or 3 consecutive transport errors |
| **3** | conditional poll `?since=&wait=0` on the §7.4 cadence | 404 |
| **4** | legacy `GET /api/state` full poll | server predates v2 |

**WebSockets rejected.** RFC 6455 needs a SHA-1/base64 handshake plus a masked frame codec —
~150 lines of new protocol with its own test burden, for a channel used in one direction. Every
action stays a `POST` so it carries an idempotency key, retries independently of stream health,
and stays testable through `TestHTTPSmoke`'s `urllib.request` pattern. (This is not in tension
with the APNs decision: HTTP/2 there is *mandated by Apple* and solved by a binary that already
exists; a WebSocket **server** cannot be shelled out to.)

SSE was proven in a real `ThreadingHTTPServer` with this project's handler shape:
`BaseHTTPRequestHandler.wbufsize == 0` so every `write()` hits the socket, and
`ThreadingHTTPServer.daemon_threads is True` so `server_close()` never joins a stream thread —
a hung stream cannot block Ctrl-C [M].

Tier 4 exists because `index.html:817` already establishes version-skew detection. The app is
never bricked by an un-upgraded server.

### 7.2 Delta protocol

**Cursor:** `"<epoch>:<seq>"`. `epoch` is 8 hex chars regenerated at process start — `_jobs`,
`_closeouts` and `_job_seq` are memory-only and `_job_seq` resets to 0 (L1660, L1706), so job
ids repeat across restarts and a restart must be unambiguously discontinuous. **`seq` advances
only when the canonical projection actually changed**, which is what makes `since == seq` a
zero-byte proof of freshness.

**Address space** — two grammars, five families, descent declared per family so the client never
infers structure from a path:

```
counts | free | order | other      leaves, replaced whole
w/<wid>                            card scalars       ─┐
w/<wid>/git                        git dict            │ descend
w/<wid>/s/<sid>                    one session         │ exactly
r/<wid>|<sid>                      one resume schedule │ one level
j/<job_id>                         one dispatch job   ─┘
w/<wid>/p | w/<wid>/order          leaves, replaced whole
```

`order` and `w/<wid>/order` are **explicit paths** because array indices are unusable:
`collect_state` re-sorts cards by `(severity, name.lower())` (L826) and sessions by
`(4.5 if handed_to else rank[status], age_s)` (L777). The 4.5 handoff weight is a subtle,
load-bearing decision that must not be reimplemented in Swift against a locally-derived age —
the tie-breaks would differ from the desktop board for the same fleet. Emitting the sid order
costs ~40 B only when it moves and keeps the sort in exactly one place.

**Ops:**

```json
{"p": "w/3f9a11c2/s/0bc2125a", "f": "status", "v": "needs_input"}
{"p": "w/3f9a11c2", "f": "card_rev", "v": "7b21e4de"}
{"p": "counts", "v": {"working":3,"needs_input":2,"limit":1,"blocked":0,"waiting":4,"ended":9}}
{"p": "w/3f9a11c2/s/0bc2125a", "x": 1}
```

**The rule that makes it work:**

```python
DELTA_SKIP = {"generated_at", "age_s", "cpu", "etime", "id"}
```

`scan_sessions` gains `active_at` (absolute epoch) beside `age_s` — one line at L659 from the
value already computed at L650. `age_s` survives in the legacy projection, which is **never
diffed**, so `index.html` is untouched. `limit.resets_at` replaces `resets_in_s`. `cpu`/`etime`
live in detail only. And the guarantee is enforced by a test, not by care:

```python
def test_wall_clock_alone_produces_an_empty_patch(self):
    """This assertion IS the mobile-delivery feature. If it fails, every tick
    ships a full payload and the stream is a downgrade."""
    a = publish_now()
    with mock_clock(+30):
        b = publish_now()
    self.assertEqual(_diff(a, b), [])
```

**Frames:**

```
event: hello
data: {"epoch":"9f2c1a04","seq":4711,"at":1784638012.8,"dg":"a41f0c93",
       "tick":3.0,"hb":25.0,"collector_ok":true,"wake_gap":0.0,
       "caps":["delta","gzip","optoken","jobs","chatafter","push"]}

event: delta
data: {"seq":4712,"at":1784638015.9,"ops":[…]}

event: hb
data: {"seq":4712,"at":1784638040.9,"dg":"a41f0c93","tick":3.0,"hb":25.0,
       "collector_ok":true,"wake_gap":0.0}

event: resync
data: {"seq":4900,"reason":"cursor_too_old"}   // epoch_changed | slow_consumer | digest_mismatch
```

`hb` carries `tick`, `hb`, `collector_ok`, `dg` and `wake_gap` — **not just `hello`**. An
already-connected subscriber must be able to learn that the server changed cadence, that the
collector is wedged, or that the Mac slept. Plus a bare `:\n` SSE comment every 5 s — 3 bytes,
invisible to the parser, whose only job is to fill a black-holed peer's send buffer so `write`
fails in seconds rather than after the full TCP retransmit ladder.

**Divergence detection**: a per-entity version counter maintained identically on both sides
from applied ops only (no float serialisation involved, so Python/Swift repr differences cannot
cause a false mismatch), digested into `dg`. A mismatch forces a resync and is logged. Without
it, a field added server-side becomes a permanently-wrong value on the phone with no error
anywhere.

**Gzip the snapshot. Never the delta, never the stream.** gzip *expands* at these sizes
(132 > 131 B, 62 > 47 B) [M], and gzipping `text/event-stream` breaks incremental delivery
because URLSession buffers to decompress. **`text/event-stream` is excluded unconditionally**,
and the SSE smoke test asserts the response has no `Content-Encoding` — URLSession adds
`Accept-Encoding: gzip` to every request automatically, so a size-threshold rule is undefined
for a response with no body length.

**Deliberate non-decision: no list/detail split.** The free-text fields (`topic`,
`last_assistant`, `last_user`, `subagent_said`) are 58 % of every session object, and splitting
them out of the board payload is the obvious win under polling. Under a delta protocol they are
*stable* — they change only when the agent speaks — so after the first snapshot they cost
nothing. Across four consecutive polls exactly one changed once [M]. Splitting would save ~4 KB
on a once-per-cold-start payload in exchange for modelling partially-loaded sessions, which is
an entire bug class for no benefit.

> ⚠ **`API.md` §9.3 currently decides this the other way** — it ships a single 80-char
> `headline` on the board and puts all four fields on the worktree-detail endpoint. That is a
> live contradiction between two documents that both claim authority over the payload, and it
> is load-bearing for `UX.md` §3.1.4, whose session row renders three of the four inline.
> **The argument above is the stronger one for a streaming client** and should win: the split
> optimises the cold-start snapshot, which happens once, at the cost of a partially-loaded
> session state on every screen that shows text. If `headline` survives regardless, `UX.md`
> §3.1.4's row must collapse to status + headline + tags — pick one and change both
> documents, because the two layouts are not compatible with one payload.

### 7.3 What it costs

| | payload | on-wire (incl. TCP/IP + WireGuard header/tag) |
|---|---|---|
| heartbeats, 144/hr at 25 s | 6.8 KB | ~29 KB |
| keepalive comments, 720/hr | 2.2 KB | ~60 KB |
| ~200 genuine changes/hr | 30 KB | ~48 KB |
| **total** | **~39 KB/hr** | **~137 KB/hr** [E] |
| **today's 5 s full poll** | **24 MB/hr** | **~26 MB/hr over 720 TCP handshakes** |

~190× on the wire, not the ~600× a payload-only comparison would suggest. **The connection-count
reduction (720/hr → ~12/hr) is the larger win anyway** — radio wakes, not bytes, dominate mobile
battery.

`HEARTBEAT_S = 25.0`, **deliberately aligned to the WireGuard keepalive**. Tailscale sends a
persistent keepalive every 25 s for peers behind NAT, which cellular CGNAT always is — so the
radio is already being woken at ≤25 s. Moving to the RRC-tail-optimal 45–50 s would not remove
a wake; it would put our packet on a *different* wake. This is a hypothesis with a stated
falsification plan (MetricKit `cellularConditionMetrics`, §9) and a one-constant fix, tracked
dynamically by the client via `hb`.

**The tunnel itself is not free.** An always-on `NEPacketTunnelProvider` is a persistent
background process with its own keepalives, and on carrier CGNAT a direct path to a Mac behind
residential NAT frequently fails, so Tailscale falls back to **DERP** — a persistent TLS relay,
100–300 ms per RTT instead of 60–120. The user will see *Tailscale* in the iOS battery list, not
orchestra. Two supported modes: always-on (the NSE can enrich notification bodies) and
on-demand scoped to the app (lower battery; the NSE cannot reach the Mac, which is why the
counts travel **in** the APNs payload rather than behind an NSE fetch).

**Low Data Mode keeps the stream** (with `low=1`, a 20 s server tick, a 50 s heartbeat, no
keepalive comments) and suppresses only discretionary fetches. Switching to a 60 s poll of the
9.2 KB snapshot would be 552 KB/hr — **13× more expensive** than the stream it replaced, while
claiming to respect the user hardest.

### 7.4 Freshness — two signals, not one

This is the trap that catches every design of this shape from both directions. Keyed on "when
did I last apply a delta," a healthy idle fleet emits zero deltas and the board dims and
disables every control after 30 s of quiet — exactly when a free worktree is most likely to be
what you want to act on. Keyed loosely on "any frame arrived," a wedged collector reads green
forever. Both are real, so they are separate signals:

- **Connection liveness** ← any frame (`hello`, `delta`, `hb`, keepalive comment) against
  `hbPeriod * 1.6 + 5`. `hb` is produced on a timer regardless of change, so an idle fleet stays
  LIVE. `hbPeriod` comes from **every** `hb`, so a client that connected during a 3 s busy
  period correctly widens its window when the server drops to 20 s.
- **Data recency** ← `serverAt`, which is the collector's `generated_at` and advances every tick
  **regardless of ops**. A wedged collector stops advancing it while frames keep arriving, and
  `collector_ok: false` names it explicitly.

| state | condition | presentation |
|---|---|---|
| **LIVE** | frames fresh, `serverAt` fresh, `collector_ok` | green dot, no chrome |
| **LAGGING** | frames fine, `serverAt` behind | amber dot + "data as of 34s ago". **No functional change.** |
| **COLLECTOR STUCK** | `collector_ok: false` | amber bar naming the exception. Actuation disabled. |
| **STALE** | no frame for `hb*3+10` | board dims to 55 %, controls **disabled with the reason on the control**, never hidden (hiding reflows under the thumb) |
| **OFFLINE** | 3 failed reconnects, or `NWPath` unsatisfied | "can't reach `<host>` — is Tailscale on?" + Retry |
| **MAC ASLEEP** | `NWPath.satisfied` but connect refused/timed out, or `wake_gap > 120` | distinct copy: *"your Mac appears to be asleep — nothing is running and no alerts will arrive"* |

> **Ages keep ticking while stale. Statuses dim.**

An age derives from an absolute mtime, so "8m ago" stays literally true with a dead stream and
degrades in the *safe* direction — counting up toward "we don't know" rather than freezing at a
reassuring number. A `● WORKING` badge on a four-minute-old snapshot is a lie.

**Clock skew** is now a first-class concern: moving from relative `age_s` to absolute
`active_at` means a phone 40 s off displays wrong ages everywhere. `hello` and every `hb` carry
`at`; the client keeps a 5-sample median skew and applies it to every age and countdown. Above
120 s, surface it — a wrong reset countdown makes `▶ resume` lie.

**Regression test, both sides:** idle fleet, zero deltas for 5 minutes → LIVE throughout.

### 7.5 Preconditions and the two guards

Actions carry `expect`:

```json
"expect": {"card_rev": "7b21e4de", "agent_id": "ag_7c21f0a9b3de", "pid": 41234}
```

`pid` here is **an assertion, not an address** — the request is routed by `sid` or `agent_id`
(principle 3: agents key on a tmux target or a salted tty, never on a pid), and the pid is
checked so a stale board fails loudly with `409 agent_moved` instead of typing into whichever
process inherited the number. `UX.md`'s Appendix A rule 20 forbids *addressing* by pid and does
not conflict with this.

`card_rev` is a digest of **exactly the facts an action depends on** — availability, the
`(sid, status, handed_to)` set, live pids, `closeout_sent`. Deliberately **not** the whole card:
include `age_s`, `topic` or `git.dirty` and every action 409s spuriously within one tick, users
learn to hammer "do it anyway," and the guard is worse than none.

Rejection is a readable diff, not a code:

```json
{"ok": false, "stale": true,
 "error": "ConfidAI changed while you were looking at it.",
 "changed": ["session 0bc2125a: working → needs_input", "live proc 41234 disappeared"],
 "was": "a3f91c02", "now": "7b21e4de"}
```

**Never auto-retry a 409.** A 409 means a human's mental model diverged from reality; only a
human closes that gap.

**Both guards ship, because they cover different hazards:**

| hazard | guard |
|---|---|
| **staleness** — right card, changed state | `card_rev` captured at **press-down**, submitted with the action → 409 with the diff |
| **mis-targeting** — the board re-sorted and a *different* card slid under your thumb | a card whose grid position changed within **700 ms** swallows the first tap and says "ConfidAI moved — tap again" (the direct port of `_movedAt`, index.html:609) |

In the mis-target case `card_rev` is perfectly fresh — for the wrong card — and the server
executes happily. On a phone, scroll momentum makes mis-targeting the *larger* hazard, and
idempotency would make that mistake exactly once, irreversibly. The confirmation sheet also
names the target card explicitly.

Reordering on iOS is semantic, not hover-based (`pointerenter`/`pointerleave` do not port — a
tap fires enter and leave may never arrive). The sketch below is **superseded by `UX.md` §4.1
and `IOS-APP.md` §4.7**, which are the specification:

```swift
.onScrollPhaseChange { _, phase in reorderHeld = (phase != .idle) }   // NOT SUFFICIENT
```

`onScrollPhaseChange` alone goes `.idle` the instant deceleration ends — precisely when the
user starts *reading*, which is when a re-sort is most hostile. And a time-based release ("held
until 700 ms after the last phase change") auto-applies the reorder inside the 500–900 ms
lift-aim-tap arc, i.e. exactly inside the mis-tap window the hold exists to close. The shipping
rule is **freeze from first touch until an explicit apply** — the `⌗ N updates` pill,
pull-to-refresh, an active-tab tap, foregrounding, or the list being at rest and fully
off-screen — with no timer anywhere. **Content within a card always applies immediately**
(except for a card with an open swipe drawer, which SwiftUI dismisses on content change). Held reorders surface as a tappable pill —
"3 cards moved · tap to re-sort" — which is the desktop's `⌗ re-sort held` with an explicit
release the desktop lacks. Invariant: **the card under your thumb never moves; what it says is
always current.**

---

## 8. Migration & compatibility

Ordered, each step independently shippable and independently revertible. `--legacy-only` is a
real flag through step 8: serve pre-v2 behaviour, no producer, no v2 routes. Any regression is
one restart away from safe. An eight-step rewrite of the wire protocol of a tool that types
into agents running `--dangerously-skip-permissions` cannot ship with "revert a 22-file split"
as its only recovery story.

| # | ships | gate before proceeding |
|---|---|---|
| **1** | **HTTP hardening, unconditional.** `protocol_version = "HTTP/1.1"` **+ `timeout = 30`** + bounded pool; body validated *before* the guard and drained on denial; `411` on chunked; negative-length rejected; `try/except` around the dispatcher; `do_HEAD`/`do_OPTIONS`; `Host` + `Origin` + `Sec-Fetch-Site` checks; `Content-Type: application/json` required on POST; CSP + `nosniff`; single-flight `cached_state`. **Closes the live CSRF and DNS-rebinding holes on a pure-loopback install.** | pipeline two POSTs on one connection, first with a malformed Content-Length, assert the second is answered; idle connection closed within 35 s; `text/plain` + foreign `Origin` → 403 |
| **2** | **The route table.** Exact `(method, path)` matching on both verbs. | `POST /api/dispatchlog` → 404, never `start_dispatch` |
| **3** | **gzip** + `Vary: Accept-Encoding`, excluding `text/event-stream`. | 36,326 → 9,202 B |
| **4a** | **`active_at`.** One line at L659. `age_s` stays. | *prerequisite for everything downstream* |
| **4b** | **`started_at`** on procs. Not a one-liner: macOS/BSD `ps` has no `etimes` column (that is Linux procps), and `lstart=` breaks the fixed regex with a space-separated date. So a real `parse_etime` for all three forms (`15:02`, `12:43:46`, `2-03:14:22`) with unit tests. | three-form parser tests |
| **5a** | **Package split, pure move.** `git mv` + import rewiring + `paths.py` + `migrate_stray_state()`. | `git diff --stat` shows zero changed lines inside function bodies; 142 tests green; the live `resume.schedule.json` still loads |
| **5b** | Test seams, `ConfigGuard` extension, `.gitignore` additions, `TestZeroDeps`, `TestMockability`. | green |
| **6** | **The state bus + producer.** `cached_state()` becomes a read with `fresh=True`. `index.html` untouched and ~1000× faster per poll. Git split, `_gitdirs`, parallel fan-out, transcript memo, single-flight for `_topo`/`_limits`. | **`collect_state` re-measured**, `BASE_S` re-derived from the real number; the empty-patch test green; linked-worktree git cache test green |
| **7** | **`/api/v2/state`** — snapshot envelope + `since=` delta + `wait=` long-poll + ETag. Tiers 2–3, fully testable through `urllib.request`. | conditional-request tests |
| **8** | **Auth**: state dir, registry, tokens, scopes, rate limits, audit; loopback only; `board_auth: "open"`. All **four** HTML files get the meta tag and the shared `api()` helper (`guide.html:280` fetches `/api/state` too — omitting it silently breaks the user chip). | auth on, real token, `/api/state` without one → 401 |
| **9** | **TLS**: key + cert generation, pin derivation, boot assertion. **Before pairing**, because the QR carries the pin. | pin stable across a reissue from the same key; `cert_pin()` raises rather than returning stale |
| **10** | **Pairing**, `qr.py`, `/pair` board view, tailnet supervisor. The phone connects. | QR syndrome test at v5 and v6; a 63-char hostname fits |
| **11** | **Idempotency + ops + locks**, and **`/api/finish` jobified** to return immediately. **Ships before the phone can actuate anything.** | two concurrent auto-picks never share a worktree; restart → `indeterminate`, never re-execute |
| **12** | **`/api/v2/stream`** — SSE. **Gated on step 8**: thread-per-connection + long-lived connections + zero auth is a trivial exhaustion vector. | 65 concurrent sockets → 65th gets 503 and the server keeps serving; no `Content-Encoding` |
| **13** | **`events.py` + `push/`**. ntfy default; APNs behind config until verified against sandbox on a real device. | a `needs_input` edge lands on a phone in ≤3 s |
| **14** | **`/api/v2/chat?after=` + ETag**; `parse_qs` replaces the query regexes (the `account` param is never URL-decoded today, L2219). | |
| **15** | HTML migrates to v2; `legacy.py` deleted. | `legacy_hits` all zero for a week |

**Compatibility contract**, stated because HTML skew is impossible but phone skew is inevitable
— a user who has not `git pull`ed in three months while the App Store auto-updates the app is
the *default* case:

> Within a major version the server MAY add fields, endpoints, and enum values in fields
> documented as open (`status`, `mode`, `flags`, `error.code`). Clients MUST ignore unknown
> fields and MUST treat unknown enum values as a documented fallback (`status` → `waiting`;
> `error.code` → generic, driven by `retriable`). Removing a field, retyping a field, or
> changing a status code requires a major bump and a new path prefix.

`GET /api/health` (unauthenticated, runs no collector) carries `version`, `api_level`,
`features[]`, `min_client_build`, `cert_not_after`, `tailnet.state`, `last_tick_at`. Without it
every skew failure looks like a permissions bug — an unknown endpoint would fall through to a
scope denial and tell the user "this device is read-only," which is both wrong and unactionable.

**Two pre-ship manual verifications** that cannot be unit-tested:

1. A **two-token** APNs batch against sandbox with a real key, asserting **both** return 200. A
   single-URL 403 proves nothing about batching — that is exactly how the `--next` bug survived.
2. **Lock-screen reply, twice**: locked since boot, and locked since last unlock. Different
   Keychain accessibility paths; the first is the one that fails silently with the wrong
   attribute.

---

## 9. Risks, open questions, and what needs a decision

### 9.1 Risks

| risk | severity | mitigation |
|---|---|---|
| **Double-dispatch during the migration window** — steps 1–10 ship a reachable phone before step 11 ships idempotency | high | the phone gets `read` scope only until step 11 lands; `act` tokens are minted but the app's actuating UI is gated on `/api/health` `features` containing `optoken` |
| **The package split breaks a mock nobody notices** | high | `TestMockability` + the two-commit rule + `git diff --stat` gate on 5a |
| **`collect_state` does not get fast enough** and a 4 s cadence is a >40 % duty cycle | medium | step 6 is gated on re-measurement; if the number is bad, cadence rises and the §7.3 latency claim is restated rather than quietly missed |
| **APNs sandbox works, production does not** — a dev-signed build's token is valid only at sandbox, and TestFlight builds are `DEBUG=0` | medium | the app reads `aps-environment` from its embedded provisioning profile at runtime, never `#if DEBUG`; the server auto-heals a `400 BadDeviceToken` by retrying the other host once and persisting the correction |
| **Push silently stops after a restore** and the app looks fine in the foreground | medium | `POST /api/push/register` on every foreground; the settings screen shows "last push delivered: 4m ago (200)" and a test-push button |
| **Tailscale is down/asleep on the phone** and every failure looks identical | medium | a five-rung reachability ladder with distinct copy per rung (§7.4); the app cannot start Tailscale programmatically, only detect and instruct |
| **Audit-log erasure** — an attacker drives rotation with 401s | low | denials coalesced (one line per key per 60 s), rotation daily not size-based, per-IP bucket evaluated before any audit write |
| **The design outruns the code** — `docs/mobile/` now holds ~16,000 lines of specification against zero lines of implementation | — | every step above is individually shippable and individually valuable to the existing board; `ROADMAP.md` M1 and M2 land before an iPhone is involved at all |
| **The specifications disagree with each other** — path prefixes, `hello`/`health`, the list/detail split, the TLS choice, the iOS floor, and the Live Activity set were each decided twice, differently | high | `API.md` §0.1/§0.2 is the single alias-and-gap table; every divergence is now flagged in place in the document that holds the losing version. **Reconcile before writing code, not during.** |

### 9.2 Open questions — measurement, not opinion

1. **Is Tailscale actually keepaliving on the user's path?** Decides `HEARTBEAT_S` (25 vs 50).
   MetricKit `cellularConditionMetrics` + the diagnostics screen answer it; the fix is one
   server constant, echoed in `hb`, tracked dynamically by the client.
2. **Does `NWPath.isConstrained` propagate through the `utun` interface?** §7.3 promises Low
   Data Mode is honoured; if the platform signal is unreliable through the tunnel, the auxiliary
   `NWPathMonitor(requiredInterfaceType:)` cross-check plus a manual override is the fallback.
   **Two hours on a real device with Tailscale on and off, before the cadence table is spec.**
3. **Does `NSLocalNetworkUsageDescription` actually get prompted** for traffic over `utun` to
   100.64/10? If not, adding it spends a scary permission prompt for nothing, on the same
   first-run screen as camera access.
4. **Real `collect_state` cost after the split.** Measurements ranged 1.55–7.06 s [M] across
   sessions; the cadence table and the duty-cycle numbers both key off the post-optimisation
   figure.

### 9.3 What needs the user's decision

**1. The package split.** This is the trade of "one file" for enforceable "installs nothing"
(§3). It is the single largest structural change and the one most visible in the README. Ship
it, or accept a 3,400-line module with the security boundary in the middle.

**2. How TLS is terminated — three options, not two. This is `ROADMAP.md` D1 and it is open.**

- **(a) `tailscale cert` + MagicDNS**, wrapped in `ssl.SSLContext` in-process. A publicly
  trusted certificate on a real hostname, so the client ships **no trust delegate and no pin**.
  This is what `ROADMAP.md` D1 and `UX.md` §1.5 recommend, and this document's §9.3 previously
  failed to list it at all. Costs: HTTPS certificates must be enabled in the tailnet admin
  console; certificates are 90-day and **not self-renewing**, so `tailscale cert` needs a
  schedule; and because the *name* is the identity, the client loses the raw-`100.x.y.z`
  fallback that §5.4 relies on when MagicDNS is slow or off.
- **(b) self-signed P-256 + SPKI pin** — what §5.4 and `API.md` §3.5 specify in full.
  Self-contained, needs nothing enabled on the tailnet, works on both the MagicDNS name and the
  raw IP, and gives a server identity independent of the coordination server. Costs a trust
  delegate, a pin-rotation story, and the verified LibreSSL explicit-curve trap.
- **(c) `tailscale serve --bg --https=443 http://127.0.0.1:4242`** — real Let's Encrypt TLS,
  orchestra never binds beyond loopback, Tailscale injects identity headers. **Mutually
  exclusive with the `Host` allowlist as written**: a `serve`-proxied request arrives with
  `Host: <node>.ts.net` and is 403'd on purpose, and allowlisting it means also switching
  `board_auth` to `nonce` or you publish the admin token to the tailnet.

Note that **(a) and (b) are equally ATS-clean** — a self-signed certificate answered by a
`URLSessionDelegate` challenge is an app's prerogative, not an ATS exception — so "ATS
compliance" is not a discriminator. The real discriminators are the raw-IP fallback, the
renewal chore, and whether you want an identity independent of MagicDNS. Pick one deliberately;
until then, §5.4, §5.5 and the QR's `f=` pin field describe **(b)** and are conditional on it.

**3. Mobile browser access, or not.** `TAILNET_POLICY["serve_html"] = False` means **no mobile
browser access until the iOS app ships.** Serving the board over the tailnet reintroduces the
CSRF surface step 1 closes and needs a second browser-auth UX. `tailscale serve` (decision 2) is
the cheap unblock if that window is unacceptable.

**4. Apple Developer account timing.** Push ships behind a config flag with ntfy as the default.
APNs cannot be verified without a real key and a real device, and §6.2 is explicit that ntfy is
a degraded channel — no inline reply, no deep links, no actions, no badge. If inline reply from
the lock screen is the feature that justifies the app (it plausibly is), the account is on the
critical path for step 13, not after it.

**5. Unattended operation.** Remote control of a laptop requires the laptop to be awake. There
is no service manager today. `caffeinate -is ./start.sh` versus a `launchd` LaunchAgent with
`KeepAlive` versus "accept that push stops when the lid closes" is a product decision, and it
determines whether the overnight limit-reset notification — one of the better reasons for the
app to exist — is a feature or a lie.

**6. QR encoder, or manual-only pairing.** `qr.py` is ~250 lines for a one-time flow. The
compare-don't-type fallback (§5.5) means a QR bug is recoverable rather than fatal, so shipping
manual-only first and adding the QR later is a legitimate cut that removes the largest single
block of new code from the security work.

### 9.4 Honest budget

| | lines [E] |
|---|---|
| bus, producer, delta, SSE | ~450 |
| route table, guard, HTTP substrate | ~230 |
| registry, tokens, scopes, pairing, CLI | ~200 |
| QR encoder (v5 + v6, two renderers) | ~250 |
| TLS, pin derivation, tailnet supervisor | ~130 |
| idempotency, ops, locks, rate, audit | ~200 |
| events, push (apns + ntfy + es256) | ~280 |
| **new Python** | **~1,740**, against 2,302 today |
| JS: `api()` helper across 20 fetch sites in 4 files | ~110 |
| `pair.html` | ~190 |
| new tests | ~700 |

The README says "zero dependencies — one python3 stdlib file." After this it says "zero
dependencies — one python3 stdlib package," and the CI test that proves it is stricter than the
sentence it replaces.
