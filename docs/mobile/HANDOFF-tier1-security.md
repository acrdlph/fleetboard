# Handoff — Tier 1 security, and the review tail

**Date:** 2026-07-24 · **For:** a fresh-context agent picking up where the review-and-harden
pass left off. **Read this, then `docs/mobile/METHOD.md` and `docs/mobile/PRODUCTION-READINESS.md`,
before touching anything.**

This is not a design doc. It is a map: what was just done, how to verify your own work, what is
left, and the traps that will bite you if you skip the setup section.

---

## 1. What just happened

A full multi-agent review of the whole system (Python engine + iOS client + web board) produced
**95 confirmed defects** (5 critical, 21 high, 67 medium, 2 low). They were then fixed in waves,
each verified against the full suite and **merged to `main` (public) and pushed**. As of this
handoff:

- **Every critical and every high is fixed.** The double-execution cluster (two agents on one
  branch) is closed by per-worktree / pick / tmux-buffer locks **and** a persisted, boot-aware
  wire-idempotency layer (`orchestra/idem.py`); the HTTP substrate is hardened (body cap, socket
  timeout, `select.poll`, exact-match routing, dispatcher `try/except`); the transcript sweep no
  longer crashes on a bad line or pins a session WORKING forever after an interrupt; the push
  pipeline prunes dead tokens and does not storm on restart; the iOS client's timeout no longer
  truncates `finish`, its staleness is honest, and it detects a server restart; the inline-reply
  banner finally carries a working Reply button (`aps.category` on the wire).
- **The three "contract" calls are made:** wire idempotency (opt-in, backward-compatible), a Host
  allowlist (closes T2), and ADR-0013 ratified with the TLS-vs-plain-HTTP docs corrected. **Token
  scopes were deliberately NOT built** — see §4.3.
- Fix commits carry a `harden:` / `ios:` prefix and a `Co-Authored-By: Claude Fable 5` trailer.
  New regression tests live in `tests/test_fixes_*.py` and `ios/Tests/OrchestraKitTests/Fixes*Tests.swift`.

**Your job:** Tier 1 of `PRODUCTION-READINESS.md` (§4 below), then the medium/low tail (§5). The
criticals and highs are done; nothing below is load-bearing for daily use, but §4.1 is the
scariest remaining gap.

---

## 2. Repo, branches, worktrees

- Two worktrees over one repo. `main` is checked out at **`/Users/achill/Downloads/orchestr`**;
  the `engine` branch at **`/Users/achill/Downloads/orchestr-engine`**. All the recent work was
  done in the engine worktree, committed on `engine`, then merged into `main`. `main` and `engine`
  are currently in lockstep. **You can work on either; keep doing the merge dance (commit on the
  working branch, then `git merge` in the `main` worktree) or simplify to one branch — your call,
  but do not lose the two-worktree layout without checking with the user.**
- Public remote: `git@github.com:acrdlph/orchestra.git`. Branch protection expects 3 status
  checks; pushes have been going green.
- **Secrets are NOT in the repo and must stay out:** the APNs `.p8` lives at `~/.orchestra/apns/`,
  the real `ios/Signing.xcconfig` (team `4K738RNZAA`) is gitignored, and `idem.store.json` /
  `resume.schedule.json` / `devices.json` / `audit.log.jsonl` are runtime state, all gitignored.
  Before any push, scan history for a `.p8` or a `BEGIN PRIVATE KEY` that is not a test fixture.

---

## 3. How to verify your own work — READ THIS

Three test surfaces. Run the one(s) your change touches; run all three before a merge.

```bash
# Python engine (stdlib unittest, ~1112 tests, ~70 s):
cd /Users/achill/Downloads/orchestr-engine && python3 -m unittest discover -s tests

# iOS logic layers (API/Model/Store/Rules — headless, ~1 s, ~158 tests):
cd /Users/achill/Downloads/orchestr-engine/ios && swift build && swift test

# The whole iOS app incl. the SwiftUI UI layer (needs the simulator, ~1–2 min):
cd /Users/achill/Downloads/orchestr-engine/ios && xcodebuild build -scheme Orchestra \
  -project Orchestra.xcodeproj -destination 'platform=iOS Simulator,name=iPhone 17 Pro Max' \
  -configuration Debug CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO
# success == the last line is "** BUILD SUCCEEDED **". The target is warnings-as-errors under
# Swift 6 strict concurrency, so a clean build also means no new warnings.
```

