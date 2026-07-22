#!/usr/bin/env python3
"""The publish point — ENGINE.md §2.5 / §3.1 / §3.2 / §3.3 / §3.5.

The load-bearing claim of this component is NOT "it sweeps". It is that `v`
bumps only when something a client cares about changed. Everything downstream
— the delta stream, and the push notifier that is the whole reason the sweep
exists — is dishonest the moment that stops being true, so most of this file
is about proving `v` stays still.

Real fixtures, not demo data: two temp git repos and a temp Claude home, with
`ps` and `cclimits` stubbed to empty so the git + transcript path runs for
real and nothing depends on the developer's live fleet.

    python3 -m unittest discover -s tests
"""

import json
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

HAVE_GIT = shutil.which("git") is not None


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


# ------------------------------------------------------ synthetic collect_state

def fake_state(at, *, cards=(("alpha", 0),), cpu=1.0, etime="01:00", age_s=5,
               other_cpu=0.5, counts=None):
    """A collect_state() result, hand-built. `publish` reads exactly four keys."""
    return {
        "generated_at": at,
        "counts": counts or {"working": len(cards)},
        "worktrees": [
            {"name": name, "availability": "busy",
             "git": {"branch": "main", "dirty": dirty},
             "sessions": [{"sid": f"s-{name}", "status": "working",
                           "last_write_at": 1000.0, "age_s": age_s}],
             "live_procs": [{"pid": 7, "cpu": cpu, "etime": etime,
                             "tty": "ttys001", "reachable": True}]}
            for name, dirty in cards],
        "other_procs": [{"pid": 9, "cpu": other_cpu, "etime": etime,
                         "cwd": "/elsewhere"}],
    }


class CacheGuard(unittest.TestCase):
    """The Observer writes through `_cache`; never leak that into another test."""

    def setUp(self):
        self._cache = dict(fb._cache)
        self._glob = fb.observer._observer

    def tearDown(self):
        fb.observer._observer = self._glob
        fb._cache.update(self._cache)


# --------------------------------------------------------------- versioning

class TestVersioning(CacheGuard):

    def test_first_publish_is_version_one(self):
        o = fb.Observer()
        self.assertIsNone(o.snapshot())
        snap = o.publish(fake_state(1000.0))
        self.assertEqual(snap.v, 1)
        self.assertEqual(snap.at, 1000.0)
        self.assertEqual(list(snap.cards), ["alpha"])

    def test_v_does_not_move_when_nothing_changed(self):
        """The subtle one. An identical view must publish no new version."""
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        snap = o.publish(fake_state(1001.0))
        self.assertEqual(snap.v, 1)                 # NOT 2
        self.assertEqual(snap.at, 1001.0)           # …but it is still true NOW
        self.assertEqual(o.stats()["publishes"], 2)

    def test_v_bumps_once_per_real_change_and_never_backwards(self):
        o = fb.Observer()
        seen = []
        for i, dirty in enumerate([0, 0, 1, 1, 2, 2, 2]):
            seen.append(o.publish(fake_state(1000.0 + i, cards=(("alpha", dirty),))).v)
        self.assertEqual(seen, [1, 1, 2, 2, 3, 3, 3])
        self.assertEqual(seen, sorted(seen))

    def test_ticking_stopwatches_do_not_bump_the_version(self):
        """age_s / cpu / etime move on their own. They still ship — they just
        do not get a vote, or v would tick once a second forever."""
        o = fb.Observer()
        o.publish(fake_state(1000.0, cpu=1.0, etime="01:00", age_s=5))
        snap = o.publish(fake_state(1001.0, cpu=98.6, etime="01:01", age_s=6,
                                    other_cpu=77.7))
        self.assertEqual(snap.v, 1)
        # …and the reading a client renders is the NEW one, not the stale one
        self.assertEqual(snap.cards["alpha"]["live_procs"][0]["cpu"], 98.6)
        self.assertEqual(snap.cards["alpha"]["sessions"][0]["age_s"], 6)
        self.assertEqual(snap.other_procs[0]["cpu"], 77.7)

    def test_a_status_change_still_bumps_even_though_age_drove_it(self):
        """age_s is out of the diff; what a threshold crossing MEANS is not."""
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        st = fake_state(1001.0, age_s=400)
        st["worktrees"][0]["sessions"][0]["status"] = "ended"
        self.assertEqual(o.publish(st).v, 2)

    def test_a_new_card_and_a_removed_card_both_bump(self):
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        self.assertEqual(o.publish(fake_state(1001.0,
                         cards=(("alpha", 0), ("beta", 0)))).v, 2)
        self.assertEqual(o.publish(fake_state(1002.0, cards=(("beta", 0),))).v, 3)
        self.assertNotIn("alpha", o.snapshot().cards)

    def test_counts_alone_can_bump(self):
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        self.assertEqual(o.publish(fake_state(1001.0, counts={"working": 9})).v, 2)

    def test_a_late_collect_never_regresses_the_snapshot(self):
        """Two threads can publish; the older one must lose, silently."""
        o = fb.Observer()
        o.publish(fake_state(2000.0, cards=(("alpha", 5),)))
        snap = o.publish(fake_state(1000.0, cards=(("alpha", 0),)))
        self.assertEqual(snap.at, 2000.0)
        self.assertEqual(snap.cards["alpha"]["git"]["dirty"], 5)
        self.assertEqual(snap.v, 1)


