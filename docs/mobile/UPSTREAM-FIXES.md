# Upstream fixes — what each `main` fix says about the architecture

`main` keeps shipping fixes while `engine` is rebuilt. This file tracks them, but **not as a
merge checklist**. The premise:

> A fix on `main` is evidence that the current architecture *made that bug possible*. The
> question is never "port the patch" — it is **"does the new design make this bug
> unrepresentable, or does it inherit the same flaw?"**

Three outcomes per fix:

| verdict | meaning |
|---|---|
| **inherited** | the new design has the same hole. Fix it in the design, not just the code. |
| **prevented** | the new design makes this class of bug structurally impossible. Record why. |
| **local** | genuinely incidental. Merge and move on. |

Check for new commits with:

```bash
git log engine..main --oneline
```

---

## `2178df9` — a stalled closeout gets told exactly what's left

*Landed 2026-07-21. `closeout_step()`, `CLOSEOUT_NUDGE_TEXT`, persistent ⧗ card note.*

**The bug.** An agent finished its closeout but left one dirty file it judged to be "another
session's in-flight work". No other session was live, so nothing would ever converge:
`✕ close` refused forever while the agent idled forever. A true deadlock.

**Five architectural findings — this one commit is the richest evidence we have.**

### 1. The actuator reads engine state mid-mutation — **inherited**

`start_finish()` now calls `scan_sessions([wt], mine, now)` synchronously, inside a mutation, to
decide what to do. This is precisely the backward dependency the ENGINE/BROKER/ACTUATOR critique
flagged, and this fix makes it *load-bearing* rather than incidental.

**Design consequence:** the actuator needs a first-class, synchronous "give me current observed
state for this worktree" call against the engine — with defined freshness semantics. If that
read is served from a stale background snapshot, the closeout logic decides on old truth and
types at the wrong moment. Specify the freshness contract; do not leave it implicit.

### 2. `_cache["t"] = 0.0` appears again — **inherited**

Another instance of reaching into cache internals to force recomputation after acting. That
pattern now appears in several places and is a direct symptom of the missing seam.

**Design consequence:** the engine must expose an explicit `invalidate(scope)` / "refresh now,
synchronously" signal that the actuator calls on completion. Poking a cache timestamp is the
current substitute for an API that does not exist yet.

### 3. `_closeouts` is durable intent state, and it is not durable — **inherited**

`_closeouts[wt_name] = time.time()` now carries richer semantics: a re-armable 60 s guard whose
clock restarts on each nudge. It is in-memory only, so a server restart silently drops an
in-flight closeout — the card reverts to `✓ finish` and re-typing the brief becomes possible.

**Design consequence:** confirms the missing "durable intent" layer. Three intent stores already
exist with three different policies (`_jobs` in-memory LRU 20, `_closeouts` in-memory,
`_resumes` persisted to JSON). They need one policy. On a phone this matters more: a closeout
armed from the phone must survive both a server restart and the app being backgrounded.

### 4. `nudge_after_s=60` is idempotency by wall-clock — **inherited**

The anti-double-type guard is "was the brief sent at least 60 s ago". That defends against a
rapid double-click on a desktop. It does **not** defend against a network retry: a mobile client
whose request times out and retries at t+61 s bypasses the guard entirely and types the nudge
twice at a live agent.

**Design consequence:** direct empirical support for [ADR 0008](adr/0008-identity-addressed-mutations.md)
and the idempotency-key requirement. Time-based guards are not idempotency. Every mutating
endpoint needs a client-supplied key with server-side dedup.

### 5. Layer 0 changes this fix's behaviour — **verified interaction**

`closeout_step` refuses whenever `any_working`, so it never types over live work. But
`working` came from `classify_session`, which Layer 0 reordered. Measured:

```
session: live, AskUserQuestion pending, wrote 30s ago
  main   classify -> working      any_working=True   -> closeout_step = 'refuse'
  engine classify -> needs_input  any_working=False  -> closeout_step = 'chat'
```

**This is an improvement, not a regression.** The 90 s window was mislabelling a
question-asking agent as `working`; the refusal was a symptom of the same bug Layer 0 fixes. An
agent stuck on a question during closeout should send the user to chat so they can answer it —
which is now what happens.

**But it must not be merged blind.** `closeout_step`'s safety ordering depends on the semantics
of `working`, so any future change to the classifier is also a change to closeout safety. Add an
integration test pinning this pair together.

---

## `cef305f` — the account picker quotes the week you'd actually spend

*Landed 2026-07-21. `index.html` only, +33 lines.*

**The bug.** cclimits' `headroom_percent` folds the session window into its minimum, so a 37 %
session read as "37 % of the week left". Worse, a model-scoped cap was ignored entirely — a
fable mission could be routed to an account whose fable week was already exhausted while the
picker promised 13 %.

### The client re-derived a policy the server already owns — **inherited, and the most important finding so far for `API.md`**

The fix makes the composer "mirror `_model_remaining`'s matching rule per account". That is the
tell: **the same policy now exists twice**, once in Python and once in JavaScript, and they had
drifted. The bug was the drift.

With one client that is a maintenance smell. With **two** clients it is a guaranteed defect: the
iOS app would need a third copy of the same rule in Swift, and it would drift too — silently,
in a build the user updates separately from the server.

**Design consequence, and it should be treated as a rule in `API.md`:**

> The server exposes **decisions**, not raw numbers for clients to interpret.

The account picker's endpoint must return, per account and per requested model: the effective
remaining percentage, **which limit binds** (umbrella week / model cap / session), whether it is
reserve-blocked, and whether it is eligible for this dispatch. The client renders that; it never
re-computes it. Any field a client must *interpret* to stay correct is a field the server should
have decided.

This also removes a whole class of mobile bug: a phone running an older build cannot mis-route a
dispatch if it never held the routing rule in the first place.

---

## Template for the next one

```markdown
## `<sha>` — <subject>

*Landed <date>. <files touched>.*

**The bug.** <what actually went wrong>

### <finding> — **inherited** | **prevented** | **local**

<what it proves about the architecture, and what changes in the design as a result>
```
