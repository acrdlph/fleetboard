# ADR 0012 — The kqueue watcher, built; and it is evidence, not truth

**Date:** 2026-07-22 · **Status:** Accepted · **Supersedes:** `ENGINE.md` §10's
"kqueue watches — not building"

## Context

The sweep was made cheap (steps 1–2: 1,641 ms → 506 ms, then a perpetual thread
at 15 % of one core). What it could not be made is *free*, because its floor is
`idle_s`: with nothing happening anywhere it still woke every three seconds,
stat-ed hundreds of files and shelled out to `ps`, forever.

`ENGINE.md` §10 declined to build a watcher, on four arguments. Three of them
were right and one was wrong, and the wrong one is the reason this is an ADR
rather than a commit message:

| §10 said | today |
|---|---|
| "a full stat sweep over 532 transcripts is 7.8 ms" | still true, and still the wrong comparison — the bill is the `git` fan-out and the `ps` that the sweep *also* runs, 1.65 CPU-s, not the stat pass |
| "a directory watch says the directory changed, not which entry" | true, and it is why directory watches are used ONLY for discovery here; writes need file watches. Re-verified: a directory produces **nothing** on in-place modification of a file inside it |
| "subagent `.jsonl` are created in nested dirs, so per-file watches cannot see them" | true, and re-verified deeper: a create in a *nested* directory does not even reach the grandparent. Session dirs are watched one level down; anything deeper is the timer's job |
| "**if ever built: hard-capped at 256 fds**" | the cap is right, the number was a guess. Measured on the real fleet the deliberate watch set is **228 fds** — 173 directories and 55 in-window transcripts — so 256 would have started truncating on this machine, on day one; the shipped cap is 2,048, above the ~1,708 worst case |

The fd ceiling itself has already been corrected once (`VERIFIED-FACTS.md`):
the binding limit is `kern.maxfilesperproc` = 61,440 with `kern.maxfiles` =
122,880 **system-wide** and 18,167 in use at idle, not `RLIMIT_NOFILE`
(1,048,576). Watching all 18,773 `.jsonl` would take ~15 % of the global file
table and break *other applications*. So the objection is real; what it forbids
is watching everything, not watching anything.

## Decision

Build it, with one rule that decides every other question.

**The watcher is evidence, never truth.** Its only output is
`Observer.nudge(reason, git=False)`. It never publishes, never classifies,
never becomes a source of state. The timer sweep is kept underneath — slower,
but never removed — so a dropped event costs **latency and nothing else**.

That rule is not caution for its own sake. A watcher that is trusted absolutely
is a watcher whose one bug is silent: a missed event would present as an agent
that simply never finished, on a board whose whole job is to tell you otherwise.

Consequences of the rule, each verified rather than assumed:

* **The watch set is bounded deliberately** — five layers (roots, `projects`
  roots, *matched* project dirs, in-window top-level transcripts, their session
  dirs). Never the 18,773 subagent files. 228 fds measured, ~1,708 worst
  case; capped at 2,048, and over the cap the excess **degrades to the timer**
  and is logged once. Truncating beats disabling: the 2,049th transcript should
  cost one file its latency, not cost the board its watcher.
* **A burst is one nudge.** Debounced at 50 ms, and — the load-bearing part —
  rate-limited at 1.0 s. Events remove the sweep's floor but they also remove
  its **ceiling**, which is the dangerous half: at `hot_s` (0.15 s) a
  continuously-written transcript would sweep ~7×/s for ~100 % of a core,
  *worse* than the timer it replaced.
* **A watch nudge does not force `git`.** `nudge()` forces the fan-out off its
  cadence because every previous caller was a completed mutation. A transcript
  write is not a mutation, it is an agent typing; forcing git per event would
  run the most expensive thing in the loop at the event rate.
* **Degradation is automatic.** Linux has no stdlib inotify binding, so
  `available()` is false, one line is logged, and the loop reads `idle_blind_s`
  (3.0 — exactly today's behaviour). The same path covers a kqueue that fails to
  open and a watch thread that dies at hour nine, because `watching` is read
  from the thread on every wait rather than latched at startup.

## What this let us change

`idle_s` **3.0 → 30.0**, and it is now a safety net rather than the mechanism.
What it bounds is no longer notification latency (events are) but the things
kqueue *cannot* see: there is no filter for process **birth** (`EVFILT_PROC`
gives us death and costs no fd, but birth stays a timer poll), an append to a
transcript that already fell out of the 48 h window, and subagent files more
than one directory deep.

Measured, `getrusage(SELF)+(CHILDREN)`, 120 s per row, on the real fleet:

| | idle CPU (nothing happening) | write → nudge | write → published version |
|---|---|---|---|
| timer only, `idle_s` 3.0 | 14 % of one core | — (up to 3 s) | — |
| watcher, `idle_s` 30.0 | **6 %** | **53 ms** | **212 ms** |

Both directions at once, which is the unusual part — latency and battery
normally trade against each other, and every earlier step in this work was that
trade. The A/B was **interleaved three times** rather than run as a sequence:
this machine's load average wandered between 8 and 50 during the measurement,
and one of the timer-only rows (13.3 % at load 47) is *flattering* to what is
being replaced, because under load the 3.0 cadence managed 13 sweeps instead of
its own 40.

30.0 and not 60.0, and the argument is not the 3 points. With one agent
actually working — the regime that decides this number, since idle it barely
matters — `idle_s` 30 costs 7 %, 15 costs 10 % and 5 costs 15 %; and of the 35
sweeps at 5.0, 17 were event-driven and the rest found nothing the events had
not already reported. Below 30 the timer is re-discovering what it was just
told. Above it, the blind spot on process birth doubles.

## Consequences

- `ENGINE.md` §10 is stale on this row and is **not** rewritten — same policy as
  ADR 0010 and ADR 0011. This ADR is the record.
- Its `256` is superseded by a measured `2,048`. Note the shape of that error:
  a number invented for a design that was not being built, which would have
  silently truncated on the first machine it met.
- `max_stale_s` had to move 8.0 → 45.0 in the same change. The loop waits
  `min(idle_s, max_stale_s)`, so a ceiling below the cadence silently *becomes*
  the cadence — raising `idle_s` alone would have cancelled the entire win three
  screens away, with every sweep counter still looking plausible.

## What was NOT tested, said plainly

Real laptop sleep. `time.monotonic()` here is `mach_absolute_time()` and
includes sleep (§4.5), so a closed lid arrives as a `control()` that should have
returned in `rebuild_s` and returned hours later — that gap is detected, and the
response is to rebuild the watch set from scratch rather than to ask whether the
knotes survived. Simulated by injecting the gap and by stop/restart; whether a
real S3 cycle preserves a kqueue knote is **unverified**, and rebuilding is what
makes the question moot rather than an answer to it.

## Alternatives rejected

- **Watch every `.jsonl`.** 15 % of the system-wide file table. A dashboard must
  never be able to stop other applications opening files.
- **Delete the timer.** Then a dropped event is a correctness bug instead of a
  latency bug, and it is invisible.
- **`ctypes` around inotify for Linux.** A second platform's worth of untested C
  ABI, for a latency optimisation, in a project whose identity is stdlib-only.
  Linux falls back and says so.