# ---------------------------------------------------------------- freshness

class TestFreshness(CacheGuard):

    def test_freshness_names_every_kind_and_advances(self):
        o = fb.Observer()
        o.publish(fake_state(1000.0), fresh={"git": 1.0, "procs": 2.0})
        self.assertEqual(o.snapshot().freshness, {"git": 1.0, "procs": 2.0})
        # a sweep that changes nothing still advances freshness — that is the
        # whole point of the no-bump path
        snap = o.publish(fake_state(1001.0), fresh={"git": 9.0})
        self.assertEqual(snap.v, 1)
        self.assertEqual(snap.freshness, {"git": 9.0, "procs": 2.0})

    def test_the_freshness_map_is_a_copy_per_snapshot(self):
        o = fb.Observer()
        first = o.publish(fake_state(1000.0), fresh={"git": 1.0})
        o.publish(fake_state(1001.0, cards=(("alpha", 3),)), fresh={"git": 2.0})
        self.assertEqual(first.freshness["git"], 1.0)   # not mutated underneath

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_collect_state_stamps_the_kinds_it_actually_probed(self):
        with FleetFixture() as fx:
            fresh = {}
            fb.collect_state(fresh=fresh)
            self.assertEqual(set(fresh) & {"worktrees", "procs", "transcripts", "git"},
                             {"worktrees", "procs", "transcripts", "git"})
            self.assertLessEqual(fresh["worktrees"], fresh["git"])
            # cclimits is never probed on the state path, so it never claims to
            # have been: with a cold cache the kind is simply absent.
            self.assertNotIn("limits", fresh)

    @unittest.skipUnless(HAVE_GIT, "git not available")
    def test_collect_state_without_fresh_is_untouched(self):
        with FleetFixture():
            self.assertIn("worktrees", fb.collect_state())


# ------------------------------------------------------------- delta_since

