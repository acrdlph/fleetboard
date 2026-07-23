"""orchestra.watcher — stop sweeping for changes; react to them.

The sweep's floor was `idle_s`: with nothing happening at all it still woke
every few seconds, stat-ed hundreds of files and shelled out to `ps`, forever.
This module removes that floor. A kqueue thread holds a BOUNDED set of file
descriptors and calls `Observer.nudge()` when one of them moves; the timer
sweep stays underneath at a much slower cadence as the safety net.

EVIDENCE, NEVER TRUTH. The only thing this module can do to the rest of the
app is say "something moved, sweep sooner". It never publishes, never
classifies, never becomes a source of state. A dropped event costs latency and
nothing else, because the timer still runs — which is the entire reason the
timer is kept rather than deleted. A watcher that is trusted absolutely is a
watcher whose one bug is silent.

WHAT IS WATCHED, AND WHY IT IS NOT EVERYTHING (VERIFIED-FACTS.md). The binding
fd ceiling on macOS is `kern.maxfilesperproc` = 61,440 with `kern.maxfiles` =
122,880 SYSTEM-WIDE and 18,167 already in use at idle — not `RLIMIT_NOFILE`
(1,048,576). Watching all 18,773 `.jsonl` would take ~30 % of the per-process
cap and ~15 % of the global file table, and exhausting that table breaks OTHER
APPLICATIONS. So the watch set is bounded deliberately, in four layers:

  ROOT      each `--root` directory                       — a new worktree
  PROJECTS  each `<claude-home>/projects`                  — a new project dir
  PROJECT   the project dirs that MATCH a worktree         — a new transcript
  SESSION   the `<session-id>/` dir of an in-window session — a new subagent file
  FILE      the in-window top-level transcripts            — writes

Measured on this fleet that is ~60 fds; the documented worst case in
VERIFIED-FACTS is ~1,000. `watch_max_fds` caps it regardless (below).

Two kqueue behaviours shape that list, both re-verified here before a line was
written:

* A directory watch fires on child CREATE (`NOTE_WRITE`) and produces NOTHING
  when a file inside it is modified in place. Directory watches are for
  discovery, file watches for writes; they are not interchangeable. A create
  inside a NESTED subdirectory does not reach the grandparent either — checked,
  it emits no event at all — which is why SESSION is its own layer and why
  subagent files deeper than one level are the timer's job.
* Registering a changelist with `nevents=0` ABORTS THE BATCH at the first bad
  fd: the good watches after it are silently never armed. (Reproduced: one dead
  fd ahead of a live directory, and the directory then saw nothing.) With room
  in the eventlist the same batch continues and hands back an `EV_ERROR` event
  per failure. Every `control()` here therefore passes `nevents=len(changes)`.

Also verified rather than recalled: `select.KQ_FILTER_USER` is NOT exposed by
Python (3.14.5), so the thread is woken through a self-pipe on
`KQ_FILTER_READ`, not `EVFILT_USER`. `EVFILT_PROC`/`NOTE_EXIT` DOES arm on a
non-child same-uid pid and costs no fd at all (the ident is the pid), so agent
DEATH is an event — but there is no filter for process BIRTH, so discovery of
new processes remains a timer poll. Any "fully event-driven" claim that omits
that is wrong.

Linux has no stdlib inotify binding. `available()` is false there, the Observer
logs one line and runs the timer at `idle_blind_s` — today's 3.0 s cadence,
unchanged. Degradation is automatic and never a crash.

WHAT IT BOUGHT, measured (ADR 0011: a performance claim without a measurement
is not a claim). Real fleet, `getrusage(SELF)+(CHILDREN)`, 120 s per row:

  * idle CPU, nothing happening anywhere: 14 % of one core -> 6 %, from
    retiring `idle_s` 3.0 in favour of 30.0 plus these events. A/B interleaved
    three times, because this machine's load average wanders between 8 and 50.
  * a write to a watched transcript: median 53 ms to a nudge and 212 ms to a
    published version, over ten writes to a real transcript on the real fleet.
    Without events the same write waits out the cadence.

Both moved in the right direction at once, which is the unusual part: latency
and battery normally trade against each other, and every previous step in this
file's history was that trade.
"""

import errno
import os
import select
import sys
import threading
import time
from dataclasses import dataclass

from . import config, gitrepo, transcripts

