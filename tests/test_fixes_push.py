#!/usr/bin/env python3
"""Wave-2 PUSH fixes — the event/push pipeline edges that were starving.

Covers the four HIGHs and the named mediums from the PUSH brief:

  * F1  the pipeline runs on a CADENCE, not only on a version bump — a dwell
        held push released by a later observe of the same projection, and
        `push_loop`'s timeout branch actually re-observing.
  * F2  a 410 / Unregistered PRUNES the token, and every attempt (success or
        failure) is recorded via `note_push`.
  * F3  `session.died` is derived, and dispatch is wired into the live sources.
  * F4  the first observe after start re-baselines silently — no restart storm.
  * F5  the device's registered APNs environment is honoured on fan-out, and a
        heal is persisted.
  * F6  a NoopSink is exempt from backoff / failure accounting.
  * F7  `hooks.install()` rewrites the script atomically and skips a no-op.
  * F8  a pending arm is cancelled on the SPECIFIC status it asserts.
  * F9  the events log is created 0o600.

    python3 -m unittest tests.test_fixes_push -v
"""

import os
import stat as statmod
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import orchestra as fb  # noqa: E402
from orchestra import notify, push, hooks  # noqa: E402
from orchestra.notify import (project, derive, Notifier, EventLog, Preferences,
                              Budget, Service)  # noqa: E402


def proj(sessions=None, worktrees=None, accounts=None, resumes=None,
         dispatch=None):
    return {"sessions": sessions or {}, "worktrees": worktrees or {},
            "accounts": accounts or {}, "resumes": resumes or {},
            "dispatch": dispatch or {}}


def sess(status, worktree="wt", account="acct", model="opus", dirty=False,
         handed_to=None):
    return {"status": status, "worktree": worktree, "account": account,
            "model": model, "topic": "t", "handed_to": handed_to,
            "dirty": dirty}


class RecordingSink:
    """A stand-in APNs sink: records every send, its environment, and answers a
    configurable Response. Carries `.last` and `.healed_environment` like the
    real APNsSink so the Service's prune/heal path can read them."""

    name = "apns"

    def __init__(self, response=None, heal_to=None):
        self._resp = response or push.Response(status=200, apns_id="A")
        self.heal_to = heal_to
        self.healed_environment = None
        self.last = None
        self.sends = []

    def send(self, token, payload, environment=None, **kw):
        self.sends.append({"token": token, "environment": environment,
                           "payload": payload})
        self.healed_environment = self.heal_to
        self.last = self._resp
        return self._resp


# ----------------------------------------------------------------- F3 derive

class TestDiedDerivation(unittest.TestCase):

    def test_a_recently_working_session_that_vanishes_dirty_dies(self):
        p1 = proj(sessions={"s1": sess("working", dirty=True)})
        p2 = proj()   # gone
        evs = derive(p1, p2, now=1.0)
        self.assertEqual([e.type for e in evs], ["session.died"])
        self.assertEqual(evs[0].worktree, "wt")
        self.assertEqual(evs[0].dedupe_key, "session.died|s1")

    def test_a_clean_exit_is_silent(self):
        """The dirty gate is the whole point: a landed, committed session that
        ends is the normal end of a turn, not a crash."""
        p1 = proj(sessions={"s1": sess("working", dirty=False)})
        p2 = proj()
        self.assertEqual(derive(p1, p2, now=1.0), [])

    def test_ending_dirty_dies_too(self):
        p1 = proj(sessions={"s1": sess("blocked", dirty=True)})
        p2 = proj(sessions={"s1": sess("ended", dirty=True)})
        self.assertEqual([e.type for e in derive(p1, p2, now=1.0)],
                         ["session.died"])

    def test_a_still_present_session_does_not_die(self):
        p1 = proj(sessions={"s1": sess("working", dirty=True)})
        p2 = proj(sessions={"s1": sess("working", dirty=True)})
        self.assertEqual(derive(p1, p2, now=1.0), [])

    def test_an_idle_session_that_vanishes_is_not_a_death(self):
        """Only a RECENTLY-alive session's disappearance is a death; a session
        already `waiting`/idle that goes away was not crashed mid-work."""
        p1 = proj(sessions={"s1": sess("waiting", dirty=True)})
        p2 = proj()
        self.assertEqual(derive(p1, p2, now=1.0), [])

    def test_it_fires_exactly_once_across_the_next_sweep(self):
        """After the death sweep, `prev` no longer holds the session, so it is
        not re-derived every subsequent quiet sweep."""
        p1 = proj(sessions={"s1": sess("working", dirty=True)})
        p2 = proj()
        first = derive(p1, p2, now=1.0)
        second = derive(p2, p2, now=2.0)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])