class TestDeltaSince(CacheGuard):

    def _wound(self, n=3):
        o = fb.Observer()
        for i in range(n):
            o.publish(fake_state(1000.0 + i, cards=(("alpha", i), ("beta", 0))))
        return o

    def test_unknown_n_returns_a_full_snapshot(self):
        o = self._wound()
        for n in (0, -1, 99):
            d = o.delta_since(n)
            self.assertEqual(d["type"], "snapshot", n)
            self.assertEqual(set(d["cards"]), {"alpha", "beta"})
            self.assertIn("other_procs", d)

    def test_known_n_returns_a_delta_naming_only_what_moved(self):
        o = self._wound()
        d = o.delta_since(o.snapshot().v - 1)
        self.assertEqual(d["type"], "delta")
        self.assertEqual(d["base"], o.snapshot().v - 1)
        self.assertEqual(set(d["cards"]), {"alpha"})      # beta never changed
        self.assertIn("freshness", d)

    def test_a_current_client_gets_an_empty_delta_not_a_snapshot(self):
        o = self._wound()
        d = o.delta_since(o.snapshot().v)
        self.assertEqual(d["type"], "delta")
        self.assertEqual(d["cards"], {})

    def test_a_removed_card_arrives_as_an_explicit_none(self):
        o = self._wound()
        base = o.snapshot().v
        o.publish(fake_state(2000.0, cards=(("beta", 0),)))
        d = o.delta_since(base)
        self.assertEqual(d["type"], "delta")
        self.assertIsNone(d["cards"]["alpha"])            # None = card removed

    def test_a_client_older_than_the_ring_gets_a_full_snapshot(self):
        o = fb.Observer()
        for i in range(fb.observer.HIST + 20):
            o.publish(fake_state(1000.0 + i, cards=(("alpha", i),)))
        self.assertEqual(len(o._hist), fb.observer.HIST)
        self.assertEqual(o.delta_since(1)["type"], "snapshot")
        self.assertEqual(o.delta_since(o.snapshot().v - 2)["type"], "delta")

    def test_delta_before_the_first_sweep_is_none(self):
        self.assertIsNone(fb.Observer().delta_since(0))


# ------------------------------------------------------------------ threading

