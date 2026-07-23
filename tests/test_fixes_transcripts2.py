#!/usr/bin/env python3
"""Wave-2 regression tests for the TRANSCRIPTS2 classification fixes.

Covers five findings in orchestra/transcripts.py, all of which pin a session at
the wrong status forever off evidence that should have expired:

  F1  an interrupted tool_use is never popped from `pending`, so the session
      reads ● WORKING / ■ BLOCKED / ▲ NEEDS ANSWER across every later turn
      (FRESHNESS.md §4.2). Expire it on the interrupt marker and on
      `turn_duration`; refuse the marker as the user's last words.
  F3  the background-launch receipt is structural, not "will be notified"
      appearing anywhere — a read-only tool's output quoting the phrase is not
      a launch and must not hold the session ● WORKING for a `delegated_s`.
  F5  the CLI's own end-of-turn workflow / background-agent counts get the same
      shelf life as a backgrounded tool_use, so a killed task stops pinning
      ● WORKING once `delegated_s` has passed.
  F6  the memo-drift and hook-veto counters are bumped under a lock, and the
      cold audit does not count world-movement as a memo lie (no false drift).
  F8  a session whose status is "unknown" (procs_known=False) must not crash
      the severity sort in scan_sessions.

    python3 -m unittest tests.test_fixes_transcripts2 -v
"""

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402
from orchestra import chat, config, status, transcripts  # noqa: E402


def _write(fp, entries):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with open(fp, "w") as f:
        for e in entries:
            f.write((e if isinstance(e, str) else json.dumps(e)) + "\n")


def _iso(ago_s=0.0):
    import datetime
    return datetime.datetime.fromtimestamp(
        time.time() - ago_s, datetime.timezone.utc).isoformat()


