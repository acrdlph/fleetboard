#!/usr/bin/env python3
"""ACTUATE fixes — §5.6 resource locks and the tmux flag terminator.

    python3 -m unittest tests.test_fixes_actuate -v

Covers: the per-worktree dispatch reservation (accept-path refusal, pick-lock
subtraction, release on failure, TTL), the per-worktree finish lock, the
per-op tmux paste buffer under the global buffer lock, the `--` end-of-options
sentinel on literal tmux sends, and the atomic + loud resume.schedule.json
persistence. Every test here failed before the fix it pins.
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402


# ------------------------------------------------- worktree reservations

class TestWorktreeReservation(unittest.TestCase):
    """§5.6: one in-flight dispatch per worktree, TTL as the crash net."""

    def setUp(self):
        fb.dispatch._wt_reservations.clear()

    def tearDown(self):
        fb.dispatch._wt_reservations.clear()

    def test_second_hold_is_refused_while_the_first_lives(self):
        self.assertTrue(fb.dispatch._reserve_worktree("alpha", now=1000.0))
        self.assertFalse(fb.dispatch._reserve_worktree("alpha", now=1001.0))

    def test_release_frees_the_hold(self):
        fb.dispatch._reserve_worktree("alpha", now=1000.0)
        fb.dispatch._release_worktree("alpha")
        self.assertTrue(fb.dispatch._reserve_worktree("alpha", now=1001.0))

    def test_ttl_expiry_frees_the_hold_by_itself(self):
        fb.dispatch._reserve_worktree("alpha", now=1000.0)
        self.assertTrue(fb.dispatch._reserve_worktree(
            "alpha", now=1000.0 + fb.dispatch.WT_RESERVE_TTL_S + 1))

    def test_different_worktrees_do_not_contend(self):
        self.assertTrue(fb.dispatch._reserve_worktree("alpha", now=1000.0))
        self.assertTrue(fb.dispatch._reserve_worktree("beta", now=1000.0))


class TestAutoPickSubtractsReservations(unittest.TestCase):
    """Two auto-picks off the same snapshot must not land in one worktree."""

    STATE = {"worktrees": [
        {"name": "a", "availability": "free", "git": {"dirty": 0}},
        {"name": "b", "availability": "free", "git": {"dirty": 3}},
        {"name": "busy", "availability": "busy", "git": {"dirty": 0}},
    ]}

    def setUp(self):
        fb.dispatch._wt_reservations.clear()
        self._saved = (fb.observer.cached_state, fb.limits.cached_limits,
                       fb.limits.limits_by_account)
        fb.observer.cached_state = lambda: {
            "worktrees": [dict(w, git=dict(w["git"])) for w in self.STATE["worktrees"]]}
        fb.limits.cached_limits = lambda: {"available": True}
        fb.limits.limits_by_account = lambda: {
            "acct": {"available": True, "headroom": 50}}

    def tearDown(self):
        (fb.observer.cached_state, fb.limits.cached_limits,
         fb.limits.limits_by_account) = self._saved
        fb.dispatch._wt_reservations.clear()

    def test_two_picks_choose_two_different_worktrees(self):
        wt1, _ = fb.dispatch._pick_defaults()
        wt2, _ = fb.dispatch._pick_defaults()
        self.assertEqual(wt1, "a")          # cleanest free, as before
        self.assertEqual(wt2, "b")          # NOT "a" again — "a" is reserved
        wt3, _ = fb.dispatch._pick_defaults()
        self.assertIsNone(wt3)              # everything free is now held

    def test_the_pick_reserves_itself(self):
        fb.dispatch._pick_defaults()
        self.assertIn("a", fb.dispatch._wt_reservations)

    def test_an_explicit_reservation_hides_the_worktree_from_the_picker(self):
        fb.dispatch._reserve_worktree("a")
        wt, _ = fb.dispatch._pick_defaults()
        self.assertEqual(wt, "b")

    def test_pick_worktree_false_picks_no_worktree_and_reserves_nothing(self):
        wt, acct = fb.dispatch._pick_defaults(pick_worktree=False)
        self.assertIsNone(wt)
        self.assertEqual(acct, "acct")
        self.assertEqual(fb.dispatch._wt_reservations, {})


class TestStartDispatchAcceptPathLock(unittest.TestCase):
    """The reservation is taken synchronously, before any worker thread."""

    def setUp(self):
        fb.dispatch._wt_reservations.clear()
        self._demo = fb.config.DEMO
        fb.config.DEMO = False
        self._run = fb.dispatch._run_dispatch
        # a worker that never settles: the job hangs "in flight" so the second
        # accept must be refused by the reservation alone, not by timing
        fb.dispatch._run_dispatch = lambda *a, **kw: None

    def tearDown(self):
        fb.dispatch._run_dispatch = self._run
        fb.config.DEMO = self._demo
        fb.dispatch._wt_reservations.clear()

    def test_second_dispatch_for_the_same_worktree_is_refused(self):
        out1 = fb.start_dispatch("close out", worktree="w1",
                                 closeout_trunk="origin/main")
        self.assertIn("job", out1)
        self.assertIn("w1", fb.dispatch._wt_reservations)   # held before spawn
        out2 = fb.start_dispatch("close out", worktree="w1",
                                 closeout_trunk="origin/main")
        self.assertFalse(out2.get("ok", True))
        self.assertNotIn("job", out2)
        self.assertIn("already in flight", out2["message"])

    def test_two_different_worktrees_both_dispatch(self):
        out1 = fb.start_dispatch("close out", worktree="w1",
                                 closeout_trunk="origin/main")
        out2 = fb.start_dispatch("close out", worktree="w2",
                                 closeout_trunk="origin/main")
        self.assertIn("job", out1)
        self.assertIn("job", out2)


class TestRunDispatchReleasesOnFailure(unittest.TestCase):
    """A failed worker frees its hold; §5.6: every lock released, every path."""

    def setUp(self):
        fb.dispatch._wt_reservations.clear()
        self._demo = fb.config.DEMO
        fb.config.DEMO = False
        self._wts = fb.gitrepo.discover_worktrees
        fb.gitrepo.discover_worktrees = lambda: []

    def tearDown(self):
        fb.gitrepo.discover_worktrees = self._wts
        fb.config.DEMO = self._demo
        fb.dispatch._wt_reservations.clear()

    def job(self):
        return {"progress": [], "done": False, "result": None}

    def test_unknown_worktree_failure_releases_the_hold(self):
        fb.dispatch._reserve_worktree("wtz")
        job = self.job()
        fb.dispatch._run_dispatch(job, "mission", "wtz", "acct", "opus", "high")
        self.assertFalse(job["result"]["ok"])
        self.assertNotIn("wtz", fb.dispatch._wt_reservations)

    def test_a_crashing_worker_still_settles_and_releases(self):
        fb.gitrepo.discover_worktrees = lambda: (_ for _ in ()).throw(
            RuntimeError("boom"))
        fb.dispatch._reserve_worktree("wtz")
        job = self.job()
        fb.dispatch._run_dispatch(job, "mission", "wtz", "acct", "opus", "high")
        self.assertTrue(job["done"])                       # board never hangs
        self.assertFalse(job["result"]["ok"])
        self.assertNotIn("wtz", fb.dispatch._wt_reservations)


# ------------------------------------------------------ finish worktree lock

class TestFinishLock(unittest.TestCase):
    """Two concurrent ✓ finish for one worktree: one runs, one clean refusal."""

    def setUp(self):
        self._demo = fb.config.DEMO
        fb.config.DEMO = False
        fb.finish._finish_locks.clear()

    def tearDown(self):
        fb.finish._finish_locks.clear()
        fb.config.DEMO = self._demo

    def test_a_press_while_one_is_in_flight_is_refused(self):
        lk = threading.Lock()
        lk.acquire()                        # somebody is mid-finish
        fb.finish._finish_locks["wt-locked"] = lk
        out = fb.start_finish("wt-locked")
        self.assertFalse(out["ok"])
        self.assertIn("already in progress", out["message"])
        lk.release()

    def test_the_lock_is_released_after_the_run(self):
        saved = fb.gitrepo.discover_worktrees
        fb.gitrepo.discover_worktrees = lambda: []
        try:
            out = fb.start_finish("wt-free")
            self.assertFalse(out["ok"])                     # unknown worktree
            self.assertIn("unknown worktree", out["message"])
            lk = fb.finish._finish_locks["wt-free"]
            self.assertTrue(lk.acquire(blocking=False))     # not left held
            lk.release()
        finally:
            fb.gitrepo.discover_worktrees = saved

    def test_locks_are_per_worktree(self):
        lk = threading.Lock()
        lk.acquire()
        fb.finish._finish_locks["wt-a"] = lk
        saved = fb.gitrepo.discover_worktrees
        fb.gitrepo.discover_worktrees = lambda: []
        try:
            out = fb.start_finish("wt-b")   # a different worktree still runs
            self.assertIn("unknown worktree", out["message"])
        finally:
            fb.gitrepo.discover_worktrees = saved
            lk.release()


# --------------------------------------------------- per-op tmux paste buffer

class TestDeliverTextBuffer(unittest.TestCase):
    """§5.6's severe hazard: a shared buffer name lets A paste B's brief."""

    def setUp(self):
        self._run, self._sleep = fb.shell.run, fb.time.sleep
        fb.time.sleep = lambda s: None
        self.calls = []
        self.paste_rc = 0

        def fake_run(cmd, **kw):
            self.calls.append(cmd)
            if "capture-pane" in cmd:
                return 0, "❯ \n"            # bare composer: send proven
            if "paste-buffer" in cmd:
                return self.paste_rc, ""
            return 0, ""
        fb.shell.run = fake_run

    def tearDown(self):
        fb.shell.run, fb.time.sleep = self._run, self._sleep

    def buffer_names(self):
        return [c[c.index("-b") + 1] for c in self.calls if "set-buffer" in c]

    def test_each_delivery_names_its_own_buffer(self):
        fb.deliver_text("sess-a", "brief A")
        fb.deliver_text("sess-b", "brief B")
        names = self.buffer_names()
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])
        self.assertNotIn("orchestra-kickoff", names)   # the shared name is gone

    def test_paste_names_the_same_buffer_the_set_filled(self):
        fb.deliver_text("sess", "brief")
        set_cmd = next(c for c in self.calls if "set-buffer" in c)
        paste_cmd = next(c for c in self.calls if "paste-buffer" in c)
        self.assertEqual(set_cmd[set_cmd.index("-b") + 1],
                         paste_cmd[paste_cmd.index("-b") + 1])

    def test_the_literal_text_sits_behind_a_dash_dash(self):
        # a dash-leading mission must not be read as set-buffer flags
        fb.deliver_text("sess", "-N 30 y")
        set_cmd = next(c for c in self.calls if "set-buffer" in c)
        self.assertEqual(set_cmd[-2:], ["--", "-N 30 y"])

    def test_failed_paste_returns_false_and_never_presses_enter(self):
        # before: the rc was ignored, Enter was pressed on a composer the text
        # never reached, and the bare prompt made the lost brief read as sent
        self.paste_rc = 1
        self.assertFalse(fb.deliver_text("sess", "brief"))
        enters = [c for c in self.calls if c[-1:] == ["Enter"]]
        self.assertEqual(enters, [])


# ------------------------------------------------ tmux flag terminator (send)

class TestSendToProcessDashDash(unittest.TestCase):
    """A chat reply of '-l ok' is a message, not send-keys flags."""

    def setUp(self):
        self._demo = fb.config.DEMO
        fb.config.DEMO = False
        self._resolve, self._run = fb.identity.resolve, fb.shell.run
        fb.identity.resolve = lambda pid, **ident: (
            {"pid": 1, "tmux_target": "sess:0.1", "tmux_sock": "fleet",
             "host": "tmux", "host_kind": "tmux", "tty": None}, None)
        self.calls = []
        fb.shell.run = lambda cmd, **kw: (self.calls.append(cmd), (0, ""))[1]

    def tearDown(self):
        fb.identity.resolve, fb.shell.run = self._resolve, self._run
        fb.config.DEMO = self._demo

    def test_dash_leading_text_is_sent_literally(self):
        res = fb.send_to_process(1, "-l ok", worktree="w")
        self.assertTrue(res["ok"])
        literal = next(c for c in self.calls if "-l" in c and "Enter" not in c)
        self.assertEqual(literal[-2:], ["--", "-l ok"])


# ---------------------------------------------- resume persistence durability

class TestResumeSaveDurability(unittest.TestCase):
    """resume.schedule.json: atomic replace, corrupt files set aside loudly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="fb-resfix-")
        self._state = fb.resume.RESUME_STATE
        fb.resume.RESUME_STATE = Path(self.tmpdir) / "resume.schedule.json"
        self._resumes = dict(fb._resumes)
        fb._resumes.clear()

    def tearDown(self):
        fb._resumes.clear()
        fb._resumes.update(self._resumes)
        fb.resume.RESUME_STATE = self._state

    def test_save_roundtrips_and_leaves_no_tmp_behind(self):
        fb._resumes["wt|s1"] = {"worktree": "wt", "sid": "s1",
                                "account": "main", "status": "pending"}
        fb.save_resumes()
        data = json.loads(fb.resume.RESUME_STATE.read_text())
        self.assertEqual(data["schedules"][0]["sid"], "s1")
        leftovers = list(Path(self.tmpdir).glob("*.tmp"))
        self.assertEqual(leftovers, [])
        fb._resumes.clear()
        fb.load_resumes()
        self.assertIn("wt|s1", fb._resumes)

    def test_a_corrupt_file_is_moved_aside_not_silently_discarded(self):
        # a truncated write (crash mid-save) used to vanish every armed 3am
        # resume with no trace; now the evidence survives as .bad
        fb.resume.RESUME_STATE.write_text('{"schedules": [{"wor')
        fb.load_resumes()
        self.assertEqual(fb._resumes, {})
        self.assertFalse(fb.resume.RESUME_STATE.exists())
        bad = fb.resume.RESUME_STATE.with_name(
            fb.resume.RESUME_STATE.name + ".bad")
        self.assertTrue(bad.exists())

    def test_an_empty_file_is_left_alone(self):
        fb.resume.RESUME_STATE.write_text("")
        fb.load_resumes()
        self.assertEqual(fb._resumes, {})
        bad = fb.resume.RESUME_STATE.with_name(
            fb.resume.RESUME_STATE.name + ".bad")
        self.assertFalse(bad.exists())      # nothing to preserve, no noise


if __name__ == "__main__":
    unittest.main()
