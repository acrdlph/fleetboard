#!/usr/bin/env python3
"""The watcher — ENGINE.md §10's deferred kqueue, built.

The load-bearing claim here is NOT "it sees writes". It is that the watcher
stays EVIDENCE: one nudge per burst, never faster than its own rate limit,
never a source of state, and never able to take the board down when a file it
holds is deleted, rotated or was never openable. The timer sweep is still
underneath — so every failure in this file should cost latency, and the tests
are written to say which.

DETERMINISM. Nothing here sleeps and then asserts. Two seams do the work:

  * `FakeClock` + `FakeKq` — a scripted event queue with the clock advanced by
    the script, so `_pump()` runs to completion on the calling thread and the
    debounce and the rate limit are asserted as arithmetic on nudge timestamps.
  * `FakeWatcher` — the Observer side, so "a watch nudge must not force git"
    is a fact about the call and not about a race.

Exactly ONE test uses a real kqueue on a real fixture, and it waits on an
Event rather than on a duration: it is there because a fully faked suite would
pass with the registration broken, which is the one bug that reproduces as
"the board just feels slow".

    python3 -m unittest discover -s tests
"""

import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402

HAVE_KQ = fb.watcher.available()
HAVE_GIT = shutil.which("git") is not None


# ------------------------------------------------------------------- the seams

class FakeClock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class FakeKq:
    """A scripted kqueue.

    `script` is a list of `(dt, events)`: the clock advances by `dt` and the
    batch is returned. An empty batch is a timeout. When the script runs out,
    `_Done` is raised — that is how `_pump()` terminates on the calling thread
    with no thread, no signal and no sleep.
    """

    def __init__(self, clock, script=(), errors=()):
        self.clock = clock
        self.script = list(script)
        self.errors = list(errors)      # kevents to hand back from a register
        self.registers = []             # (changelist, nevents) as `_apply` sent it
        self.closed = False

    def control(self, changelist, nevents, timeout=None):
        if changelist:
            self.registers.append((list(changelist), nevents))
            out, self.errors = self.errors, []
            return out
        if not self.script:
            raise fb.watcher._Done()
        dt, events = self.script.pop(0)
        self.clock.advance(dt)
        return list(events)

    def close(self):
        self.closed = True


class FakeWatcher:
    """The Observer's side of the seam."""

    def __init__(self, nudge, pids=None):
        self.nudge, self.pids = nudge, pids or (lambda: ())
        self.alive = False
        self.rearms = []

    @property
    def running(self):
        return self.alive

    def start(self):
        self.alive = True
        return True

    def stop(self, timeout=5.0):
        self.alive = False

    def rearm(self, reason=""):
        self.rearms.append((reason, tuple(self.pids())))

    def stats(self):
        return {"watching": self.alive}


def vnode(fd, fflags):
    return select.kevent(fd, select.KQ_FILTER_VNODE, select.KQ_EV_CLEAR, fflags)


class WatcherFixture:
    """A watcher wired to a FakeKq over real paths.

    Real paths because `_rebuild` does `os.open(..., O_EVTONLY)` and `os.stat`
    for the inode identity — faking those would fake exactly the part that has
    to be right. The kqueue is the only thing replaced.
    """

    def __init__(self, dirs=(), files=(), pids=(), **kw):
        self.clock = FakeClock()
        self.wall = FakeClock(1_000_000.0)
        self.nudges = []                # (clock, reason)
        self.logs = []
        self.set = fb.WatchSet(tuple(dirs), tuple(files), tuple(pids),
                               len(dirs) + len(files))
        self.kq = FakeKq(self.clock)
        kw.setdefault("debounce_s", 0.05)
        kw.setdefault("min_interval_s", 1.0)
        kw.setdefault("max_window_s", 2.0)
        kw.setdefault("rebuild_s", 30.0)
        self.w = fb.Watcher(lambda r: self.nudges.append((self.clock.t, r)),
                            targets=lambda: self.set, clock=self.clock,
                            wall=self.wall, log=self.logs.append,
                            kq_factory=lambda: self.kq, **kw)
        self.w._kq = self.kq

    def fd(self, path):
        return self.w._paths[str(path)][0]

    def rebuild(self):
        self.w._rebuild()
        self.w._rebuild_wanted = False
        return self

    def run(self, script):
        self.kq.script = list(script)
        self.w._pump()
        return self


# ----------------------------------------------------------------- the debounce