def _tool_use(name, tid="tu1"):
    return {"type": "assistant", "cwd": "/w",
            "message": {"model": "claude-fable-5", "content": [
                {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}


def _tool_result(text, tid="tu1", ts=None):
    e = {"type": "user", "cwd": "/w", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": text}]}}
    if ts is not None:
        e["timestamp"] = ts
    return e


# --------------------------------------------------------------- F1: interrupt

class TestInterruptExpiresPending(unittest.TestCase):
    """FRESHNESS.md §4.2: an interrupted turn writes '[Request interrupted by
    user]' and NO tool_result, so the cut-off tool_use must be expired anyway."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _tail(self, entries):
        fp = self.tmp / "s.jsonl"
        _write(fp, entries)
        return transcripts.parse_session_tail(fp)

    def test_interrupt_marker_clears_pending_bash(self):
        # tool_use -> interrupt -> new prompt: the FRESHNESS §4.2 scenario.
        tail = self._tail([
            {"type": "user", "cwd": "/w", "message": {"content": "run the tests"}},
            _tool_use("Bash"),
            {"type": "user", "cwd": "/w",
             "message": {"content": "[Request interrupted by user]"}},
            {"type": "user", "cwd": "/w", "message": {"content": "never mind"}},
        ])
        self.assertEqual(tail["pending_tools"], [])

    def test_interrupt_marker_clears_pending_askuserquestion(self):
        # the worst variant: an interrupted question pinned ▲ NEEDS ANSWER
        # regardless of skip_perms and later turns.
        tail = self._tail([
            _tool_use("AskUserQuestion"),
            {"type": "user", "cwd": "/w",
             "message": {"content": "[Request interrupted by user]"}},
        ])
        self.assertEqual(tail["pending_tools"], [])

    def test_interrupt_marker_in_content_block_shape(self):
        # the marker can arrive as a content list, not just a bare string.
        tail = self._tail([
            _tool_use("Bash"),
            {"type": "user", "cwd": "/w", "message": {"content": [
                {"type": "text", "text": "[Request interrupted by user for tool use]"}]}},
        ])
        self.assertEqual(tail["pending_tools"], [])

    def test_turn_duration_clears_pending(self):
        # a turn cannot close while a tool genuinely awaits approval, so an
        # unresolved tool_use that outlived its turn_duration is expired too.
        tail = self._tail([
            _tool_use("Bash"),
            {"type": "system", "subtype": "turn_duration", "cwd": "/w",
             "durationMs": 10, "timestamp": _iso()},
        ])
        self.assertEqual(tail["pending_tools"], [])
        self.assertTrue(tail["turn_ended"])

    def test_genuinely_pending_tool_survives(self):
        # no interrupt, no turn_duration, no result: the tool IS still running
        # and must remain pending (the fix must not over-expire).
        tail = self._tail([
            {"type": "user", "cwd": "/w", "message": {"content": "go"}},
            _tool_use("Bash"),
        ])
        self.assertEqual(tail["pending_tools"], ["Bash"])

    def test_tool_launched_after_the_marker_is_still_pending(self):
        # positional: a later real tool_use re-populates pending after an
        # earlier interrupt cleared it.
        tail = self._tail([
            _tool_use("Bash", tid="tu1"),
            {"type": "user", "cwd": "/w",
             "message": {"content": "[Request interrupted by user]"}},
            {"type": "user", "cwd": "/w", "message": {"content": "try again"}},
            _tool_use("Grep", tid="tu2"),
        ])
        self.assertEqual(tail["pending_tools"], ["Grep"])

    def test_marker_is_not_quoted_as_the_users_last_words(self):
        tail = self._tail([
            {"type": "user", "cwd": "/w", "message": {"content": "the real question"}},
            _tool_use("Bash"),
            {"type": "user", "cwd": "/w",
             "message": {"content": "[Request interrupted by user]"}},
        ])
        self.assertEqual(tail["last_user"], "the real question")

    def test_real_prompt_refuses_the_marker(self):
        self.assertIsNone(transcripts._real_prompt("[Request interrupted by user]"))


# ------------------------------------------------------- F3: structural launch

class TestStructuralBackgroundLaunch(unittest.TestCase):
    """A launch is a RECEIPT (a backgrounding tool + a launch-shaped phrase in a
    short result), not the phrase 'will be notified' appearing anywhere."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _bg(self, tool, text):
        fp = self.tmp / "s.jsonl"
        _write(fp, [_tool_use(tool, tid="tu1"),
                    _tool_result(text, tid="tu1", ts=_iso())])
        return transcripts.parse_session_tail(fp)["bg_launched_at"]

    def test_real_bash_receipt_registers(self):
        got = self._bg("Bash", "Command running in background with ID: b3l1. "
                               "You will be notified when it completes.")
        self.assertEqual(len(got), 1)

    def test_workflow_receipt_registers(self):
        got = self._bg("Workflow", "Workflow launched in background. Task ID: w8. "
                                    "You'll be notified when it finishes.")
        self.assertEqual(len(got), 1)

    def test_read_output_quoting_the_promise_is_not_a_launch(self):
        # a Read whose file content quotes the notify promise — this is the
        # exact false positive: no notification ever pairs it.
        got = self._bg("Read", "def note():\n    # you will be notified when it "
                               "completes\n    return 1\n")
        self.assertEqual(got, ())

    def test_webfetch_quoting_the_promise_is_not_a_launch(self):
        got = self._bg("WebFetch", "Our system: you will be notified by email.")
        self.assertEqual(got, ())

    def test_large_blob_with_a_receipt_phrase_is_not_a_launch(self):
        # a backgrounding tool, but the phrase is buried in a huge blob — that
        # is quoted content, not a short harness receipt.
        blob = ("x" * (transcripts._BG_RECEIPT_MAX + 50)
                + " running in background with ID: fake. you will be notified")
        got = self._bg("Bash", blob)
        self.assertEqual(got, ())

    def test_bare_promise_without_a_receipt_shape_is_not_a_launch(self):
        got = self._bg("Bash", "You will be notified shortly.")
        self.assertEqual(got, ())


# ---------------------------------------------------- shared scan_sessions rig

class _ScanRig(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = self.tmp / ".claude"
        (self.home / "projects").mkdir(parents=True)
        self.wt = self.tmp / "repo"
        self.wt.mkdir()
        self._saved_homes = config.CFG.get("homes")
        config.CFG["homes"] = [str(self.home)]
        self.addCleanup(self._restore)
        fb.memo_clear()

    def _restore(self):
        config.CFG["homes"] = self._saved_homes
        fb.memo_clear()

    def _proj(self):
        p = self.home / "projects" / fb.munge(str(self.wt))
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _session(self, entries, sid="sess0001"):
        _write(self._proj() / f"{sid}.jsonl", entries)

    def _scan(self, procs=(), cold=False):
        return transcripts.scan_sessions(
            [{"path": str(self.wt)}], list(procs), time.time(), cold=cold)


# ---------------------------------------------------------- F5: delegated aging

class TestDelegatedShelfLife(_ScanRig):
    """The CLI's pendingWorkflowCount / pendingBackgroundAgentCount get the same
    `delegated_s` bound as a backgrounded tool_use."""

    def _turn_end(self, ago_s, wf=0, bg=0, dated=True):
        e = {"type": "system", "subtype": "turn_duration", "cwd": str(self.wt),
             "durationMs": 10, "pendingWorkflowCount": wf,
             "pendingBackgroundAgentCount": bg}
        if dated:
            e["timestamp"] = _iso(ago_s)
        return e

    def test_recent_dated_count_is_kept(self):
        self._session([
            {"type": "user", "cwd": str(self.wt), "message": {"content": "delegate"}},
            {"type": "assistant", "cwd": str(self.wt),
             "message": {"content": [{"type": "text", "text": "on it"}]}},
            self._turn_end(ago_s=5, wf=1),
        ])
        s = self._scan()[str(self.wt)][0]
        self.assertEqual(s["pending_workflows"], 1)

    def test_stale_dated_count_ages_out(self):
        # a workflow the CLI reported 10 minutes past `delegated_s` ago whose
        # task was killed: the count no longer explains the session.
        stale = config.CFG["delegated_s"] + 120
        self._session([
            {"type": "user", "cwd": str(self.wt), "message": {"content": "delegate"}},
            {"type": "assistant", "cwd": str(self.wt),
             "message": {"content": [{"type": "text", "text": "on it"}]}},
            self._turn_end(ago_s=stale, wf=1, bg=2),
        ])
        s = self._scan()[str(self.wt)][0]
        self.assertEqual(s["pending_workflows"], 0)
        self.assertEqual(s["pending_bg_agents"], 0)

    def test_undated_count_degrades_to_unbounded_trust(self):
        # an undated turn_duration cannot be aged; it keeps the prior behaviour
        # rather than silently dropping a live delegation (older CLIs / the
        # synthetic entries test_integration writes).
        self._session([
            {"type": "user", "cwd": str(self.wt), "message": {"content": "delegate"}},
            {"type": "assistant", "cwd": str(self.wt),
             "message": {"content": [{"type": "text", "text": "on it"}]}},
            self._turn_end(ago_s=0, wf=3, dated=False),
        ])
        s = self._scan()[str(self.wt)][0]
        self.assertEqual(s["pending_workflows"], 3)


# ------------------------------------------------------------- F8: unknown rank

class TestUnknownStatusSort(_ScanRig):
    """'unknown' (procs_known=False) must not KeyError the severity sort."""

    def test_scan_sessions_survives_unknown_status(self):
        self._session([
            {"type": "user", "cwd": str(self.wt), "message": {"content": "hello"}},
            {"type": "assistant", "cwd": str(self.wt),
             "message": {"content": [{"type": "text", "text": "hi"}]}},
        ])
        saved = status.classify_session
        # patched through the module seam, the way scan_sessions reaches it.
        transcripts.status.classify_session = lambda *a, **k: ("unknown", False)
        self.addCleanup(setattr, transcripts.status, "classify_session", saved)
        by_wt = self._scan()                     # must not raise
        sessions = by_wt[str(self.wt)]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["status"], "unknown")


# ------------------------------------------------------------------ F6: drift

class TestDriftCounters(_ScanRig):

    def test_bump_drift_increments_under_the_lock(self):
        m = transcripts.StatMemo(4)
        self.assertEqual(m.drift, 0)
        m.bump_drift()
        m.bump_drift()
        self.assertEqual(m.drift, 2)

    def test_concurrent_bump_drift_loses_nothing(self):
        # the unlocked `+= 1` this replaced dropped increments under contention.
        m = transcripts.StatMemo(4)

        def hammer():
            for _ in range(2000):
                m.bump_drift()

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(m.drift, 4 * 2000)

    def test_tree_dir_keys_reports_the_structure(self):
        sub = self.tmp / "tree"
        (sub / "a" / "b").mkdir(parents=True)
        (sub / "a" / "x.jsonl").write_text("{}\n")
        keys = transcripts._tree_dir_keys(sub)
        self.assertIn(str(sub), keys)
        self.assertIn(str(sub / "a"), keys)
        self.assertIn(str(sub / "a" / "b"), keys)

    def test_cold_scan_over_a_stable_tree_records_no_drift(self):
        # warm the memo, then a cold audit over an unchanging fleet must count
        # zero drift — the invariant the whole memo rests on.
        self._session([
            {"type": "user", "cwd": str(self.wt), "message": {"content": "go"}},
            {"type": "assistant", "cwd": str(self.wt),
             "message": {"content": [{"type": "text", "text": "done"}]}},
        ])
        sub = self._proj() / "sess0001"          # the subagent tree
        (sub / "child").mkdir(parents=True)
        _write(sub / "child" / "sub.jsonl", [
            {"type": "assistant",
             "message": {"content": [{"type": "text", "text": "subagent said"}]}}])
        self._scan()                              # warm
        before = fb.memo_drift()
        self._scan(cold=True)                     # audit a stable tree
        self.assertEqual(fb.memo_drift(), before)


if __name__ == "__main__":
    unittest.main()
