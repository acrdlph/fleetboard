#!/usr/bin/env python3
"""Claude Code hooks — ADR 0007, ENGINE.md §7. Step 6.

The claim under test is not "a POST is received". It is the pair of promises
that make hooks safe to ship at all:

  1. A hook LOWERS LATENCY AND RAISES CONFIDENCE. `■ BLOCKED` and `◆ YOUR TURN`
     are the same footprint from outside a process — a live pid and an idle
     transcript — and the only thing that separates them is the CLI's own word.
  2. A DROPPED HOOK COSTS LATENCY, NEVER TRUTH. Everything here that is not
     about (1) is about (2): the TTL, the vetoes, the anti-flicker path, the
     route that answers 200 with no Observer running, and the fact that
     `collect_state()` with no hooks is byte-identical to what it always was.

The vocabulary in `orchestra/hooks.py` was MEASURED against Claude Code 2.1.218
by wiring all 30 hook events to a logger and driving real sessions — headless,
then interactive, then interactive with a permission dialog open. The payloads
pinned in `MEASURED_*` below are transcribed from that capture, so a CLI change
that renames a field breaks a test here rather than silently turning every
session back into a guess.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestra as fb  # noqa: E402
from orchestra import hooks  # noqa: E402

# The fleet fixture, reused rather than re-grown: a second temp git repo + temp
# Claude home in this file would be a second thing to keep in step with the
# collector, and `discover_worktrees` is exactly the seam that has broken twice.
from test_integration import (_Fleet, HAVE_GIT, assistant_msg,  # noqa: E402
                              turn_end, user_msg, write_transcript)


SID = "eddd3050-38cc-44d5-893a-3f5110978da2"
SID2 = "895a4c5e-4d5a-4545-9829-a2b0f4f04e24"

# Verbatim from the capture. Keys, not prose — see the module docstring.
MEASURED_STOP = {
    "session_id": SID2,
    "transcript_path": f"/Users/x/.claude/projects/-tmp-wd/{SID2}.jsonl",
    "cwd": "/private/tmp/wd", "prompt_id": "114e95d5-3bb4-4b11-8f90-7d3961a5d9dc",
    "permission_mode": "bypassPermissions", "hook_event_name": "Stop",
    "stop_hook_active": False, "last_assistant_message": "Done.",
    "background_tasks": [], "session_crons": [],
}
MEASURED_PERMISSION_NOTIFICATION = {
    "session_id": SID,
    "transcript_path": f"/Users/x/.claude/projects/-tmp-wd/{SID}.jsonl",
    "cwd": "/private/tmp/wd", "prompt_id": "55824ecd-d9df-4a88-9fe2-017dc345f084",
    "hook_event_name": "Notification", "message": "Claude needs your permission",
    "notification_type": "permission_prompt",
}
MEASURED_IDLE_NOTIFICATION = {
    "session_id": SID, "hook_event_name": "Notification",
    "message": "Claude is waiting for your input",
    "notification_type": "idle_prompt",
}


# ------------------------------------------------------------- the vocabulary

class TestVocabulary(unittest.TestCase):
    """`hook_status` is the whole event vocabulary and it is pure."""

    def test_the_two_ambiguous_statuses_are_what_ADR_0007_named(self):
        # This IS the feature. Everything else in the file protects it.
        self.assertEqual(hooks.hook_status("Notification", "permission_prompt"),
                         "blocked")
        self.assertEqual(hooks.hook_status("Notification", "idle_prompt"),
                         "waiting")

    def test_a_permission_dialog_is_blocked_by_either_event_that_reports_it(self):
        # The CLI fires BOTH for one dialog; they must agree, or which one
        # arrives last decides the card.
        self.assertEqual(hooks.hook_status("PermissionRequest"), "blocked")
        self.assertEqual(hooks.hook_status("Notification", "permission_prompt"),
                         "blocked")

    def test_stop_is_the_end_of_turn(self):
        self.assertEqual(hooks.hook_status("Stop"), "waiting")
        self.assertEqual(hooks.hook_status("StopFailure"), "waiting")

    def test_the_working_events_are_the_ones_that_bracket_a_turn(self):
        for e in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertEqual(hooks.hook_status(e), "working", e)

    def test_SessionEnd_asserts_nothing_because_clear_is_one_of_its_reasons(self):
        # The most tempting entry to add, and the one that would free a worktree
        # under a live agent that typed /clear. See HOOK_STATUS.
        self.assertIsNone(hooks.hook_status("SessionEnd"))

    def test_MessageDisplay_asserts_nothing_and_is_not_installed(self):
        self.assertIsNone(hooks.hook_status("MessageDisplay"))
        self.assertNotIn("MessageDisplay", hooks.INSTALLED_EVENTS)

    def test_an_unknown_event_degrades_to_inference_rather_than_raising(self):
        # The CLI adds events. A route agents block on must not learn them the
        # hard way.
        self.assertIsNone(hooks.hook_status("SomethingShippedNextMonth"))
        self.assertIsNone(hooks.hook_status(None))
        self.assertIsNone(hooks.hook_status("Notification", "a_type_from_2027"))
        self.assertIsNone(hooks.hook_status("Notification", None))

    def test_a_hook_only_speaks_about_its_own_session(self):
        # `agent_needs_input` / `worker_permission_prompt` describe a BACKGROUND
        # agent or a teammate, not the session whose id is on the payload.
        self.assertIsNone(hooks.hook_status("Notification", "agent_needs_input"))
        self.assertIsNone(hooks.hook_status("Notification",
                                            "worker_permission_prompt"))

    def test_every_installed_event_is_one_this_module_has_an_opinion_about(self):
        # Except SessionEnd, which is installed deliberately: we want the edge
        # (it proves the session is hooked) without the status.
        for e in hooks.INSTALLED_EVENTS:
            if e in ("SessionEnd", "Notification"):
                continue
            self.assertIsNotNone(hooks.hook_status(e),
                                 f"{e} is installed but asserts nothing — "
                                 f"that is a fork per turn for no signal")


# ------------------------------------------------------------------ the store

class TestHookEdges(unittest.TestCase):

    def setUp(self):
        self.h = hooks.HookEdges(ttl_s=10.0)
        self.t = 1000.0

    def test_an_edge_is_readable_and_expires(self):
        self.h.record(SID, "Stop", self.t)
        self.assertEqual(self.h.status(SID, self.t + 9.9), "waiting")
        self.assertIsNone(self.h.status(SID, self.t + 10.0))

    def test_expiry_is_the_whole_safety_argument(self):
        # ENGINE.md §7.2. One dropped hook must never pin a session forever.
        self.h.record(SID, "PermissionRequest", self.t)
        self.assertEqual(self.h.live(self.t), {SID: "blocked"})
        self.assertEqual(self.h.live(self.t + 11.0), {})

    def test_the_newest_edge_supersedes_the_older_one(self):
        self.h.record(SID, "PreToolUse", self.t)
        self.h.record(SID, "Stop", self.t + 1)
        self.assertEqual(self.h.status(SID, self.t + 2), "waiting")

    def test_an_event_that_asserts_nothing_neither_renews_nor_destroys(self):
        # TWO failures, one guard, and the first draft of this test only caught
        # one of them — a mutation that deleted the guard entirely survived it.
        #
        #   renew:   a TTL that any traffic can refresh is not a TTL, and a
        #            stream of MessageDisplay POSTs arrives at the model's token
        #            rate, so a stale `Stop` would live forever.
        #   destroy: overwriting the `Stop` with a status-less edge would throw
        #            away the one fact we have and fall back to inference
        #            instantly — the opposite failure, from the same missing
        #            line.
        self.h.record(SID, "Stop", self.t)
        for i in range(20):
            self.h.record(SID, "MessageDisplay", self.t + i)
        self.assertEqual(self.h.status(SID, self.t + 5.0), "waiting")   # kept
        self.assertIsNone(self.h.status(SID, self.t + 10.0))            # not renewed

    def test_a_malformed_session_id_is_dropped_not_stored(self):
        self.assertIsNone(self.h.record("x" * 5000, "Stop", self.t))
        self.assertIsNone(self.h.record("", "Stop", self.t))
        self.assertIsNone(self.h.record(None, "Stop", self.t))
        self.assertIsNone(self.h.record("../../etc/passwd", "Stop", self.t))
        self.assertEqual(self.h.stats(self.t)["hook_sessions"], 0)

    def test_the_table_is_bounded_against_a_peer_posting_garbage(self):
        # §4.7. Loopback trust permits this; it must cost a fixed amount of RAM.
        h = hooks.HookEdges(ttl_s=10.0, max_edges=8)
        for i in range(200):
            h.record(f"{i:08d}-0000-0000-0000-000000000000", "Stop", self.t + i)
        self.assertLessEqual(h.stats(self.t + 200)["hook_sessions"], 8)
        self.assertGreater(h.stats(self.t + 200)["hook_evicted"], 0)

    def test_eviction_keeps_the_newest(self):
        h = hooks.HookEdges(ttl_s=1000.0, max_edges=2)
        for i, s in enumerate(("a", "b", "c")):
            h.record(f"{s * 8}-0000-0000-0000-000000000000", "Stop", self.t + i)
        self.assertIsNone(h.status("a" * 8 + "-0000-0000-0000-000000000000",
                                   self.t + 3))
        self.assertEqual(h.status("c" * 8 + "-0000-0000-0000-000000000000",
                                  self.t + 3), "waiting")

    def test_live_is_one_snapshot_and_excludes_the_silent_events(self):
        self.h.record(SID, "Stop", self.t)
        self.h.record(SID2, "SessionEnd", self.t)
        self.assertEqual(self.h.live(self.t), {SID: "waiting"})

    def test_recording_is_safe_under_concurrent_readers(self):
        # The server thread writes while the sweep thread reads. A store that
        # needs a lucky interleaving is a store that wedges an agent.
        stop = threading.Event()
        errors = []

        def reader():
            try:
                while not stop.is_set():
                    self.h.live()
                    self.h.stats()
            except Exception as e:                      # noqa: BLE001
                errors.append(e)

        ts = [threading.Thread(target=reader) for _ in range(4)]
        for t in ts:
            t.start()
        for i in range(2000):
            self.h.record(f"{i % 50:08d}-0000-0000-0000-000000000000", "Stop")
        stop.set()
        for t in ts:
            t.join(5)
        self.assertEqual(errors, [])

    def test_the_measured_payloads_decode_into_the_statuses_they_describe(self):
        for payload, want in ((MEASURED_STOP, "waiting"),
                              (MEASURED_PERMISSION_NOTIFICATION, "blocked"),
                              (MEASURED_IDLE_NOTIFICATION, "waiting")):
            h = hooks.HookEdges(ttl_s=10.0)
            got = h.record(payload["session_id"], payload["hook_event_name"],
                           self.t,
                           notification_type=payload.get("notification_type"))
            self.assertEqual(got, want, payload["hook_event_name"])

    def test_the_session_id_is_the_transcript_stem(self):
        # The whole join, and the reason this costs one dict: `scan_sessions`
        # already keys on `fp.stem`.
        p = Path(MEASURED_STOP["transcript_path"])
        self.assertEqual(p.stem, MEASURED_STOP["session_id"])


# ------------------------------------------------------- the ladder, reconciled

class TestLadderPlacement(unittest.TestCase):
    """ENGINE.md §7.3: a higher-ranked source WINS on the same question; a
    lower-ranked one VETOES a question the higher one cannot answer."""

    def classify(self, **kw):
        base = dict(age_s=1.0, alive=True, pending_tools=[], delegated=0,
                    skip_perms=False, working_s=90.0, shells=0,
                    quiet_s=25.0, block_grace_s=60.0, orphan_grace_s=90.0)
        base.update(kw)
        return fb.classify_session(**base)[0]

    # ---- rule (1): the hook wins on its own question

    def test_a_dialog_on_screen_beats_every_file_signal(self):
        # An idle transcript on a live pid reads WORKING then WAITING. Only the
        # CLI knows a permission dialog is up, and it says so.
        self.assertEqual(self.classify(age_s=200.0), "waiting")
        self.assertEqual(self.classify(age_s=200.0, hook="blocked"), "blocked")
        self.assertEqual(self.classify(age_s=1.0, hook="blocked"), "blocked")

    def test_needs_input_from_a_hook_needs_no_AskUserQuestion_on_disk(self):
        self.assertEqual(self.classify(age_s=200.0, hook="needs_input"),
                         "needs_input")

    def test_a_stop_hook_ends_the_turn_without_waiting_out_quiet_s(self):
        self.assertEqual(self.classify(age_s=2.0), "working")
        self.assertEqual(self.classify(age_s=2.0, hook="waiting"), "waiting")

    def test_a_working_hook_saves_a_thinking_agent_from_being_called_done(self):
        # A long turn with no tool call writes nothing between the prompt and
        # the answer, so at quiet_s the board summons the user to an agent that
        # is mid-sentence.
        self.assertEqual(self.classify(age_s=40.0), "waiting")
        self.assertEqual(self.classify(age_s=40.0, hook="working"), "working")

    # ---- rule (2): the vetoes the hook cannot overrule

    def test_a_hook_cannot_claim_a_dead_process_is_waiting_for_you(self):
        # Rank 2 (the process table) beats rank 1 on "does it exist".
        for h in ("blocked", "needs_input", "waiting", "working"):
            self.assertEqual(self.classify(alive=False, age_s=500.0, hook=h),
                             "ended", h)

    def test_a_stop_hook_loses_to_delegated_work_still_outstanding(self):
        # `Stop` fires while background tasks keep running — its own payload
        # carries `background_tasks`. "Still busy" beats "the turn closed".
        self.assertEqual(self.classify(delegated=1, hook="waiting"), "working")

    def test_a_stop_hook_loses_to_a_live_background_shell(self):
        self.assertEqual(self.classify(shells=1, hook="waiting"), "working")

    def test_a_stop_hook_loses_to_an_unresolved_tool_use(self):
        self.assertEqual(self.classify(pending_tools=["Bash"], age_s=1.0,
                                       hook="waiting"), "working")

    def test_a_dialog_hook_still_outranks_those_because_nothing_else_sees_it(self):
        # The asymmetry is the point: `waiting` duplicates a fact the disk also
        # carries, so the disk may veto it. `blocked` duplicates nothing.
        self.assertEqual(self.classify(shells=1, hook="blocked"), "blocked")
        self.assertEqual(self.classify(delegated=3, hook="blocked"), "blocked")

    def test_a_wholesale_probe_failure_still_beats_everything(self):
        # `unknown` exists so a broken `ps` never claims ENDED or FREE. A hook
        # must not restore confidence we do not have.
        self.assertEqual(self.classify(procs_known=False, hook="blocked"),
                         "unknown")

    def test_no_hook_is_byte_identical_to_the_ladder_before_step_6(self):
        for kw in (dict(), dict(age_s=200.0), dict(alive=False, age_s=200.0),
                   dict(pending_tools=["Bash"], age_s=200.0),
                   dict(turn_ended=True), dict(delegated=2), dict(shells=1)):
            self.assertEqual(self.classify(**kw),
                             self.classify(hook=None, **kw), kw)


# -------------------------------------------------------- the board, end to end

@unittest.skipUnless(HAVE_GIT, "git not available")
class TestHookedBoard(_Fleet):
    """A real transcript, a real worktree, a real hook edge."""

    def setUp(self):
        super().setUp()
        fb.CFG["quiet_s"] = 25.0
        fb.CFG["block_grace_s"] = 60.0
        fb.CFG["orphan_grace_s"] = 90.0
        self.sid = SID
        # An agent that spoke 200 s ago and has been silent since: alive, idle
        # transcript, nothing pending. THE AMBIGUOUS CASE — the board can only
        # guess, and it guesses ◆ YOUR TURN.
        write_transcript(self.home, self.repo, "main", sid=self.sid, entries=[
            user_msg("do the thing"), assistant_msg("working on it")])
        fp = (self.home / "projects" / fb.munge(str(self.repo))
              / f"{self.sid}.jsonl")
        import os
        os.utime(fp, (time.time() - 200, time.time() - 200))
        self.live(shells=0)

    def live(self, shells=0):
        fb.procs.claude_processes = lambda **_: [{
            "pid": 4242, "cpu": 0.1, "etime": "10:00", "tty": None,
            "host": None, "cwd": str(self.repo), "cmd": "claude",
            "account": None, "tmux_target": None, "shells": shells}]

    def sessions(self, hooks_map=None):
        fb._cache["state"] = None
        st = fb.collect_state(hooks=hooks_map)
        return st["worktrees"][0]["sessions"]

    def test_without_a_hook_the_board_guesses_your_turn(self):
        s = self.sessions()[0]
        self.assertEqual(s["status"], "waiting")
        self.assertNotIn("hooked", s)
        self.assertNotIn("status_src", s)

    def test_a_permission_hook_turns_the_guess_into_an_observation(self):
        s = self.sessions({self.sid: "blocked"})[0]
        self.assertEqual(s["status"], "blocked")
        self.assertTrue(s["hooked"])
        self.assertEqual(s["status_src"], "observed")

    def test_a_vetoed_hook_is_hooked_but_honestly_inferred(self):
        # The ladder overruled the hook; the card must not claim to be observed.
        self.live(shells=2)
        s = self.sessions({self.sid: "waiting"})[0]
        self.assertEqual(s["status"], "working")     # the shell vetoed it
        self.assertTrue(s["hooked"])
        self.assertNotIn("status_src", s)
        self.assertEqual(fb.memo_stats()["hook_vetoed"], 1)

    def test_a_hook_for_a_session_that_is_not_here_changes_nothing(self):
        s = self.sessions({"00000000-0000-0000-0000-000000000000": "blocked"})[0]
        self.assertEqual(s["status"], "waiting")
        self.assertNotIn("hooked", s)

    def test_the_limit_join_still_runs_on_top_of_a_hooked_status(self):
        # ORDER: the hook lands inside `scan_sessions`, so a hooked ◆ YOUR TURN
        # on an exhausted account still becomes ⛔ LIMIT HIT.
        fb.limits.cached_limits = lambda refresh=False: {
            "available": True, "accounts": {"home": {"session": {
                "utilization": 100, "resets_at": "2030-01-01T00:00:00Z"}}}}
        try:
            s = self.sessions({self.sid: "waiting"})[0]
        finally:
            fb.limits.cached_limits = lambda refresh=False: {"available": False}
        self.assertIn(s["status"], ("limit", "waiting"))
        if s["status"] == "limit":
            self.assertTrue(s["hooked"])

    def test_an_unhooked_fleet_pays_nothing_on_the_wire(self):
        a = json.dumps(self.sessions(), sort_keys=True)
        b = json.dumps(self.sessions(None), sort_keys=True)
        self.assertEqual(a, b)
        self.assertNotIn("status_src", a)


# -------------------------------------------------------------- the TTL, live

@unittest.skipUnless(HAVE_GIT, "git not available")
class TestExpiryFallsBackToInference(_Fleet):

    def setUp(self):
        super().setUp()
        fb.CFG["quiet_s"] = 25.0
        self.sid = SID
        write_transcript(self.home, self.repo, "main", sid=self.sid, entries=[
            user_msg("go"), assistant_msg("ok"), turn_end()])
        import os
        fp = (self.home / "projects" / fb.munge(str(self.repo))
              / f"{self.sid}.jsonl")
        os.utime(fp, (time.time() - 200, time.time() - 200))
        fb.procs.claude_processes = lambda **_: [{
            "pid": 4242, "cpu": 0.1, "etime": "10:00", "tty": None,
            "host": None, "cwd": str(self.repo), "cmd": "claude",
            "account": None, "tmux_target": None, "shells": 0}]

    def test_a_dropped_hook_costs_latency_not_truth(self):
        edges = hooks.HookEdges(ttl_s=10.0)
        edges.record(self.sid, "PermissionRequest", 1000.0)

        fb._cache["state"] = None
        blocked = fb.collect_state(hooks=edges.live(1005.0))
        self.assertEqual(blocked["worktrees"][0]["sessions"][0]["status"],
                         "blocked")

        fb._cache["state"] = None
        after = fb.collect_state(hooks=edges.live(1011.0))
        # Inference resumed. Not stuck on BLOCKED forever, which is the failure
        # the TTL exists to prevent — nothing would ever have corrected it.
        s = after["worktrees"][0]["sessions"][0]
        self.assertEqual(s["status"], "waiting")
        self.assertNotIn("hooked", s)


class TestExpiryPassesThroughSettle(unittest.TestCase):
    """§6.3(a): a hook expiring must not by itself change what is displayed."""

    def test_a_hook_expiring_de_escalates_only_after_the_dwell(self):
        # blocked (2) -> waiting (4) is a de-escalation, so it must dwell.
        st, since = fb.settle("blocked", "waiting", now=100.0, since=99.0,
                              dwell_s=3.0)
        self.assertEqual(st, "blocked")
        st, since = fb.settle("blocked", "waiting", now=103.0, since=99.0,
                              dwell_s=3.0)
        self.assertEqual(st, "waiting")

    def test_a_hook_arriving_escalates_at_once(self):
        st, _ = fb.settle("waiting", "blocked", now=100.0, since=99.9,
                          dwell_s=3.0)
        self.assertEqual(st, "blocked")

    def test_the_settler_damps_a_hooked_status_like_any_other(self):
        s = fb.Settler(dwell_s=3.0)
        sess = {"/wt": [{"sid": SID, "status": "blocked"}]}
        s.apply(sess, 100.0)
        sess["/wt"][0]["status"] = "waiting"           # the edge expired
        s.apply(sess, 101.0)
        self.assertEqual(sess["/wt"][0]["status"], "blocked")
        sess["/wt"][0]["status"] = "waiting"
        s.apply(sess, 104.0)
        self.assertEqual(sess["/wt"][0]["status"], "waiting")


# ---------------------------------------------------------------- the observer

class TestObserverIngest(unittest.TestCase):

    def setUp(self):
        self.obs = fb.Observer(watch=False, hook_ttl_s=10.0)

    def test_ingest_returns_what_the_board_understood(self):
        self.assertEqual(self.obs.hook(SID, "Stop"), "waiting")
        self.assertIsNone(self.obs.hook(SID2, "MessageDisplay"))

    def test_a_meaningful_edge_nudges_the_loop_and_a_silent_one_does_not(self):
        # The point of a hook is that `Stop` collapses WORKING -> YOUR TURN on
        # the NEXT sweep, not up to idle_s later. But a MessageDisplay per token
        # would run the sweep at the model's output rate.
        before = self.obs.stats()["nudges"]
        self.obs.hook(SID, "Stop")
        self.assertEqual(self.obs.stats()["nudges"], before + 1)
        self.obs.hook(SID, "MessageDisplay")
        self.assertEqual(self.obs.stats()["nudges"], before + 1)

    def test_a_hook_never_forces_a_git_fan_out(self):
        # An agent finishing a turn does not move the working tree; forcing git
        # per turn is the cost GIT_S exists to bound.
        self.obs._git._forced = False
        self.obs.hook(SID, "Stop")
        self.assertFalse(self.obs._git._forced)
        self.obs.nudge("a mutation")          # …which DOES, for contrast
        self.assertTrue(self.obs._git._forced)

    def test_ingest_never_raises_whatever_it_is_handed(self):
        for bad in (None, "", 12345, "../etc", "x" * 9000):
            self.assertIsNone(self.obs.hook(bad, "Stop"))
        self.assertIsNone(self.obs.hook(SID, None))
        self.assertIsNone(self.obs.hook(SID, {"not": "a string"}))

    def test_the_counters_are_published_for_the_board_to_read(self):
        self.obs.hook(SID, "Stop")
        self.obs.hook(SID2, "MessageDisplay")
        st = self.obs.stats()
        self.assertEqual(st["hook_received"], 2)
        self.assertEqual(st["hook_ignored"], 1)
        self.assertEqual(st["hook_live"], 2)
        self.assertEqual(st["hook_ttl_s"], 10.0)

    def test_the_module_level_ingest_answers_with_no_observer_running(self):
        # THE AGENT IS BLOCKED ON THIS CALL. A board started without a sweep —
        # the documented rollback — must still answer.
        save = fb.observer._observer
        fb.observer._observer = None
        try:
            self.assertIsNone(fb.observer.hook(SID, "Stop"))
            self.assertEqual(fb.observer.hook_stats()["hook_received"], 0)
        finally:
            fb.observer._observer = save


# -------------------------------------------------------------------- the route

class TestRoute(unittest.TestCase):

    def test_the_hook_route_is_not_written_to_the_audit_log(self):
        # Several per agent turn from every hooked session. Logging them buries
        # the eleven lines that matter; `observer.stats()` counts instead.
        self.assertFalse(fb.auth.audited("POST", "/api/hook"))
        self.assertFalse(fb.auth.audited("POST", "/api/hook?x=1"))

    def test_the_suppression_is_segment_exact(self):
        # Matching too eagerly is the UNSAFE direction for a list that silences
        # logging — the mirror image of `_under_admin`.
        self.assertTrue(fb.auth.audited("POST", "/api/hooked-up"))
        self.assertTrue(fb.auth.audited("POST", "/api/hookish"))

    def test_every_other_mutation_is_still_audited(self):
        for p in ("/api/send", "/api/dispatch", "/api/finish", "/api/reserve"):
            self.assertTrue(fb.auth.audited("POST", p), p)

    def test_the_route_is_not_exempt_from_authentication(self):
        # Loopback trust is what lets it through, not an exemption. A phone on
        # the tailnet still needs its token; `EXEMPT` stays one entry long.
        self.assertNotIn(("POST", "/api/hook"), fb.EXEMPT)

    def test_a_cross_site_page_cannot_post_a_hook_through_your_browser(self):
        v = fb.auth.check("127.0.0.1", None, "POST", "/api/hook",
                          origin="https://evil.example", host="127.0.0.1:4242",
                          content_type="application/json")
        self.assertFalse(v.ok)
        v = fb.auth.check("127.0.0.1", None, "POST", "/api/hook",
                          origin=None, host="127.0.0.1:4242",
                          content_type="text/plain")
        self.assertFalse(v.ok)

    def test_a_local_hook_with_the_right_media_type_is_admitted(self):
        v = fb.auth.check("127.0.0.1", None, "POST", "/api/hook",
                          origin=None, host="127.0.0.1:4242",
                          content_type="application/json")
        self.assertTrue(v.ok)


# ------------------------------------------------------------- installation

class TestInstallation(unittest.TestCase):
    """ADR 0007's open question. The rule has no exceptions: orchestra never
    writes a file Claude Code already owns."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-hooks-"))
        # REBOUND, exactly like `auth.REGISTRY` in its own tests, and for a
        # reason this suite learned the hard way: an earlier draft let one test
        # call `install()` against the real `HOOK_DIR`, which rewrote the
        # developer's own live hook script with the DEFAULT port — silently
        # unhooking every agent on the machine until somebody noticed the board
        # had stopped saying "observed". A test that writes outside its temp
        # directory is a test that can break the thing it is testing.
        self._paths = (hooks.HOOK_DIR, hooks.SETTINGS_PATH, hooks.SCRIPT_PATH)
        hooks.HOOK_DIR = self.tmp
        hooks.SETTINGS_PATH = self.tmp / "hooks.settings.json"
        hooks.SCRIPT_PATH = self.tmp / "post-hook.sh"

    def tearDown(self):
        (hooks.HOOK_DIR, hooks.SETTINGS_PATH,
         hooks.SCRIPT_PATH) = self._paths
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_fixture_really_redirects_every_write(self):
        # The guard on the guard: if `install()` ever stops honouring these
        # globals, every other test in this class starts writing to the real
        # `.orchestra/` again and nothing would say so.
        hooks.install(1234)
        self.assertTrue((self.tmp / "post-hook.sh").exists())
        self.assertEqual(hooks.install(1234).parent, self.tmp)

    def test_the_fragment_wires_the_events_the_ambiguity_lives_in(self):
        # NAMED, not `set(INSTALLED_EVENTS)`. Comparing the fragment to the
        # constant it is built from is a tautology: a mutation that reduced
        # INSTALLED_EVENTS to ("Stop",) — silently unhooking every permission
        # dialog on the board — survived exactly that assertion.
        frag = hooks.settings_fragment(4242, "/x/post-hook.sh")
        for must in ("Notification",        # permission_prompt / idle_prompt
                     "PermissionRequest",   # the dialog, again
                     "Stop",                # end of turn
                     "UserPromptSubmit", "PreToolUse", "PostToolUse"):
            self.assertIn(must, frag["hooks"],
                          f"{must} carries a status and is not installed")
        self.assertEqual(set(frag["hooks"]), set(hooks.INSTALLED_EVENTS))

    def test_the_fragment_wires_one_command_per_event(self):
        frag = hooks.settings_fragment(4242, "/x/post-hook.sh")
        for ev, entries in frag["hooks"].items():
            self.assertEqual(entries[0]["hooks"][0]["type"], "command", ev)
            self.assertIn("post-hook.sh", entries[0]["hooks"][0]["command"], ev)

    def test_a_script_path_with_a_space_survives_the_command_line(self):
        frag = hooks.settings_fragment(4242, "/a b/post-hook.sh")
        cmd = frag["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(subprocess.run(["sh", "-c", f"printf %s {cmd}"],
                                        capture_output=True, text=True).stdout,
                         "/a b/post-hook.sh")

    def test_install_writes_only_inside_its_own_directory(self):
        hooks.install(4242, self.tmp)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["hooks.settings.json", "post-hook.sh"])

    def test_the_script_is_executable_and_carries_the_port(self):
        hooks.install(4321, self.tmp)
        sh = self.tmp / "post-hook.sh"
        self.assertTrue(sh.stat().st_mode & 0o111)
        self.assertIn(":4321/api/hook", sh.read_text())

    def test_the_script_can_never_block_or_fail_an_agent(self):
        body = hooks.install(4242, self.tmp).parent.joinpath(
            "post-hook.sh").read_text()
        # exit 0 unconditionally: exit 2 on a PreToolUse BLOCKS THE TOOL CALL.
        self.assertTrue(body.rstrip().endswith("exit 0"))
        # a timeout: a wedged board costs the agent two seconds, not its turn.
        self.assertIn("-m 2", body)
        # silent: stdout from a UserPromptSubmit hook is SHOWN TO CLAUDE.
        self.assertIn(">/dev/null", body)

    def test_the_script_really_exits_zero_when_nothing_is_listening(self):
        hooks.install(1, self.tmp)                       # port 1: refused
        r = subprocess.run([str(self.tmp / "post-hook.sh")], input="{}",
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr, "")

    def test_install_is_idempotent(self):
        a = hooks.install(4242, self.tmp).read_text()
        b = hooks.install(4242, self.tmp).read_text()
        self.assertEqual(a, b)

    def test_installed_is_false_when_the_port_moved(self):
        save = fb.CFG["port"]
        try:
            fb.CFG["port"] = 4242
            hooks.install(4242)
            self.assertTrue(hooks.installed())
            fb.CFG["port"] = 9999
            # A fragment pointing at a dead port is worse than none: it LOOKS
            # installed, and every session silently stays inferred.
            self.assertFalse(hooks.installed())
        finally:
            fb.CFG["port"] = save

    def test_the_dispatch_flag_is_a_settings_layer_and_never_a_rewrite(self):
        cmd = fb.dispatch.closeout_shell(Path("/tmp/home"), "haiku", "b", "main")
        self.assertIn("--settings ", cmd)
        # NOT the user's file. Seven of eight homes here have no hooks key and
        # one has two somebody depends on; a merge-and-rewrite is a data-loss
        # bug that shows up as their tooling silently not running.
        self.assertNotIn("settings.json'", cmd.replace(
            str(hooks.SETTINGS_PATH), ""))
        self.assertIn(str(hooks.SETTINGS_PATH), cmd)

    def test_a_dispatch_that_cannot_install_still_dispatches(self):
        save = hooks.install
        hooks.install = lambda *a, **k: (_ for _ in ()).throw(OSError("full"))
        try:
            self.assertEqual(fb.dispatch._hook_flag(), "")
            cmd = fb.dispatch.closeout_shell(Path("/tmp/h"), None, "b", "main")
            self.assertNotIn("--settings", cmd)
            self.assertIn("claude --dangerously-skip-permissions", cmd)
        finally:
            hooks.install = save


if __name__ == "__main__":
    unittest.main(verbosity=2)