# The cap, and what happens at it.
#
# Sized against the WORST case, not the typical one, so that hitting it means
# something unusual rather than something Tuesday. Measured on the live fleet
# the set is 228 fds (1 root + 8 `projects` roots + 164 matched project/session
# dirs + 55 in-window transcripts). The documented worst case is every one of
# the 702 top-level transcripts inside the window at once, with a session dir
# each, across all 295 project dirs: ~1,708. 2,048 clears that and is 3.3 % of
# `kern.maxfilesperproc` (61,440) and 1.7 % of the 122,880 system-wide table —
# the number that actually matters, since exhausting THAT breaks other
# applications, not just this board.
#
# `ENGINE.md` §10 said "hard-capped at 256 fds if it is ever built". That was a
# number invented for a design nobody was building: it would have started
# truncating on this machine on day one. ADR 0012.
#
# Over the cap the watch set is TRUNCATED in priority order (roots and project
# dirs first, then the newest transcripts) and the remainder is left to the
# timer sweep, which never stopped running — logged once, never repeatedly, and
# never by opening the fds first to find out. Truncating beats disabling: the
# 2,049th transcript should cost one file its latency, not cost the whole board
# its watcher.
WATCH_MAX_FDS = 2048        # config key "watch_max_fds"

# Debounce. An agent writing 50 lines must produce ONE nudge, not 50 sweeps.
DEBOUNCE_S = 0.05           # quiet period that ends a burst; key "watch_debounce_s"

# The rate limit, and it is the load-bearing number in this file.
#
# Events remove the sweep's floor but they also remove its CEILING, and that is
# the dangerous half. A transcript being appended to continuously would nudge
# every `hot_s` (0.15 s) — `hot_s` exists to stop a burst of MUTATIONS spinning
# the loop and is sized for a handful of them, not for an agent typing. At 0.15
# s and ~0.15 CPU-s per git-free sweep that is ~100 % of one core while any
# agent is working: worse than the timer it replaced. So event-driven nudges get
# their own floor. At 1.0 s the busy case costs ~0.15 CPU-s/s — the same ~15 %
# this loop already cost, but only while something is actually happening, and 0
# when it is not. The price is that a write landing just after a nudge waits out
# the rest of the second; the FIRST write after any quiet spell is still ~50 ms.
MIN_INTERVAL_S = 1.0        # min seconds between event nudges; key "watch_min_interval_s"

# The debounce must not be extendable forever by a writer that never pauses.
MAX_WINDOW_S = 2.0          # never defer a nudge longer than this; key "watch_max_window_s"

# The watch set is rebuilt on this clock as well as on every directory event, so
# a transcript that enters the 48 h window with no create event of its own (a
# resumed old session) still gets watched, and a pid that has appeared since the
# last sweep still gets its exit armed. The Observer also calls `rearm()` after
# each sweep, which is the path that actually keeps pids current.
REBUILD_S = 30.0            # config key "watch_rebuild_s"

# §4.5. `time.monotonic()` on this machine is `mach_absolute_time()` and
# INCLUDES sleep, so a lid-close shows up as a wall gap far larger than the
# timeout we asked for. Everything held is then suspect: files may have been
# rotated, worktrees removed, pids recycled. The response is a full rebuild and
# one nudge — cheap, and correct whether or not the fds actually survived.
WAKE_GAP_S = 30.0

_AUTO = object()            # "pick a kqueue if this platform has one"

_VNODE_FFLAGS = (select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND
                 | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME
                 | select.KQ_NOTE_REVOKE) if hasattr(select, "KQ_NOTE_WRITE") else 0
_GONE = ((select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME | select.KQ_NOTE_REVOKE)
         if hasattr(select, "KQ_NOTE_DELETE") else 0)


def available():
    """True where a watcher can be built at all. macOS: kqueue. Linux: no —
    the stdlib ships no inotify binding, and `ctypes` around it would be a
    second platform's worth of untested C ABI for a latency optimisation."""
    return hasattr(select, "kqueue") and hasattr(select, "KQ_FILTER_VNODE")


@dataclass(frozen=True)
class WatchSet:
    """What to watch, already truncated to the cap.

    `dirs` are watched for child CREATE, `files` for writes, `pids` for exit.
    `wanted` is how many paths there were BEFORE the cap, so `truncated` is a
    number somebody can act on rather than a boolean.
    """
    dirs: tuple = ()
    files: tuple = ()
    pids: tuple = ()
    wanted: int = 0

    @property
    def fds(self):
        return len(self.dirs) + len(self.files)

    @property
    def truncated(self):
        return max(0, self.wanted - self.fds)


