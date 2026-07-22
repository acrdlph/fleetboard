# orchestra — architecture evolution & iOS client

**Start here.** This directory holds the design for two linked pieces of work:

1. **Re-architecting orchestra's state layer** so status is fresh, accurate, and pushable.
2. **A native iOS client** that drives the fleet from a phone while sessions keep running on
   the Mac.

They are one programme, not two. The backend changes exist largely *because* a phone client is
coming — see [ADR 0004](adr/0004-backend-before-ios.md) for why they are sequenced the way they
are.

---

## Current status

| | |
|---|---|
| **Phase** | B — backend, in flight |
| **Shipped** | steps 0–3 + 5 + identity: the board watches on its own clock, reacts to writes, and costs 5.3% of a core. An ended turn is read off the transcript rather than waited out, and delegated work — including the background tasks the CLI's own counts miss — holds the turn open |
| **Next** | step 4 (SSE) — see *Development path* |
| **Tests** | 415 · characterization 4,216 cases |
| **Last updated** | 2026-07-22 |

Design documents are being generated and reconciled. Until each is listed as **settled** below,
treat it as draft.

| document | covers | status |
|---|---|---|
| [`VERIFIED-FACTS.md`](VERIFIED-FACTS.md) | measurements and platform capabilities, taken empirically on the dev machine | **settled** |
| [`ENGINE.md`](ENGINE.md) | the component decomposition — what knows, what tells, what does | **settled** |
| [`FRESHNESS.md`](FRESHNESS.md) | killing the status lag: collector, event-driven invalidation, the status model | **settled** |
| `ARCHITECTURE.md` | system architecture, auth, push pipeline, realtime, migration | in progress |
| `API.md` | the full HTTP contract both clients consume | in progress |
| `UX.md` | mobile information architecture, every flow, the visual system | in progress |
| `IOS-APP.md` | iOS engineering plan | in progress |
| `ROADMAP.md` | phased delivery plan | in progress |

> **`VERIFIED-FACTS.md` outranks every other document.** Its contents were measured, not
> recalled. If a design doc contradicts it, the design doc is wrong.

> **Naming note.** The *project* is `orchestra`; the *checkout directories* are still
> `~/Downloads/orchestr` and `~/Downloads/orchestr-engine`. That is deliberate — a directory
> name is invisible to git, to the config (which points at the parent, `~/Downloads`), and to
> the code. Renaming them would break the `git worktree` link (absolute paths are baked into
> `.git/worktrees/*/gitdir`) and would make Claude Code treat this as a brand-new project,
> since session and memory directories are keyed by munged cwd — the same mechanic orchestra
> itself uses to map transcripts to worktrees. Not worth it. Paths in these docs point at the
> real directories.

---

## The problem, in one screen

orchestra computes state **lazily** — only when a client asks
(`cached_state()`, now `orchestra/observer.py`). Measured on a 9-worktree fleet:

```
collect_state()          1641 ms
  git_info x9            1277 ms   78%   ← five git processes per worktree, 45 spawns
  scan_sessions           335 ms   20%   ← re-tails 128KB of every transcript, every time
  claude_processes        112 ms    7%
```

Plus a 4 s server cache and a 5 s browser poll → **~10.6 s** from a real change to pixels.

Separately, `CFG["working_s"] = 90` holds `● WORKING` for up to **90 seconds** after a session
stops. That is not lag; that is the heuristic being coarse.

> **Both are now fixed (steps 1–3 and 5).** `/api/state` is a 0.8 ms dict read off a background
> sweep, a write reaches the board in ~1 s, and the 90 s window is gone: 84 % of sessions resolve
> on the CLI's own end-of-turn marker with no clock at all, and the residual falls to
> `quiet_s = 45` — a number taken off the measured misfire table, not off a feeling. The
> paragraphs above are kept as the record of what the work started from.

**The framing that organises all of this work** — three different problems needing three
different fixes:

| class | meaning | fixed by |
|---|---|---|
| **latency** | truth changed, we have not looked yet | faster collection, then push |
| **hysteresis** | we looked, but the rule holds the old value | precise write timestamps |
| **ambiguity** | the signal cannot distinguish two states | better signals (hooks) |

Conflating them is the main way this work goes wrong. Faster polling does nothing for the
second two.

And the structural blocker: **lazy observation makes push impossible.** A notification's whole
job is to reach you when you are *not* looking — but with no client attached, nothing computes,
so nothing is ever detected. See [ADR 0006](adr/0006-observation-is-continuous.md).

---

## Decisions so far

