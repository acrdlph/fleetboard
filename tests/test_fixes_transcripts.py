#!/usr/bin/env python3
"""Regression tests for the TRANSCRIPTS crash-guard fixes.

One valid-JSON line that is NOT an object — `42`, `"str"`, `[1,2]`, `null` —
used to reach `.get(...)` on a non-dict in session_topic, find_last_user,
last_assistant_text (transcripts.py) and read_chat (chat.py), raising
AttributeError and aborting the whole sweep (the head never scrolls away, so
one such line in an in-window transcript froze the board for up to 48 h).
`parse_session_tail` already guarded this with `isinstance(e, dict)`; these
tests pin the guard in the four loops that lacked it.

    python3 -m unittest tests.test_fixes_transcripts -v
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402
from orchestra import chat, config, transcripts  # noqa: E402

# The four valid-JSON, non-object shapes a garbage line can take.
SCALAR_LINES = ["42", '"a bare string"', "[1, 2, 3]", "null"]


def _write(fp, lines):
    fp.parent.mkdir(parents=True, exist_ok=True)
    with open(fp, "w") as f:
        for ln in lines:
            f.write(ln + "\n")


def _obj(e):
    return json.dumps(e)


class TestScalarLineGuards(unittest.TestCase):
    """The three transcripts.py readers must skip a non-object line, not raise
    on it, and still return the fact carried by the real entries around it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))

    def test_session_topic_survives_scalar_head_lines(self):
        fp = self.tmp / "s.jsonl"
        _write(fp, SCALAR_LINES + [
            _obj({"type": "user", "message": {"role": "user",
                                              "content": "fix the parser"}})])
        # Before the guard this raised AttributeError on `42.get(...)`.
        self.assertEqual(transcripts.session_topic(fp), "fix the parser")

    def test_last_assistant_text_survives_scalar_tail_lines(self):
        fp = self.tmp / "s.jsonl"
        _write(fp, [
            _obj({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "done"}]}}),
        ] + SCALAR_LINES)
        self.assertEqual(transcripts.last_assistant_text(fp), "done")

    def test_find_last_user_survives_scalar_tail_lines(self):
        fp = self.tmp / "s.jsonl"
        _write(fp, [
            _obj({"type": "user", "message": {"role": "user",
                                              "content": "run the tests"}}),
        ] + SCALAR_LINES)
        self.assertEqual(transcripts.find_last_user(fp), "run the tests")

    def test_every_scalar_shape_is_tolerated_individually(self):
        # Each garbage shape on its own must not crash any of the three loops.
        for scalar in SCALAR_LINES:
            fp = self.tmp / "one.jsonl"
            _write(fp, [scalar,
                        _obj({"type": "user",
                              "message": {"content": "hi"}}),
                        _obj({"type": "assistant",
                              "message": {"content": [{"type": "text",
                                                       "text": "yo"}]}})])
            self.assertEqual(transcripts.session_topic(fp), "hi", scalar)
            self.assertEqual(transcripts.find_last_user(fp), "hi", scalar)
            self.assertEqual(transcripts.last_assistant_text(fp), "yo", scalar)


class TestReadChatScalarGuard(unittest.TestCase):
    """chat.read_chat is the reader behind /api/chat; a scalar line in the tail
    must not take it down."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.home = self.tmp / ".claude"
        (self.home / "projects").mkdir(parents=True)
        self._saved_homes = transcripts.claude_homes
        transcripts.claude_homes = lambda: [self.home]
        self.addCleanup(setattr, transcripts, "claude_homes", self._saved_homes)
        self.account = config.account_label(self.home)

    def test_read_chat_survives_scalar_line(self):
        fp = self.home / "projects" / "proj" / "s1.jsonl"
        _write(fp, [
            _obj({"type": "user", "message": {"content": "hello there"}}),
        ] + SCALAR_LINES + [
            _obj({"type": "assistant",
                  "message": {"content": [{"type": "text",
                                           "text": "hi back"}]}}),
        ])
        out = chat.read_chat(self.account, "s1")
        self.assertTrue(out["ok"], out)
        roles = [(m["role"], m["text"]) for m in out["messages"]]
        self.assertIn(("you", "hello there"), roles)
        self.assertIn(("agent", "hi back"), roles)


class TestScanSessionsScalarGuard(unittest.TestCase):
    """End to end: a scalar line in an in-window transcript must not abort the
    whole sweep. Before the fix, session_topic/find_last_user run inside
    _read_facts on every in-window transcript, so one such line raised into
    scan_sessions."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        self.home = self.tmp / ".claude"
        (self.home / "projects").mkdir(parents=True)
        self.wt = self.tmp / "repo"
        self.wt.mkdir()
        self._saved_homes = config.CFG.get("homes")
        config.CFG["homes"] = [str(self.home)]
        self.addCleanup(self._restore_homes)
        fb.memo_clear()

    def _restore_homes(self):
        config.CFG["homes"] = self._saved_homes
        fb.memo_clear()

    def test_scan_sessions_survives_scalar_transcript_line(self):
        proj = self.home / "projects" / fb.munge(str(self.wt))
        fp = proj / "sess1234.jsonl"
        _write(fp, SCALAR_LINES + [
            _obj({"type": "user", "cwd": str(self.wt),
                  "message": {"content": "do the thing"}}),
            _obj({"type": "assistant", "cwd": str(self.wt),
                  "message": {"content": [{"type": "text",
                                           "text": "working on it"}]}}),
        ])
        worktrees = [{"path": str(self.wt)}]
        by_wt = transcripts.scan_sessions(worktrees, [], time.time())
        sessions = by_wt[str(self.wt)]
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["topic"], "do the thing")


if __name__ == "__main__":
    unittest.main()