@unittest.skipUnless(HAVE_KQ, "kqueue only")
class TestOneNudgePerBurst(unittest.TestCase):
    """§ 'a burst must coalesce into ONE nudge, not 50 sweeps'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-w-"))
        self.f = self.tmp / "sess.jsonl"
        self.f.write_text("{}\n")
        self.fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()

    def tearDown(self):
        self.fx.w._teardown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fifty_writes_are_one_nudge(self):
        """Three batches, not two: with only one continuation a debounce that
        gave up after the FIRST quiet-less batch would produce the same single
        nudge and the test would pass on a broken drain."""
        fd = self.fx.fd(self.f)
        write = vnode(fd, select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND)
        self.fx.run([(0.0, [write] * 20), (0.0, [write] * 15),
                     (0.0, [write] * 15), (0.05, [])])
        self.assertEqual([r for _t, r in self.fx.nudges], ["watch:transcript"])
        self.assertEqual(self.fx.w.events, 50)
        self.assertEqual(self.fx.w.coalesced, 30)

    def test_the_rate_limit_is_the_floor_between_event_nudges(self):
        """Without this, events remove the sweep's CEILING as well as its floor:
        a transcript being appended to continuously would nudge every `hot_s`
        and cost more than the timer it replaced."""
        fd = self.fx.fd(self.f)
        w = vnode(fd, select.KQ_NOTE_WRITE)
        self.fx.run([(0.0, [w]), (0.05, []),          # burst 1 -> nudge
                     (0.0, [w]), (0.05, []), (0.95, [])])   # burst 2 -> held
        at = [t for t, _r in self.fx.nudges]
        self.assertEqual(len(at), 2)
        self.assertAlmostEqual(at[1] - at[0], self.fx.w.min_interval_s, places=6)

    def test_the_first_nudge_after_a_quiet_spell_is_not_rate_limited(self):
        """The trailing `(0.95, [])` is load-bearing: without a batch left for
        the rate limit to wait through, a `_last_nudge_at` initialised to 0.0
        instead of -inf produces the same timestamp by running out of script,
        and the test passes on a watcher that holds its first nudge for a
        whole interval."""
        fd = self.fx.fd(self.f)
        self.fx.run([(0.0, [vnode(fd, select.KQ_NOTE_WRITE)]), (0.05, []),
                     (0.95, [])])
        self.assertEqual([t for t, _r in self.fx.nudges], [0.05])

    def test_a_writer_that_never_pauses_still_nudges_inside_the_max_window(self):
        """`min_interval_s` defers a nudge; `max_window_s` stops it deferring
        one forever. A transcript that is never quiet for `debounce_s` would
        otherwise hold the board indefinitely."""
        fd = self.fx.fd(self.f)
        # `_settle` directly: a pump would just start the next burst on the
        # writer's next event, and what is under test is one settle's ceiling.
        self.fx.kq.script = [(0.03, [vnode(fd, select.KQ_NOTE_WRITE)])] * 500
        self.fx.w._settle({"transcript"})
        self.assertEqual(len(self.fx.nudges), 1)
        self.assertGreaterEqual(self.fx.nudges[0][0], self.fx.w.max_window_s)
        self.assertLess(self.fx.nudges[0][0], self.fx.w.max_window_s + 0.05)


# ------------------------------------------------------------ what an event means

@unittest.skipUnless(HAVE_KQ, "kqueue only")
class TestWhatAnEventMeans(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-w-"))
        self.f = self.tmp / "sess.jsonl"
        self.f.write_text("{}\n")
        self.fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()

    def tearDown(self):
        self.fx.w._teardown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_directory_event_means_re_enumerate_a_file_event_does_not(self):
        """Verified on this machine: a directory fires NOTE_WRITE when a child
        is CREATED and nothing at all when a file inside it is appended to. So
        the two are not interchangeable — a directory event is discovery and
        must schedule a rebuild, a file event is a write and must not.

        `_react` rather than `_pump`, because `_pump` services the rebuild it
        was asked for before it returns and the flag is gone by the time the
        test could look at it.
        """
        w = self.fx.w
        self.assertEqual(w._react([vnode(self.fx.fd(self.f),
                                         select.KQ_NOTE_WRITE)]), {"transcript"})
        self.assertFalse(w._rebuild_wanted)
        self.assertEqual(w._react([vnode(self.fx.fd(self.tmp),
                                         select.KQ_NOTE_WRITE)]), {"project"})
        self.assertTrue(w._rebuild_wanted)

    def test_a_deleted_file_is_dropped_and_the_set_re_established(self):
        """Watches must survive rotation, replacement and deletion. NOTE_DELETE
        means the fd we hold now points at something nobody will write to
        again — keeping it is a watch that can never fire."""
        w = self.fx.w
        fd = self.fx.fd(self.f)
        self.assertEqual(w._react([vnode(fd, select.KQ_NOTE_DELETE)]), {"gone"})
        self.assertNotIn(fd, w._fds)
        self.assertNotIn(str(self.f), w._paths)
        self.assertTrue(w._rebuild_wanted)
        w._rebuild()                               # …and it comes back
        self.assertIn(str(self.f), w._paths)

    def test_a_transcript_replaced_under_its_own_name_is_re_opened(self):
        """The watch set is keyed on `(path, inode)`, never on the path alone.
        An fd held on the old inode sees every later write go nowhere — and
        that failure is SILENT: the file simply stops producing events.

        Note what cannot be asserted: that the NUMBER changed. The kernel hands
        back the fd it just freed, so `old_fd == new_fd` here — an assertion on
        the integer would read as proof and be measuring recycling. The inode
        and a fresh registration are the evidence.
        """
        w = self.fx.w
        old_ino = w._paths[str(self.f)][1]
        regs = len(self.fx.kq.registers)
        replacement = self.tmp / "new.jsonl"
        replacement.write_text("{}\n{}\n")
        os.replace(replacement, self.f)            # same path, new inode
        w._rebuild()
        self.assertNotEqual(w._paths[str(self.f)][1], old_ino)
        self.assertEqual(len(self.fx.kq.registers), regs + 1)
        armed = [e.ident for changes, _n in self.fx.kq.registers[regs:]
                 for e in changes]
        self.assertEqual(armed, [w._paths[str(self.f)][0]])

    def test_a_path_that_left_the_watch_set_is_closed(self):
        self.assertIn(str(self.f), self.fx.w._paths)
        self.fx.set = fb.WatchSet((str(self.tmp),), (), (), 1)
        self.fx.w._rebuild()
        self.assertNotIn(str(self.f), self.fx.w._paths)
        self.assertEqual(len(self.fx.w._fds), 1)

    def test_a_vanished_path_costs_no_fd_and_no_exception(self):
        self.f.unlink()
        self.fx.w._rebuild()
        self.assertNotIn(str(self.f), self.fx.w._paths)
        self.assertTrue(self.fx.w._paths)          # the directory is still there


# ------------------------------------------------------------------ not dying

@unittest.skipUnless(HAVE_KQ, "kqueue only")
class TestTheWatcherDoesNotDie(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-w-"))
        self.f = self.tmp / "sess.jsonl"
        self.f.write_text("{}\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registration_leaves_room_for_the_errors_it_may_get_back(self):
        """THE TRAP, reproduced before this module was written: `control()` with
        `nevents=0` ABORTS the changelist at the first bad fd and silently never
        arms anything after it. With room in the eventlist the same batch keeps
        going and reports each failure as an EV_ERROR event. So every register
        here must pass `nevents == len(changes)`, and a later refactor that
        "tidies" it to 0 must go red."""
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        self.assertTrue(fx.kq.registers)
        for changes, nevents in fx.kq.registers:
            self.assertEqual(nevents, len(changes))
            self.assertGreater(nevents, 0)
        fx.w._teardown()

    def test_an_ev_error_drops_that_watch_and_asks_for_a_rebuild(self):
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        fd = fx.fd(self.f)
        err = select.kevent(fd, select.KQ_FILTER_VNODE,
                            select.KQ_EV_ERROR, 0, 9)     # EBADF
        self.assertEqual(fx.w._react([err]), set())        # not evidence
        self.assertEqual(fx.w.errors, 1)
        self.assertNotIn(fd, fx.w._fds)
        self.assertTrue(fx.w._rebuild_wanted)
        fx.w._teardown()

    def test_an_ev_error_alone_does_not_nudge_and_does_not_stop_the_pump(self):
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        err = select.kevent(fx.fd(self.f), select.KQ_FILTER_VNODE,
                            select.KQ_EV_ERROR, 0, 9)
        fx.run([(0.0, [err]), (0.0, [])])
        self.assertEqual(fx.nudges, [])
        self.assertGreaterEqual(fx.w.rebuilds, 2)          # it re-established
        fx.w._teardown()

    def test_a_registration_error_at_arm_time_drops_only_that_watch(self):
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),))
        fx.kq.errors = [select.kevent(999_999, select.KQ_FILTER_VNODE,
                                      select.KQ_EV_ERROR, 0, 9)]
        fx.rebuild()
        self.assertEqual(fx.w.errors, 1)
        self.assertEqual(len(fx.w._fds), 2)        # both real watches survived
        fx.w._teardown()

    def test_a_pid_that_leaves_the_fleet_is_disarmed_not_just_forgotten(self):
        """`EV_ONESHOT` means the kernel keeps the knote until it fires, so a
        pid merely dropped from the set still nudges the board once — later, on
        its own schedule, for a process no card shows."""
        fx = WatcherFixture(dirs=(str(self.tmp),), pids=(4242,)).rebuild()
        self.assertEqual(fx.w._armed_pids, {4242})
        fx.set = fb.WatchSet((str(self.tmp),), (), (), 1)
        regs = len(fx.kq.registers)
        fx.w._rebuild()
        self.assertEqual(fx.w._armed_pids, set())
        deletes = [e for changes, _n in fx.kq.registers[regs:] for e in changes
                   if e.filter == select.KQ_FILTER_PROC
                   and e.flags & select.KQ_EV_DELETE]
        self.assertEqual([e.ident for e in deletes], [4242])
        fx.w._teardown()

    def test_a_pid_that_already_exited_is_a_race_not_an_error(self):
        fx = WatcherFixture(dirs=(str(self.tmp),), pids=(4242,))
        fx.kq.errors = [select.kevent(4242, select.KQ_FILTER_PROC,
                                      select.KQ_EV_ERROR, 0, 3)]   # ESRCH
        fx.rebuild()
        self.assertEqual(fx.w.errors, 0)
        self.assertEqual(fx.w._armed_pids, set())
        fx.w._teardown()

    def test_a_watch_set_that_raises_does_not_kill_the_watcher(self):
        fx = WatcherFixture(dirs=(str(self.tmp),))

        def boom():
            raise RuntimeError("scandir wedged")
        fx.w._targets = boom
        fx.w._rebuild()
        self.assertEqual(fx.w.errors, 1)
        self.assertTrue(any("scandir wedged" in m for m in fx.logs))
        fx.w._rebuild()
        self.assertEqual(len(fx.logs), 1)          # logged ONCE, not per rebuild
        fx.w._teardown()

    def test_a_nudge_that_raises_is_swallowed(self):
        """Evidence, never a command: the watcher may not take the board down
        because the thing it notified blew up."""
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        fx.w._nudge = lambda reason: (_ for _ in ()).throw(RuntimeError("nope"))
        fx.run([(0.0, [vnode(fx.fd(self.f), select.KQ_NOTE_WRITE)]), (0.05, [])])
        self.assertEqual(fx.w.nudges, 1)
        fx.w._teardown()

    def test_a_rearm_poke_is_not_evidence_of_anything(self):
        """`rearm()` wakes the pump through a self-pipe (`KQ_FILTER_USER` is not
        exposed by Python, verified). That wake must re-enumerate and NOT nudge —
        the Observer calls `rearm` after every sweep, so a poke that counted as
        evidence would make the loop nudge itself once per sweep, forever."""
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        fx.w._pipe_r, fx.w._pipe_w = os.pipe()
        os.write(fx.w._pipe_w, b"x")
        poke = select.kevent(fx.w._pipe_r, select.KQ_FILTER_READ, select.KQ_EV_ADD)
        self.assertEqual(fx.w._react([poke]), set())
        self.assertEqual(fx.w.events, 0)
        self.assertEqual(fx.nudges, [])
        fx.w._teardown()

    def test_an_event_on_an_fd_we_already_closed_is_ignored(self):
        fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()
        fd = fx.fd(self.f)
        fx.w._close(fd)
        fx.run([(0.0, [vnode(fd, select.KQ_NOTE_WRITE)]), (0.05, [])])
        self.assertEqual(fx.nudges, [])
        fx.w._teardown()


# --------------------------------------------------------------- sleep and wake

@unittest.skipUnless(HAVE_KQ, "kqueue only")
class TestSleepWake(unittest.TestCase):
    """ENGINE.md §4.5. `time.monotonic()` here is `mach_absolute_time()` and
    INCLUDES sleep, so a closed lid shows up as a `control()` that should have
    returned in `rebuild_s` and returned nine hours later.

    What is actually tested: that the gap is detected, that the watch set is
    rebuilt rather than trusted, and that exactly one nudge comes out of it.
    What is NOT tested here — stated plainly rather than implied — is whether a
    real macOS kqueue knote survives a real S3 sleep. Rebuilding makes that
    question moot, which is why the response is a rebuild and not a check.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-w-"))
        self.f = self.tmp / "sess.jsonl"
        self.f.write_text("{}\n")
        self.fx = WatcherFixture(dirs=(str(self.tmp),), files=(str(self.f),)).rebuild()

    def tearDown(self):
        self.fx.w._teardown()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_nine_hour_gap_rebuilds_the_watch_set_and_nudges_once(self):
        w = self.fx.w
        before = w.rebuilds
        self.fx.wall.advance(9 * 3600)             # the lid was shut
        self.assertTrue(w._detect_wake(9 * 3600))
        self.assertEqual(w.wakes, 1)
        self.assertTrue(w._rebuild_wanted)
        self.assertEqual([r for _t, r in self.fx.nudges], ["watch:wake"])
        w._rebuild()
        self.assertEqual(w.rebuilds, before + 1)

    def test_an_ordinary_timeout_is_not_a_wake(self):
        """`rebuild_s` expiring is the normal case and must not be read as a
        wake — every timeout would otherwise nudge, and the idle CPU this whole
        step exists to remove would come straight back."""
        self.assertFalse(self.fx.w._detect_wake(self.fx.w.rebuild_s + 1))
        self.assertEqual(self.fx.w.wakes, 0)
        self.assertEqual(self.fx.nudges, [])

    def test_a_watcher_stopped_and_restarted_re_establishes_every_watch(self):
        """The other half of §4.5, and the half that can be tested for real:
        whatever the fds were, after a stop/start the watch set is built from
        the world as it is now."""
        w = self.fx.w
        w._teardown()
        self.assertEqual(w._fds, {})
        w._kq = self.fx.kq
        w._rebuild()
        self.assertEqual(sorted(p for _k, p in w._fds.values()),
                         sorted([str(self.tmp), str(self.f)]))