Every load-bearing choice is recorded in [`adr/`](adr/) with its context, its consequences, and
the alternatives that were rejected and why. Read these before proposing changes — most obvious
objections have already been considered and answered there.

| # | decision | status |
|---|---|---|
| [0001](adr/0001-transport-tailscale.md) | Reach the server over Tailscale, not the public internet | accepted |
| [0002](adr/0002-client-native-swiftui.md) | The mobile client is native SwiftUI, iOS only | accepted |
| [0003](adr/0003-push-apns.md) | Push via APNs, driven from stdlib Python (openssl + curl) | accepted |
| [0004](adr/0004-backend-before-ios.md) | Settle the contract once; backend first; iOS last | accepted |
| [0005](adr/0005-sse-on-threadinghttpserver.md) | SSE on the existing ThreadingHTTPServer — no rewrite | accepted, **verified** |
| [0006](adr/0006-observation-is-continuous.md) | Observation becomes continuous and client-independent | accepted |
| [0007](adr/0007-hooks-as-first-class-signal.md) | Claude Code hooks as a first-class signal source | accepted in principle |
| [0008](adr/0008-identity-addressed-mutations.md) | Mutations addressed by durable identity, never by pid | accepted, **implemented** (`orchestra/identity.py`) |
| [0009](adr/0009-api-v1.md) | The versioned API starts at `/api/v1` | accepted |
| [0011](adr/0011-measurement-supersedes-the-design-doc.md) | Where ENGINE.md and measurement disagree, measurement wins | accepted |
| [0012](adr/0012-the-watcher-is-evidence-not-truth.md) | The kqueue watcher, built — and it is evidence, not truth | accepted, **implemented** (`orchestra/watcher.py`); supersedes ENGINE.md §10 |

---

## Development path

Each step is independently shippable and independently valuable. Steps 1–2 make the **existing
browser board** dramatically faster and carry no iOS risk at all — they would be worth doing
even if the phone client were cancelled.

