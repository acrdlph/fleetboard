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

import contextlib
import io
import json
import os
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

def fake_state(at, *, cards=(("alpha", 0),), cpu=1.0, etime="01:00",
               last_write_at=1000.0,
               other_cpu=0.5, counts=None, status="working"):
    """A collect_state() result, hand-built. `publish` reads exactly four keys."""
    return {
        "generated_at": at,
        "counts": counts or {"working": len(cards)},
        "worktrees": [
            {"name": name, "availability": "busy",
             "git": {"branch": "main", "dirty": dirty},
             "sessions": [{"sid": f"s-{name}", "status": status,
                           "last_write_at": last_write_at}],
             "live_procs": [{"pid": 7, "cpu": cpu, "etime": etime,
                             "tty": "ttys001", "reachable": True}]}
            for name, dirty in cards],
        "other_procs": [{"pid": 9, "cpu": other_cpu, "etime": etime,
                         "cwd": "/elsewhere"}],
    }


class CacheGuard(unittest.TestCase):
    """The Observer writes through `_cache`; never leak that into another test.

    `watch` off, for every test in this file: an Observer that starts now also
    starts a kqueue thread, and with no config loaded that thread watches the
    DEVELOPER'S OWN fleet and nudges this loop whenever a real agent writes a
    line. Every timing assertion here would become a function of what the
    machine happened to be doing. The watcher has its own file and its own
    fixtures; here it is an input to be held still.
    """

    def setUp(self):
        self._cache = dict(fb._cache)
        self._glob = fb.observer._observer
        self._watch = fb.CFG.get("watch")
        fb.CFG["watch"] = False

    def tearDown(self):
        fb.CFG["watch"] = self._watch
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
        """cpu / etime move on their own. They still ship — they just do not
        get a vote, or v would tick once a second forever."""
        o = fb.Observer()
        o.publish(fake_state(1000.0, cpu=1.0, etime="01:00"))
        snap = o.publish(fake_state(1001.0, cpu=98.6, etime="01:01",
                                    other_cpu=77.7))
        self.assertEqual(snap.v, 1)
        # …and the reading a client renders is the NEW one, not the stale one
        self.assertEqual(snap.cards["alpha"]["live_procs"][0]["cpu"], 98.6)
        self.assertEqual(snap.other_procs[0]["cpu"], 77.7)

    def test_a_session_has_no_stopwatch_left_to_exempt(self):
        """The exemption list for sessions is EMPTY, and that is the assertion.

        `age_s` was the only entry and it is off the wire, so a session object
        is time-invariant end to end and the version diffs it whole. If a
        now-derived field ever comes back, either it is exempted here — and
        this goes red — or it is not, and `v` starts ticking on the clock."""
        self.assertEqual(fb.observer._UNDIFFED_SESSION_KEYS, ())

    def test_a_write_bumps_the_version_even_with_nothing_else_changed(self):
        """The flip side of taking `age_s` out of the diff. A moving age was a
        function of WHEN YOU ASKED and had to be exempted; `last_write_at` moves
        only when an agent actually wrote a line, so it is real news and gets a
        vote — a tool_use that changes no status and no visible text still
        reaches a client as a new version."""
        o = fb.Observer()
        o.publish(fake_state(1000.0, last_write_at=1000.0))
        self.assertEqual(o.publish(fake_state(1001.0, last_write_at=1000.5)).v, 2)

    def test_a_status_change_bumps(self):
        """What a threshold crossing MEANS is on the wire and in the diff."""
        o = fb.Observer()
        o.publish(fake_state(1000.0))
        st = fake_state(1001.0)
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

    def test_the_two_branches_carry_the_same_envelope(self):
        """The shape of the bug this class was found by: the snapshot branch
        carried `other_procs` and the delta branch did not, so a resuming
        client's view depended on WHICH branch answered it. Whatever the two
        branches carry, they must carry the same thing — `base` excepted, which
        is what makes a delta a delta."""
        o = self._wound()
        full = o.delta_since(0)
        delta = o.delta_since(o.snapshot().v - 1)
        self.assertEqual(full["type"], "snapshot")
        self.assertEqual(delta["type"], "delta")
        self.assertEqual(set(delta) - {"base"}, set(full))
        self.assertNotIn("base", full)


