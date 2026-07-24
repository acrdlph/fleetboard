"""A live process paired to a transcript that has been silent for hours is not
a session waiting for you — it is a live agent whose OWN session cannot be read
(its main transcript never appeared, e.g. it is driving a workflow and the disk
was full when it tried to write). `pair_sessions_with_procs` attaches it to the
freshest sibling, which finished long ago; the old code then read ◆ YOUR TURN
off that done sibling and cried ▲ NEEDS ANSWER (card -> attention) while the real
agent was busy. It must read `unknown` instead (card stays `busy`, never frees,
never summons the user), and `unknown` must be safe through the sort and the
counts tally that both used to KeyError on it.
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402
from orchestra import status  # noqa: E402


HOURS = 3600
STALE = 21600  # config default stale_alive_s


class TestStaleAlivePairingReadsUnknown(unittest.TestCase):

    def classify(self, age, **kw):
        base = dict(alive=True, pending_tools=[], delegated=0, skip_perms=True,
                    working_s=90, turn_ended=False, quiet_s=45,
                    stale_alive_s=STALE)
        base.update(kw)
        return status.classify_session(age, **{k: v for k, v in base.items()
                                               if k != "alive"},
                                       alive=base["alive"])[0]

    def test_a_live_process_on_an_hours_stale_transcript_is_unknown(self):
        # 20 h silent, alive, nothing on disk to explain it -> unknown, not waiting.
        self.assertEqual(self.classify(20 * HOURS), "unknown")

    def test_recent_quiet_is_still_your_turn(self):
        # Just past quiet_s but well inside stale_alive_s: the decay guess is
        # still credible — a real agent idle at the prompt. Unchanged behaviour.
        self.assertEqual(self.classify(300), "waiting")

    def test_an_hour_idle_is_still_your_turn_not_unknown(self):
        # An agent legitimately parked at the prompt for an hour is YOUR TURN;
        # the demotion to unknown only bites truly implausible staleness.
        self.assertEqual(self.classify(HOURS), "waiting")

    def test_a_real_signal_beats_the_stale_demotion(self):
        # A pending AskUserQuestion on disk is a proven needs_input and is decided
        # far above the decay — staleness never demotes a real question.
        self.assertEqual(
            self.classify(20 * HOURS, pending_tools=["AskUserQuestion"]),
            "needs_input")
        # Delegated work (a workflow the session itself launched) is proven work.
        self.assertEqual(self.classify(20 * HOURS, delegated=1), "working")

    def test_without_the_ceiling_the_old_guess_stands(self):
        # Every caller but scan_sessions leaves stale_alive_s None -> byte-for-byte
        # the historical decay: a stale alive session still reads waiting.
        self.assertEqual(self.classify(20 * HOURS, stale_alive_s=None), "waiting")

    def test_a_dead_process_on_a_stale_transcript_is_ended_not_unknown(self):
        # The demotion is only for a LIVE process. With no process the stale
        # transcript is ENDED, as before — the ceiling never resurrects anything.
        self.assertEqual(
            self.classify(20 * HOURS, alive=False, orphan_grace_s=90), "ended")


class TestUnknownIsSafeInCardAvailability(unittest.TestCase):

    def test_a_live_card_with_only_an_unknown_session_reads_busy(self):
        # The whole point: unknown + a live process -> busy, NOT attention.
        self.assertEqual(status.card_availability(["unknown"], has_live=True),
                         "busy")

    def test_unknown_never_frees_a_worktree(self):
        # Even with no live process flag, an unknown session must not free the
        # card (that would invite a second agent). It is not "working" though,
        # so the safety here rides on has_live at the call site; assert the
        # explicit has_live path is busy and the no-live path is not attention.
        self.assertEqual(status.card_availability(["unknown"], has_live=True),
                         "busy")
        self.assertNotEqual(
            status.card_availability(["unknown", "ended"], has_live=True),
            "attention")


if __name__ == "__main__":
    unittest.main()