class TestSweepThread(CacheGuard):

    def setUp(self):
        super().setUp()
        self._collect = fb.observer.collect_state
        self.calls = []

    def tearDown(self):
        fb.observer.collect_state = self._collect
        super().tearDown()

    def _stub(self, at=None):
        def collect(fresh=None, git=None, cold=False):
            self.calls.append(time.time())
            if fresh is not None:
                fresh["procs"] = time.time()
            return fake_state(at or time.time())
        fb.observer.collect_state = collect

    def test_importing_the_package_starts_no_thread(self):
        self.assertIsNone(fb.observer._observer)
        self.assertEqual([t for t in threading.enumerate()
                          if t.name == "observer-sweep"], [])

    def test_the_thread_sweeps_and_publishes(self):
        self._stub()
        o = fb.Observer(idle_s=0.02, hot_s=0.0)
        o.start()
        try:
            self.assertIsNotNone(o.wait_for(0, timeout=5))
            self.assertTrue(o.running)
        finally:
            o.stop()
        self.assertFalse(o.running)
        self.assertGreaterEqual(o.stats()["sweeps"], 1)

    def test_the_thread_refreshes_the_request_path_cache(self):
        self._stub()
        fb._cache["state"], fb._cache["t"] = None, 0.0
        o = fb.Observer(idle_s=0.02, hot_s=0.0)
        o.start()
        try:
            o.wait_for(0, timeout=5)
        finally:
            o.stop()
        self.assertIsNotNone(fb._cache["state"])
        before = len(self.calls)
        fb.cached_state()                    # served from the warm cache…
        self.assertEqual(len(self.calls), before)   # …with no collect at all

    def test_a_parked_invalidation_survives_an_in_flight_sweep(self):
        """finish() parks _cache["t"] = 0.0 mid-sweep. A sweep that started
        BEFORE the mutation must not paper over it with pre-mutation data."""
        started = threading.Event()
        release = threading.Event()

        def collect(fresh=None, git=None, cold=False):
            started.set()
            release.wait(5)
            return fake_state(time.time())
        fb.observer.collect_state = collect
        fb._cache["state"], fb._cache["t"] = {"stale": True}, time.time()
        o = fb.Observer(idle_s=5.0)
        t = threading.Thread(target=o.sweep, daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        fb._cache["t"] = 0.0                 # the mutation lands mid-collect
        release.set()
        t.join(5)
        self.assertEqual(fb._cache["t"], 0.0)          # invalidation intact
        self.assertEqual(fb._cache["state"], {"stale": True})
        self.assertEqual(o.snapshot().v, 1)            # …but it still published

    def test_nudge_pulls_the_next_sweep_forward(self):
        self._stub()
        o = fb.Observer(idle_s=30.0, hot_s=0.0)
        o.start()
        try:
            deadline = time.time() + 5
            while not self.calls and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.calls), 1)
            time.sleep(0.2)
            self.assertEqual(len(self.calls), 1)       # idle_s=30 — still asleep
            o.nudge("test")
            deadline = time.time() + 5
            while len(self.calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.calls), 2)
        finally:
            o.stop()
        self.assertEqual(o.stats()["nudges"], 1)

    def test_a_nudge_that_lands_mid_sweep_is_not_forgotten(self):
        """A mutation completes while a sweep is already in flight: that sweep
        carries pre-mutation data, so the nudge has to survive it."""
        inside, release = threading.Event(), threading.Event()

        def collect(fresh=None, git=None, cold=False):
            self.calls.append(time.time())
            if len(self.calls) == 1:
                inside.set()
                release.wait(5)
            return fake_state(time.time())
        fb.observer.collect_state = collect
        o = fb.Observer(idle_s=30.0, hot_s=0.0)
        o.start()
        try:
            self.assertTrue(inside.wait(5))
            o.nudge("mutation done")         # arrives DURING the collect
            release.set()
            deadline = time.time() + 5
            while len(self.calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.calls), 2)   # not 1 — not waiting out 30s
        finally:
            release.set()
            o.stop()

    def test_hot_s_is_a_floor_so_a_burst_of_nudges_cannot_spin_the_loop(self):
        self._stub()
        o = fb.Observer(idle_s=30.0, hot_s=1.0)
        o.start()
        try:
            deadline = time.time() + 5
            # `sweeps` ticks AFTER the collect returns, so this waits for the
            # loop to be parked in its wait — the nudges below land there, not
            # mid-sweep, which is the path the floor guards.
            while o.stats()["sweeps"] < 1 and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            for _ in range(20):
                o.nudge("burst")
            time.sleep(0.4)
            self.assertEqual(len(self.calls), 1)    # held off by hot_s
            deadline = time.time() + 3
            while len(self.calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(self.calls), 2)    # …then exactly one sweep
        finally:
            o.stop()

    def test_module_level_nudge_is_a_no_op_with_no_observer(self):
        fb.observer._observer = None
        fb.observer.nudge("nothing is listening")      # must not raise

    def test_a_wedged_probe_does_not_kill_the_loop(self):
        boom = [3]

        def collect(fresh=None, git=None, cold=False):
            self.calls.append(time.time())
            if boom[0] > 0:
                boom[0] -= 1
                raise RuntimeError("git wedged")
            return fake_state(time.time())
        fb.observer.collect_state = collect
        o = fb.Observer(idle_s=0.01, hot_s=0.0)
        o.start()
        try:
            self.assertIsNotNone(o.wait_for(0, timeout=5))
        finally:
            o.stop()
        self.assertGreaterEqual(o.stats()["errors"], 3)
        self.assertIn("git wedged", o.stats()["last_error"])

    def test_wait_for_times_out_rather_than_hanging(self):
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        t0 = time.time()
        self.assertIsNone(o.wait_for(o.snapshot().v, timeout=0.05))
        self.assertLess(time.time() - t0, 2)

    def test_start_is_idempotent(self):
        self._stub()
        o = fb.Observer(idle_s=0.05)
        try:
            self.assertIs(o.start(), o.start())
            self.assertEqual(len([t for t in threading.enumerate()
                                  if t.name == "observer-sweep"]), 1)
        finally:
            o.stop()


# --------------------------------------------------- the compatibility seam