# ---------------------------------------------------------------- the watch set

class TestTheWatchSet(unittest.TestCase):
    """VERIFIED-FACTS: the binding fd ceiling is `kern.maxfilesperproc`
    (61,440) with `kern.maxfiles` 122,880 SYSTEM-WIDE, not `RLIMIT_NOFILE`.
    Watching all 18,773 `.jsonl` would take 15 % of the global file table and
    break other applications. So the set is bounded by construction, and these
    tests are about what is deliberately NOT in it.
    """

    KEYS = ("roots", "homes", "pattern", "session_window_h")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-ws-"))
        self.root = self.tmp / "code"
        (self.root / "alpha").mkdir(parents=True)
        (self.root / "alpha" / ".git").mkdir()
        self.home = self.tmp / "home"
        (self.home / "projects").mkdir(parents=True)
        self.proj = self.home / "projects" / fb.munge(str(self.root / "alpha"))
        self.proj.mkdir()
        self.tr = self.proj / "sess-a.jsonl"
        self.tr.write_text("{}\n")
        self.saved = {k: fb.CFG.get(k) for k in self.KEYS}
        fb.CFG.update({"roots": [str(self.root)], "homes": [str(self.home)],
                       "pattern": "", "session_window_h": 48})

    def tearDown(self):
        fb.CFG.update(self.saved)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_five_layers_and_nothing_else(self):
        ws = fb.build_watch_set()
        self.assertEqual(set(ws.dirs), {str(self.root), str(self.home / "projects"),
                                        str(self.proj)})
        self.assertEqual(set(ws.files), {str(self.tr)})

    def test_a_session_directory_is_watched_but_its_files_are_not(self):
        """NEVER the subagent files — 18,773 of them, growing ~982/day. The
        session DIRECTORY is one fd and catches a workflow starting; the files
        under it are enumerated on demand by the sweep. A create in a NESTED
        directory reaches neither, which is measured (the grandparent sees
        nothing) and is the timer's job."""
        sub = self.proj / "sess-a"
        (sub / "deep").mkdir(parents=True)
        (sub / "agent.jsonl").write_text("{}\n")
        (sub / "deep" / "deeper.jsonl").write_text("{}\n")
        ws = fb.build_watch_set()
        self.assertIn(str(sub), ws.dirs)
        self.assertNotIn(str(sub / "deep"), ws.dirs)
        self.assertEqual(set(ws.files), {str(self.tr)})

    def test_a_project_no_worktree_matches_is_not_watched(self):
        """A watch there would nudge the board every time the user's agent in
        some unrelated repo wrote a line — idle CPU spent on a card that does
        not exist."""
        other = self.home / "projects" / fb.munge("/somewhere/else")
        other.mkdir()
        (other / "sess-x.jsonl").write_text("{}\n")
        ws = fb.build_watch_set()
        self.assertNotIn(str(other), ws.dirs)
        self.assertNotIn(str(other / "sess-x.jsonl"), ws.files)

    def test_a_transcript_outside_the_window_is_left_to_the_timer(self):
        old = self.proj / "sess-old.jsonl"
        old.write_text("{}\n")
        stale = time.time() - 49 * 3600
        os.utime(old, (stale, stale))
        ws = fb.build_watch_set()
        self.assertNotIn(str(old), ws.files)
        self.assertIn(str(self.tr), ws.files)

    def test_the_cap_truncates_discovery_last_and_reports_the_remainder(self):
        """Over the cap the excess degrades to the timer sweep, which never
        stopped running — truncating beats disabling, because the 1,025th
        transcript should cost one file its latency and not cost the board its
        watcher. Discovery layers survive: losing a project dir loses a whole
        card, losing a file watch loses one transcript a beat."""
        ws = fb.build_watch_set(cap=2)
        self.assertEqual(ws.fds, 2)
        self.assertEqual(ws.truncated, 2)
        self.assertEqual(ws.files, ())
        self.assertEqual(ws.dirs, (str(self.root), str(self.home / "projects")))

    def test_the_newest_transcripts_are_the_ones_that_survive_the_cap(self):
        cold = self.proj / "sess-cold.jsonl"
        cold.write_text("{}\n")
        old = time.time() - 3600
        os.utime(cold, (old, old))
        ws = fb.build_watch_set(cap=4)
        self.assertEqual(ws.files, (str(self.tr),))
        self.assertNotIn(str(cold), ws.files)

    @unittest.skipUnless(HAVE_KQ, "kqueue only")
    def test_the_cap_is_logged_once_never_per_rebuild(self):
        fx = WatcherFixture(max_fds=2)
        fx.set = fb.build_watch_set(cap=2)
        fx.rebuild()
        fx.w._rebuild()
        fx.w._rebuild()
        self.assertEqual(len([m for m in fx.logs if "capped" in m]), 1)
        self.assertEqual(fx.w.truncated, 2)
        self.assertEqual(fx.w.stats()["watch_truncated"], 2)
        fx.w._teardown()

    @unittest.skipUnless(HAVE_KQ, "kqueue only")
    def test_the_fd_cost_is_reported_from_what_was_actually_opened(self):
        """"Report and bound the fd cost" — and report the OPENED count, not
        the wanted one: a path that vanished between enumeration and `os.open`
        costs nothing and must not be billed."""
        gone = self.proj / "sess-gone.jsonl"
        gone.write_text("{}\n")
        fx = WatcherFixture(dirs=(str(self.proj),),
                            files=(str(self.tr), str(gone)))
        gone.unlink()                              # …between enumerate and open
        fx.rebuild()
        self.assertEqual(fx.w.stats()["watch_fds"], 2)
        self.assertEqual(len(fx.w._fds), 2)
        self.assertLessEqual(fx.w.stats()["watch_fds"], fx.w.stats()["watch_max_fds"])
        fx.w._teardown()