| step | what | result |
|---|---|---|
| **0** ✅ | three unattended-path bugs | an armed 3am resume fired at all; one resume stopped costing 3× usage; 27/654 transcripts stopped quoting the harness back as you |
| **1** ✅ | git storm: 5 spawns → 2, parallelised | `collect_state` 1641 → 506 ms |
| **2** ✅ | publish point — background sweep, versioned immutable snapshots | `/api/state` 506 ms → **0.8 ms**; push becomes possible at all |
| **—** ✅ | make the sweep affordable: git on a 15 s cadence, transcript memo, `(pid,start)` cwd memo | 55% → 15% of a core |
| **3** ✅ | kqueue watcher — react to writes instead of sweeping | idle **5.3%** of a core; write→board ~1 s (was a 30 s cadence); 220 fds |
| **—** ✅ | identity-addressed mutations (ADR 0008) | a recycled pid is refused, not delivered to the wrong agent |
| **5** ✅ | **status model — `working_s = 90`** | phase 1: the CLI's own end-of-turn marker is read positionally and wired into `classify_session`, so **84 %** of in-window sessions resolve by observation and stop waiting out the window (median lateness removed: the full 90 s). Phase 2: the residual 16 % fall to `quiet_s = 45`, chosen off the misfire table (2.71 % against 5.80 % at ENGINE.md's 25), with `settle()` making escalation instant and de-escalation dwell 3 s. Phase 3: `delegated` stops under-counting — a tool_use that LAUNCHED background work counts until its `<task-notification>` arrives (`delegated_s = 600`), taking the end-of-turn misfire rate from **5.09 % to 4.42 %** over 904 replayed claims at no measured cost. Phase 4: the last two inherited numbers stop being `working_s` under another name — `block_grace_s = 60` (the p95–p99 band of genuine tool-run silence, 1.03 % false ■ BLOCKED against 0.82 % at 90, and free on the board's own working set), and `orphan_grace_s` stays at **90** because the measurement says the timer is standing in for a guard that does not exist. |
| **4** ⬜ | SSE + delta protocol; retire the 5 s browser poll | the browser finally sees the ~1 s the server already knows |
| **6** ⬜ | Claude Code hooks; reconcile signal sources by rank | `BLOCKED`/`YOUR TURN` become observed, not inferred |
| **7** ⬜ | auth, device pairing, tailnet bind | safe to reach from a phone |
| **8** ⬜ | APNs event pipeline | alerts reach a locked phone |
| **9** ⬜ | iOS client, against the settled contract | the actual app |

**Why 5 before 4.** Notifications fire on status *transitions*. Building the SSE stream and the
APNs pipeline on a status model we already know is wrong means every transition changes
underneath them later, and the notifier gets rebuilt. Settle what a status means, then stream it.
Step 3 is also what makes step 5 possible: the 90 s window existed because a stateless collector
could only ask "is the mtime within 90 s?" — precise write timestamps now exist and are unused.

## Open items — deliberately deferred, not forgotten

| item | why it is parked | where |
|---|---|---|
| `age_s` still ships beside `last_write_at` | one release of overlap so nothing breaks; remove with step 6 | `transcripts.py` |
| `classify_session` takes `procs_known` and **nothing passes it** | The guard for a wholesale `ps`/`lsof` failure — "never claim ENDED, never claim FREE" — exists in the signature and in the characterization, and no call site supplies it, so a blind probe currently publishes ○ ENDED for every session past its orphan grace. It is also the reason `orphan_grace_s` cannot be shortened on measurement alone: with 0 observed probe failures there is no distribution to place a number in, and the timer is the only thing standing in for the guard. Replace the timer with the evidence, then the number stops mattering | `status.py`, `transcripts.py` |
| `skip_perms` is poisoned by `claude`'s own helper processes | It is `all("--dangerously-skip-permissions" in p["cmd"])` over the worktree's procs, and `claude bg-spare` / `claude bg-pty-host` match `claude ` while carrying no such flag. Observed live on `ConfidAi7`: three procs, two real agents that both skip permissions and one `bg-spare` helper, so the whole worktree reads `skip_perms = False` and its agents become eligible for a ■ BLOCKED they can never resolve — over 25 minutes of board ticks, **every** ■ BLOCKED on the fleet came from that one worktree. The helper is also *paired to a session* by `pair_sessions_with_procs` — the same session held pid 70327 for 797 consecutive ticks, so its status was read off a 6-hour-old PTY daemon rather than an agent. Needs its own measurement of which `claude <subcommand>` forms are agents | `transcripts.py`, `procs.py` |
| `subagents_active` still reads `working_s` | a display hint on the card, not a status, and there is no measured gap distribution for subagent writes yet. Moving it to `quiet_s` would be a guess wearing a measured number's name | `transcripts.py` |
| transcript memo can be defeated by a size+mtime_ns+inode-identical rewrite | adversarial only — transcripts are append-only; the 60 s cold reconcile bounds it | ADR 0011 |
| `dirty` cannot be memoised | it is the working tree; no cheap stat sees an edit. Bounded by `GIT_S` and dated by `freshness["git"]` | ADR 0011 |
| a dispatch's new branch is not nudged | the branch is cut by the launched agent minutes later, with no signal back; bounded by `GIT_S` | `dispatch.py` |
| `ENGINE.md` is stale in four places | measurement supersedes it; the doc is a design record, not rewritten | ADR 0011 |
| the transcript corpus is ~5 GB / 18,773 files, +1,000/day | orchestra's own inputs are a slow disk leak; wants a retention policy | — |

**The load-bearing interface is the delta/event format introduced at step 4.** The browser
consumes it over SSE, the APNs pipeline is derived from it, and the Swift client reconciles
against it. Design it once, correctly, for all three consumers — that is the whole point of the
sequencing in ADR 0004.

---

## How to pick this work up

1. Read `VERIFIED-FACTS.md`. Do not re-derive its measurements; do not contradict them.
2. Read the ADRs in order. They are short and they carry the *why*.
3. Read `ENGINE.md` for the component boundaries, then `FRESHNESS.md` for the mechanism.
4. Check *Current status* above for where the work actually is.
5. Re-measure before optimising. The baseline command:

   ```bash
   python3 - <<'EOF'
   import time
   import orchestra as o          # run from the repo root; ADR 0010 made it a package
   o.load_config()
   t = time.time(); o.collect_state()
   print(f"collect_state: {(time.time()-t)*1000:.0f} ms")
   EOF
   ```

## Conventions

- **New decisions get an ADR.** Copy the shape of an existing one: Context, Decision,
  Consequences, Alternatives rejected. Number sequentially. Never edit an accepted ADR to
  reverse it — write a new one that supersedes it, and mark the old one `Superseded by ADR
  NNNN`. The history is the value.
- **Tests stay stdlib `unittest`**, matching `tests/`. Zero dependencies, same as the app.
- **Preserve the render invariants.** `index.html` encodes hard-won rules — the tick must not
  clobber open controls, and the board must not re-sort under the cursor. Any push-based update
  path must preserve them; they are stated testably in `FRESHNESS.md`.
- **Zero dependencies is a real constraint**, not a slogan. Trading it requires an ADR.