def build_watch_set(now=None, cap=None, pids=()):
    """Enumerate the bounded watch set (see the module docstring's five layers).

    Cheap by construction: one `iterdir` per Claude home, one per matched
    project dir, and a `stat` per top-level transcript in a matched project.
    No transcript is opened, no subagent tree is walked and no memo is touched —
    this runs on the watcher thread and must never become a second collector.

    Priority order is also truncation order: the layers that discover NEW things
    come first, because losing one of those loses a card entirely, whereas
    losing a file watch loses one transcript a beat of latency. Within the file
    layer the newest transcripts win — the ones being written to now.
    """
    now = time.time() if now is None else now
    cap = WATCH_MAX_FDS if cap is None else cap
    window_s = config.CFG["session_window_h"] * 3600
    dirs, files = [], []

    for root in config.CFG["roots"]:
        p = os.path.expanduser(str(root))
        if os.path.isdir(p):
            dirs.append(p)

    worktrees = gitrepo.discover_worktrees()
    wt_prefixes = {w["path"]: gitrepo.munge(w["path"]) for w in worktrees}
    sessions = []                       # (mtime, transcript, session dir)
    for home in transcripts.claude_homes():
        proj_root = str(home / "projects")
        if os.path.isdir(proj_root):
            dirs.append(proj_root)
        try:
            entries = sorted(os.listdir(proj_root))
        except OSError:
            continue
        for name in entries:
            if gitrepo.match_worktree(name, wt_prefixes) is None:
                continue                # a project nobody's board renders — a
                                        # watch here would nudge for nothing
            proj = os.path.join(proj_root, name)
            if not os.path.isdir(proj):
                continue
            dirs.append(proj)
            try:
                it = list(os.scandir(proj))
            except OSError:
                continue
            for e in it:
                if not e.name.endswith(".jsonl"):
                    continue
                try:
                    st = e.stat()
                except OSError:
                    continue
                if now - st.st_mtime > window_s:
                    continue
                sub = e.path[: -len(".jsonl")]
                sessions.append((st.st_mtime, e.path,
                                 sub if os.path.isdir(sub) else None))

    sessions.sort(reverse=True)         # newest first: truncation keeps the live ones
    files = [t for _m, t, _s in sessions]
    session_dirs = [s for _m, _t, s in sessions if s]

    # Priority: roots/projects/project dirs, then transcripts, then session
    # dirs. Session dirs come last because a workflow that starts unseen is
    # found by the next timer sweep, while a project dir that is not watched
    # means a whole new session arrives late.
    ordered = [("dir", d) for d in dict.fromkeys(dirs)]
    ordered += [("file", f) for f in files]
    ordered += [("dir", d) for d in session_dirs]
    wanted = len(ordered)
    kept = ordered[:max(0, cap)]
    return WatchSet(tuple(p for k, p in kept if k == "dir"),
                    tuple(p for k, p in kept if k == "file"),
                    tuple(pids), wanted)