# ------------------------------------------------------------ platform fallback

class TestPlatformFallback(unittest.TestCase):
    """Linux has no stdlib inotify binding. Degradation must be automatic,
    logged once, and never a crash."""

    def test_no_kqueue_means_no_watcher_and_one_logged_line(self):
        logs = []
        w = fb.Watcher(lambda r: None, kq_factory=None, log=logs.append)
        self.assertFalse(w.start())
        self.assertFalse(w.running)
        self.assertFalse(w.start())
        self.assertEqual(len(logs), 1)
        self.assertIn("timer sweep", logs[0])

    def test_a_kqueue_that_fails_to_open_degrades_rather_than_raising(self):
        logs = []

        def boom():
            raise OSError("no descriptors")
        w = fb.Watcher(lambda r: None, kq_factory=boom, log=logs.append)
        self.assertFalse(w.start())
        self.assertEqual(len(logs), 1)
        self.assertIn("unavailable", logs[0])

    @unittest.skipUnless(sys.platform == "darwin", "macOS is the primary platform")
    def test_the_platform_facts_this_module_was_built_on_still_hold(self):
        """VERIFIED-FACTS, pinned rather than recalled. Every one of these was
        measured before a line was written, and three of them are things the
        design docs got wrong at some point: `O_EVTONLY` IS exposed (do not
        hand-roll 0x8000), `EVFILT_PROC` IS there (process death is observable),
        and `KQ_FILTER_USER` is NOT (hence the self-pipe, not EVFILT_USER)."""
        self.assertTrue(fb.watcher.available())
        self.assertEqual(getattr(os, "O_EVTONLY", None), 32768)
        for name in ("KQ_FILTER_VNODE", "KQ_FILTER_PROC", "KQ_FILTER_READ",
                     "KQ_NOTE_WRITE", "KQ_NOTE_EXTEND", "KQ_NOTE_DELETE",
                     "KQ_NOTE_RENAME", "KQ_NOTE_REVOKE", "KQ_NOTE_EXIT"):
            self.assertTrue(hasattr(select, name), name)
        self.assertFalse(hasattr(select, "KQ_FILTER_USER"))


