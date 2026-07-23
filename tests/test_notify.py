#!/usr/bin/env python3
"""`orchestra.notify` — the event pipeline and the quality control on top.

The pipeline has two halves that fail differently, and the tests split on that
seam. `derive` is PURE — it diffs two projections — so it is tested exhaustively
against hand-built projections with nothing standing in: every event type, and
the edges that must NOT fire (a stopwatch moved, a status held, a handed-off
limit). The `Notifier` is STATEFUL — dedup, dwell, coalescing, quiet hours,
budget — so it is tested by feeding it a SEQUENCE of projections and asserting
on what it decided to push, which is the only thing that proves suppression
suppressed and coalescing coalesced.

The direction that is dangerous here is the false alarm (METHOD.md §7): a board
that cries wolf gets muted, taking the P1 with it. So the suppression tests are
the load-bearing ones, and each is watched to FAIL when its rule is removed.

    python3 -m unittest tests.test_notify -v
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402
from orchestra import notify  # noqa: E402
from orchestra.notify import (project, derive, Event, EventLog, Notifier,
                              Preferences, compose, quiet_now, Budget,
                              EMPTY_PROJECTION)


def proj(sessions=None, worktrees=None, accounts=None, resumes=None,
         dispatch=None):
    """A projection, hand-built. Every sub-map defaults empty."""
    return {"sessions": sessions or {}, "worktrees": worktrees or {},
            "accounts": accounts or {}, "resumes": resumes or {},
            "dispatch": dispatch or {}}


def sess(status, worktree="wt", account="acct", model="opus", handed_to=None):
    return {"status": status, "worktree": worktree, "account": account,
            "model": model, "topic": "a topic", "handed_to": handed_to}


# ------------------------------------------------------------- the projection

class TestProjection(unittest.TestCase):
    """The flattening from a Snapshot to what a notification can turn on. The
    point of taking the diff here is that a moving stopwatch is NOT here."""

    def test_a_stopwatch_moving_produces_no_edge(self):
        """The reason the diff is on a projection and not on a card: `cpu` and
        `etime` resample every second, and a notifier diffing raw cards would
        fire on a CPU percentage."""
        class Snap:
            cards = {"wt": {"name": "wt", "availability": "busy",
                            "live_procs": [{"pid": 1, "cpu": 0.5, "etime": "01:00"}],
                            "sessions": [{"sid": "s1", "status": "working",
                                          "account": "a", "model": "opus"}]}}
        p1 = project(Snap())
        Snap.cards["wt"]["live_procs"][0]["cpu"] = 0.9
        Snap.cards["wt"]["live_procs"][0]["etime"] = "02:00"
        p2 = project(Snap())
        self.assertEqual(p1["sessions"], p2["sessions"])
        self.assertEqual(derive(p1, p2), [])

    def test_it_reads_a_dict_snapshot_too(self):
        snap = {"worktrees": [{"name": "wt", "availability": "free",
                               "sessions": []}]}
        p = project(snap)
        self.assertEqual(p["worktrees"], {"wt": "free"})

    def test_a_finished_dispatch_job_projects_its_result(self):
        jobs = {"job-1": {"done": True,
                          "result": {"ok": True, "worktree": "wt",
                                     "account": "a", "session": "mission-x"}}}
        p = project(dispatch_jobs=jobs)
        self.assertEqual(p["dispatch"]["job-1"]["ok"], True)
        self.assertEqual(p["dispatch"]["job-1"]["worktree"], "wt")


# ------------------------------------------------------------------- derive

class TestDeriveSessionEdges(unittest.TestCase):

    def edge(self, before, after, **kw):
        p1 = proj(sessions={"s1": sess(before)} if before else {})
        p2 = proj(sessions={"s1": sess(after, **kw)})
        return derive(p1, p2, now=1000.0)

    def test_into_needs_input(self):
        evs = self.edge("working", "needs_input")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].type, "session.needs_answer")
        self.assertEqual(evs[0].level, "P1")
        self.assertTrue(evs[0].open)
        self.assertTrue(evs[0].dedupe_key.startswith("session.needs_answer|s1|"))

    def test_into_your_turn(self):
        evs = self.edge("working", "waiting")
        self.assertEqual(evs[0].type, "session.your_turn")
        self.assertTrue(evs[0].open)

    def test_into_blocked(self):
        evs = self.edge("working", "blocked")
        self.assertEqual(evs[0].type, "session.blocked")

    def test_a_status_that_holds_emits_nothing(self):
        self.assertEqual(self.edge("needs_input", "needs_input"), [])

    def test_a_brand_new_session_at_working_is_silent(self):
        """A session appearing already WORKING is not an edge worth a push —
        only a crossing INTO an attention status is."""
        p1 = proj()
        p2 = proj(sessions={"s1": sess("working")})
        self.assertEqual(derive(p1, p2, now=1.0), [])

    def test_a_model_scoped_limit_strands_one_session(self):
        evs = self.edge("waiting", "limit")
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].type, "account.limit_hit")

    def test_a_handed_off_limit_is_not_an_alert(self):
        """The classic false positive: work already continued on another
        account, so `handed_to` means this is a non-problem (ARCHITECTURE §6.3)."""
        evs = self.edge("waiting", "limit", handed_to="acct2")
        self.assertEqual(evs, [])


class TestDeriveAccountEdges(unittest.TestCase):

    def test_account_becomes_exhausted(self):
        p1 = proj(accounts={"work": {"exhausted": False}})
        p2 = proj(accounts={"work": {"exhausted": True, "group": "weekly"}})
        evs = derive(p1, p2, now=1.0)
        self.assertEqual(evs[0].type, "account.limit_hit")
        self.assertEqual(evs[0].account, "work")

    def test_account_resets(self):
        p1 = proj(accounts={"work": {"exhausted": True}})
        p2 = proj(accounts={"work": {"exhausted": False}})
        evs = derive(p1, p2, now=1.0)
        self.assertEqual(evs[0].type, "account.limit_reset")
        self.assertEqual(evs[0].level, "P3")

    def test_a_still_exhausted_account_is_silent(self):
        p1 = proj(accounts={"work": {"exhausted": True}})
        p2 = proj(accounts={"work": {"exhausted": True}})
        self.assertEqual(derive(p1, p2, now=1.0), [])


class TestDeriveResumeEdges(unittest.TestCase):

    def test_armed_fired_failed(self):
        base = {"worktree": "wt", "account": "a"}
        p0 = proj()
        p1 = proj(resumes={"wt|s1": {"status": "pending", **base}})
        p2 = proj(resumes={"wt|s1": {"status": "done", "message": "sent", **base}})
        p3 = proj(resumes={"wt|s1": {"status": "failed", "message": "no reach", **base}})
        self.assertEqual(derive(p0, p1, now=1.0)[0].type, "resume.armed")
        fired = derive(p1, p2, now=1.0)[0]
        self.assertEqual(fired.type, "resume.fired")
        self.assertEqual(fired.detail, "sent")
        failed = derive(p2, p3, now=1.0)[0]
        self.assertEqual(failed.type, "resume.failed")
        self.assertEqual(failed.level, "P1")


class TestDeriveDispatchEdges(unittest.TestCase):

    def test_succeeded_and_failed(self):
        p0 = proj()
        ok = proj(dispatch={"j1": {"done": True, "ok": True, "worktree": "wt"}})
        bad = proj(dispatch={"j2": {"done": True, "ok": False, "worktree": "wt2"}})
        self.assertEqual(derive(p0, ok, now=1.0)[0].type, "dispatch.succeeded")
        self.assertEqual(derive(p0, bad, now=1.0)[0].type, "dispatch.failed")

    def test_a_running_job_is_not_an_edge(self):
        p0 = proj()
        running = proj(dispatch={"j1": {"done": False, "ok": False}})
        self.assertEqual(derive(p0, running, now=1.0), [])


class TestDeriveWorktreeFree(unittest.TestCase):

    def test_busy_to_free(self):
        p1 = proj(worktrees={"wt": "busy"})
        p2 = proj(worktrees={"wt": "free"})
        self.assertEqual(derive(p1, p2, now=1.0)[0].type, "worktree.free")

    def test_appearing_free_is_not_an_edge(self):
        """A worktree that is free the first time we see it was not FREED — it
        was already free. Only a transition into free is the event."""
        p1 = proj()
        p2 = proj(worktrees={"wt": "free"})
        self.assertEqual(derive(p1, p2, now=1.0), [])

    def test_free_to_free_is_silent(self):
        p1 = proj(worktrees={"wt": "free"})
        p2 = proj(worktrees={"wt": "free"})
        self.assertEqual(derive(p1, p2, now=1.0), [])


class TestGenerationCounter(unittest.TestCase):
    """A question answered and re-asked is a NEW condition, not a duplicate."""

    def test_re_entering_needs_input_advances_the_generation(self):
        gens = {}
        p_w = proj(sessions={"s1": sess("working")})
        p_n = proj(sessions={"s1": sess("needs_input")})
        e1 = derive(p_w, p_n, now=1.0, gens=gens)[0]
        e2 = derive(p_n, p_w, now=2.0, gens=gens)   # answered
        e3 = derive(p_w, p_n, now=3.0, gens=gens)[0]  # asked again
        self.assertNotEqual(e1.dedupe_key, e3.dedupe_key,
                            "a re-asked question must get a fresh dedupe key")
        self.assertTrue(e1.dedupe_key.endswith("|1"))
        self.assertTrue(e3.dedupe_key.endswith("|2"))


# ------------------------------------------------------------------ log

class TestEventLog(unittest.TestCase):

    def make(self, ev_type="session.needs_answer", key="k", **kw):
        return Event(id="", at=1.0, type=ev_type, level="P1", dedupe_key=key,
                     **kw)

    def test_ids_are_monotonic_and_zero_padded(self):
        log = EventLog()
        stored = log.append([self.make(key="a"), self.make(key="b")])
        self.assertEqual([e.id for e in stored], ["evt-000001", "evt-000002"])

    def test_since_returns_events_after_the_cursor(self):
        log = EventLog()
        log.append([self.make(key=str(i)) for i in range(5)])
        page = log.since("evt-000002", limit=10)
        self.assertEqual([e["id"] for e in page["events"]],
                         ["evt-000003", "evt-000004", "evt-000005"])
        self.assertFalse(page["reset"])

    def test_an_unknown_cursor_is_a_reset_not_a_replay(self):
        """A cursor that aged out or came from another epoch must force a
        resync, never a silent partial replay across the gap."""
        log = EventLog()
        log.append([self.make(key="a")])
        page = log.since("evt-999999")
        self.assertTrue(page["reset"])
        self.assertEqual(page["events"], [])

    def test_the_ring_caps_and_the_sequence_persists(self):
        log = EventLog(cap=3)
        log.append([self.make(key=str(i)) for i in range(5)])
        # only the last 3 are retained…
        page = log.since()
        self.assertEqual(len(page["events"]), 3)
        # …but the sequence kept climbing, so ids never reuse.
        self.assertEqual(page["events"][-1]["id"], "evt-000005")

    def test_open_keys_reflect_only_the_latest_state_of_each_condition(self):
        """A question opened then answered is not open. `/events/open` is what
        the phone withdraws stale lock-screen notifications against."""
        log = EventLog()
        log.append([self.make(key="session.needs_answer|s1|1", open=True)])
        self.assertIn("session.needs_answer|s1|1", log.open_keys())
        # the your_turn that follows closes it — different key, open=False
        log.append([self.make(ev_type="session.your_turn",
                              key="session.needs_answer|s1|1", open=False)])
        self.assertNotIn("session.needs_answer|s1|1", log.open_keys())

    def test_it_persists_across_a_reload(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "events.json")
            log = EventLog(path=path)
            log.append([self.make(key="a"), self.make(key="b")])
            reloaded = EventLog(path=path)
            page = reloaded.since()
            self.assertEqual(len(page["events"]), 2)
            # the sequence resumes rather than restarting at 1
            reloaded.append([self.make(key="c")])
            self.assertEqual(reloaded.since()["events"][-1]["id"], "evt-000003")
        finally:
            __import__("shutil").rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- preferences

class TestPreferencesAndQuietHours(unittest.TestCase):

    def test_a_type_default_applies_when_no_rule_is_set(self):
        p = Preferences()
        self.assertTrue(p.wants("session.needs_answer"))    # default on
        self.assertFalse(p.wants("session.your_turn"))      # default OFF

    def test_a_rule_overrides_the_default(self):
        p = Preferences(rules={"session.your_turn": True,
                               "session.needs_answer": False})
        self.assertTrue(p.wants("session.your_turn"))
        self.assertFalse(p.wants("session.needs_answer"))

    def test_quiet_hours_wrap_midnight(self):
        """23:00→08:00 is the normal case and the one a naive `from<=t<to` gets
        backwards — it would be loud all night."""
        p = Preferences(quiet_from="23:00", quiet_to="08:00", tz_offset_min=0)
        # 02:00 UTC is inside the window
        two_am = 2 * 3600
        self.assertTrue(quiet_now(p, now=two_am))
        # 12:00 UTC is outside it
        self.assertFalse(quiet_now(p, now=12 * 3600))

    def test_quiet_hours_are_in_the_devices_zone_not_the_servers(self):
        """A phone in California and a server in UTC must both mean the phone's
        night. Offset −480 (PST); 02:00 PST is 10:00 UTC."""
        p = Preferences(quiet_from="23:00", quiet_to="08:00",
                        tz_offset_min=-480)
        ten_utc = 10 * 3600            # == 02:00 in the device's zone
        self.assertTrue(quiet_now(p, now=ten_utc))

    def test_no_window_is_never_quiet(self):
        self.assertFalse(quiet_now(Preferences(), now=2 * 3600))


# ------------------------------------------------------------------ compose

class TestCompose(unittest.TestCase):

    def ev(self, type_="session.needs_answer", **kw):
        base = dict(worktree="ConfidAI-auth", account="work", model="opus",
                    level=fb.notify.EVENT_TYPES[type_]["level"])
        base.update(kw)
        return Event(id="evt-000001", at=1784636700.0, type=type_,
                     dedupe_key="k", **base)

    def test_the_title_carries_no_leading_glyph(self):
        """A glyph in the title makes VoiceOver speak the codepoint before
        every alert (UX.md §8.5). It lives in the subtitle instead."""
        c = compose(self.ev())
        self.assertNotIn("▲", c["payload"]["aps"]["alert"]["title"])
        self.assertIn("▲", c["payload"]["aps"]["alert"]["subtitle"])

    def test_no_prose_rides_the_wire_under_structural_privacy(self):
        """The default. Identifiers only; the transcript line is fetched by the
        NSE over the tailnet and never transits Apple."""
        c = compose(self.ev(detail="should I rotate the JWT per request?"),
                    privacy="structural")
        blob = repr(c["payload"])
        self.assertNotIn("rotate the JWT", blob)
        self.assertNotIn("body", c["payload"]["aps"]["alert"])

    def test_detail_privacy_includes_the_body(self):
        c = compose(self.ev(detail="the question text"), privacy="detail")
        self.assertEqual(c["payload"]["aps"]["alert"]["body"], "the question text")

    def test_expiration_is_an_absolute_epoch(self):
        """`int(at + ttl)`, never a bare duration — a duration means "expired
        in 1970", one attempt, no store-and-forward."""
        c = compose(self.ev())
        self.assertGreater(c["headers"]["expiration"], 1784636700)
        self.assertIsInstance(c["headers"]["expiration"], int)

    def test_p1_is_time_sensitive_and_priority_10(self):
        c = compose(self.ev("session.needs_answer"))
        self.assertEqual(c["payload"]["aps"]["interruption-level"], "time-sensitive")
        self.assertEqual(c["headers"]["priority"], 10)

    def test_p3_is_passive_priority_5_and_silent(self):
        c = compose(self.ev("account.limit_reset", account="work"))
        self.assertEqual(c["payload"]["aps"]["interruption-level"], "passive")
        self.assertEqual(c["headers"]["priority"], 5)
        self.assertNotIn("sound", c["payload"]["aps"])

    def test_a_discrete_fact_never_collapses(self):
        """A collapse-id supersedes an undelivered push — on a discrete fact it
        deletes history, so none of ours sets it."""
        c = compose(self.ev("session.needs_answer"))
        self.assertIsNone(c["headers"]["collapse_id"])

    def test_thread_id_groups_by_worktree(self):
        c = compose(self.ev(), server="studio-mac")
        self.assertEqual(c["payload"]["aps"]["thread-id"],
                         "studio-mac|ConfidAI-auth")