**Hard rules that the suite enforces implicitly — break one and the build goes red in a way that
is hard to diagnose:**

1. **Python mock seam.** Tests stub by reassigning module attributes, so intra-package calls MUST
   resolve at call time: `from . import othermod` then `othermod.func()`. **Never**
   `from .othermod import func`. Adding one such line silently breaks dozens of mocks.
2. **The golden characterization fixture** (`tests/golden/characterization.json`, ~98k lines) pins
   the classified STATE projection. Any change to `transcripts.py` / `status.py` / `observer.py`
   *classification* output changes it, and re-recording is a deliberate, diff-verified ritual (see
   METHOD.md). Most of the recent fixes were chosen to NOT touch it — if yours must, re-record
   ONCE in the same commit and read the diff to confirm only the intended cases moved.
3. **`test_send_keys_reaches_the_shell` (test_integration) is a known flake** — a 5 s tmux
   round-trip that loses under load and passes in isolation. If it is your only failure and the run
   was slow (>90 s), re-run it alone before believing it. CI skips it for this reason.
4. **iOS UI layer has no headless tests** — only `swift build`/`swift test` cover API/Model/Store.
   The SwiftUI `UI/` and `App/` code is verified by `xcodebuild` compilation + reasoning. (Closing
   that gap is a tail item, §5.)

**If you delegate to sub-agents:** they have a recurring habit of doing the work correctly but
failing to emit their final structured report (a `StructuredOutput retry cap` error). **Do not
trust the report; verify the tree yourself** — run the tests, read the diff. Also: **forbid git
commands inside agents** (`stash`/`checkout`/`reset`/`add`/`commit`) — the tree is shared and one
agent's `git stash` will eat another's uncommitted work (this happened; it cost a recovery). Give
each agent a disjoint set of files and a NEW test file so nothing collides.

---

## 4. Tier 1 — the tasks (in order)

From `PRODUCTION-READINESS.md`. The server types into terminals running
`--dangerously-skip-permissions` and dispatches agents that spend money, so this tier is small and
high-stakes.

### 4.1 Biometric gate on the app (do first — the scariest gap)
Today anyone holding the **unlocked** phone can open Orchestra and drive the fleet. Add
`LocalAuthentication`: an `LAContext` wrapper, and a gate in `ios/App/RootView.swift` (or at
minimum in front of every *mutation* — dispatch/send/finish/resume — in the act paths). Face ID /
Touch ID with a passcode fallback. **Verify with the `xcodebuild` simulator command above.** Note
the review's own caveat (threat T10): client-side biometry is advisory; the controls that actually
hold are server-side (scopes, rate limits, audit, revoke). Do not oversell it in copy.

### 4.2 A formal security review of the exposed surface
Run the `/security-review` treatment (or a focused security audit workflow) on: `orchestra/auth.py`
(the guard, the exempt `/api/v1/pair` door, the Host allowlist, the rate-limit bucket),
`orchestra/server.py` (`do_POST`, the idempotency wrapper, body handling), and the actuation layer
(`dispatch.py`/`finish.py`/`resume.py`/`terminal.py` — tmux/osascript quoting of hostile mission
text). `METHOD.md §7` names which direction is dangerous. The first review already hardened these;
this is the independent second look before long-term trust.

### 4.3 Token scopes (`read` / `act` / `admin`) — the deliberately-deferred one
Today one token grants full fleet control. `auth.py` **reserves** the design and its own comment
warns *"a half-built scope ladder is worse than an honest absence."* This is why it was NOT bolted
on as a review fix. It interlocks with the app: two Keychain items (a `read` token usable with no
user present for background refresh, an `act` token behind biometry), the `AfterFirstUnlock` vs
`WhenUnlocked` storage classes, and the two self-service endpoints (`/devices/self/apns`,
`/devices/self/reissue-act`). The full design is in `ARCHITECTURE.md §5.3`. Build it whole, with
tests for every scope-denial path, or not at all.