# ------------------------------------------------------------ observer wiring

class TestTheObserverSide(unittest.TestCase):

    def setUp(self):
        self._cache = dict(fb._cache)
        self._watch = fb.CFG.get("watch")

    def tearDown(self):
        fb.CFG["watch"] = self._watch
        fb._cache.update(self._cache)

    def _observer(self, **kw):
        kw.setdefault("watch", True)
        kw.setdefault("watcher_factory", FakeWatcher)
        return fb.Observer(**kw)

    def test_a_watch_nudge_does_not_force_a_git_fan_out(self):
        """THE expensive mistake available here. A transcript write is not a
        mutation, it is an agent working, and it arrives as often as an agent
        types. Forcing git on each one runs the fan-out at the event rate —
        by the measured table in observer.py the single most expensive thing
        this loop can do, and the exact cost `git_s` exists to bound."""
        o = self._observer()
        o._git._forced = False
        o.nudge("watch:transcript", git=False)
        self.assertFalse(o._git._forced)
        self.assertEqual(o.stats()["nudges"], 1)   # …but the sweep still comes forward
        o.nudge("finish/exit")                     # a real mutation still does
        self.assertTrue(o._git._forced)

    def test_the_watcher_is_wired_to_the_no_git_nudge(self):
        o = self._observer()
        o._git._forced = False
        o._watcher.nudge("watch:transcript")
        self.assertFalse(o._git._forced)
        self.assertEqual(o.stats()["nudges"], 1)

    def test_watch_off_builds_no_watcher_at_all(self):
        """The rollback, and the Linux path: an Observer that never opens a
        file descriptor and behaves exactly as it did before this module."""
        fb.CFG["watch"] = False
        o = fb.Observer()
        self.assertIsNone(o._watcher)
        self.assertFalse(o.watching)
        self.assertFalse(o.stats()["watching"])

    def test_the_blind_cadence_is_a_ceiling_not_a_substitute(self):
        """`Observer(idle_s=0.01)` must mean 0.01 — an explicit argument losing
        to a default is the one rule every cadence in observer.py keeps."""
        o = self._observer(idle_s=30.0, idle_blind_s=3.0)
        self.assertEqual(o.effective_idle_s, 3.0)      # not started: blind
        o._watcher.start()
        self.assertEqual(o.effective_idle_s, 30.0)
        fast = self._observer(idle_s=0.01, idle_blind_s=3.0)
        self.assertEqual(fast.effective_idle_s, 0.01)

    def test_a_watcher_that_dies_drops_the_loop_back_to_the_blind_cadence(self):
        """Read from `running` on every wait, never latched at startup: a
        watcher that dies at hour nine must cost three seconds of latency
        rather than thirty, with nobody watching."""
        o = self._observer(idle_s=30.0, idle_blind_s=3.0)
        o._watcher.start()
        self.assertEqual(o.effective_idle_s, 30.0)
        o._watcher.alive = False                   # the thread died
        self.assertEqual(o.effective_idle_s, 3.0)
        self.assertEqual(o.stats()["idle_effective_s"], 3.0)

    def test_max_stale_s_below_idle_s_would_silently_become_the_cadence(self):
        """The trap raising `idle_s` walked into: the loop waits
        `min(idle_s, max_stale_s)`, so an 8 s ceiling under a 30 s cadence
        cancels the whole idle-CPU win three screens away, with every sweep
        counter still looking plausible. `stats()` publishes both."""
        o = self._observer(idle_s=30.0, max_stale_s=8.0)
        o._watcher.start()
        self.assertEqual(o.effective_idle_s, 30.0)
        self.assertEqual(o.stats()["idle_effective_s"], 8.0)
        self.assertGreaterEqual(fb.CFG["max_stale_s"], fb.CFG["idle_s"])

    def test_the_sweep_rearms_the_watcher_with_the_fleet_it_just_found(self):
        """`EVFILT_PROC`/`NOTE_EXIT` costs no fd — the ident is the pid — so
        agent DEATH is an event. There is no filter for process BIRTH, which is
        why the pid list comes from the snapshot and not from the watcher."""
        o = self._observer()
        state = {"generated_at": 1000.0, "counts": {}, "worktrees": [
            {"name": "a", "sessions": [],
             "live_procs": [{"pid": 11, "cpu": 0.0, "etime": "1"},
                            {"pid": 12, "cpu": 0.0, "etime": "1"}]}],
            "other_procs": [{"pid": 13, "cpu": 0.0, "etime": "1"}]}
        collect = fb.observer.collect_state
        try:
            fb.observer.collect_state = lambda fresh=None, git=None, cold=False: state
            o.sweep()
        finally:
            fb.observer.collect_state = collect
        self.assertEqual(o._watcher.rearms, [("sweep", (11, 12, 13))])

    def test_the_pids_come_from_the_published_snapshot_not_from_thin_air(self):
        o = self._observer()
        self.assertEqual(o._live_pids(), ())       # before the first sweep
        o.publish({"generated_at": 1.0, "counts": {}, "other_procs": [],
                   "worktrees": [{"name": "a", "sessions": [],
                                  "live_procs": [{"pid": 5, "cpu": 0.0,
                                                  "etime": "1"}]}]})
        self.assertEqual(o._live_pids(), (5,))

    def test_stopping_the_observer_stops_the_watcher(self):
        o = self._observer()
        o._watcher.start()
        o.stop()
        self.assertFalse(o._watcher.running)