# ------------------------------------------------------------------ notifier

class RecordingSink:
    """A sink that records every send and answers 200. Stands in for APNs."""

    name = "apns"

    def __init__(self, response=None):
        from orchestra import push
        self.sent = []
        self._resp = response or push.Response(status=200, apns_id="A")

    def send(self, token, payload, **kw):
        self.sent.append({"token": token, "payload": payload, "headers": kw})
        return self._resp


class TestNotifierSuppression(unittest.TestCase):
    """The gates, each proven by feeding a sequence and asserting on pushes."""

    def notifier(self, prefs=None, **kw):
        self.sink = RecordingSink()
        return Notifier(sink=self.sink, log=EventLog(), prefs=prefs,
                        budget=Budget(), **kw)

    def push_seq(self, n, *projections, token="ab", prefs=None, counts=None):
        pushed = []
        for i, p in enumerate(projections):
            pushed.append(n.observe(p, now=1000.0 + i, device_token=token,
                                    counts=counts))
        return pushed

    def test_a_your_turn_is_off_by_default_and_pushes_nothing(self):
        """The single most important default: `waiting` happens at the end of
        every turn, and pushing it gets notifications disabled in a week.

        The third observe is PAST your_turn's 20 s dwell, so a session held at
        waiting would flush from `_pending` if the default let it through — the
        test isolates the DEFAULT from the dwell, and fails if the default is
        flipped to on."""
        n = self.notifier()
        p1 = proj(sessions={"s1": sess("working")})
        p2 = proj(sessions={"s1": sess("waiting")})
        n.observe(p1, now=1000.0, device_token="ab")
        n.observe(p2, now=1001.0, device_token="ab")     # edge into waiting
        n.observe(p2, now=1030.0, device_token="ab")     # 29 s later, past dwell
        self.assertEqual(self.sink.sent, [])
        # but it IS in the durable log — the badge and the app feed still see it
        self.assertEqual(len(n.log.since()["events"]), 1)

    def test_a_held_condition_pushes_once(self):
        """Dedup is structural: a question HELD across many sweeps is one edge
        and one push. This fails if `derive` ever fired on an unchanged status
        — the fifty-notifications-for-one-question bug."""
        n = self.notifier()
        p_w = proj(sessions={"s1": sess("working")})
        p_n = proj(sessions={"s1": sess("needs_input")})
        self.push_seq(n, p_w, p_n, p_n, p_n, p_n)
        self.assertEqual(len(self.sink.sent), 1)

    def test_a_recurring_condition_fires_each_episode(self):
        """The counterpart, and the reason a permanent 'already fired' set was
        wrong: a worktree freed, taken, and freed again is TWO real events and
        must push twice. A suppression set would swallow the second forever."""
        n = self.notifier(prefs=Preferences(rules={"worktree.free": True}))
        free = proj(worktrees={"wt": "free"})
        busy = proj(worktrees={"wt": "busy"})
        self.push_seq(n, busy, free, busy, free)
        self.assertEqual(len(self.sink.sent), 2)

    def test_a_re_asked_question_is_a_new_push(self):
        """A session answered and re-asking is a distinct question — the
        generation counter makes it a new condition, and it pushes again."""
        n = self.notifier()
        work = proj(sessions={"s1": sess("working")})
        ask = proj(sessions={"s1": sess("needs_input")})
        self.push_seq(n, work, ask, work, ask)
        self.assertEqual(len(self.sink.sent), 2)

    def test_a_flap_within_the_dwell_does_not_push(self):
        """needs_input has dwell 0, but blocked has 40s. A block that resolves
        before its dwell must push nothing — 'awaiting approval' and 'tool
        running' are the same bytes until the silence outlasts a real tool."""
        n = self.notifier()
        p_w = proj(sessions={"s1": sess("working")})
        p_b = proj(sessions={"s1": sess("blocked")})
        p_ok = proj(sessions={"s1": sess("working")})
        n.observe(p_w, now=1000.0, device_token="ab")
        n.observe(p_b, now=1005.0, device_token="ab")   # blocked (dwell 40s)
        n.observe(p_ok, now=1010.0, device_token="ab")  # resolved inside dwell
        # …and a sweep PAST the original dwell. The resolving edge must have
        # cancelled the arm — otherwise the stale pending fires here, on a
        # session that is no longer blocked, which is the flap.
        n.observe(p_ok, now=1060.0, device_token="ab")
        self.assertEqual(self.sink.sent, [],
                         "a block that resolved inside its dwell must not push")

    def test_a_block_that_holds_past_dwell_pushes(self):
        n = self.notifier()
        p_w = proj(sessions={"s1": sess("working")})
        p_b = proj(sessions={"s1": sess("blocked")})
        n.observe(p_w, now=1000.0, device_token="ab")
        n.observe(p_b, now=1001.0, device_token="ab")
        self.assertEqual(self.sink.sent, [])          # still dwelling
        n.observe(p_b, now=1050.0, device_token="ab")  # 49s later, still blocked
        self.assertEqual(len(self.sink.sent), 1)

    def test_three_agents_needing_you_is_one_notification(self):
        """Coalescing. A burst of the same kind in one sweep is one push
        carrying a count, not three lock-screen lines."""
        n = self.notifier()
        p0 = proj(sessions={"s1": sess("working"), "s2": sess("working"),
                            "s3": sess("working")})
        p1 = proj(sessions={"s1": sess("needs_input"),
                            "s2": sess("needs_input"),
                            "s3": sess("needs_input")})
        n.observe(p0, now=1000.0, device_token="ab")
        n.observe(p1, now=1001.0, device_token="ab")
        self.assertEqual(len(self.sink.sent), 1)
        self.assertEqual(self.sink.sent[0]["payload"]["counts"]["coalesced"], 3)

    def test_different_types_are_not_coalesced(self):
        """A needs-answer and a limit-reset are different actions and stay two
        lines even in the same sweep. (Both zero-dwell, so the test isolates
        coalescing from dwell.)"""
        n = self.notifier()
        p0 = proj(sessions={"s1": sess("working")},
                  accounts={"work": {"exhausted": True}})
        p1 = proj(sessions={"s1": sess("needs_input")},
                  accounts={"work": {"exhausted": False}})
        n.observe(p0, now=1000.0, device_token="ab")
        n.observe(p1, now=1001.0, device_token="ab")
        self.assertEqual(len(self.sink.sent), 2)

    def test_quiet_hours_hold_a_p2_but_still_log_it(self):
        prefs = Preferences(quiet_from="23:00", quiet_to="08:00",
                            tz_offset_min=0, rules={"account.limit_hit": True})
        n = self.notifier(prefs=prefs)
        p0 = proj(accounts={"work": {"exhausted": False}})
        p1 = proj(accounts={"work": {"exhausted": True}})
        two_am = 2 * 3600
        n.observe(p0, now=two_am, device_token="ab")
        n.observe(p1, now=two_am + 1, device_token="ab")
        self.assertEqual(self.sink.sent, [])           # held
        self.assertEqual(len(n.log.since()["events"]), 1)  # logged

    def test_quiet_hours_let_a_p1_through_when_allowed(self):
        prefs = Preferences(quiet_from="23:00", quiet_to="08:00",
                            tz_offset_min=0, quiet_allow_p1=True)
        n = self.notifier(prefs=prefs)
        p0 = proj(sessions={"s1": sess("working")})
        p1 = proj(sessions={"s1": sess("needs_input")})
        two_am = 2 * 3600
        n.observe(p0, now=two_am, device_token="ab")
        n.observe(p1, now=two_am + 1, device_token="ab")
        self.assertEqual(len(self.sink.sent), 1)

    def test_the_budget_drops_the_push_and_keeps_the_fact(self):
        n = self.notifier()
        n.budget = Budget(per_hour={"P1": 2})
        # five distinct questions in the same hour; only two may push.
        for i in range(5):
            p_w = proj(sessions={f"s{i}": sess("working")})
            p_n = proj(sessions={f"s{i}": sess("needs_input")})
            n.observe(p_w, now=1000.0 + i, device_token="ab")
            n.observe(p_n, now=1000.5 + i, device_token="ab")
        self.assertEqual(len(self.sink.sent), 2)
        self.assertEqual(len(n.log.since(limit=200)["events"]), 5)

    def test_the_pipeline_runs_to_completion_with_no_sink(self):
        """The state the user is in until they create a key: every gate runs,
        the event is logged, only the last hop is a no-op — indistinguishable
        from working to everything above it."""
        n = Notifier(sink=None, log=EventLog())
        p_w = proj(sessions={"s1": sess("working")})
        p_n = proj(sessions={"s1": sess("needs_input")})
        n.observe(p_w, now=1000.0)
        pushed = n.observe(p_n, now=1001.0)
        self.assertEqual(len(pushed), 1)               # decided to push
        self.assertEqual(len(n.log.since()["events"]), 1)  # and logged

    def test_a_muted_notifier_pushes_nothing(self):
        prefs = Preferences(muted_until=2000.0)
        n = self.notifier(prefs=prefs)
        p_w = proj(sessions={"s1": sess("working")})
        p_n = proj(sessions={"s1": sess("needs_input")})
        n.observe(p_w, now=1000.0, device_token="ab")
        n.observe(p_n, now=1001.0, device_token="ab")
        self.assertEqual(self.sink.sent, [])


