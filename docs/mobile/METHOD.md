# Method — how to change this system without shipping a silent bug

Every serious defect found while rebuilding orchestra's observer had the same shape:
**something that looked fine and was quietly wrong.** Not a crash, not a red test — a green
one that had stopped testing anything, a measurement that could not have detected what it
claimed to measure, a number in the right units and the wrong magnitude.

This document is the practice that came out of that. It is not general software advice; every
rule below is here because it caught something real in this codebase, and the incident is named.

---

## 1. The corpus is a dataset, and the experiment already ran

`~/.claude*/projects/**/*.jsonl` holds ~18,700 transcripts of real agent sessions. Almost every
question about "how do agents actually behave" is already answered there. Before designing
around an assumption, go and count.

| question | how it was answered | result |
|---|---|---|
| how often is a turn end observable? | parse 79 in-window transcripts | 82 % |
| how long are genuine thinking pauses? | 3,720 inter-entry gaps, filtered to unexplained | p50 0.9 s · p95 25.9 s · p99 87.3 s |
| does the board quote the harness back at the user? | run `find_last_user` over 654 transcripts | 27 did |
| how often does a background task never report back? | 2,000 launches | 76 (3.8 %) |

**Counterfactual replay is the strongest technique here.** For every decision the board *would
have made* in the past, the transcript records what happened **next**. If the board was about to
say "your turn" and the agent's own next word follows with no human prompt in between, the board
was about to be wrong. That is a free ground-truth label, at scale, with no experiment to run.

That is where `904 claims → 132 misfires → 5.09 % → 4.42 %` comes from: new logic scored against
a past whose answers are already written down.

Two rules for replay:

- **Replay through the shipped code**, imported, not through a paraphrase of it. A
  reimplementation scores the reimplementation.
- **Hold the population fixed** when comparing variants, or you are measuring two things at once.

---

## 2. Measure the thing, in the right unit

**CPU is `resource.getrusage(RUSAGE_SELF) + RUSAGE_CHILDREN`.** Not wall time, not `ps -o time`
on the parent. Both understate this workload by ~2×, for different reasons: the sweep is a
parallel fan-out, so wall < CPU; and most of the cost is billed to `git`/`ps` children, so the
parent's own accounting misses it. Measured: 8 % parent-only against 56 % actual.

> An earlier revision divided sweep milliseconds by the cadence, called it a duty cycle, and
> reported ~17 %. The real figure was 55 % of a core, continuously. Every number in that comment
> was wrong in the same direction. Correcting the *accounting* changed nothing about the code
> and changed the decision completely.

**A performance claim without a measurement is not a claim.** Every threshold constant in
`config.py` carries the distribution it came from and the misfire rate it accepts.

---

## 3. Ask the question you think you are asking

The single most productive failure mode in this project was measuring the wrong thing and
believing the answer.

> **The 3 % / 82 % incident.** "Does a session record its turn ending?" was first measured as
> *"is `turn_duration` the last line of the file?"* → **3 %**. The real question is *"does
> `turn_duration` appear after the last assistant message?"* → **82 %**. The gap is entirely
> `last-prompt` and `file-history-snapshot` entries, which the CLI appends *after* a turn closes.
> A prior analysis had reported 56 %, also wrong.
>
> Three different numbers for one question, none of them typos. The 3 % reading would have
> justified abandoning the best signal in the system.

**A negative result from a test that could not have succeeded is not evidence.**

> **The memo that seemed safe.** The transcript memo keys on `(size, mtime_ns, dev, ino)`. To
> find its boundary I rewrote a file in place preserving size and mtime — the memo returned
> fresh content, which looked like proof of robustness. It was not: `os.utime` with *float
> seconds* cannot restore the nanosecond component, so the key genuinely differed. Forcing it
> with `os.utime(..., ns=...)` defeated the memo immediately.
>
> The first test could not have failed. That makes its pass worth nothing.

---

## 4. A test that cannot fail is not a test

Every new test here is watched **red** before it is trusted. Break the thing it covers, see the
failure, restore. This is not ceremony — it has caught tests that never ran at all.

