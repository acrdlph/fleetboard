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
| **Phase** | A — design (nearly settled) · first code shipped |
| **Shipped** | **Layer 0** — classifier ladder reordered (orchestra.py `classify_session`). 142 tests pass; input-space diff confirms only the two intended changes. |
| **Next milestone** | collapse the git subprocess storm (see *Development path*, step 1) |
| **Last updated** | 2026-07-21 |

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

orchestra computes state **lazily** — only when a client asks (`cached_state()`,
orchestra.py:1000). Measured on a 9-worktree fleet:

```
collect_state()          1641 ms
  git_info x9            1277 ms   78%   ← five git processes per worktree, 45 spawns
  scan_sessions           335 ms   20%   ← re-tails 128KB of every transcript, every time
  claude_processes        112 ms    7%
```

Plus a 4 s server cache and a 5 s browser poll → **~10.6 s** from a real change to pixels.

Separately, `CFG["working_s"] = 90` holds `● WORKING` for up to **90 seconds** after a session
stops. That is not lag; that is the heuristic being coarse.

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
| [0008](adr/0008-identity-addressed-mutations.md) | Mutations addressed by durable identity, never by pid | accepted, **fixes a live bug** |
| [0009](adr/0009-api-v1.md) | The versioned API starts at `/api/v1` | accepted |

---

## Development path

Each step is independently shippable and independently valuable. Steps 1–2 make the **existing
browser board** dramatically faster and carry no iOS risk at all — they would be worth doing
even if the phone client were cancelled.

| step | what | expected result |
|---|---|---|
| **1** | Collapse the git storm: one `git status --porcelain=v2 --branch` per worktree instead of five calls; parallelise across worktrees; memoise on `stat()` of `.git/HEAD` + index | 1641 ms → ~200 ms |
| **2** | Move collection off the request path onto a continuous background loop with a monotonic state version | requests stop blocking; push becomes possible |
| **3** | Event-driven invalidation via `kqueue` — react to transcript and git writes instead of sweeping | sub-second detection |
| **4** | SSE to the browser with a delta protocol; retire the 5 s poll | sub-second board updates |
| **5** | Revisit the status model — tighten `working_s = 90` using precise write timestamps, with an anti-flicker rule | `WORKING` stops lying |
| **6** | Ingest Claude Code hooks; reconcile signal sources by rank | `BLOCKED` / `YOUR TURN` become observed, not inferred |
| **7** | Auth, device pairing, tailnet bind | the server is safe to reach from a phone |
| **8** | APNs event pipeline | alerts reach a locked phone |
| **9** | iOS client, against the now-settled contract | the actual app |

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
   import time, importlib.util
   spec = importlib.util.spec_from_file_location("o", "orchestra.py")
   o = importlib.util.module_from_spec(spec); spec.loader.exec_module(o)
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