# ------------------------------------------- a delta RECONSTRUCTS the snapshot

def ordered_state(at, cards, *, other=(9,), counts=None):
    """A collect_state() result with the card ORDER and each card's status
    under the test's control — the two things the reconstruction claim is
    about. `cards` is [(name, dirty, status)]; `other` is loose-process pids."""
    return {
        "generated_at": at,
        "counts": counts or {"working": len(cards)},
        "worktrees": [
            {"name": name, "availability": "busy",
             "git": {"branch": "main", "dirty": dirty},
             "sessions": [{"sid": f"s-{name}", "status": status,
                           "last_write_at": 1000.0}],
             "live_procs": []}
            for name, dirty, status in cards],
        "other_procs": [{"pid": pid, "cpu": 0.5, "etime": "01:00",
                         "cwd": "/elsewhere"} for pid in other],
    }


class Client:
    """The browser's half of §3.5, in the smallest form that can still be wrong.

    This is `Fleet.apply` from `stream.js` transcribed — the SharedWorker holds
    exactly this state and patches it exactly this way. Two reasons it lives
    here as well as in JS: it makes the delta contract a claim the Python suite
    can falsify with no browser and no node, and it is the reference the JS is
    checked against in `tests/test_stream_js.py`.

    A gap is an exception rather than a silent resync, because these tests
    never lose a frame — one arriving out of sequence would mean `delta_since`
    changed its cursor semantics.
    """

    def __init__(self):
        self.v, self.cards, self.counts, self.other = None, {}, None, None

    def apply(self, f):
        if f["type"] == "snapshot":
            self.cards = dict(f["cards"])
        else:
            assert f["base"] == self.v, f"gap: base {f['base']} at v {self.v}"
            for key, card in f["cards"].items():
                if card is None:
                    self.cards.pop(key, None)      # None = the card was removed
                else:
                    self.cards[key] = card
        assert set(self.cards) == set(f["order"]), (
            f"the frame's order names {sorted(f['order'])}, the client holds "
            f"{sorted(self.cards)} — the delta did not carry every change")
        self.cards = {k: self.cards[k] for k in f["order"]}
        self.counts, self.other, self.v = f["counts"], f["other_procs"], f["v"]
        return self

    @property
    def view(self):
        """Everything the board draws off a frame, ORDER INCLUDED — a dict
        compares equal regardless of key order, so the cards travel as a list."""
        return {"cards": list(self.cards.items()),
                "counts": self.counts, "other_procs": self.other}


def served(snap):
    """The same view, taken from the snapshot whole — what a client that had
    just connected would render."""
    return {"cards": list(snap.cards.items()),
            "counts": snap.counts, "other_procs": snap.other_procs}