> **Green on the first mutation.** In step 5 a mutation ("an assistant message does not clear the
> turn marker") came back green because a *different* code path — a human prompt withdrawing the
> marker — masked it. Two tests now exist specifically to isolate that path.
>
> A second mutation ("`turn_ended` defaults to True") was also green until a test pinned the
> default explicitly.

**Clear `__pycache__` before every mutation run:**

```bash
find . -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

> A same-size edit (`+= 1` → `+= 2`) inside the same second leaves both mtime and size unchanged.
> Python reuses the stale `.pyc`, the mutated code never executes, and the mutation looks caught.
> Cost: one confused half-hour, twice.

---

## 5. The safety net must not depend on the thing being changed

The unit suite monkeypatches module globals at 67 sites. That is fine until you move a function
between modules — then `orchestra.git_info` is no longer the name the caller resolves, the patch
silently stops applying, and **the test keeps passing while testing nothing.** A test that fails
is a nuisance; a test that lies is a trap.

So `tests/characterize.py` exists: 5,656 cases that patch **nothing**, call only public
functions with explicit arguments, and byte-compare against a recorded golden. It resolves the
app through `load_orchestra()`, which handles either layout, so the same harness runs on both
sides of a refactor and doubles as proof the package facade is complete.

Two rules that keep it honest:

- **Never re-record to make a failure disappear.** Re-record only in the commit that
  deliberately changes behaviour, and verify the diff **semantically** — parse both snapshots
  and compare section by section — never by eyeballing.
- **Point it at the real path.** The first payload net snapshotted `cached_state()` in *demo*
  mode, which returns a separate hand-written fixture and never touches the compose path. It was
  worthless, and a mutation proved it: a field added inside `collect_state`'s card loop went
  completely undetected. It now builds a real three-worktree fleet on disk.

**Mutation-test the net itself.** Ours catches: added field, renamed field, inverted severity
rank, changed availability string, doubled counts tally.

---

## 6. Changing *when* you observe changes *what observing costs*

The move from lazy to perpetual observation created a bug that could not previously exist.

> **`git status` writes.** Plain `git status` refreshes and rewrites `.git/index` — the inode
> changes on every call — so it takes `index.lock`. Once per look was invisible. Once per
> worktree every few seconds, forever, means the loser of a lock collision is **the user's own
> agent**, whose `git commit` fails with `Unable to create '.git/index.lock'`.
>
> Fixed with `--no-optional-locks`, which is also *faster* (16.5 ms vs 17.6 ms) because not
> writing beats writing.

Generalise: before making anything continuous, audit it for side effects that were acceptable
*because* they were rare. In an observer, **any** write is suspect — file writes, subprocess
side effects, mutation of another module's state. `collect_state` was also reaping another
module's dictionary as a side effect of *observing*; under a perpetual loop that became a
scheduled background action nobody requested.

---

## 7. Know which direction is dangerous

Not all errors cost the same. In this system the asymmetries are:

| direction | cost |
|---|---|
| a stopped agent reads `WORKING` | you check on it for nothing — annoying |
| **a working agent reads available** | **dispatch puts a second agent on that worktree** |
| a status flickers `WORKING → YOUR TURN → WORKING` | summons the user for nothing — worse than being 90 s late |
| a chat reply reaches the wrong agent | it *acts on it* under `--dangerously-skip-permissions` |
| a resume fires twice | real money, unattended |

Design against the expensive direction, and say which one it is. `orphan_grace_s` stayed at 90 s
because the measurement was thin and it feeds worktree-FREE feeds dispatch targeting: *a
conservative wrong answer there is survivable; an aggressive one is not.*

---

## 8. Ceilings quietly become cadences

> **Twice, the same shape.** Raising `idle_s` from 3 s to 30 s left `STATE_TTL_S` at 4 s — so the
> cache expired 4 s into every 30 s cycle and 13 of 30 polls fell back to a full synchronous
> collect, 8–17 s each. The publish point's entire benefit was silently undone.
>
> Independently, `max_stale_s` was left at 8 s under the same raised cadence.

When you raise a cadence, grep for every bound that was implicitly *below* it. A ceiling set
under a lower cadence does not stay a ceiling; it becomes the new cadence.

---

## 9. Design documents are good at structure and bad at numbers

`ENGINE.md` was wrong six times, each caught only by measurement — a per-repository git
abbreviation length, a walk-skip that would have silently deleted a feature, a memo that cannot
exist, a cadence costing 164 % of a core, a threshold below the p95 of the population it exists
to tolerate, and a `settle()` that re-stamps its own dwell clock.

Its *structural* judgement — the decomposition, the seam rules, versioning semantics, "flicker
is worse than lag" — has been consistently right.

**So: trust design documents on shape, verify them on quantity.** The document is not rewritten
to match; that would erase the record of what was considered. ADR 0011 indexes where it is stale.

---

## 10. Write down the boundary of every cache

Not "this is safe" — *"this is safe until X, and here is what X costs."*

The transcript memo can be defeated by an in-place rewrite preserving size, nanosecond mtime and
inode simultaneously. Adversarial rather than realistic — transcripts are append-only — and the
60 s cold reconcile bounds it by recomputing from scratch and counting disagreements as `drift`.

Every memo added here is required to be **defeatable by the cold reconcile**, and `drift` must be
observable. A cache whose staleness nobody can measure is a cache nobody can trust.

---

## Provenance

Findings above marked with measurements were produced either directly in the main session or by
subagents that reported their method. Where a subagent's number was later re-measured
independently it is the re-measured value that appears — three times the two disagreed, which is
itself the argument for §1 and §3.