@unittest.skipUnless(HAVE_GIT, "git not available")
class TestSynchronousFallback(CacheGuard):
    """The rollback story: with no thread running, cached_state() is exactly
    what it was before there was an Observer."""

    def test_no_thread_means_collect_on_the_calling_thread(self):
        with FleetFixture():
            fb.observer._observer = None
            fb._cache["state"] = None
            st = fb.cached_state()
            self.assertEqual([w["name"] for w in st["worktrees"]], ["alpha", "beta"])
            self.assertIs(fb.cached_state(), st)          # …then the 4s cache
            fb._cache["t"] = 0.0                          # a mutation invalidates
            self.assertIsNot(fb.cached_state(), st)

    def test_the_fallback_collect_is_published_too(self):
        """Otherwise a mutation's effect would never reach the version."""
        with FleetFixture():
            o = fb.Observer()
            fb.observer._observer = o
            fb._cache["state"] = None
            fb.cached_state()
            self.assertEqual(o.snapshot().v, 1)
            self.assertEqual(set(o.snapshot().cards), {"alpha", "beta"})
            self.assertIn("git", o.snapshot().freshness)

    def test_a_real_unchanged_fleet_publishes_no_second_version(self):
        """The claim, end to end: two full sweeps of the real compose path over
        a fleet nobody touched, and `v` does not move.

        `git_s=0` puts git back on every sweep, which is what this test is
        about: it asserts the DIFF is quiet on an unchanged fleet and loud on a
        changed one, not that the cadence is. The cadence has its own class
        below, and the whole point of it is that a working-tree edit lands
        within GIT_S rather than instantly.
        """
        with FleetFixture():
            o = fb.Observer(git_s=0.0)
            o.sweep()
            self.assertEqual(o.snapshot().v, 1)
            o.sweep()
            self.assertEqual(o.snapshot().v, 1)
            self.assertEqual(o.stats()["sweeps"], 2)
            # …and a real edit does move it
            (Path(o.snapshot().cards["alpha"]["path"]) / "new").write_text("x\n")
            o.sweep()
            self.assertEqual(o.snapshot().v, 2)
            self.assertEqual(tuple(o._hist)[-1][1], ("alpha",))


# --------------------------------------------------------- git's own clock

