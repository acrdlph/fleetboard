"""The inline-reply affordance rides on `aps.category`, which iOS reads AT
DELIVERY to decide whether to hang a reply text field on the banner — no app
code (and no NSE) runs first. Before this, `compose` never put a category on the
wire, so a delivered banner had no Reply button however the app was built. These
pin that the wire now carries the category the app registered, and that the
answerable rule stays in step with `ios Push.isAnswerable`.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402
from orchestra import notify  # noqa: E402
from orchestra.notify import Event, compose  # noqa: E402


def _ev(type_, session_id=None, **kw):
    return Event(id="evt-1", at=1784636700.0, type=type_, dedupe_key="k",
                 level=notify.EVENT_TYPES.get(type_, {"level": "P2"})["level"],
                 worktree="ConfidAI-auth", session_id=session_id, **kw)


class TestReplyCategoryOnTheWire(unittest.TestCase):

    def cat(self, type_, session_id=None):
        return compose(_ev(type_, session_id=session_id))["payload"]["aps"]["category"]

    def test_answerable_with_a_session_gets_the_reply_category(self):
        self.assertEqual(self.cat("session.needs_answer", "sid-abc"), "ORC_REPLY")
        self.assertEqual(self.cat("session.blocked", "sid-abc"), "ORC_REPLY")

    def test_answerable_without_a_session_is_info_not_a_dead_reply_box(self):
        # A reply has nowhere to go with no sid — offer the app, not a dead box.
        self.assertEqual(self.cat("session.needs_answer", None), "ORC_INFO")

    def test_a_nudge_is_info_not_reply(self):
        # your_turn / died are notices, not questions.
        self.assertEqual(self.cat("session.your_turn", "sid-abc"), "ORC_INFO")
        self.assertEqual(self.cat("session.died", "sid-abc"), "ORC_INFO")

    def test_every_compose_names_a_registered_category(self):
        # An absent category = the app's default = no reply field ever, so the
        # wire must always name one the app registered.
        for t in ("session.needs_answer", "session.blocked",
                  "session.your_turn", "session.died"):
            self.assertIn(self.cat(t, "sid"), {"ORC_REPLY", "ORC_INFO"})

    def test_the_identifiers_and_rule_match_the_ios_client(self):
        # ios/Sources/Orchestra/Model/Push.swift — PushCategory.reply/.info and
        # Push.isAnswerable. If these drift, the button is dead or goes nowhere.
        self.assertEqual(notify.REPLY_CATEGORY, "ORC_REPLY")
        self.assertEqual(notify.INFO_CATEGORY, "ORC_INFO")
        self.assertEqual(notify.ANSWERABLE_EVENTS,
                         frozenset({"session.needs_answer", "session.blocked"}))


if __name__ == "__main__":
    unittest.main()