# ------------------------------------------------------------------ end to end

@unittest.skipUnless(HAVE_KQ and HAVE_GIT, "kqueue and git required")
class TestARealWriteReachesTheBoard(unittest.TestCase):
    """The one test with a real kqueue on a real fixture.

    A fully faked suite passes with the registration broken, and that bug
    reproduces as "the board just feels slow" — the least debuggable symptom
    this project has. So: two real git worktrees, a real Claude home, a real
    `select.kqueue`, and an append to a transcript.

    It waits on the version rather than on a duration — the assertion is "a
    version arrived", never "0.3 s was enough".
    """

    KEYS = ("roots", "homes", "pattern", "exclude_accounts", "watch")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-e2e-"))
        root = self.tmp / "code"
        d = root / "alpha"
        d.mkdir(parents=True)
        for a in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t.t"),
                  ("config", "user.name", "t")):
            subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
        (d / "f").write_text("1\n")
        subprocess.run(["git", "-C", str(d), "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", "seed"],
                       check=True, capture_output=True)
        home = self.tmp / "home"
        self.proj = home / "projects" / fb.munge(str(d))
        self.proj.mkdir(parents=True)
        self.tr = self.proj / "sess-a.jsonl"
        self.tr.write_text(json.dumps(
            {"type": "user", "cwd": str(d), "message": {"content": "build it"}}) + "\n")
        self.saved = {k: fb.CFG.get(k) for k in self.KEYS}
        self.demo, self.procs, self.cl = (fb.config.DEMO,
                                          fb.procs.claude_processes,
                                          fb.limits.cached_limits)
        fb.config.DEMO = False
        fb.CFG.update({"roots": [str(root)], "homes": [str(home)], "pattern": "",
                       "exclude_accounts": [], "watch": True})
        fb.procs.claude_processes = lambda **_: []
        fb.limits.cached_limits = lambda refresh=False: {"available": False}
        self._cache = dict(fb._cache)
        fb._cache["state"], fb._cache["t"] = None, 0.0

    def tearDown(self):
        fb.CFG.update(self.saved)
        (fb.config.DEMO, fb.procs.claude_processes,
         fb.limits.cached_limits) = self.demo, self.procs, self.cl
        fb._cache.update(self._cache)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_append_bumps_the_version_without_waiting_out_the_cadence(self):
        o = fb.Observer(idle_s=600.0, max_stale_s=600.0, hot_s=0.0, git_s=600.0)
        o._watcher.min_interval_s = 0.0
        o.start()
        try:
            first = o.wait_for(0, timeout=20)
            self.assertIsNotNone(first, "no first sweep")
            # the watch set must actually contain the transcript, or what
            # follows would be proving the timer works
            deadline = time.time() + 10
            while str(self.tr) not in o._watcher._paths and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn(str(self.tr), o._watcher._paths)
            with open(self.tr, "a") as f:
                f.write(json.dumps({"type": "assistant", "message": {
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": "done"}]}}) + "\n")
                f.flush()
            later = o.wait_for(first.v, timeout=20)
            self.assertIsNotNone(later, "the write never reached a version")
            self.assertGreater(later.v, first.v)
            self.assertGreaterEqual(o._watcher.nudges, 1)
        finally:
            o.stop()

    def test_a_new_transcript_appearing_is_discovered_and_then_watched(self):
        """A directory watch fires on CREATE and says nothing about writes, so
        the create must schedule a rebuild — otherwise the new session's every
        later line waits out `idle_s`."""
        o = fb.Observer(idle_s=600.0, max_stale_s=600.0, hot_s=0.0, git_s=600.0)
        o._watcher.min_interval_s = 0.0
        o.start()
        try:
            self.assertIsNotNone(o.wait_for(0, timeout=20))
            new = self.proj / "sess-b.jsonl"
            new.write_text(json.dumps(
                {"type": "user", "message": {"content": "second"}}) + "\n")
            deadline = time.time() + 20
            while str(new) not in o._watcher._paths and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn(str(new), o._watcher._paths)
        finally:
            o.stop()

    def test_the_watcher_closes_every_descriptor_it_opened(self):
        o = fb.Observer(idle_s=600.0, max_stale_s=600.0, git_s=600.0)
        o.start()
        try:
            self.assertIsNotNone(o.wait_for(0, timeout=20))
            self.assertGreater(len(o._watcher._fds), 0)
            fds = list(o._watcher._fds)
        finally:
            o.stop()
        self.assertEqual(o._watcher._fds, {})
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)


if __name__ == "__main__":
    unittest.main()
