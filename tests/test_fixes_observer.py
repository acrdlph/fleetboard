#!/usr/bin/env python3
"""Two concurrency fixes in orchestra/observer.py, each pinned by the
interleaving that used to lose.

* The lost wakeup: `_loop` read `_nudge_at` for the deadline and only THEN
  cleared `_wake`, so a nudge whose whole body ran between the read and the
  clear was erased outright — deadline computed pre-nudge, set() wiped — and
  the loop waited out the full cadence. The clear now happens before the
  read; because `nudge` writes `_nudge_at` before it sets the event, a nudge
  after the clear is either seen by the read or leaves the event set.

* The request-path cache clobber: `cached_state` wrote `_cache` blindly after
  its synchronous collect, stamping a fresh clock over a mutation's parked
  `_cache["t"] = 0.0` — after which the sweep's own compare-and-swap rightly
  refused to touch the cache and the polled board served pre-mutation state
  for up to `republish_s`. The request path now keeps the same CAS the sweep
  does, and serves the fresh state uncached when it loses.

    python3 -m unittest tests.test_fixes_observer -v
"""

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402


def fake_state(at):
    """The smallest collect_state() shape `publish` accepts."""
    return {"generated_at": at, "counts": {}, "worktrees": [], "other_procs": []}


class CacheGuard(unittest.TestCase):
    """Same hygiene as tests/test_observer.py: these tests write through the
    module cache, patch the collect seam and rebind the process-wide observer,
    so every one of those is saved and restored. `watch` off so no kqueue
    thread over the developer's own fleet nudges a timing assertion."""

    def setUp(self):
        self._cache = dict(fb._cache)
        self._glob = fb.observer._observer
        self._collect = fb.observer.collect_state
        self._watch = fb.CFG.get("watch")
        fb.CFG["watch"] = False

    def tearDown(self):
        fb.CFG["watch"] = self._watch
        fb.observer.collect_state = self._collect
        fb.observer._observer = self._glob
        fb._cache.update(self._cache)


class TestNudgeInTheClearWindowIsNotLost(CacheGuard):

    def test_a_nudge_landing_at_the_clear_is_honoured(self):
        """Inject a nudge at the exact point `_wake.clear()` runs — the one
        interleaving the old ordering lost. With the clear after the
        `_nudge_at` read, the nudge's deadline was ignored AND its set() was
        wiped, and the loop slept the full 30 s; with the clear before the
        read, the read sees `_nudge_at` and the next sweep is immediate."""
        o = fb.Observer(idle_s=30.0, idle_blind_s=30.0, max_stale_s=30.0,
                        hot_s=0.0, watch=False)
        sweeps = []
        o.sweep = lambda cold=False: sweeps.append(time.time())

        class NudgeAtClear(threading.Event):
            """An Event whose first clear() carries a concurrent nudge —
            deterministically, where a real race lands once in a million."""
            fired = False

            def clear(self):
                if not self.fired:
                    self.fired = True
                    o.nudge("landed in the clear window")
                super().clear()

        o._wake = NudgeAtClear()
        t = threading.Thread(target=o._loop, daemon=True)
        t.start()
        try:
            deadline = time.time() + 5
            while len(sweeps) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertGreaterEqual(len(sweeps), 2,
                                    "the nudge was swallowed by the clear — "
                                    "the loop is waiting out the full cadence")
        finally:
            o._stop.set()
            o._wake.set()
            t.join(5)
        self.assertFalse(t.is_alive())


class TestRequestPathCacheCAS(CacheGuard):

    def test_a_parked_invalidation_survives_an_in_flight_request_collect(self):
        """finish() parks _cache["t"] = 0.0 while a request-thread collect is
        in flight. The collect predates the mutation, so its result must not
        land in the cache with a fresh clock — the same rule the sweep already
        keeps (`test_a_parked_invalidation_survives_an_in_flight_sweep`)."""
        started, release = threading.Event(), threading.Event()

        def collect(fresh=None, git=None, cold=False, settle=None, hooks=None):
            started.set()
            release.wait(5)
            return fake_state(time.time())
        fb.observer.collect_state = collect
        fb.observer._observer = None
        stale = {"stale": True}
        fb._cache["state"] = stale
        fb._cache["t"] = time.time() - (fb.observer.STATE_TTL_S + 2.0)
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("state",
                                                           fb.cached_state()),
                             daemon=True)
        t.start()
        self.assertTrue(started.wait(5))
        fb._cache["t"] = 0.0                 # the mutation lands mid-collect
        release.set()
        t.join(5)
        self.assertEqual(fb._cache["t"], 0.0)          # invalidation intact
        self.assertIs(fb._cache["state"], stale)       # …and not clobbered
        # the fresh (pre-mutation, but freshly collected) state is still served
        self.assertIn("generated_at", got["state"])
        # …and the very next request re-collects, post-mutation
        calls = []
        fb.observer.collect_state = lambda **kw: calls.append(1) or \
            fake_state(time.time())
        fb.cached_state()
        self.assertEqual(len(calls), 1)

    def test_an_undisturbed_request_collect_still_caches(self):
        """The CAS must lose only when something moved under it: the plain
        expired-cache path still writes, and the next request is served warm."""
        calls = []

        def collect(fresh=None, git=None, cold=False, settle=None, hooks=None):
            calls.append(1)
            return fake_state(time.time())
        fb.observer.collect_state = collect
        fb.observer._observer = None
        fb._cache["state"] = {"stale": True}
        fb._cache["t"] = time.time() - (fb.observer.STATE_TTL_S + 2.0)
        st = fb.cached_state()
        self.assertEqual(len(calls), 1)
        self.assertIs(fb._cache["state"], st)          # cached…
        self.assertIs(fb.cached_state(), st)           # …and served warm
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