class TestDeltaReconstructsTheSnapshot(CacheGuard):
    """The claim the streaming board rests on: a client that applied every
    delta sees EXACTLY what a client that took the snapshot whole sees.

    Everything below drives the real `Observer.publish` / `delta_since` — the
    only thing faked is `collect_state`'s output, so the composition, the
    version rule and the changed-key ring are all the shipped ones.
    """

    def test_a_stream_of_deltas_ends_where_a_snapshot_would(self):
        o, c = fb.Observer(), Client()
        # a card arrives, a card changes, a card's status flips (which RE-SORTS
        # the board), a card leaves, a loose process appears — one publish each
        script = [
            [("alpha", 0, "working"), ("beta", 0, "working")],
            [("alpha", 1, "working"), ("beta", 0, "working")],
            [("beta", 0, "needs_input"), ("alpha", 1, "working")],
            [("beta", 0, "needs_input"), ("alpha", 1, "working"),
             ("gamma", 0, "working")],
            [("beta", 0, "needs_input"), ("gamma", 0, "working")],
        ]
        for i, cards in enumerate(script):
            o.publish(ordered_state(1000.0 + i, cards))
            c.apply(o.delta_since(c.v or 0))
            self.assertEqual(c.view, served(o.snapshot()), f"step {i}")
        o.publish(ordered_state(2000.0, script[-1], other=(9, 11)))
        c.apply(o.delta_since(c.v))
        self.assertEqual(c.view, served(o.snapshot()))

    def test_the_delta_carries_the_board_order_not_just_the_changes(self):
        """A status flip re-sorts the board on the server. The delta names only
        the card that flipped, so a client patching its own dict keeps its OLD
        positions unless the frame says otherwise."""
        o, c = fb.Observer(), Client()
        o.publish(ordered_state(1000.0, [("alpha", 0, "working"),
                                         ("beta", 0, "working")]))
        c.apply(o.delta_since(0))
        self.assertEqual([k for k, _ in c.view["cards"]], ["alpha", "beta"])
        # beta needs input -> the server sorts it first; alpha is untouched
        o.publish(ordered_state(1001.0, [("beta", 0, "needs_input"),
                                         ("alpha", 0, "working")]))
        d = o.delta_since(c.v)
        self.assertEqual(set(d["cards"]), {"beta"}, "alpha must not be resent")
        c.apply(d)
        self.assertEqual([k for k, _ in c.view["cards"]], ["beta", "alpha"])

    def test_a_loose_process_alone_bumps_the_version_and_rides_the_delta(self):
        """`other_procs` is part of the composed view `publish` diffs, so a
        claude process outside every watched worktree moves `v` with no card
        changing at all — an EMPTY delta. Without the list on the frame that
        bump is unexplainable and the ⌁ live-agents tile drifts."""
        o, c = fb.Observer(), Client()
        o.publish(ordered_state(1000.0, [("alpha", 0, "working")]))
        c.apply(o.delta_since(0))
        before = o.snapshot().v
        o.publish(ordered_state(1001.0, [("alpha", 0, "working")], other=(9, 11)))
        self.assertEqual(o.snapshot().v, before + 1, "a loose proc must bump v")
        d = o.delta_since(c.v)
        self.assertEqual(d["cards"], {}, "no card changed")
        c.apply(d)
        self.assertEqual([p["pid"] for p in c.view["other_procs"]], [9, 11])
        self.assertEqual(c.view, served(o.snapshot()))

    def test_every_bump_term_reaches_a_delta_consumer(self):
        """THE CLASS, not the instance. `publish` bumps `v` on three terms —
        cards, `counts`, `other_procs`. For each one ALONE, a client holding the
        previous version must land exactly where a client that took the snapshot
        whole lands. `other_procs` was the one that did not: the version moved,
        the frame said `{"cards": {}}`, and the client stayed wrong until its
        cursor fell out of the 512-version ring."""
        base = [("alpha", 0, "working"), ("beta", 0, "working")]
        moves = {
            "cards": dict(cards=[("alpha", 1, "working"), ("beta", 0, "working")]),
            "counts": dict(cards=base, counts={"working": 1, "waiting": 1}),
            "other_procs": dict(cards=base, other=(9, 11)),
        }
        for term, move in moves.items():
            with self.subTest(term=term):
                o, c = fb.Observer(), Client()
                o.publish(ordered_state(1000.0, base))
                c.apply(o.delta_since(0))
                before = o.snapshot().v
                o.publish(ordered_state(1001.0, **move))
                self.assertEqual(o.snapshot().v, before + 1,
                                 f"{term} alone must bump v")
                d = o.delta_since(c.v)
                self.assertEqual(d["type"], "delta")
                c.apply(d)
                # the whole claim: the delta reconstructed the snapshot
                self.assertEqual(c.view, served(o.snapshot()))

    def test_nothing_outside_the_composed_view_can_bump_the_version(self):
        """The other end of the same invariant, and the half that keeps it true
        tomorrow. The three terms above ride every frame; a FOURTH would not,
        and would be exactly this bug again. So the terms are pinned closed:
        every other top-level field of `collect_state`'s result moves without
        moving `v`, because a client either derives it (`free_worktrees`),
        fetches it beside the stream (`hostname`, `user`), or already has it on
        every frame (`generated_at`, which rides as `at` with no vote).

        Add a term to `publish` without adding it to `delta_since` and this
        test says so before a phone does."""
        for field, value in [("hostname", "elsewhere"), ("user", "someone"),
                             ("free_worktrees", ["alpha"]),
                             ("generated_at", 1001.0),
                             ("resumes", {"alpha": 1}), ("a_field_from_2027", 1)]:
            with self.subTest(field=field):
                o = fb.Observer()
                first = ordered_state(1000.0, [("alpha", 0, "working")])
                first.update(hostname="here", user="me", free_worktrees=[],
                             resumes={}, a_field_from_2027=0)
                o.publish(first)
                after = dict(first, generated_at=1001.0)
                after[field] = value
                self.assertEqual(o.publish(after).v, 1,
                                 f"{field} moved v but rides no frame")

    def test_a_client_that_lagged_catches_up_in_one_delta(self):
        """The suspended-phone path (ADR 0004): four versions happen while the
        client is away, and the single delta it asks for lands it exactly where
        a fresh snapshot would."""
        o, c = fb.Observer(), Client()
        o.publish(ordered_state(1000.0, [("alpha", 0, "working")]))
        c.apply(o.delta_since(0))
        away = c.v
        for i, cards in enumerate([
                [("alpha", 1, "working"), ("beta", 0, "working")],
                [("alpha", 1, "working"), ("beta", 1, "working")],
                [("beta", 1, "needs_input"), ("alpha", 1, "working")],
                [("beta", 1, "needs_input")]]):
            o.publish(ordered_state(1001.0 + i, cards))
        self.assertEqual(o.snapshot().v, away + 4)
        d = o.delta_since(away)
        self.assertEqual(d["type"], "delta")
        self.assertEqual(d["v"], away + 4, "one delta covers every version")
        c.apply(d)
        self.assertEqual(c.view, served(o.snapshot()))

    def test_a_delta_can_span_more_than_one_version(self):
        """The stream's loop waits on the version and THEN asks for a delta, so
        publishes that land in between are coalesced into one frame. A client
        that treated `v != base + 1` as a lost frame would resync on every
        busy moment — the gap test is `base`, and only `base`."""
        o, c = fb.Observer(), Client()
        o.publish(ordered_state(1000.0, [("alpha", 0, "working")]))
        c.apply(o.delta_since(0))
        for i in range(3):
            o.publish(ordered_state(1001.0 + i, [("alpha", i + 1, "working")]))
        d = o.delta_since(c.v)
        self.assertEqual(d["base"], c.v)
        self.assertGreater(d["v"], d["base"] + 1)
        c.apply(d)
        self.assertEqual(c.view, served(o.snapshot()))


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
        def collect(fresh=None, git=None, cold=False, settle=None):
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

    def test_a_request_between_sweeps_does_not_collect_on_the_request_thread(self):
        """`idle_s` 30 with a 4 s cache means 26 s of every 30 collect on the
        request thread — the exact cost the sweep exists to remove.

        Measured before this was fixed: 13 of 30 one-second polls ran a full
        collect_state, and /api/state on the nine-worktree fleet answered in
        8-17 s instead of 0.7 ms. The cache is refreshed at the END of a sweep,
        so what the request path may trust is the cadence PLUS a sweep, not
        STATE_TTL_S — which was only ever right while `idle_s` was 3.0.
        """
        self._stub()
        o = fb.Observer(idle_s=30.0, hot_s=0.0, watch=False, idle_blind_s=30.0)
        self.addCleanup(o.stop)
        fb.observer._observer = o
        o.start()
        self.assertIsNotNone(o.wait_for(0, timeout=5))
        # a poll landing well past STATE_TTL_S but well inside the cadence
        self.assertGreater(o.republish_s, 3 * fb.observer.STATE_TTL_S)
        fb._cache["t"] = time.time() - (fb.observer.STATE_TTL_S + 2.0)
        before = len(self.calls)
        fb.cached_state()
        self.assertEqual(len(self.calls), before,
                         "a poll between sweeps collected on the request thread")
        # …and the safety net is intact: past the thread's own promise, and for
        # a mutation that parks the cache, the request still collects
        fb._cache["t"] = time.time() - (o.republish_s + 1.0)
        fb.cached_state()
        self.assertEqual(len(self.calls), before + 1)
        fb._cache["t"] = 0.0                          # what finish() parks
        fb.cached_state()
        self.assertEqual(len(self.calls), before + 2)

    def test_a_stopped_thread_hands_the_request_path_back_to_state_ttl(self):
        """`running` is read per request, never latched: an Observer that died
        must not leave the request path trusting a cache nobody refreshes."""
        self._stub()
        o = fb.Observer(idle_s=30.0, hot_s=0.0, watch=False, idle_blind_s=30.0)
        fb.observer._observer = o
        o.start()
        self.assertIsNotNone(o.wait_for(0, timeout=5))
        o.stop()
        before = len(self.calls)
        fb._cache["t"] = time.time() - (fb.observer.STATE_TTL_S + 2.0)
        fb.cached_state()
        self.assertEqual(len(self.calls), before + 1,
                         "a dead sweep left the request path trusting its cache")

    def test_a_parked_invalidation_survives_an_in_flight_sweep(self):
        """finish() parks _cache["t"] = 0.0 mid-sweep. A sweep that started
        BEFORE the mutation must not paper over it with pre-mutation data."""
        started = threading.Event()
        release = threading.Event()

        def collect(fresh=None, git=None, cold=False, settle=None):
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

        def collect(fresh=None, git=None, cold=False, settle=None):
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

        def collect(fresh=None, git=None, cold=False, settle=None):
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