class TestProjectionDirtyGate(unittest.TestCase):

    def test_project_reads_dirty_from_card_git(self):
        class Snap:
            cards = {"wt": {"name": "wt", "availability": "busy",
                            "git": {"dirty": 3, "ahead": 0},
                            "sessions": [{"sid": "s1", "status": "working"}]}}
        p = project(Snap())
        self.assertTrue(p["sessions"]["s1"]["dirty"])

    def test_unlanded_counts_as_dirty(self):
        class Snap:
            cards = {"wt": {"name": "wt", "availability": "busy",
                            "git": {"dirty": 0, "ahead": 2},
                            "sessions": [{"sid": "s1", "status": "working"}]}}
        self.assertTrue(project(Snap())["sessions"]["s1"]["dirty"])

    def test_a_clean_landed_worktree_is_not_dirty(self):
        class Snap:
            cards = {"wt": {"name": "wt", "availability": "busy",
                            "git": {"dirty": 0, "ahead": 0},
                            "sessions": [{"sid": "s1", "status": "working"}]}}
        self.assertFalse(project(Snap())["sessions"]["s1"]["dirty"])


# --------------------------------------------------------------- F8 / F1 QC

class TestPendingCancel(unittest.TestCase):

    def sink_notifier(self):
        self.sink = RecordingSink()
        return Notifier(sink=self.sink, log=EventLog(), budget=Budget())

    def test_a_block_that_becomes_a_question_does_not_double_fire(self):
        """F8: the blocked arm (dwell 40 s) must be cancelled when the block
        turns into a QUESTION — the needs_answer already pushed, and a second
        P1 claiming 'is blocked' for the same session is the mislabelled false
        alarm the fix removes."""
        n = self.sink_notifier()
        n.observe(proj(sessions={"s1": sess("working")}), now=1000.0,
                  device_token="ab")
        n.observe(proj(sessions={"s1": sess("blocked")}), now=1001.0,
                  device_token="ab")            # arm blocked (dwell 40)
        n.observe(proj(sessions={"s1": sess("needs_input")}), now=1010.0,
                  device_token="ab")            # becomes a question, pushes P1
        n.observe(proj(sessions={"s1": sess("needs_input")}), now=1060.0,
                  device_token="ab")            # past the blocked dwell
        types = [s["payload"]["ev"] for s in self.sink.sends]
        self.assertEqual(types, ["session.needs_answer"],
                         "the stale blocked arm must not fire a second P1")

    def test_a_dwell_is_released_by_a_later_observe_of_the_same_projection(self):
        """F1: the cadence guarantee at the QC layer — a blocked condition that
        HOLDS fires on a later observe that carries no new version, which is
        exactly what `push_loop`'s timeout branch now produces on a quiet
        board."""
        n = self.sink_notifier()
        held = proj(sessions={"s1": sess("blocked")})
        n.observe(proj(sessions={"s1": sess("working")}), now=1000.0,
                  device_token="ab")
        n.observe(held, now=1001.0, device_token="ab")     # arm
        self.assertEqual(self.sink.sends, [])              # still dwelling
        n.observe(held, now=1050.0, device_token="ab")     # same proj, later
        self.assertEqual(len(self.sink.sends), 1)


# ----------------------------------------------------------- F6 NoopSink