@unittest.skipUnless(HAVE_GIT, "git not available")
class TestGitCadence(CacheGuard):
    """§2.5's `git_s`. The git fan-out is 79 % of a sweep's CPU (1.26 of
    1.59 CPU-s over nine worktrees, getrusage including children), so the
    perpetual loop is unaffordable until git stops running every sweep.

    It cannot be a stat memo: `dirty` is the working tree and nothing cheap
    detects an edit there. So it is a cadence, and a cadence is only honest if
    three things hold — the age is published, a nudge can pull it forward, and
    the cold reconcile catches it lying. One test each.
    """

    def _dirty(self, o, name="alpha"):
        return o.snapshot().cards[name]["git"]["dirty"]

    def test_git_runs_once_and_is_reused_until_its_clock_comes_round(self):
        with FleetFixture():
            o = fb.Observer(git_s=60.0)
            for _ in range(3):
                fb._cache["state"] = None
                o.sweep()
            st = o.stats()
            self.assertEqual(st["git_probes"], 1)      # one fan-out, three sweeps
            self.assertEqual(st["git_reuses"], 2)
            # …and every card still carries FULL git data, not a hole
            for card in o.snapshot().cards.values():
                self.assertEqual(card["git"]["branch"], "main")
                self.assertTrue(card["git"]["commit"]["hash"])

    def test_a_working_tree_edit_lands_within_git_s_not_instantly(self):
        """The cost of the cadence, stated as a test rather than a hope."""
        with FleetFixture():
            o = fb.Observer(git_s=60.0)
            o.sweep()
            (Path(o.snapshot().cards["alpha"]["path"]) / "new").write_text("x\n")
            o.sweep()
            self.assertEqual(self._dirty(o), 0)        # still the cached answer
            self.assertEqual(o.snapshot().v, 1)
            o._git.every_s = 0.0                       # the clock comes round
            o.sweep()
            self.assertEqual(self._dirty(o), 1)
            self.assertEqual(o.snapshot().v, 2)

    def test_freshness_git_is_the_clock_of_the_probe_not_of_the_sweep(self):
        """§3.3. A board that renders 'git 0s ago' off a reused answer is worse
        than one that renders nothing."""
        with FleetFixture():
            o = fb.Observer(git_s=60.0)
            o.sweep()
            probed = o.snapshot().freshness["git"]
            time.sleep(0.05)
            o.sweep()
            snap = o.snapshot()
            self.assertEqual(snap.freshness["git"], probed)   # did NOT advance
            self.assertLess(snap.freshness["git"], snap.at)   # …and says so
            self.assertGreater(snap.freshness["transcripts"], probed)
            o._git.force()
            o.sweep()
            self.assertGreater(o.snapshot().freshness["git"], probed)

    def test_a_nudge_pulls_git_forward(self):
        """Every mutation that moves git already nudges — finish/exit parks a
        worktree on the trunk, dispatch cuts a branch. Serving 15 s-old branch
        data straight after one is the whole failure this prevents."""
        with FleetFixture():
            o = fb.Observer(git_s=600.0)
            o.sweep()
            (Path(o.snapshot().cards["alpha"]["path"]) / "new").write_text("x\n")
            o.sweep()
            self.assertEqual(self._dirty(o), 0)
            o.nudge("finish/exit")
            o.sweep()
            self.assertEqual(self._dirty(o), 1)
            self.assertEqual(o.stats()["git_probes"], 2)

    def test_a_worktree_that_appears_probes_git_on_the_spot(self):
        """A new card with no git data is worse than a slightly stale one."""
        with FleetFixture() as fx:
            o = fb.Observer(git_s=600.0)
            o.sweep()
            d = fx.tmp / "code" / "gamma"
            d.mkdir()
            _git(d, "init", "-q", "-b", "trunk")
            _git(d, "config", "user.email", "t@t.t")
            _git(d, "config", "user.name", "t")
            (d / "f").write_text("1\n")
            _git(d, "add", "-A")
            _git(d, "commit", "-q", "-m", "seed")
            fb._cache["state"] = None
            o.sweep()
            card = o.snapshot().cards["gamma"]
            self.assertEqual(card["git"]["branch"], "trunk")
            self.assertTrue(card["git"]["commit"]["hash"])
            # off-clock and per-root: the sitting worktrees were NOT re-probed,
            # so the new card cost one `git`, not nine
            self.assertEqual(o.stats()["git_probes"], 2)
            self.assertEqual(set(o._git._at) - {str(d)},
                             {str(fx.tmp / "code" / n) for n in ("alpha", "beta")})
            self.assertLess(o._git._at[str(fx.tmp / "code" / "alpha")],
                            o._git._at[str(d)])

    def test_a_worktree_that_vanishes_stops_dragging_the_clock(self):
        cad = fb.GitCadence(every_s=600.0)
        with FleetFixture() as fx:
            roots = [str(fx.tmp / "code" / n) for n in ("alpha", "beta")]
            by, at = cad.resolve(roots)
            self.assertEqual(set(by), set(roots))
            by, at2 = cad.resolve(roots[:1])
            self.assertEqual(set(cad._info), {roots[0]})   # beta forgotten
            self.assertEqual(at2, cad._at[roots[0]])

    def test_the_cold_reconcile_bypasses_the_cadence_and_counts_the_lie(self):
        """LAW: a memo nobody audits is worse than a slow sweep (§4.3 #1/#4).
        Here the disagreement is not a lie — the cadence never claimed to be
        current — but it is the measured cost of the cadence, and unmeasured
        cost is how 15.0 quietly becomes 300.0."""
        with FleetFixture():
            o = fb.Observer(git_s=600.0)
            o.sweep(cold=True)
            self.assertEqual(o.snapshot().drift, 0)
            (Path(o.snapshot().cards["alpha"]["path"]) / "new").write_text("x\n")
            o.sweep()                       # warm: serves the stale answer
            self.assertEqual(self._dirty(o), 0)
            o.sweep(cold=True)              # cold: recomputes, and compares
            self.assertEqual(self._dirty(o), 1)
            self.assertEqual(o.stats()["git_drift"], 1)
            self.assertEqual(o.stats()["drift"], 1)
            self.assertEqual(o.snapshot().drift, 1)
            # beta was never touched, so exactly one card disagreed
            o.sweep(cold=True)
            self.assertEqual(o.stats()["git_drift"], 1)

    def test_drift_reaches_a_snapshot_that_published_no_new_version(self):
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        o._drift = 3
        snap = o.publish(fake_state(1001.0))
        self.assertEqual(snap.v, 1)
        self.assertEqual(snap.drift, 3)

    def test_git_s_is_a_config_key_not_a_constant(self):
        saved = fb.CFG.get("git_s")
        try:
            fb.CFG["git_s"] = 7.5
            self.assertEqual(fb.Observer()._git.every_s, 7.5)
            self.assertEqual(fb.Observer(git_s=1.0)._git.every_s, 1.0)
        finally:
            fb.CFG["git_s"] = saved
        self.assertEqual(fb.CFG["git_s"], fb.observer.GIT_S)

    def test_collect_state_with_no_git_seam_is_the_old_function(self):
        """The rollback, and what keeps characterize.py byte-identical: nothing
        about the cadence exists unless a caller passes it in."""
        with FleetFixture():
            fresh = {}
            before = time.time()
            st = fb.collect_state(fresh=fresh)
            self.assertGreaterEqual(fresh["git"], before)
            self.assertEqual(st["worktrees"][0]["git"]["branch"], "main")