class TestNotifierBackoff(unittest.TestCase):

    def test_a_retriable_failure_holds_the_next_push(self):
        from orchestra import push
        sink = RecordingSink(response=push.Response(status=503))
        n = Notifier(sink=sink, log=EventLog())
        p_w = proj(sessions={"s1": sess("working")})
        p_n1 = proj(sessions={"s1": sess("needs_input")})
        p_w2 = proj(sessions={"s2": sess("working")})
        p_n2 = proj(sessions={"s2": sess("needs_input")})
        n.observe(p_w, now=1000.0, device_token="ab")
        n.observe(p_n1, now=1001.0, device_token="ab")   # 503 → backoff armed
        self.assertEqual(len(sink.sent), 1)
        n.observe(p_w2, now=1001.5, device_token="ab")
        n.observe(p_n2, now=1002.0, device_token="ab")   # inside backoff window
        self.assertEqual(len(sink.sent), 1, "a 503 must hold the next send")


class TestPushRoutesOnTheWire(unittest.TestCase):
    """The self-service push routes end to end, over a real socket, with a real
    pairing-minted token. Reuses the auth suite's wire harness so the door is
    the shipped one, not a mock — a route that authenticates differently under
    test than in production is the failure this proves against."""

    @classmethod
    def setUpClass(cls):
        import tests.test_auth as ta
        cls.ta = ta

    def setUp(self):
        import threading
        self.dir = Path(tempfile.mkdtemp(prefix="fb-notify-"))
        self._saved = (fb.auth.REGISTRY, fb.auth.AUDIT_LOG)
        fb.auth.REGISTRY = self.dir / "devices.json"
        fb.auth.AUDIT_LOG = self.dir / "audit.log.jsonl"
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        self._cfg = dict(fb.CFG)
        fb.config.DEMO = True
        self._svc = notify._service
        notify._service = notify.Service(log_path=str(self.dir / "events.json"))
        self.srv = fb.Server(("127.0.0.1", 0), self.ta.RemoteHandler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        fb.auth.REGISTRY, fb.auth.AUDIT_LOG = self._saved
        fb.auth._forget_registry()
        fb.config.DEMO = False
        notify._service = self._svc
        fb.CFG.clear()
        fb.CFG.update(self._cfg)
        __import__("shutil").rmtree(self.dir, ignore_errors=True)

    def req(self, method, path, token=None, body=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        head = {}
        if token:
            head["Authorization"] = f"Bearer {token}"
        if body is not None:
            head["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=head)
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def test_a_paired_device_registers_its_own_push_token(self):
        pub, token = fb.auth.add_device("iPhone")
        body = json.dumps({"backend": "apns", "token": "cd" * 32,
                           "environment": "sandbox",
                           "settings": {"time_sensitive_allowed": False}})
        status, blob = self.req("POST", "/api/v1/devices/self/push",
                                token=token, body=body)
        self.assertEqual(status, 200)
        payload = json.loads(blob)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["warnings"], "a false time_sensitive must warn")
        # it was stored against THIS device, hash-free and token-carrying
        stored = fb.auth.get_push(pub["id"])
        self.assertEqual(stored["token"], "cd" * 32)

    def test_a_malformed_token_is_422(self):
        _, token = fb.auth.add_device("iPhone")
        status, blob = self.req("POST", "/api/v1/devices/self/push",
                                token=token,
                                body=json.dumps({"token": "nothex"}))
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(blob)["error"], "push_token_invalid")

    def test_loopback_cannot_register_a_device_it_is_nobody(self):
        """These routes identify the caller by its token; a trusted-loopback
        request holds none, so it is refused rather than silently writing to a
        device that does not exist. Needs the loopback handler — the class's
        default handler fakes a tailnet peer, which is refused at the door
        before it can reach this branch."""
        import http.client
        import threading
        srv = fb.Server(("127.0.0.1", 0), self.ta.LoopbackHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/v1/devices/self/push",
                         body=json.dumps({"token": "cd" * 32}),
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            status, blob = r.status, r.read()
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(blob)["error"], "device_required")

    def test_the_self_route_is_not_admin_and_a_phone_reaches_it(self):
        """The carve-out: /devices/self/* is read-scoped. A phone token — never
        issued admin — must reach it, or push is structurally impossible."""
        self.assertFalse(fb.auth.admin("POST", "/api/v1/devices/self/push"))
        self.assertTrue(fb.auth.admin("POST",
                        "/api/v1/devices/abc123/revoke"))

    def test_events_route_serves_the_durable_log(self):
        _, token = fb.auth.add_device("iPhone")
        # drive one event into the shared service log
        notify.service().log.append([Event(
            id="", at=1.0, type="session.needs_answer", level="P1",
            dedupe_key="session.needs_answer|s1|1", worktree="wt", open=True)])
        status, blob = self.req("GET", "/api/v1/events", token=token)
        self.assertEqual(status, 200)
        page = json.loads(blob)
        self.assertEqual(len(page["events"]), 1)
        self.assertEqual(page["events"][0]["type"], "session.needs_answer")
        # and /open reflects the still-open condition
        status, blob = self.req("GET", "/api/v1/events/open", token=token)
        self.assertIn("session.needs_answer|s1|1", json.loads(blob)["open"])

    def test_events_suffix_is_a_404_not_the_route(self):
        """`/api/v1/eventsX` must not reach the events route — the prefix-
        routing hole the whole codebase forbids (API.md §2.3)."""
        _, token = fb.auth.add_device("iPhone")
        status, _ = self.req("GET", "/api/v1/eventsX", token=token)
        self.assertEqual(status, 404)

    def test_push_test_route_reports_no_key(self):
        _, token = fb.auth.add_device("iPhone")
        self.req("POST", "/api/v1/devices/self/push", token=token,
                 body=json.dumps({"token": "cd" * 32, "environment": "sandbox"}))
        status, blob = self.req("POST", "/api/v1/push/test", token=token,
                                body="{}")
        self.assertEqual(status, 200)
        r = json.loads(blob)
        # no key configured in this test → the transport says so, precisely
        self.assertFalse(r["ok"])
        self.assertEqual(r["backend"], "none")


class TestFacade(unittest.TestCase):

    def test_the_package_exports_the_pipeline(self):
        self.assertIs(fb.derive, notify.derive)
        self.assertIs(fb.notify.Notifier, Notifier)


if __name__ == "__main__":
    unittest.main(verbosity=2)