class TestNoopSinkExemption(unittest.TestCase):

    def test_a_noop_sink_counts_as_would_be_sent_and_never_backs_off(self):
        n = Notifier(sink=push.NoopSink("no key"), log=EventLog())
        n.observe(proj(sessions={"s1": sess("working")}), now=1000.0,
                  device_token="ab")
        pushed = n.observe(proj(sessions={"s1": sess("needs_input")}),
                           now=1001.0, device_token="ab")
        self.assertEqual(len(pushed), 1, "a NoopSend must count as would-be-sent")
        self.assertFalse(n.backoff.blocked(1002.0),
                         "a NoopSink's status-0 must not arm the shared backoff")


# --------------------------------------------------------- F2 / F4 / F5 Service

class _ServiceCase(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fb-push-fix-"))
        self._saved = (fb.auth.REGISTRY, fb.auth.AUDIT_LOG)
        fb.auth.REGISTRY = self.dir / "devices.json"
        fb.auth.AUDIT_LOG = self.dir / "audit.log.jsonl"
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        self._real_sink = push.sink

    def tearDown(self):
        push.sink = self._real_sink
        fb.auth.REGISTRY, fb.auth.AUDIT_LOG = self._saved
        fb.auth._forget_registry()
        __import__("shutil").rmtree(self.dir, ignore_errors=True)

    def device(self, environment="sandbox"):
        pub, _ = fb.auth.add_device("iPhone")
        fb.auth.set_push(pub["id"], {"backend": "apns", "token": "ab" * 32,
                                     "environment": environment})
        return pub["id"]

    def use_sink(self, sink):
        push.sink = lambda creds=None: sink

    def svc(self):
        return Service(log_path=str(self.dir / "events.json"))


class TestServicePruneAndNote(_ServiceCase):

    def test_a_410_prunes_the_token(self):
        devid = self.device()
        sink = RecordingSink(push.Response(status=410, reason="Unregistered"))
        self.use_sink(sink)
        s = self.svc()
        s.observe(proj(sessions={"s1": sess("working")}), now=1000.0)  # baseline
        s.observe(proj(sessions={"s1": sess("needs_input")}), now=1001.0)
        self.assertEqual(fb.auth.push_devices(), [],
                         "a 410 must drop the dead token from the fan-out set")

    def test_a_failed_send_is_still_recorded_via_note_push(self):
        devid = self.device()
        sink = RecordingSink(push.Response(status=503))   # retriable failure
        self.use_sink(sink)
        s = self.svc()
        s.observe(proj(sessions={"s1": sess("working")}), now=1000.0)  # baseline
        s.observe(proj(sessions={"s1": sess("needs_input")}), now=1001.0)
        stored = fb.auth.get_push(devid)
        self.assertEqual(stored["last_push_status"], "503",
                         "every attempt, not only a 200, must reach note_push")

    def test_the_device_environment_is_honoured_on_fan_out(self):
        self.device(environment="sandbox")
        sink = RecordingSink(push.Response(status=200, apns_id="A"))
        self.use_sink(sink)
        s = self.svc()
        s.observe(proj(sessions={"s1": sess("working")}), now=1000.0)  # baseline
        s.observe(proj(sessions={"s1": sess("needs_input")}), now=1001.0)
        self.assertTrue(sink.sends)
        self.assertEqual(sink.sends[0]["environment"], "sandbox")

    def test_a_healed_environment_is_persisted(self):
        devid = self.device(environment="production")
        sink = RecordingSink(push.Response(status=200, apns_id="A"),
                             heal_to="sandbox")
        self.use_sink(sink)
        s = self.svc()
        s.observe(proj(sessions={"s1": sess("working")}), now=1000.0)  # baseline
        s.observe(proj(sessions={"s1": sess("needs_input")}), now=1001.0)
        self.assertEqual(fb.auth.get_push(devid)["environment"], "sandbox",
                         "a 400→other-host heal must be written back to the device")


class TestServiceBaseline(_ServiceCase):

    def test_the_first_observe_after_start_emits_nothing(self):
        """F4: a standing question at start must NOT re-push on every restart —
        the persisted log already holds it. The first observe seeds the diff
        baseline silently."""
        self.device()
        sink = RecordingSink(push.Response(status=200, apns_id="A"))
        self.use_sink(sink)
        s = self.svc()
        # the world is ALREADY standing at a question the instant we start.
        n = s.observe(proj(sessions={"s1": sess("needs_input")}), now=1000.0)
        self.assertEqual(n, 0)
        self.assertEqual(sink.sends, [], "a restart must not re-buzz standing P1s")
        self.assertEqual(len(s.log.since()["events"]), 0,
                         "the baseline appends nothing — the log already has it")

    def test_a_new_condition_after_the_baseline_still_fires(self):
        self.device()
        sink = RecordingSink(push.Response(status=200, apns_id="A"))
        self.use_sink(sink)
        s = self.svc()
        s.observe(proj(sessions={"s1": sess("working")}), now=1000.0)  # baseline
        s.observe(proj(sessions={"s1": sess("needs_input")}), now=1001.0)
        self.assertEqual(len(sink.sends), 1)


# ------------------------------------------------------------ F1 push_loop

class TestPushLoopCadence(unittest.TestCase):

    def test_a_timeout_still_observes_the_current_snapshot(self):
        """F1: `wait_for` returning None (no new version) must NOT skip the
        sweep — the loop re-observes the current snapshot so dwell-held pushes
        and account/resume/dispatch edges are not starved on a quiet board."""
        class Snap:
            v = 7
            counts = {}
            cards = {}

        class FakeObs:
            def __init__(self):
                self.calls = 0
            def wait_for(self, after, timeout=30.0):
                self.calls += 1
                if self.calls >= 2:
                    raise KeyboardInterrupt      # break the while True after one
                return None                      # first call: a timeout
            def snapshot(self):
                return Snap()

        saved = notify._service
        try:
            svc = Service(log_path=None)
            observed = []
            svc.observe = lambda proj, now=None, counts=None: observed.append(proj)
            notify._service = svc
            with self.assertRaises(KeyboardInterrupt):
                notify.push_loop(FakeObs())
            self.assertEqual(len(observed), 1,
                             "the timeout branch must still drive one observe")
        finally:
            notify._service = saved


# ------------------------------------------------------------ F7 hooks atomic

class TestHooksAtomicInstall(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-hooks-fix-"))

    def tearDown(self):
        __import__("shutil").rmtree(self.tmp, ignore_errors=True)

    def test_install_leaves_no_temp_file_behind(self):
        hooks.install(4242, self.tmp)
        names = sorted(p.name for p in self.tmp.iterdir())
        self.assertEqual(names, ["hooks.settings.json", "post-hook.sh"],
                         "os.replace must not leave a .tmp artefact")

    def test_the_script_is_atomic_and_executable(self):
        hooks.install(4321, self.tmp)
        sh = self.tmp / "post-hook.sh"
        self.assertTrue(sh.stat().st_mode & 0o111)
        self.assertIn(":4321/api/hook", sh.read_text())

    def test_a_no_op_rerun_does_not_rewrite(self):
        """The content-match skip: an idempotent rerun leaves the mtime alone."""
        js = hooks.install(4242, self.tmp)
        before = os.stat(self.tmp / "post-hook.sh").st_mtime_ns
        again = hooks.install(4242, self.tmp)
        after = os.stat(self.tmp / "post-hook.sh").st_mtime_ns
        self.assertEqual(js.read_text(), again.read_text())
        self.assertEqual(before, after, "an unchanged script must not be rewritten")


# ------------------------------------------------------------ F9 log perms

class TestEventLogPerms(unittest.TestCase):

    def test_the_events_log_is_created_0600(self):
        tmp = Path(tempfile.mkdtemp(prefix="fb-log-fix-"))
        try:
            path = tmp / "events.log.json"
            log = EventLog(path=str(path))
            log.append([notify.Event(id="", at=1.0, type="session.needs_answer",
                                     level="P1", dedupe_key="k",
                                     topic="a secret prompt line")])
            mode = statmod.S_IMODE(os.stat(path).st_mode)
            self.assertEqual(mode, 0o600,
                             "the events log carries prompt topics — 0600 like "
                             "audit.log.jsonl, not world-readable 0644")
        finally:
            __import__("shutil").rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