---

## 5. The review tail — remaining confirmed findings

All criticals/highs are fixed. These confirmed **mediums** are what's left; none is load-bearing,
but the first few are worth doing. The full ranked, filterable list of all 95 findings is in the
review dashboard artifact (ask the user for the `claude.ai/code/artifact/...` link) and the raw
data was at `w14m2yyu1.output` in the session scratchpad.

**Worth doing (grouped):**
- **Test-suite integrity (do these — they protect the open-source promise):**
  - The two enforcement tests `ARCHITECTURE.md §4.5` specifies — `TestZeroDeps` (AST-walk every
    import is stdlib + every shelled `argv[0]` is in a binary allowlist) and `TestMockability` (no
    `from .<module> import <lowercase>`) — **do not exist**. Write them; they mechanically enforce
    §3 rules 1 and the installs-nothing promise.
  - The golden fixture and some tests **embed the author's hostname/user**, so they fail on any
    other machine / in others' CI. Parameterize them. (`tests/characterize.py` and friends.)
- **Observer:** the `§4.5` post-wake push-suppression is only partially delivered (a notification
  storm after the lid opens is possible); and the cross-boot delta `epoch` was deferred (the iOS
  client already detects a restart via a backwards-version heuristic, so this is now cleanliness —
  but if you add it, it spans `observer.py` + `server.py`'s SSE `id:` line + `stream.js`, and the
  iOS client already parses `<epoch>:<v>`).
- **Actuation:** `finish` closeout briefs are sent with no submission receipt (the two-step trusts
  the send); `resume_loop` fires serially so one slow tmux resume delays every other due schedule.
- **Auth/transport:** no tailnet supervisor in `__main__` (ADR-0015's retry/backoff bind is a boot
  gate, not a supervised thread); config validation is JSON-syntax-only (a type error surfaces late
  and badly).
- **Web:** CSP/`nosniff`/`no-referrer` headers are promised on the HTML boards but never emitted —
  and the pages are pervasively inline, so a real CSP needs nonces or a refactor. **This is a
  judgment call:** either emit a CSP compatible with the inline content or correct the docs to stop
  promising one. Left for a human decision.
- **iOS:** the entire SwiftUI `UI`/`App` layer is outside every automated test (the `Package.swift`
  finding) — add a UI test target or ViewInspector-style coverage; and `FleetView`'s re-sort hold
  is the narrowed version (UX.md §4.1's full "N updates" pill + explicit-apply is not built).

**The 2 lows and the rest of the mediums** are hygiene (dead code, a QR external-oracle test gap,
minor doc mismatches) — triage from the dashboard; none blocks anything.

---

## 6. Conventions you'll trip on if you don't know them

- **The design docs are deliberately stale in places** (ADR 0011): `ENGINE.md` and parts of
  `FRESHNESS.md` keep known-wrong numbers on purpose, with an index of divergences. **Trust the
  code and the golden, not the prose.** `API.md §0.1` is the authoritative alias table; the path
  prefix is `/api/v1/…` and the unversioned `/api/state` etc. are frozen legacy.
- **The four HTML boards are frozen-behaviour** — the server serves them and they must keep
  working; bug-fix them (as the AudioContext leak was fixed) but don't restructure.
- **Absolute time on the wire, monotonic for durations** — every duration in the engine now uses
  `time.monotonic()`; `time.time()` appears only in values a client derives an age from
  (`generated_at`, `fresh[]`). Preserve that split.
- The APNs pipeline is real code but **push has never been proven end-to-end to a 200** — if you
  touch it, the `--send-test-push` path is how you'd prove it (needs the `.p8`, already in place).

Good luck. The bones are good; this tier is about turning "works for me" into "safe to leave
running."