class Watcher:
    """One kqueue thread whose only output is `nudge(reason)`.

    Seams, so the tests can be deterministic without a wall-clock sleep
    deciding pass/fail: `kq_factory` supplies the object `control()` is called
    on, `targets` supplies the watch set, `clock` supplies the monotonic
    reading the debounce and the rate limit are computed from, and `wall`
    supplies the sleep/wake detector's clock. The fakes in the suite drive
    `_pump()` on the calling thread with a scripted event queue and assert on
    the nudges that come out.
    """

    def __init__(self, nudge, *, debounce_s=None, min_interval_s=None,
                 max_window_s=None, rebuild_s=None, max_fds=None,
                 pids=None, targets=None, kq_factory=_AUTO,
                 clock=time.monotonic, wall=time.time, log=None):
        self._nudge = nudge
        self.debounce_s = _knob("watch_debounce_s", DEBOUNCE_S, debounce_s)
        self.min_interval_s = _knob("watch_min_interval_s", MIN_INTERVAL_S, min_interval_s)
        self.max_window_s = _knob("watch_max_window_s", MAX_WINDOW_S, max_window_s)
        self.rebuild_s = _knob("watch_rebuild_s", REBUILD_S, rebuild_s)
        self.max_fds = int(_knob("watch_max_fds", WATCH_MAX_FDS, max_fds))
        self._pids = pids or (lambda: ())
        self._targets = targets or (
            lambda: build_watch_set(cap=self.max_fds, pids=self._pids()))
        # `_AUTO` and not `or`: an explicit `kq_factory=None` is how a test says
        # "this platform has no kqueue", and truthiness would quietly hand it
        # the real one — the Linux fallback proved by a test that never took it.
        self._kq_factory = ((select.kqueue if available() else None)
                            if kq_factory is _AUTO else kq_factory)
        self._clock, self._wall = clock, wall
        self._log = log or (lambda msg: print(msg, file=sys.stderr))

        self._kq = None
        self._fds = {}              # fd -> (kind, path)
        self._paths = {}            # path -> (fd, st_ino)
        self._armed_pids = set()
        self._pipe_r = self._pipe_w = None
        self._thread = None
        self._stop = threading.Event()
        self._rebuild_wanted = True
        self._logged = set()
        # -inf, not 0.0: the rate limit must not hold the FIRST nudge back by a
        # whole interval just because the clock happens to start near its own
        # origin. Only visible under a fake clock — which is exactly the kind of
        # bug a fake clock exists to find.
        self._last_nudge_at = float("-inf")

        self.events = 0             # kevents received
        self.nudges = 0             # nudges emitted (a burst is ONE)
        self.coalesced = 0          # events that a nudge swallowed
        self.rebuilds = 0
        self.errors = 0             # EV_ERROR + failed opens
        self.wakes = 0              # sleep/wake rebuilds
        self.truncated = 0          # paths the cap left to the timer

    # ------------------------------------------------------------- lifecycle

    @property
    def running(self):
        t = self._thread
        return bool(t is not None and t.is_alive())

    def start(self):
        """True if the watcher is now watching. False — logged once — if this
        platform cannot, which is not an error: the timer sweep is the whole
        mechanism again, exactly as it was before this module existed."""
        if self.running:
            return True
        if self._kq_factory is None:
            self._log_once("platform",
                           "orchestra: no kqueue on this platform — the board "
                           "falls back to the timer sweep (idle_blind_s)")
            return False
        try:
            self._kq = self._kq_factory()
            self._pipe_r, self._pipe_w = os.pipe()
            os.set_blocking(self._pipe_r, False)
            self._kq.control([select.kevent(self._pipe_r, select.KQ_FILTER_READ,
                                            select.KQ_EV_ADD)], 1, 0)
        except Exception as exc:                       # noqa: BLE001
            self._log_once("start", f"orchestra: watcher unavailable — {exc}")
            self._teardown()
            return False
        self._stop.clear()
        self._rebuild_wanted = True
        self._thread = threading.Thread(target=self._pump, name="observer-watch",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=5.0):
        self._stop.set()
        self._poke()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)
        self._thread = None
        self._teardown()

    def rearm(self, reason=""):
        """Rebuild the watch set at the next opportunity. Never blocks, never
        raises — the Observer calls it after every sweep, which is what keeps
        the pid exit watches in step with the snapshot."""
        self._rebuild_wanted = True
        self._poke()

    def _poke(self):
        try:
            if self._pipe_w is not None:
                os.write(self._pipe_w, b"x")
        except OSError:
            pass

    def _teardown(self):
        for fd in list(self._fds):
            self._close(fd)
        self._fds.clear()
        self._paths.clear()
        self._armed_pids.clear()
        for fd in (self._pipe_r, self._pipe_w):
            try:
                if fd is not None:
                    os.close(fd)
            except OSError:
                pass
        self._pipe_r = self._pipe_w = None
        if self._kq is not None:
            try:
                self._kq.close()
            except Exception:                          # noqa: BLE001
                pass
        self._kq = None

    def _close(self, fd):
        kp = self._fds.pop(fd, None)
        if kp is not None and self._paths.get(kp[1], (None,))[0] == fd:
            self._paths.pop(kp[1], None)
        try:
            os.close(fd)
        except OSError:
            pass

    def _log_once(self, key, msg):
        if key not in self._logged:
            self._logged.add(key)
            self._log(msg)

    # ------------------------------------------------------------- the watch

    def _rebuild(self):
        """Diff the live watch set against the wanted one.

        Diffed rather than torn down and rebuilt, and keyed on `(path, inode)`
        rather than on the path: a transcript that was ROTATED or replaced under
        its own name keeps its path, and an fd held on the old inode sees every
        subsequent write go nowhere. That is the same trap the transcript memo's
        `(dev, ino)` identity exists for, in the one place where getting it
        wrong is silent — the file simply stops producing events.
        """
        self.rebuilds += 1
        try:
            ws = self._targets()
        except Exception as exc:                       # noqa: BLE001
            self.errors += 1
            self._log_once("targets", f"orchestra: watch set failed — {exc}")
            return
        self.truncated = ws.truncated
        if ws.truncated:
            self._log_once(
                "cap", f"orchestra: watch set capped at {self.max_fds} fds — "
                       f"{ws.truncated} path(s) left to the timer sweep")
        want = {p: "dir" for p in ws.dirs}
        want.update({p: "file" for p in ws.files})

        for path in [p for p in self._paths if p not in want]:
            self._close(self._paths[path][0])
        changes = []
        for path in want:
            try:
                ino = os.stat(path).st_ino
            except OSError:
                cur = self._paths.get(path)
                if cur:
                    self._close(cur[0])
                continue
            cur = self._paths.get(path)
            if cur is not None:
                if cur[1] == ino:
                    continue                # same file, still armed
                self._close(cur[0])         # rotated under its own name
            try:
                fd = os.open(path, os.O_EVTONLY)
            except OSError:
                self.errors += 1
                continue
            self._fds[fd] = (want[path], path)
            self._paths[path] = (fd, ino)
            changes.append(select.kevent(fd, select.KQ_FILTER_VNODE,
                                         select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                                         _VNODE_FFLAGS))

        # EVFILT_PROC costs no fd — the ident is the pid — so agent DEATH is
        # free to watch. Arming a pid that already exited fails with ESRCH,
        # which is a normal race and not an error worth counting.
        want_pids = set(ws.pids)
        for pid in self._armed_pids - want_pids:
            self._armed_pids.discard(pid)
            # DISARM, not just forget. `EV_ONESHOT` means the kernel drops the
            # knote when it fires, so a pid left armed after it stopped being
            # ours still nudges the board once, on its own schedule, for a
            # process no card shows. `ENOENT` here is the ordinary case — it
            # already exited and the oneshot already went — and is not an error.
            changes.append(select.kevent(pid, select.KQ_FILTER_PROC,
                                         select.KQ_EV_DELETE, select.KQ_NOTE_EXIT))
        for pid in want_pids - self._armed_pids:
            self._armed_pids.add(pid)
            changes.append(select.kevent(pid, select.KQ_FILTER_PROC,
                                         select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                         select.KQ_NOTE_EXIT))
        self._apply(changes)

    def _apply(self, changes):
        """Register a changelist WITH ROOM FOR THE ERRORS.

        `nevents=0` would abort the batch at the first bad fd and silently leave
        every later watch unarmed — reproduced, and the reason this is its own
        function. With room, kqueue reports each failure as an `EV_ERROR` event
        and keeps going.
        """
        if not changes:
            return
        try:
            errs = self._kq.control(changes, len(changes), 0)
        except OSError as exc:
            self.errors += 1
            self._log_once("apply", f"orchestra: watch registration failed — {exc}")
            return
        for e in errs:
            if not (e.flags & select.KQ_EV_ERROR):
                continue
            if e.filter == select.KQ_FILTER_PROC:
                # ESRCH: the pid exited between the sweep that named it and this
                # register. ENOENT: the knote we tried to delete had already
                # fired (EV_ONESHOT) or was never there. Both are the ordinary
                # race between a process table and a snapshot of it, not errors.
                self._armed_pids.discard(e.ident)
                if e.data not in (errno.ESRCH, errno.ENOENT):
                    self.errors += 1
                continue
            self.errors += 1
            self._close(e.ident)

    # -------------------------------------------------------------- the pump

    def _pump(self):
        last = self._wall()
        while not self._stop.is_set():
            if self._rebuild_wanted:
                self._rebuild_wanted = False
                self._rebuild()
            try:
                evs = self._kq.control(None, 64, self.rebuild_s)
            except OSError as exc:
                if exc.errno == errno.EINTR:
                    continue
                self.errors += 1
                self._log_once("pump", f"orchestra: watch pump failed — {exc}")
                break
            except _Done:
                break
            now_wall = self._wall()
            if self._detect_wake(now_wall - last):
                last = now_wall
                continue
            last = now_wall
            if self._stop.is_set():
                break
            if not evs:
                # the rebuild clock came round with nothing to report
                self._rebuild_wanted = True
                continue
            reasons = self._react(evs)
            if reasons:
                self._settle(reasons)

    def _detect_wake(self, gap):
        """A `control()` that should have returned in `rebuild_s` returned nine
        hours later: the lid was shut. §4.5 — everything held is suspect, so the
        watch set is rebuilt from scratch and one nudge asks for a fresh sweep.
        Whether the fds themselves survived is not something this has to know,
        which is the point of rebuilding rather than trusting them."""
        if gap <= max(WAKE_GAP_S, 3 * self.rebuild_s):
            return False
        self.wakes += 1
        self._rebuild_wanted = True
        self._emit("wake")
        return True

    def _react(self, evs):
        """Turn kevents into nudge reasons. The ONLY interpretation in this
        module, and it is deliberately coarse: which file moved is the sweep's
        business, not the watcher's."""
        reasons = set()
        for e in evs:
            self.events += 1
            if e.filter == select.KQ_FILTER_READ and e.ident == self._pipe_r:
                try:
                    os.read(self._pipe_r, 4096)
                except OSError:
                    pass
                self.events -= 1        # a rearm is not evidence of anything
                continue
            if e.flags & select.KQ_EV_ERROR:
                self.errors += 1
                if e.filter == select.KQ_FILTER_VNODE:
                    self._close(e.ident)
                self._rebuild_wanted = True
                continue
            if e.filter == select.KQ_FILTER_PROC:
                self._armed_pids.discard(e.ident)
                reasons.add("exit")
                continue
            kp = self._fds.get(e.ident)
            if kp is None:
                continue                # closed under us; the fd is already gone
            if e.fflags & _GONE:
                # rotated, replaced or removed — the fd we hold is now pointing
                # at nothing anybody will write to again
                self._close(e.ident)
                self._rebuild_wanted = True
                reasons.add("gone")
                continue
            if kp[0] == "dir":
                # a directory says a child appeared, never which one and never
                # that one was appended to — so it means "re-enumerate", and the
                # new transcript is watched from the next rebuild on
                self._rebuild_wanted = True
                reasons.add("project")
            else:
                reasons.add("transcript")
        return reasons

    def _settle(self, reasons):
        """Coalesce a burst into ONE nudge, then hold the floor.

        Two clocks, and they do different jobs. `debounce_s` ends the burst: an
        agent writing 50 lines produces one quiet gap and one nudge, not 50
        sweeps. `min_interval_s` is the rate limit that keeps a CONTINUOUS
        writer from turning the loop into a spin — without it, events would
        remove the sweep's ceiling along with its floor. `max_window_s` stops
        the second one deferring a nudge forever.
        """
        t0 = self._clock()
        quiet = False
        while not self._stop.is_set():
            now = self._clock()
            if now - t0 >= self.max_window_s:
                break
            ready_at = self._last_nudge_at + self.min_interval_s
            if quiet and now >= ready_at:
                break
            timeout = self.debounce_s if not quiet else (ready_at - now)
            timeout = max(0.0, min(timeout, t0 + self.max_window_s - now))
            try:
                more = self._kq.control(None, 64, timeout)
            except _Done:
                break
            except OSError:
                break
            if more:
                seen = self.events
                reasons |= self._react(more)
                # off `events`, not `len(more)`: a `rearm` poke rides the same
                # queue and is not an event, so counting the batch would inflate
                # the one number that says how much work the debounce saved
                self.coalesced += self.events - seen
                quiet = False
            else:
                quiet = True
        self._emit("+".join(sorted(reasons)) or "watch")

    def _emit(self, reason):
        self._last_nudge_at = self._clock()
        self.nudges += 1
        try:
            self._nudge(f"watch:{reason}")
        except Exception:                              # noqa: BLE001
            pass                        # evidence, never a command

    # --------------------------------------------------------------- read API

    def stats(self):
        return {"watching": self.running, "watch_fds": len(self._fds),
                "watch_pids": len(self._armed_pids),
                "watch_events": self.events, "watch_nudges": self.nudges,
                "watch_coalesced": self.coalesced,
                "watch_rebuilds": self.rebuilds, "watch_errors": self.errors,
                "watch_wakes": self.wakes, "watch_truncated": self.truncated,
                "watch_max_fds": self.max_fds,
                "watch_min_interval_s": self.min_interval_s}


class _Done(Exception):
    """Raised by a test's fake kqueue when its script runs out, so `_pump` can
    be driven to completion on the calling thread with no thread and no sleep."""


def _knob(key, default, given):
    """Same rule as `observer._cadence`: explicit argument > config key >
    constant, and `given is None` rather than truthiness so a deliberate 0.0
    reaches the watcher as the value it is."""
    return float(config.CFG.get(key, default) if given is None else given)