# ---------------------------------------------------------- cadences as config

# The five knobs and where each is read. `git_s` lands on the GitCadence rather
# than the Observer, which is exactly the kind of asymmetry a table catches and
# five hand-written asserts do not.
CADENCES = [
    ("idle_s", "IDLE_S", lambda o: o.idle_s),
    ("idle_blind_s", "IDLE_BLIND_S", lambda o: o.idle_blind_s),
    ("hot_s", "HOT_S", lambda o: o.hot_s),
    ("git_s", "GIT_S", lambda o: o._git.every_s),
    ("reconcile_s", "RECONCILE_S", lambda o: o.reconcile_s),
    ("max_stale_s", "MAX_STALE_S", lambda o: o.max_stale_s),
]


class TestCadencesAreConfig(CacheGuard):
    """Phase 3: the loop's cadences are settings, not constants.

    `idle_s` is the knob a user reaches for — the battery/latency trade, 17 %
    of a core at 3.0 against 28 % at 1.0 on the fleet these were measured on —
    and the right value depends on whose laptop it is. The other four come
    along because a loop with one tunable cadence and four hardcoded ones is a
    loop nobody can reason about, and because `git_s` turns out to move the
    bill further than `idle_s` does.
    """

    def setUp(self):
        super().setUp()
        self._cfg = dict(fb.CFG)
        self._cpath = fb.config.CONFIG_PATH

    def tearDown(self):
        fb.CFG.clear()
        fb.CFG.update(self._cfg)     # CFG is mutated in place, never rebound
        fb.config.CONFIG_PATH = self._cpath
        super().tearDown()

    def test_every_cadence_is_a_config_key(self):
        for key, _const, read in CADENCES:
            with self.subTest(key=key):
                fb.CFG[key] = 4.25
                self.assertEqual(read(fb.Observer()), 4.25)

    def test_an_explicit_argument_still_beats_the_file(self):
        """The tests drive this loop at cadences no user would choose; a config
        key that overrode its own caller would make every timing test a
        function of whatever is on disk."""
        for key, _const, read in CADENCES:
            with self.subTest(key=key):
                fb.CFG[key] = 4.25
                self.assertEqual(read(fb.Observer(**{key: 0.5})), 0.5)

    def test_zero_is_a_value_and_not_a_missing_setting(self):
        """`or` instead of `is None` would swap a deliberate 0.0 for whatever
        is behind it, and the mistake would be invisible: the loop would simply
        run at 3.0 while the setting said 0.0.

        The explicit-0.0 case needs the config key set to something ELSE, or
        `given or CFG[key]` falls through to an identical 0.0 and the bug reads
        as correct — which is exactly what the first version of this test did.
        """
        for key, _const, read in CADENCES:
            with self.subTest(key=key):
                fb.CFG[key] = 0.0                       # 0 from the file
                self.assertEqual(read(fb.Observer()), 0.0)
                fb.CFG[key] = 9.0                       # …and 0 from the caller
                self.assertEqual(read(fb.Observer(**{key: 0.0})), 0.0)

    def test_the_defaults_on_disk_are_the_measured_constants(self):
        """observer.py carries the measurements next to the constants (ADR
        0011). If CFG's defaults drift from them, the documented numbers stop
        describing what actually runs."""
        for key, const, _read in CADENCES:
            with self.subTest(key=key):
                self.assertEqual(self._cfg[key], getattr(fb.observer, const))

    def test_a_config_file_on_disk_reaches_the_loop(self):
        """The round trip a user actually performs: edit orchestra.config.json,
        start the board, get that cadence."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "orchestra.config.json"
            p.write_text(json.dumps({"idle_s": 1.5, "idle_blind_s": 0.4,
                                     "hot_s": 0.05,
                                     "git_s": 30.0, "reconcile_s": 20.0,
                                     "max_stale_s": 2.0}))
            fb.load_config(["--config", str(p)])
            self.assertEqual(fb.config.CONFIG_PATH, p)
            o = fb.Observer()
            self.assertEqual([read(o) for _k, _c, read in CADENCES],
                             [1.5, 0.4, 0.05, 30.0, 20.0, 2.0])
            # …and the running loop can be asked what it picked up, so "did my
            # edit take?" is answerable without reading the file back
            st = o.stats()
            self.assertEqual([st["idle_s"], st["idle_blind_s"], st["hot_s"],
                              st["git_s"], st["reconcile_s"], st["max_stale_s"]],
                             [1.5, 0.4, 0.05, 30.0, 20.0, 2.0])

    def test_idle_s_has_a_flag_because_it_is_the_battery_knob(self):
        """`--idle-s` beats the file, like every other flag. The other four are
        deliberately file-only, so asking for them is an argparse error rather
        than a silently ignored word."""
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "orchestra.config.json"
            p.write_text(json.dumps({"idle_s": 1.5}))
            fb.load_config(["--config", str(p), "--idle-s", "0.75"])
            self.assertEqual(fb.CFG["idle_s"], 0.75)
            self.assertEqual(fb.Observer().idle_s, 0.75)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                fb.load_config(["--hot-s", "0.9"])

    def test_a_cadence_is_a_float_however_it_was_written(self):
        """JSON says 2, `--idle-s` says '2'; the loop does arithmetic on it."""
        fb.CFG["idle_s"] = 2
        self.assertIsInstance(fb.Observer().idle_s, float)


# ---------------------------------------------------------------- anti-flicker

class TestSettlerDampsOnlyTheWayDown(CacheGuard):
    """ENGINE.md §6.3(a) with the state it needs. `status.settle` is the rule
    and is pinned in test_orchestra; this is about the memory — who owns it,
    what it forgets, and that the board is composed from what it publishes."""

    def _sessions(self, st, sid="s1"):
        return {"/wt": [{"sid": sid, "status": st}]}

    def test_a_de_escalation_waits_for_the_dwell_and_then_lands(self):
        s = fb.Settler(dwell_s=3.0)
        now = 1000.0
        ss = self._sessions("working")
        s.apply(ss, now)
        self.assertEqual(ss["/wt"][0]["status"], "working")
        ss = self._sessions("waiting")
        s.apply(ss, now + 2.9)
        self.assertEqual(ss["/wt"][0]["status"], "working")   # held
        self.assertEqual(s.stats()["settle_held"], 1)
        ss = self._sessions("waiting")
        s.apply(ss, now + 3.0)
        self.assertEqual(ss["/wt"][0]["status"], "waiting")

    def test_an_escalation_is_never_delayed(self):
        s = fb.Settler(dwell_s=600.0)
        now = 1000.0
        s.apply(self._sessions("working"), now)
        ss = self._sessions("needs_input")
        s.apply(ss, now + 0.001)
        self.assertEqual(ss["/wt"][0]["status"], "needs_input")
        self.assertEqual(s.stats()["settle_held"], 0)

    def test_a_session_that_leaves_the_board_is_forgotten(self):
        # …or a session returning after two days would inherit a status from
        # another day, and the rule is about CONSECUTIVE sweeps.
        s = fb.Settler(dwell_s=600.0)
        now = 1000.0
        s.apply(self._sessions("working"), now)
        s.apply({"/wt": []}, now + 1)
        self.assertEqual(s.stats()["settle_tracked"], 0)
        ss = self._sessions("ended")
        s.apply(ss, now + 2)
        self.assertEqual(ss["/wt"][0]["status"], "ended")     # no stale dwell

    def test_the_dwell_is_a_config_key(self):
        saved = fb.CFG.get("flicker_dwell_s")
        try:
            fb.CFG["flicker_dwell_s"] = 12.5
            self.assertEqual(fb.Settler().dwell_s, 12.5)
            self.assertEqual(fb.Settler(dwell_s=0.5).dwell_s, 0.5)  # arg still wins
        finally:
            if saved is None:
                fb.CFG.pop("flicker_dwell_s", None)
            else:
                fb.CFG["flicker_dwell_s"] = saved

    def test_the_sweep_publishes_through_its_own_settler(self):
        state = {"v": fake_state(time.time())}

        def collect(fresh=None, git=None, cold=False, settle=None):
            st = dict(state["v"])
            if settle is not None:
                settle({"/wt": st["worktrees"][0]["sessions"]}, time.time())
            return st

        saved = fb.observer.collect_state
        fb.observer.collect_state = collect
        try:
            o = fb.Observer(dwell_s=600.0)
            o.sweep()
            self.assertEqual(o.snapshot().cards["alpha"]["sessions"][0]["status"],
                             "working")
            state["v"] = fake_state(time.time() + 1, status="waiting")
            o.sweep()
            self.assertEqual(o.snapshot().cards["alpha"]["sessions"][0]["status"],
                             "working")      # damped, by the sweep's own memory
            self.assertEqual(o.stats()["settle_held"], 1)
        finally:
            fb.observer.collect_state = saved


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestSettlingHappensBeforeTheBoardIsComposed(CacheGuard):
    """A held status has to be held everywhere. Settle after the cards are
    built and the session says ● WORKING while the counts strip says ○ ENDED
    and the card says FREE — which is the one that feeds dispatch targeting."""

    def _age_out(self, tmp):
        old = time.time() - 10000
        for fp in (tmp / "home" / "projects").rglob("*.jsonl"):
            os.utime(fp, (old, old))

    def test_the_counts_and_the_availability_agree_with_the_held_status(self):
        with FleetFixture() as fx:
            s = fb.Settler(dwell_s=600.0)
            st = fb.collect_state(settle=s.apply)
            self.assertEqual(st["counts"]["working"], 2)
            self._age_out(fx.tmp)
            fb.memo_clear()
            st = fb.collect_state(settle=s.apply)
            card = st["worktrees"][0]
            self.assertEqual(card["sessions"][0]["status"], "working")   # held
            self.assertEqual(st["counts"], {"working": 2, "needs_input": 0,
                                            "limit": 0, "blocked": 0,
                                            "waiting": 0, "ended": 0})
            self.assertEqual(card["availability"], "busy")
            self.assertEqual(st["free_worktrees"], [])

    def test_the_request_path_shares_the_sweeps_memory(self):
        # Two clocks for one status is the bug: a collect on the request thread
        # that skipped the settler would show the user an un-damped status and
        # leave the sweep's memory disagreeing with what was just rendered.
        with FleetFixture() as fx:
            o = fb.Observer(dwell_s=600.0)
            fb.observer._observer = o
            self.assertEqual(
                fb.cached_state()["worktrees"][0]["sessions"][0]["status"], "working")
            self._age_out(fx.tmp)
            fb.memo_clear()
            fb._cache["state"], fb._cache["t"] = None, 0.0
            self.assertEqual(
                fb.cached_state()["worktrees"][0]["sessions"][0]["status"], "working")
            self.assertEqual(o._settler.stats()["settle_held"], 2)


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
        fb.procs.claude_processes = lambda **_: []
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