# ------------------------------------------------------- the read-only rule

@unittest.skipUnless(HAVE_GIT, "git not available")
class TestTheSweepMutatesNothing(CacheGuard):
    """§2.5: the observer may not write state a mutation path owns.

    Lazily, `collect_state` reaped `finish._closeouts` for cards whose terminal
    had gone — a write that only ran when somebody looked. Perpetually, that is
    a scheduled background action nobody requested, so it is gone: the card
    stops ADVERTISING the flag, and finish reaps its own map.
    """

    def setUp(self):
        super().setUp()
        self._saved_closeouts = dict(fb._closeouts)

    def tearDown(self):
        fb._closeouts.clear()
        fb._closeouts.update(self._saved_closeouts)
        super().tearDown()

    def test_sweeping_forever_never_reaps_a_closeout_flag(self):
        with FleetFixture():
            fb._closeouts.clear()
            fb._closeouts["alpha"] = ts = time.time() - 30   # no live procs
            o = fb.Observer()
            for _ in range(3):
                fb._cache["state"] = None
                o.sweep()
            self.assertNotIn("closeout_sent", o.snapshot().cards["alpha"])
            self.assertEqual(fb._closeouts, {"alpha": ts})
            self.assertEqual(o.snapshot().v, 1)   # …and nothing looked changed


# ------------------------------------------------------------------ fixture

class FleetFixture:
    """Two real git worktrees and a real Claude home, selected purely through
    config — no monkeypatching of the compose path, so collect_state runs end
    to end. `ps`/`cclimits` are stubbed empty; they are the two inputs that
    would otherwise depend on the developer's live machine."""

    KEYS = ("roots", "homes", "pattern", "exclude_accounts")

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-obs-"))
        root = self.tmp / "code"
        root.mkdir(parents=True)
        home = self.tmp / "home"
        (home / "projects").mkdir(parents=True)
        for name in ("alpha", "beta"):
            d = root / name
            d.mkdir()
            _git(d, "init", "-q", "-b", "main")
            _git(d, "config", "user.email", "t@t.t")
            _git(d, "config", "user.name", "t")
            (d / "f").write_text("1\n")
            _git(d, "add", "-A")
            _git(d, "commit", "-q", "-m", "seed")
            proj = home / "projects" / fb.munge(str(d))
            proj.mkdir(parents=True)
            (proj / f"sess-{name}.jsonl").write_text("\n".join(json.dumps(e) for e in [
                {"type": "user", "cwd": str(d), "gitBranch": "main",
                 "message": {"content": f"build {name}"}},
                {"type": "assistant", "cwd": str(d),
                 "message": {"model": "claude-opus-4-8",
                             "content": [{"type": "text", "text": f"on {name}"}]}},
            ]) + "\n")
        self.saved = {k: fb.CFG.get(k) for k in self.KEYS}
        self.demo, self.procs, self.cl = (fb.config.DEMO,
                                          fb.procs.claude_processes,
                                          fb.limits.cached_limits)
        fb.config.DEMO = False
        fb.CFG.update({"roots": [str(root)], "homes": [str(home)],
                       "pattern": "", "exclude_accounts": []})
        fb.procs.claude_processes = lambda: []
        fb.limits.cached_limits = lambda refresh=False: {"available": False}
        fb._cache["state"], fb._cache["t"] = None, 0.0
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                fb.CFG.pop(k, None)
            else:
                fb.CFG[k] = v
        (fb.config.DEMO, fb.procs.claude_processes,
         fb.limits.cached_limits) = self.demo, self.procs, self.cl
        fb._cache["state"], fb._cache["t"] = None, 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


if __name__ == "__main__":
    unittest.main()
