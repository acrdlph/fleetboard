#!/usr/bin/env python3
"""orchestra integration tests — exercise the REAL pipeline against controlled
fixtures (temp git repos, temp Claude homes with real transcripts, a real tmux
session), not demo data. Deterministic: no dependency on the user's live fleet.

Live-system inputs that would make the read pipeline non-deterministic
(`ps`/`lsof` for processes, `cclimits` for limits) are stubbed to empty so the
git + transcript code paths run for real. The tmux test uses its own socket and
a plain shell — it never launches `claude`.

    python3 -m unittest discover -s tests
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402

HAVE_GIT = shutil.which("git") is not None
HAVE_TMUX = shutil.which("tmux") is not None


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


def write_transcript(path, cwd, branch, *, sid, entries):
    """Write a Claude Code .jsonl transcript at <home>/projects/<munged>/<sid>.jsonl."""
    proj = path / "projects" / fb.munge(str(cwd))
    proj.mkdir(parents=True, exist_ok=True)
    fp = proj / f"{sid}.jsonl"
    with open(fp, "w") as f:
        for e in entries:
            e.setdefault("cwd", str(cwd))
            e.setdefault("gitBranch", branch)
            f.write(json.dumps(e) + "\n")
    return fp


def user_msg(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_msg(text=None, tool=None):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    if tool:
        content.append({"type": "tool_use", "id": "tu_1", "name": tool, "input": {}})
    return {"type": "assistant", "message": {"role": "assistant",
            "model": "claude-fable-5", "content": content}}


def turn_end(pending_workflows=0, pending_bg_agents=0):
    return {"type": "system", "subtype": "turn_duration",
            "durationMs": 1000, "pendingWorkflowCount": pending_workflows,
            "pendingBackgroundAgentCount": pending_bg_agents}


def iso(ago_s=0.0):
    return datetime.datetime.fromtimestamp(
        time.time() - ago_s, datetime.timezone.utc).isoformat()


# The two halves of a background launch, exactly as the CLI writes them: the
# tool_use, and a tool_result that RESOLVES it while the work carries on
# elsewhere. `receipt` is the harness's own wording — the phrase is the signal.
BG_RECEIPTS = {
    "Bash": "Command running in background with ID: b3l1ahei. Output is being "
            "written to: /tmp/tasks/b3l1ahei.output. You will be notified when "
            "it completes.",
    "BashTimeout": "Command did not complete within its 180s timeout and was "
                   "moved to the background (ID: bx1dek5dm). You will be "
                   "notified when it completes.",
    # note where the phrase sits: nine lines below the headline, past the
    # transcript dir, the script path and the resume instructions. Matching the
    # first line of a receipt would miss this one.
    "Workflow": "Workflow launched in background. Task ID: w8oz82r5k\n"
                "Summary: verify the findings\n"
                "Transcript dir: /tmp/subagents/workflows/wf_49ffc8f0\n"
                "Run ID: wf_49ffc8f0\n\n"
                "You will be notified when it completes. Use /workflows to "
                "watch live progress.",
    "Agent": "Async agent launched successfully.\nagentId: a9f3cc3f87252\n"
             "The agent is working in the background. You will be notified "
             "automatically when it completes.",
    "Monitor": "Monitor started (task b1h0ax9vj, persistent — runs until "
               "TaskStop or session end). You will be notified on each event.",
}


def bg_launch(tid="toolu_bg1", tool="Bash", receipt="Bash", ago_s=0.0, dated=True):
    stamp = {"timestamp": iso(ago_s)} if dated else {}
    return [
        {"type": "assistant", **stamp, "message": {
            "role": "assistant", "model": "claude-fable-5", "content": [
                {"type": "tool_use", "id": tid, "name": tool,
                 "input": {"command": "sleep 900", "run_in_background": True}}]}},
        {"type": "user", **stamp, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid,
             "content": BG_RECEIPTS[receipt] if receipt in BG_RECEIPTS else receipt}]}},
    ]


def notification(tid="toolu_bg1", status="completed", shape="user"):
    body = ("<task-notification>\n<task-id>b3l1ahei</task-id>\n"
            + (f"<tool-use-id>{tid}</tool-use-id>\n" if tid else "")
            + (f"<status>{status}</status>\n" if status else "")
            + "<summary>Background command \"sleep\" completed</summary>\n"
              "</task-notification>")
    if shape == "user":
        return {"type": "user", "message": {"role": "user", "content": body}}
    if shape == "queue-operation":
        return {"type": "queue-operation", "operation": "enqueue", "content": body}
    if shape == "attachment":
        return {"type": "attachment", "attachment": {
            "type": "queued_command", "prompt": body,
            "commandMode": "task-notification"}}
    raise ValueError(shape)


class _Fleet(unittest.TestCase):
    """One real git worktree, one real Claude home, live inputs stubbed empty.
    No tests of its own — `setUp`/`tearDown` only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-it-"))
        self.root = self.tmp / "code"; self.root.mkdir()
        self.home = self.tmp / "home"; (self.home / "projects").mkdir(parents=True)
        # a real git repo acting as a worktree
        self.repo = self.root / "myapp"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t.t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "README").write_text("hi\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "initial commit")
        (self.repo / "dirtyfile").write_text("x\n")   # one uncommitted file

        # point orchestra at the fixtures; neutralize live-system inputs
        self._save = {k: fb.CFG.get(k) for k in
                      ("roots", "homes", "pattern", "exclude_accounts",
                       "reserve_percent", "quiet_s", "working_s", "delegated_s",
                       "block_grace_s", "orphan_grace_s")}
        self._demo, self._procs, self._cl = (fb.config.DEMO,
                                             fb.procs.claude_processes,
                                             fb.limits.cached_limits)
        fb.config.DEMO = False
        fb.CFG["roots"] = [str(self.root)]
        fb.CFG["homes"] = [str(self.home)]
        fb.CFG["pattern"] = ""
        fb.CFG["exclude_accounts"] = []
        fb.CFG["reserve_percent"] = {}
        fb.procs.claude_processes = lambda **_: []                       # no live procs
        fb.limits.cached_limits = lambda refresh=False: {"available": False}
        fb._cache["state"] = None                              # bust the 4s cache
        self._closeouts = dict(fb._closeouts)
        # The stat memo outlives a collect by design — never across a fixture,
        # or one test's inodes answer for another's. Its counters are
        # process-lifetime health readings (that is the point of `scan_drift`),
        # so a test that deliberately poisons the memo has to put them back or
        # every later assertion on `drift` inherits the lie.
        fb.memo_clear()
        self._counters = [(m, m.hits, m.misses, m.evictions, m.drift)
                          for m in (fb.transcripts._FACTS, fb.transcripts._TREES)]
        for m, *_ in self._counters:
            m.hits = m.misses = m.evictions = m.drift = 0

    def tearDown(self):
        fb.memo_clear()
        for m, h, mi, e, d in self._counters:
            m.hits, m.misses, m.evictions, m.drift = h, mi, e, d
        fb._closeouts.clear()
        fb._closeouts.update(self._closeouts)
        for k, v in self._save.items():
            if v is None:
                fb.CFG.pop(k, None)
            else:
                fb.CFG[k] = v
        (fb.config.DEMO, fb.procs.claude_processes,
         fb.limits.cached_limits) = self._demo, self._procs, self._cl
        fb._cache["state"] = None
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestCollectPipeline(_Fleet):
    """discover_worktrees + git_info + scan_sessions + collect_state, for real."""

    def test_worktree_and_git_info(self):
        wts = fb.discover_worktrees()
        self.assertEqual([w["name"] for w in wts], ["myapp"])
        g = fb.git_info(wts[0]["git"])
        self.assertEqual(g["branch"], "main")
        self.assertEqual(g["dirty"], 1)                        # the uncommitted file
        self.assertEqual(g["commit"]["subject"], "initial commit")

    def test_transcript_parsed_into_session(self):
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="Working on the login screen now"),
            user_msg("/model opus"),          # slash stub — must NOT become the topic
            turn_end(),
        ])
        st = fb.collect_state()
        card = next(w for w in st["worktrees"] if w["name"] == "myapp")
        self.assertEqual(len(card["sessions"]), 1)
        s = card["sessions"][0]
        self.assertEqual(s["account"], "home")                # account_label of the temp home
        self.assertEqual(s["topic"], "Build a login screen")  # slash stub skipped
        self.assertEqual(s["last_assistant"], "Working on the login screen now")

    def test_last_write_at_is_absolute_not_now_derived(self):
        # ENGINE.md §3.4: no wire field may be derived from now(). last_write_at
        # is the transcript's own mtime, so two collections a second apart yield
        # the SAME number — which is what lets the equality diff mean something.
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="done"), turn_end()])
        stamp = time.time() - 30
        os.utime(fp, (stamp, stamp))
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertAlmostEqual(s["last_write_at"], stamp, places=3)
        fb._cache["state"] = None
        again = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertEqual(again["last_write_at"], s["last_write_at"])

    def test_last_write_at_follows_the_subagent_tree(self):
        # a Workflow writes only under <sid>/ while the main transcript sits
        # untouched — the same max() age_s is built from, so the absolute must
        # track it too or the client animates the wrong clock.
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("delegate everything"), assistant_msg(text="delegated"),
            turn_end()])
        old = time.time() - 600
        os.utime(fp, (old, old))
        sub = fp.with_suffix("")
        sub.mkdir()
        sf = sub / "agent-1.jsonl"
        sf.write_text(json.dumps(assistant_msg(text="subagent reporting")) + "\n")
        fresh = time.time() - 5
        os.utime(sf, (fresh, fresh))
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertAlmostEqual(s["last_write_at"], fresh, places=3)

    def test_fresh_session_working_old_session_ended(self):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="done"), turn_end()])
        # fresh file, no live proc → recent mtime classifies as WORKING
        st = fb.collect_state()
        self.assertEqual(st["worktrees"][0]["sessions"][0]["status"], "working")
        self.assertEqual(st["worktrees"][0]["availability"], "busy")
        # age it past working_s → with no live proc it becomes ENDED
        old = time.time() - 3 * fb.CFG["working_s"]
        os.utime(fp, (old, old))
        fb._cache["state"] = None
        st = fb.collect_state()
        self.assertEqual(st["worktrees"][0]["sessions"][0]["status"], "ended")
        self.assertEqual(st["worktrees"][0]["availability"], "free")

    def test_pending_workflow_reported(self):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("delegate everything"), assistant_msg(text="delegated"),
            turn_end(pending_workflows=1)])
        old = time.time() - 3 * fb.CFG["working_s"]     # not "working" by age
        os.utime(fp, (old, old))
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertEqual(s["pending_workflows"], 1)

    def test_pending_background_agent_keeps_working(self):
        # the ConfidAi8 case: tmux shows "✻ Waiting for 1 background agent to
        # finish", transcript idle for minutes — with a live process that is
        # delegated work still in flight, not "needs you"
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("build the lesson"), assistant_msg(text="L88 is building"),
            turn_end(pending_bg_agents=1)])
        old = time.time() - 3 * fb.CFG["working_s"]     # not "working" by age
        os.utime(fp, (old, old))
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude", "account": None,
            "tmux_target": None, "shells": 0}]
        fb._cache["state"] = None
        st = fb.collect_state()
        card = next(w for w in st["worktrees"] if w["name"] == "myapp")
        s = card["sessions"][0]
        self.assertEqual(s["pending_bg_agents"], 1)
        self.assertEqual(s["status"], "working")
        self.assertEqual(card["availability"], "busy")

    def test_closeout_flag_rides_the_card_and_dies_with_the_terminal(self):
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("close it out"), assistant_msg(text="on it"), turn_end()])
        fb._closeouts.clear()
        fb._closeouts["myapp"] = ts = time.time() - 30
        # terminal alive → the card advertises step two (✕ close)
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude --dangerously-skip-permissions",
            "account": None, "tmux_target": "s:0", "shells": 0}]
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertEqual(card["closeout_sent"], ts)
        # terminal gone → the card stops advertising it; no stale ✕ close on a
        # freed card. The ENTRY survives: reaping it is finish's job, and a
        # perpetual sweep must not perform a mutation nobody asked for.
        fb.procs.claude_processes = lambda **_: []
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertNotIn("closeout_sent", card)
        self.assertEqual(fb._closeouts["myapp"], ts)

    def test_repeated_sweeps_never_touch_the_closeout_map(self):
        # the seam the always-running loop makes dangerous: collect_state is
        # called on a schedule now, so it may not write state finish owns.
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("close it out"), assistant_msg(text="on it"), turn_end()])
        fb._closeouts.clear()
        fb._closeouts["myapp"] = time.time() - 30       # live terminal below
        fb._closeouts["gone-wt"] = time.time() - 30     # no card at all
        fb._closeouts["ancient"] = time.time() - 10 * fb.CLOSEOUT_TTL_S
        before = dict(fb._closeouts)
        fb.procs.claude_processes = lambda **_: []
        for _ in range(3):
            fb._cache["state"] = None
            fb.collect_state()
        self.assertEqual(fb._closeouts, before)
        # …and finish's own pruning does drop what has gone stale
        fb._prune_closeouts()
        self.assertNotIn("ancient", fb._closeouts)
        self.assertIn("myapp", fb._closeouts)


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestTurnEndedIsPositional(_Fleet):
    """`turn_ended` off real bytes: a `system`/`turn_duration` entry AFTER the
    last assistant message, not one anywhere in the tail.

    Why the distinction is the whole test class: every completed turn in a
    session's history leaves a `turn_duration` behind, so "one exists in the
    128 KB tail" is true of 85 % of in-window transcripts on this machine and
    says nothing about NOW. Replaying all 79 in-window transcripts entry by
    entry — each prefix a moment the board could have read the file — the naive
    rule declares the user's turn while the agent is mid-turn at **912 of 2,244
    observable moments (40.6 %), in 54 of 79 sessions (68 %)**. That failure is
    the worst this project has: a working agent reported as needing you.
    """

    def _proc(self, cmd="claude"):
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": cmd, "account": None,
            "tmux_target": None, "shells": 0}]

    def _tail(self, entries):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=entries)
        return fb.transcripts.parse_session_tail(fp)

    def _session(self):
        fb._cache["state"] = None
        return fb.collect_state()["worktrees"][0]["sessions"][0]

    def test_a_marker_after_the_last_word_ends_the_turn(self):
        self.assertTrue(self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end()])["turn_ended"])

    def test_a_marker_from_an_EARLIER_turn_does_not(self):
        """THE WRONG VERSION, pinned. Turn 1 closed; turn 2 is in flight. A
        rule that only asks 'is there a turn_duration in the tail' says yes
        here and reports a busy agent as idle."""
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            user_msg("now do another"), assistant_msg(text="on it")])
        self.assertFalse(tail["turn_ended"])

    def test_the_agent_speaking_again_withdraws_the_marker_on_its_own(self):
        """The one shape that isolates the positional rule. Everything else
        that follows a closed turn (a human prompt) is withdrawn by a second
        rule as well, so a naive 'is there a turn_duration in the tail' passes
        those tests by accident — verified by mutation. Here the agent simply
        speaks again with no new prompt in between: a background agent
        reporting back, a hook resuming it, a turn continued after machine text
        the board refuses to read as a prompt. Only position tells the truth.
        """
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            assistant_msg(text="actually, one more thing")])
        self.assertFalse(tail["turn_ended"])

    def test_a_marker_before_a_tool_call_does_not(self):
        # the same shape with the agent's second turn spent in a tool
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            user_msg("now do another"), assistant_msg(tool="Bash")])
        self.assertFalse(tail["turn_ended"])

    def test_a_human_prompt_after_the_marker_withdraws_it(self):
        # the user has typed and the agent has not spoken yet: the marker is
        # already stale. Telling them "◆ YOUR TURN" would summon them to the
        # session they are typing in (FRESHNESS.md §4.2).
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            user_msg("now do another")])
        self.assertFalse(tail["turn_ended"])

    def test_a_slash_stub_after_the_marker_does_not_withdraw_it(self):
        # "/model opus" is not a prompt and starts no turn — same rule that
        # keeps it out of `topic`
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            user_msg("/model opus")])
        self.assertTrue(tail["turn_ended"])

    def test_the_bookkeeping_written_after_a_turn_does_not_withdraw_it(self):
        """Why 'AFTER THE LAST ASSISTANT MESSAGE' and not 'the last entry'.

        The CLI appends its own housekeeping once a turn closes — `last-prompt`,
        `file-history-snapshot`, `ai-title`, `mode`. Measured on this machine's
        79 in-window transcripts, `turn_duration` is the LITERAL last entry in
        2 of them (2.5 %) but the last thing after the agent's final word in 66
        (84 %). Reading the last line is the analysis error that made this whole
        signal look useless; a rule that only wins in 2.5 % of sessions is not
        worth wiring.
        """
        tail = self._tail([
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            {"type": "last-prompt", "prompt": "do a thing"},
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "ai-title", "title": "a thing"}])
        self.assertTrue(tail["turn_ended"])

    def test_no_marker_at_all_leaves_it_false(self):
        self.assertFalse(self._tail([
            user_msg("do a thing"), assistant_msg(text="thinking")])["turn_ended"])

    def test_a_tail_that_says_nothing_claims_nothing(self):
        # 16 % of in-window sessions carry no marker at all, and a tail can be
        # bookkeeping end to end (a 128 KB paste, a snapshot flood). The
        # ABSENCE of evidence must fall through to the clock, never assert the
        # turn is over — the default is the one place a mistake is invisible.
        self.assertFalse(self._tail([
            {"type": "file-history-snapshot", "snapshot": {}},
            {"type": "system", "subtype": "hook_result"}])["turn_ended"])

    def test_the_counts_on_the_wire_come_from_the_turn_that_ended(self):
        tail = self._tail([
            user_msg("delegate"), assistant_msg(text="delegated"),
            turn_end(pending_bg_agents=2)])
        self.assertTrue(tail["turn_ended"])
        self.assertEqual(tail["pending_bg_agents"], 2)

    # ---------------------------------------------------------- end to end

    def test_a_stopped_agent_is_the_users_turn_without_waiting_out_the_window(self):
        """The user's original complaint, on real bytes: a session that stopped
        one second ago. The 90 s window used to hold it at ● WORKING."""
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="done"), turn_end()])
        self._proc()
        s = self._session()
        self.assertLess(s["age_s"], fb.CFG["working_s"])   # inside the window
        self.assertEqual(s["status"], "waiting")
        self.assertTrue(s["turn_ended"])                   # observed, on the wire
        self.assertEqual(fb.collect_state()["worktrees"][0]["availability"], "attention")

    def test_a_mid_turn_agent_with_an_older_marker_stays_working(self):
        """The same fresh transcript, one assistant line later. If this ever
        goes red the board is calling working agents idle."""
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            user_msg("now do another"), assistant_msg(text="on it")])
        self._proc()
        s = self._session()
        self.assertEqual(s["status"], "working")
        self.assertNotIn("turn_ended", s)      # conditional key: absent = unknown
        self.assertEqual(fb.collect_state()["worktrees"][0]["availability"], "busy")

    def test_an_agent_that_resumed_after_the_marker_stays_working(self):
        # same claim, in the shape only POSITION can catch (see
        # test_the_agent_speaking_again_withdraws_the_marker_on_its_own)
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="done"), turn_end(),
            assistant_msg(text="one more thing")])
        self._proc()
        s = self._session()
        self.assertEqual(s["status"], "working")
        self.assertNotIn("turn_ended", s)

    def test_an_ended_turn_awaiting_a_background_agent_stays_working(self):
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("build the lesson"), assistant_msg(text="L88 is building"),
            turn_end(pending_bg_agents=1)])
        self._proc()
        s = self._session()
        self.assertTrue(s["turn_ended"])
        self.assertEqual(s["pending_bg_agents"], 1)
        self.assertEqual(s["status"], "working")


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestOutstandingBackgroundWorkIsDelegated(_Fleet):
    """A tool_use that LAUNCHED background work counts as delegated until its
    `<task-notification>` arrives.

    The hole this closes, measured: of the 154 end-of-turn claims step 5
    replayed, 23 saw the agent speak again with no human prompt in between, and
    `pendingWorkflowCount`/`pendingBackgroundAgentCount` read 0 on every one of
    them while a `<task-notification>` was what woke the session. Those two
    counts are right when non-zero — they stay wired — but they do not see a
    backgrounded Bash, and a backgrounded Bash resumes the session just the
    same.

    What makes it invisible without this is the shape of the receipt: a
    background launch RESOLVES its tool_use immediately (the result is the
    harness saying "you will be notified", not the work), so `pending_tools`
    is empty again the instant the task starts. Every other signal in the
    ladder reads idle.
    """

    def _proc(self):
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude", "account": None,
            "tmux_target": None, "shells": 0}]

    def _tail(self, entries):
        return fb.transcripts.parse_session_tail(
            write_transcript(self.home, self.repo, "main", sid="s1", entries=entries))

    def _session(self, entries):
        write_transcript(self.home, self.repo, "main", sid="s1", entries=entries)
        self._proc()
        fb._cache["state"] = None
        return fb.collect_state()["worktrees"][0]["sessions"][0]

    # ------------------------------------------------------------ the reading

    def test_a_backgrounded_launch_is_outstanding_and_leaves_no_pending_tool(self):
        tail = self._tail([user_msg("run the deploy"), *bg_launch(), turn_end()])
        self.assertEqual(len(tail["bg_launched_at"]), 1)
        # the half of the story that made this necessary
        self.assertEqual(tail["pending_tools"], [])
        self.assertTrue(tail["turn_ended"])

    def test_every_launcher_the_harness_backgrounds_is_read_the_same_way(self):
        # the phrase, not a list of tool names: a Bash moved to the background
        # after its timeout carries no run_in_background at all, and Workflow /
        # Agent / Monitor each word their receipt differently
        for receipt in ("Bash", "BashTimeout", "Workflow", "Agent", "Monitor"):
            with self.subTest(receipt=receipt):
                tail = self._tail([user_msg("go"),
                                   *bg_launch(receipt=receipt), turn_end()])
                self.assertEqual(len(tail["bg_launched_at"]), 1)

    def test_a_foreground_result_is_not_delegation(self):
        tail = self._tail([user_msg("go"),
                           *bg_launch(receipt="total 8\ndrwxr-xr-x  file"),
                           turn_end()])
        self.assertEqual(tail["bg_launched_at"], ())

    def test_a_launch_that_errored_never_started(self):
        tail = self._tail([user_msg("go"), *bg_launch(
            receipt="<tool_use_error>Invalid workflow script. You will be "
                    "notified when it completes.</tool_use_error>"), turn_end()])
        self.assertEqual(tail["bg_launched_at"], ())

    def test_an_undated_launch_is_not_counted(self):
        """The guard is only ever as strong as its clock. An entry with no
        timestamp cannot be aged, so it degrades to the behaviour this file had
        before the outstanding set existed — never to an unbounded hold."""
        tail = self._tail([user_msg("go"), *bg_launch(dated=False), turn_end()])
        self.assertEqual(tail["bg_launched_at"], ())

    # ------------------------------------------------------- what clears it

    def test_the_notification_clears_it_in_all_three_shapes_on_disk(self):
        # a `user` entry, a `queue-operation` with the text at top-level
        # `content`, an `attachment` with it at `attachment.prompt` — 1,303 /
        # 3,794 / 898 occurrences in the corpus. Reading only `message.content`
        # sees a third of them.
        for shape in ("user", "queue-operation", "attachment"):
            with self.subTest(shape=shape):
                tail = self._tail([user_msg("go"), *bg_launch(), turn_end(),
                                   notification(shape=shape)])
                self.assertEqual(tail["bg_launched_at"], ())
                # …and the notification is machine text, so it does not read as
                # a human prompt and does not withdraw the marker
                self.assertTrue(tail["turn_ended"])

    def test_a_notification_for_another_task_leaves_this_one_outstanding(self):
        tail = self._tail([user_msg("go"), *bg_launch(tid="toolu_bg1"),
                           turn_end(), notification(tid="toolu_other")])
        self.assertEqual(len(tail["bg_launched_at"]), 1)

    def test_an_interim_monitor_event_does_not_count_as_reporting_back(self):
        """A Monitor streams `<event>` notifications under its task-id while it
        is still running — 397 of them in the corpus, none carrying a
        `<tool-use-id>`. Reading one as the report drops the guard mid-stream.
        """
        tail = self._tail([user_msg("go"), *bg_launch(receipt="Monitor"),
                           turn_end(), notification(tid=None, status=None)])
        self.assertEqual(len(tail["bg_launched_at"]), 1)

    def test_a_notification_with_no_terminal_status_is_not_the_report(self):
        """The second lock, pinned on its own. Today the id alone would do the
        job — no id-bearing notification in the corpus lacks a status, so the
        test above passes with or without the `<status>` check and proves
        nothing about it (verified by mutation: dropping the check left it
        green). This is the shape that check exists for, and the shape a future
        CLI release would introduce by giving an interim event an id."""
        tail = self._tail([user_msg("go"), *bg_launch(receipt="Monitor"),
                           turn_end(), notification(status=None)])
        self.assertEqual(len(tail["bg_launched_at"]), 1)

    def test_the_agent_quoting_a_notification_cannot_clear_its_own_guard(self):
        """A notification is something the harness writes INTO a session. An
        agent reasoning out loud about one — reading a log, explaining what it
        is waiting for — is not the task reporting back, and must not be able
        to talk the board out of a guard that exists to describe it."""
        quoted = notification()["message"]["content"]
        tail = self._tail([user_msg("go"), *bg_launch(), turn_end(),
                           assistant_msg(text="still waiting: " + quoted)])
        self.assertEqual(len(tail["bg_launched_at"]), 1)

    def test_a_failed_or_killed_task_still_reports_back(self):
        for status in ("failed", "killed", "stopped"):
            with self.subTest(status=status):
                tail = self._tail([user_msg("go"), *bg_launch(), turn_end(),
                                   notification(status=status)])
                self.assertEqual(tail["bg_launched_at"], ())

    def test_two_launches_are_counted_and_cleared_one_at_a_time(self):
        entries = [user_msg("go"), *bg_launch(tid="toolu_a"),
                   *bg_launch(tid="toolu_b"), turn_end()]
        self.assertEqual(len(self._tail(entries)["bg_launched_at"]), 2)
        self.assertEqual(len(self._tail(entries + [notification(tid="toolu_a")])
                              ["bg_launched_at"]), 1)

    # --------------------------------------------------------- end to end

    def test_an_ended_turn_awaiting_a_background_task_stays_working(self):
        s = self._session([user_msg("run the deploy"), *bg_launch(), turn_end()])
        self.assertTrue(s["turn_ended"])
        self.assertEqual(s["pending_bg_tools"], 1)
        self.assertEqual(s["pending_workflows"], 0)   # the counts the CLI wrote
        self.assertEqual(s["pending_bg_agents"], 0)   # …which are 0 here
        self.assertEqual(s["status"], "working")

    def test_the_notification_hands_the_turn_back(self):
        s = self._session([user_msg("run the deploy"), *bg_launch(), turn_end(),
                           notification()])
        self.assertEqual(s["pending_bg_tools"], 0)
        self.assertEqual(s["status"], "waiting")

    def test_a_launch_past_its_shelf_life_stops_explaining_the_silence(self):
        """3.8 % of background launches in the corpus never report back at all.
        Unbounded, one of those pins a live session at ● WORKING for the whole
        48 h window — an agent that never asks for you."""
        s = self._session([user_msg("run the deploy"),
                           *bg_launch(ago_s=fb.CFG["delegated_s"] + 60),
                           turn_end()])
        self.assertEqual(s["pending_bg_tools"], 0)
        self.assertEqual(s["status"], "waiting")

    def test_the_shelf_life_is_measured_against_now_not_against_the_file(self):
        """`parse_session_tail` is memoised on the transcript's stat, so it must
        hand out TIMESTAMPS and let the caller age them. If the count were
        computed inside the parse it would be frozen at whatever it was when
        the file last moved, and a launch would stay 'outstanding' for as long
        as the session sat still — the stalest possible reading of the freshest
        possible signal."""
        entries = [user_msg("run the deploy"), *bg_launch(ago_s=120), turn_end()]
        self.assertEqual(self._session(entries)["pending_bg_tools"], 1)
        fb.CFG["delegated_s"] = 60          # the file has not changed; the rule has
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertEqual(s["pending_bg_tools"], 0)
        self.assertEqual(s["status"], "waiting")

    def test_a_session_that_went_quiet_does_not_keep_its_launch_fresh(self):
        """The divergence that names the clock. A launch 2 minutes before the
        transcript's LAST write, on a transcript that then sat still for half an
        hour, is half an hour old — not two minutes. Aged against the file's own
        clock it would read as inside the shelf life forever, which is the
        unbounded hold the shelf life exists to prevent: quiet is exactly the
        state in which a stuck task stops being an explanation."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("run the deploy"),
            *bg_launch(ago_s=fb.CFG["delegated_s"] * 3 + 120), turn_end()])
        quiet = time.time() - fb.CFG["delegated_s"] * 3    # last write, long ago
        os.utime(fp, (quiet, quiet))
        self._proc()
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertEqual(s["pending_bg_tools"], 0)

    def test_the_launch_alone_never_resurrects_a_session_with_no_process(self):
        # FLICKER IS WORSE THAN LAG, and ENDED feeds worktree-FREE feeds
        # dispatch: delegated work must not out-vote "there is no agent here".
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("run the deploy"), *bg_launch(), turn_end()])
        fb.procs.claude_processes = lambda **_: []
        fp = self.home / "projects" / fb.munge(str(self.repo)) / "s1.jsonl"
        old = time.time() - 3 * fb.CFG["working_s"]
        os.utime(fp, (old, old))
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertEqual(card["sessions"][0]["status"], "ended")
        self.assertEqual(card["availability"], "free")


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestTheQuietTimerIsWired(_Fleet):
    """`CFG["quiet_s"]` against real bytes on disk — the 16 % of sessions with
    no end-of-turn marker, which are the only ones a timer still decides.

    The unit tests pin the ladder; this pins that the number the ladder reads
    is the CONFIGURED one. `classify_session` takes `working_s` and `quiet_s`
    as separate arguments precisely so they can diverge, and a call site that
    passed `working_s` for both would be invisible to every test above.
    """

    def _proc(self):
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude", "account": None,
            "tmux_target": None, "shells": 0}]

    def _aged(self, fp, secs):
        t = time.time() - secs
        os.utime(fp, (t, t))
        fb.memo_clear()
        fb._cache["state"] = None
        return fb.collect_state()["worktrees"][0]["sessions"][0]

    def _unmarked(self):
        # deliberately NO turn_end: this is the residual the timer covers
        return write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="thinking about it")])

    def test_a_live_agent_gone_quiet_is_the_users_turn_at_quiet_s(self):
        fp = self._unmarked()
        self._proc()
        q = fb.CFG["quiet_s"]
        self.assertEqual(self._aged(fp, q - 5)["status"], "working")
        self.assertEqual(self._aged(fp, q + 5)["status"], "waiting")
        # and the number is the CONFIGURED one, not working_s wearing its name
        self.assertLess(q, fb.CFG["working_s"])
        self.assertEqual(self._aged(fp, fb.CFG["working_s"] - 5)["status"], "waiting")

    def test_lowering_the_key_lowers_the_threshold(self):
        fp = self._unmarked()
        self._proc()
        fb.CFG["quiet_s"] = 10
        self.assertEqual(self._aged(fp, 9)["status"], "working")
        self.assertEqual(self._aged(fp, 11)["status"], "waiting")

    def test_a_subagent_write_resets_the_quiet_clock(self):
        # EXISTING behaviour, and the reason the timer is safe to shorten:
        # `last_write_at` is max(mtime, sub_mtime), so a session whose main
        # transcript has been idle for ten quiet windows while a workflow
        # writes under <session-id>/ is still working. Shortening quiet_s
        # multiplies the cost of getting this wrong tenfold.
        fp = self._unmarked()
        self._proc()
        old = time.time() - 10 * fb.CFG["quiet_s"]
        os.utime(fp, (old, old))
        sub = fp.with_suffix("")
        sub.mkdir()
        sf = sub / "agent-1.jsonl"
        sf.write_text(json.dumps(assistant_msg(text="subagent still going")) + "\n")
        fresh = time.time() - 1
        os.utime(sf, (fresh, fresh))
        fb.memo_clear()
        fb._cache["state"] = None
        s = fb.collect_state()["worktrees"][0]["sessions"][0]
        self.assertAlmostEqual(s["last_write_at"], fresh, places=3)
        self.assertEqual(s["status"], "working")

    def test_the_orphan_grace_still_covers_the_whole_working_window(self):
        # No live process — a just-exec'd agent, or a lagging proc read. ENDED
        # feeds worktree-FREE feeds dispatch targeting, so this one keeps the
        # LONGER window on purpose: quiet_s must not make a busy worktree look
        # free. (`free_worktrees` is what /api/dispatch picks from.)
        fp = self._unmarked()
        fb.procs.claude_processes = lambda **_: []
        s = self._aged(fp, fb.CFG["quiet_s"] + 5)
        self.assertEqual(s["status"], "working")
        fb._cache["state"] = None
        self.assertEqual(fb.collect_state()["free_worktrees"], [])


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestTheGraceKeysAreWired(_Fleet):
    """`CFG["block_grace_s"]` and `CFG["orphan_grace_s"]` against real bytes.

    Both defaulted to `working_s` for two releases, so every call site could
    have gone on passing `working_s` for all three clocks and no test above
    would have noticed. These move each key on its own, to a value nothing
    else in CFG shares, and read the answer off `collect_state` — the same
    proof `TestTheQuietTimerIsWired` gives for the third clock.
    """

    def _proc(self, cmd="claude"):
        fb.procs.claude_processes = lambda **_: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": cmd, "account": None,
            "tmux_target": None, "shells": 0}]

    def _aged(self, fp, secs):
        t = time.time() - secs
        os.utime(fp, (t, t))
        fb.memo_clear()
        fb._cache["state"] = None
        return fb.collect_state()["worktrees"][0]["sessions"][0]

    def _tool_left_open(self):
        # an assistant tool_use with no matching tool_result: on disk this is
        # "a tool is running" and "the user has been asked to approve it" at
        # exactly the same bytes. Read, not Bash — a Bash would be answered by
        # the `shells` branch one level up and never reach the grace.
        return write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("look at the file"), assistant_msg(tool="Read")])

    def test_a_stalled_tool_is_blocked_at_the_block_grace(self):
        fp = self._tool_left_open()
        self._proc()                       # no --dangerously-skip-permissions
        b = fb.CFG["block_grace_s"]
        self.assertEqual(self._aged(fp, b - 5)["status"], "working")
        self.assertEqual(self._aged(fp, b + 5)["status"], "blocked")
        # …and it is the CONFIGURED number, not working_s wearing its name
        self.assertLess(b, fb.CFG["working_s"])
        self.assertEqual(self._aged(fp, fb.CFG["working_s"] - 5)["status"], "blocked")

    def test_lowering_the_block_key_lowers_the_threshold(self):
        fp = self._tool_left_open()
        self._proc()
        fb.CFG["block_grace_s"] = 10
        self.assertEqual(self._aged(fp, 5)["status"], "working")
        self.assertEqual(self._aged(fp, 15)["status"], "blocked")

    def test_skip_permissions_still_means_there_is_nothing_to_approve(self):
        # the grace only ever decides sessions that CAN be asked; 81 % of the
        # board's working set cannot, and must stay ● WORKING however long the
        # tool runs.
        fp = self._tool_left_open()
        self._proc(cmd="claude --dangerously-skip-permissions")
        s = self._aged(fp, fb.CFG["block_grace_s"] * 10)
        self.assertEqual(s["status"], "working")
        self.assertTrue(s["tool_running"])

    def test_a_fresh_write_with_no_process_ends_at_the_orphan_grace(self):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="on it")])
        fb.procs.claude_processes = lambda **_: []
        o = fb.CFG["orphan_grace_s"]
        self.assertEqual(self._aged(fp, o - 5)["status"], "working")
        self.assertEqual(self._aged(fp, o + 5)["status"], "ended")

    def test_lowering_the_orphan_key_frees_the_worktree_sooner(self):
        # THE DANGEROUS DIRECTION, pinned end to end: ENDED is what makes a
        # card free, and `free_worktrees` is what /api/dispatch picks from. A
        # change to this key must be visible HERE, not just in a status string.
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("do a thing"), assistant_msg(text="on it")])
        fb.procs.claude_processes = lambda **_: []
        fb.CFG["orphan_grace_s"] = 10
        self.assertEqual(self._aged(fp, 5)["status"], "working")
        fb._cache["state"] = None
        self.assertEqual(fb.collect_state()["free_worktrees"], [])
        self.assertEqual(self._aged(fp, 15)["status"], "ended")
        fb._cache["state"] = None
        self.assertEqual(fb.collect_state()["free_worktrees"], ["myapp"])


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestTranscriptMemo(_Fleet):
    """The stat memo underneath scan_sessions, against real files.

    LAW, and the reason this class exists rather than a benchmark: a stale memo
    is worse than a slow sweep. Every reuse below is proved to be defeated by
    the thing that would make it wrong, and the cold reconcile is proved to
    catch it when it is not.
    """

    def _sessions(self):
        fb._cache["state"] = None
        return fb.collect_state()["worktrees"][0]["sessions"]

    def _count_parses(self):
        """Wrap the real parse so a reuse is visible as a call that did not
        happen, not as a stopwatch reading."""
        real, seen = fb.transcripts.parse_session_tail, []

        def counted(fp):
            seen.append(str(fp))
            return real(fp)
        fb.transcripts.parse_session_tail = counted
        self.addCleanup(setattr, fb.transcripts, "parse_session_tail", real)
        return seen

    def test_an_unchanged_transcript_is_parsed_once_not_once_per_sweep(self):
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="working on it"), turn_end()])
        seen = self._count_parses()
        for _ in range(4):
            self.assertEqual(self._sessions()[0]["last_assistant"], "working on it")
        self.assertEqual(len(seen), 1)          # four sweeps, one parse

    def test_an_append_defeats_the_memo(self):
        """The one thing a transcript actually does. If this reuses, the board
        shows an agent still saying what it said ten minutes ago."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="working on it"), turn_end()])
        self.assertEqual(self._sessions()[0]["last_assistant"], "working on it")
        with open(fp, "a") as f:
            f.write(json.dumps(assistant_msg(text="finished it")) + "\n")
        self.assertEqual(self._sessions()[0]["last_assistant"], "finished it")

    def test_a_transcript_replaced_at_the_same_path_size_and_mtime_is_a_miss(self):
        """Step 0's trap, reproduced on disk: same path, same size, same mtime,
        different inode. Keying on the path would serve the dead file's parse —
        that exact mistake made a delivered message read as undelivered and
        cost 3x usage."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="aaaaaaa"), turn_end()])
        self.assertEqual(self._sessions()[0]["last_assistant"], "aaaaaaa")
        st = os.stat(fp)
        other = fp.with_name("replacement.jsonl")
        write_transcript(self.home, self.repo, "main", sid="replacement",
                         entries=[user_msg("Build a login screen"),
                                  assistant_msg(text="bbbbbbb"), turn_end()])
        self.assertEqual(os.stat(other).st_size, st.st_size)   # same size…
        os.utime(other, ns=(st.st_atime_ns, st.st_mtime_ns))   # …same mtime…
        self.assertNotEqual(os.stat(other).st_ino, st.st_ino)  # …other inode
        os.replace(other, fp)
        self.assertEqual(self._sessions()[0]["last_assistant"], "bbbbbbb")

    def test_the_boundary_an_in_place_rewrite_preserving_all_four_stats(self):
        """The documented limit of the key (ADR 0011, `StatMemo`'s docstring).

        Four numbers decide a hit — size, mtime_ns, dev, ino — so a rewrite
        that preserves all four serves the old parse. This test exists to keep
        the docstring HONEST: if somebody strengthens the key, this goes red
        and the prose describing the boundary has to move with it.

        It is adversarial, not realistic — transcripts are appended to, and
        `test_an_append_defeats_the_memo` above is the real case — and the
        second half is why it is a recorded boundary rather than a bug: the
        cold reconcile reads the file anyway and counts the disagreement.
        """
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="aaaaaaa"), turn_end()])
        self.assertEqual(self._sessions()[0]["last_assistant"], "aaaaaaa")
        st = os.stat(fp)
        body = fp.read_text().replace("aaaaaaa", "bbbbbbb")   # same byte count
        fp.write_text(body)
        os.utime(fp, ns=(st.st_atime_ns, st.st_mtime_ns))
        after = os.stat(fp)
        self.assertEqual((after.st_size, after.st_mtime_ns, after.st_dev, after.st_ino),
                         (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino))
        # …and so the memo hands back text that is no longer in the file
        self.assertEqual(self._sessions()[0]["last_assistant"], "aaaaaaa")
        self.assertEqual(fb.memo_stats()["scan_drift"], 0)    # silently, so far

        fb._cache["state"] = None                             # the bound: 60 s
        s = fb.collect_state(cold=True)["worktrees"][0]["sessions"][0]
        self.assertEqual(s["last_assistant"], "bbbbbbb")      # read from the file
        self.assertEqual(fb.memo_stats()["scan_drift"], 1)    # …and counted

    def test_a_float_utime_cannot_restore_the_nanoseconds_it_truncated(self):
        """Why the boundary above needs `ns=`. The first attempt to demonstrate
        it used `os.utime(fp, (atime, mtime))`, the memo correctly missed, and
        that read as proof the key was safe. It was proof of nothing: float
        seconds cannot carry st_mtime_ns, so the key moved. A negative result
        from a test that could not have succeeded is not evidence."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="aaaaaaa"), turn_end()])
        self.assertEqual(self._sessions()[0]["last_assistant"], "aaaaaaa")
        st = os.stat(fp)
        fp.write_text(fp.read_text().replace("aaaaaaa", "bbbbbbb"))
        os.utime(fp, (st.st_atime, st.st_mtime))              # float, not ns=
        after = os.stat(fp)
        self.assertEqual(after.st_size, st.st_size)
        self.assertNotEqual(after.st_mtime_ns, st.st_mtime_ns)
        self.assertEqual(self._sessions()[0]["last_assistant"], "bbbbbbb")

    def test_the_cold_reconcile_re_reads_and_counts_a_memo_that_lies(self):
        """§4.3 #1/#4. Nothing else can catch this: a stale parse looks exactly
        like a quiet agent, so a memo nobody audits fails silently forever."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="the truth"), turn_end()])
        self.assertEqual(self._sessions()[0]["last_assistant"], "the truth")
        st = os.stat(fp)
        ident = (str(fp), st.st_dev, st.st_ino)
        key = (st.st_size, st.st_mtime_ns)
        poisoned = dict(fb.transcripts._FACTS.peek(ident)[1])
        poisoned["last_assistant"] = "a lie"
        fb.transcripts._FACTS.put(ident, key, poisoned, time.time())
        self.assertEqual(self._sessions()[0]["last_assistant"], "a lie")
        self.assertEqual(fb.memo_stats()["scan_drift"], 0)

        fb._cache["state"] = None
        s = fb.collect_state(cold=True)["worktrees"][0]["sessions"][0]
        self.assertEqual(s["last_assistant"], "the truth")   # read from the file
        self.assertEqual(fb.memo_stats()["scan_drift"], 1)   # …and counted
        self.assertEqual(self._sessions()[0]["last_assistant"], "the truth")

    def test_a_subagent_appending_inside_an_unchanged_directory_moves_the_clock(self):
        """The memo remembers WHICH files a subagent tree holds, never WHEN
        they were written — a directory's mtime does not move when a file
        inside it is appended to. Cache that and a live workflow session goes
        quiet on the board with nothing to show it ever ran."""
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("delegate everything"), assistant_msg(text="delegated"),
            turn_end()])
        old = time.time() - 600
        os.utime(fp, (old, old))
        sub = fp.with_suffix("")
        (sub / "deep").mkdir(parents=True)
        sf = sub / "deep" / "agent-1.jsonl"
        sf.write_text(json.dumps(assistant_msg(text="starting")) + "\n")
        stamp = time.time() - 300
        os.utime(sf, (stamp, stamp))
        dir_before = os.stat(sub / "deep").st_mtime_ns
        self.assertAlmostEqual(self._sessions()[0]["last_write_at"], stamp, places=3)

        with open(sf, "a") as f:               # the subagent says more
            f.write(json.dumps(assistant_msg(text="still going")) + "\n")
        moved = time.time() - 5
        os.utime(sf, (moved, moved))
        self.assertEqual(os.stat(sub / "deep").st_mtime_ns, dir_before)  # dir sat still
        s = self._sessions()[0]
        self.assertAlmostEqual(s["last_write_at"], moved, places=3)
        self.assertTrue(s["subagents_active"])
        self.assertEqual(s["subagent_said"], "still going")

    def test_a_new_subagent_file_defeats_the_directory_memo(self):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("delegate everything"), assistant_msg(text="delegated"),
            turn_end()])
        old = time.time() - 600
        os.utime(fp, (old, old))
        sub = fp.with_suffix("")
        sub.mkdir()
        (sub / "a.jsonl").write_text(json.dumps(assistant_msg(text="first")) + "\n")
        os.utime(sub / "a.jsonl", (old + 1, old + 1))
        self.assertEqual(self._sessions()[0]["subagent_said"], "first")
        (sub / "b.jsonl").write_text(json.dumps(assistant_msg(text="second")) + "\n")
        self.assertEqual(self._sessions()[0]["subagent_said"], "second")

    def test_the_cold_reconcile_counts_a_directory_memo_that_lies(self):
        fp = write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("delegate everything"), assistant_msg(text="delegated"),
            turn_end()])
        sub = fp.with_suffix("")
        sub.mkdir()
        (sub / "a.jsonl").write_text(json.dumps(assistant_msg(text="first")) + "\n")
        self._sessions()
        st = os.stat(sub)
        ident = (str(sub), st.st_dev, st.st_ino)
        key = (st.st_size, st.st_mtime_ns)
        fb.transcripts._TREES.put(ident, key, ((), ()), time.time())  # "empty"
        self.assertEqual(fb.memo_stats()["tree_drift"], 0)
        fb._cache["state"] = None
        fb.collect_state(cold=True)
        self.assertEqual(fb.memo_stats()["tree_drift"], 1)

    def test_nothing_on_the_wire_aliases_memo_state(self):
        """A Snapshot is frozen and two sweeps share one memo value. A card
        that handed out the memo's own list would let a later mutation of the
        card rewrite an already-published snapshot."""
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"),
            assistant_msg(text="working", tool="Bash")])
        first = self._sessions()[0]["pending_tools"]
        self.assertEqual(first, ["Bash"])
        first.append("MUTATED")
        self.assertEqual(self._sessions()[0]["pending_tools"], ["Bash"])

    def test_the_memo_does_not_grow_with_the_number_of_sweeps(self):
        """§4.7. The corpus grows ~982 .jsonl/day and the sweep never stops."""
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"), assistant_msg(text="working")])
        self._sessions()
        size = len(fb.transcripts._FACTS) + len(fb.transcripts._TREES)
        for _ in range(10):
            self._sessions()
        self.assertEqual(len(fb.transcripts._FACTS) + len(fb.transcripts._TREES),
                         size)

    def test_the_scan_reaps_entries_nobody_is_reading_any_more(self):
        """The cap is the hard bound; this is the one that matters day to day,
        because a transcript that leaves the 48 h window is never read again
        and would otherwise sit in the memo until 2,048 newer ones pushed it
        out. Proved by wiring, not by waiting an hour."""
        write_transcript(self.home, self.repo, "main", sid="s1", entries=[
            user_msg("Build a login screen"), assistant_msg(text="working")])
        self._sessions()
        self.assertTrue(len(fb.transcripts._FACTS))
        for m in (fb.transcripts._FACTS, fb.transcripts._TREES):
            self.addCleanup(setattr, m, "idle_s", m.idle_s)
            m.idle_s = 0.0                      # everything is instantly idle
        self._sessions()
        self.assertEqual(len(fb.transcripts._FACTS), 0)
        self.assertEqual(len(fb.transcripts._TREES), 0)


@unittest.skipUnless(HAVE_GIT, "git not available")
@unittest.skipUnless(HAVE_GIT, "git not available")
class TestGitInfoPorcelainV2(unittest.TestCase):
    """The three ways `status --porcelain=v2 --branch` bites silently.

    git_info reads branch, upstream, ahead/behind and the dirty count out of one
    v2 call. Every failure mode below renders a confidently WRONG value rather
    than raising, so each gets a repo built to trigger it.
    """

    def repo(self, name="app"):
        d = Path(tempfile.mkdtemp(prefix="fb-gi-")) / name
        d.mkdir(parents=True)
        git(d, "init", "-q", "-b", "main")
        git(d, "config", "user.email", "t@t.t")
        git(d, "config", "user.name", "t")
        (d / "a").write_text("1\n"); git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "c0")
        return d

    def test_detached_head_keeps_gits_own_abbreviation(self):
        """v2 says only "(detached)" — no sha. And the label cannot be rebuilt
        by slicing branch.oid, because git's abbrev length is per-repository
        (measured 8 chars in one worktree of the dev fleet, 9 in the others).
        The rendered label must match `rev-parse --short` exactly."""
        d = self.repo()
        (d / "b").write_text("2\n"); git(d, "add", "-A"); git(d, "commit", "-q", "-m", "c1")
        git(d, "checkout", "-q", "HEAD~1")
        short = subprocess.run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True).stdout.strip()
        self.assertEqual(fb.git_info(d)["branch"], f"detached@{short}")
        self.assertNotIn("(detached)", fb.git_info(d)["branch"])

    def test_no_upstream_leaves_ahead_behind_unset(self):
        """`# branch.ab` is ABSENT with no tracking ref — not "+0 -0". A parser
        that assumes the line exists reports 0/0 for every untracked branch,
        which reads as "in sync" when nothing is known at all."""
        d = self.repo()
        git(d, "checkout", "-q", "-b", "feat/no-upstream")
        info = fb.git_info(d)
        self.assertEqual(info["branch"], "feat/no-upstream")
        self.assertIsNone(info["ahead"])
        self.assertIsNone(info["behind"])

    def test_ahead_behind_orientation_is_not_inverted(self):
        """`# branch.ab +A -B` is ahead-then-behind; `rev-list --left-right`
        puts the UPSTREAM first. Read positionally, every count on the board
        flips. Built deliberately asymmetric — 2 ahead, 3 behind — so a swap
        cannot pass."""
        d = self.repo()
        git(d, "checkout", "-q", "-b", "feat/x")
        for i in range(3):     # 3 commits that will belong to the upstream only
            (d / f"u{i}").write_text("u\n"); git(d, "add", "-A")
            git(d, "commit", "-q", "-m", f"u{i}")
        git(d, "update-ref", "refs/remotes/origin/feat/x", "HEAD")
        git(d, "reset", "-q", "--hard", "HEAD~3")
        for i in range(2):     # 2 commits only we have
            (d / f"m{i}").write_text("m\n"); git(d, "add", "-A")
            git(d, "commit", "-q", "-m", f"m{i}")
        # No real remote, so wire tracking by hand. The fetch refspec is the
        # part that is easy to miss: without it git cannot map refs/heads/feat/x
        # on origin to refs/remotes/origin/feat/x, @{u} fails to resolve, and
        # the test silently degrades into the no-upstream case above.
        git(d, "config", "remote.origin.url", ".")
        git(d, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        git(d, "config", "branch.feat/x.remote", "origin")
        git(d, "config", "branch.feat/x.merge", "refs/heads/feat/x")
        # Sanity-check the fixture itself: the two forms disagree on ORDER, which
        # is the entire point. v2 says "+2 -3" (ahead, behind); rev-list says
        # "3\t2" (behind, ahead). If this ever stops holding, the fixture — not
        # git_info — is what changed.
        v2 = subprocess.run(["git", "-C", str(d), "status", "--porcelain=v2",
                             "--branch"], capture_output=True, text=True).stdout
        self.assertIn("# branch.ab +2 -3", v2, "fixture did not build 2-ahead/3-behind")
        info = fb.git_info(d)
        self.assertEqual(info["ahead"], 2, "ahead/behind look inverted")
        self.assertEqual(info["behind"], 3, "ahead/behind look inverted")

    def test_dirty_counts_one_line_per_path(self):
        """v2 uses different record types (1/2/u/?) than v1's two-char codes.
        The count must still be one per path, untracked included."""
        d = self.repo()
        (d / "a").write_text("changed\n")        # modified, unstaged
        (d / "new1").write_text("x\n")           # untracked
        (d / "new2").write_text("y\n")           # untracked
        (d / "staged").write_text("z\n"); git(d, "add", "staged")
        self.assertEqual(fb.git_info(d)["dirty"], 4)

    def test_clean_repo_is_zero_dirty(self):
        self.assertEqual(fb.git_info(self.repo())["dirty"], 0)

    def test_reading_a_worktree_does_not_rewrite_its_index(self):
        """Plain `git status` refreshes .git/index and takes index.lock — a
        WRITE, on the path the sweep now runs forever. The agent working in
        that worktree is who pays: a colliding `git commit` fails outright."""
        d = self.repo()
        (d / "clean").write_text("keep\n"); git(d, "add", "-A")
        git(d, "commit", "-q", "-m", "c1")
        (d / "a").write_text("changed\n")
        idx = d / ".git" / "index"
        # Provoke the refresh with a CLEAN tracked file whose stat no longer
        # matches the cache. Staleness on a MODIFIED file is not enough: git
        # sees the size differ, calls it modified and has no new stat worth
        # caching, so whether it rewrites the index comes down to racy-index
        # timing — measured 11 of 120 runs where the plain form did NOT write,
        # which is exactly how this control failed under a loaded full suite.
        # A clean file forces the write: refresh hashes it, finds the content
        # matches, and must store the refreshed stat. 0 of 120 flakes.
        old = time.time() - 5
        stale = lambda: (os.utime(d / "clean", (old, old)),
                         os.utime(d / "a", (old, old)))
        stale()
        before = idx.stat()
        fb.git_info(d)
        after = idx.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        # and the fixture is honest: the plain form DOES rewrite it
        stale()
        rc = subprocess.run(["git", "-C", str(d), "status", "--porcelain=v2",
                             "--branch"], capture_output=True)
        self.assertEqual(rc.returncode, 0, "control git failed to run at all")
        self.assertNotEqual((before.st_ino, before.st_mtime_ns),
                            (idx.stat().st_ino, idx.stat().st_mtime_ns))


class TestTopologyPipeline(unittest.TestCase):
    """branch_topology against a real repo with a real origin/main ref."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-topo-"))
        self.root = self.tmp / "code"; self.root.mkdir()
        self.repo = self.root / "app"; self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@t.t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a").write_text("1\n"); git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c0")
        # pin origin/main at c0 (no real remote needed)
        git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        # branch ahead by two commits
        git(self.repo, "checkout", "-q", "-b", "feat/x")
        (self.repo / "b").write_text("2\n"); git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c1")
        (self.repo / "c").write_text("3\n"); git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "c2")

        self._save = {k: fb.CFG.get(k) for k in ("roots", "pattern")}
        self._demo = fb.config.DEMO
        fb.config.DEMO = False
        fb.CFG["roots"] = [str(self.root)]; fb.CFG["pattern"] = ""
        fb._topo["data"] = None

    def tearDown(self):
        for k, v in self._save.items():
            fb.CFG[k] = v if v is not None else fb.CFG.pop(k, None)
        fb.config.DEMO = self._demo
        fb._topo["data"] = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_branch_ahead_and_base(self):
        topo = fb.branch_topology()
        self.assertEqual(len(topo["groups"]), 1)
        grp = topo["groups"][0]
        self.assertEqual(grp["base"], "origin/main")
        br = next(b for b in grp["branches"] if b["branch"] == "feat/x")
        self.assertEqual(br["ahead"], 2)      # c1, c2
        self.assertEqual(br["behind"], 0)
        self.assertEqual(br["worktree"], "app")


@unittest.skipUnless(HAVE_TMUX, "tmux not available")
class TestTmuxActuation(unittest.TestCase):
    """The tmux plumbing dispatch/send rely on — real session, own socket,
    plain shell (never launches claude)."""

    SOCK = "orchestra-test"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fb-tmux-"))
        self.sess = "ittest"
        # tmux new-session flakes on loaded CI runners (the server occasionally
        # fails to fork). A transient hiccup here should SKIP the actuation
        # test, not error the whole build — the plumbing it exercises is
        # unchanged whether or not this one server starts.
        r = subprocess.run(["tmux", "-L", self.SOCK, "new-session", "-d",
                            "-s", self.sess], capture_output=True, text=True)
        if r.returncode != 0:
            self.skipTest(f"tmux server wouldn't start: {r.stderr.strip()}")

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.SOCK, "kill-server"],
                       capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_send_keys_reaches_the_shell(self):
        # exactly the calls send_to_process/dispatch make: type a line, Enter
        out = self.tmp / "out.txt"
        subprocess.run(["tmux", "-L", self.SOCK, "send-keys", "-t", self.sess,
                        "-l", f"echo INTEGRATION_OK > {out}"], check=True)
        subprocess.run(["tmux", "-L", self.SOCK, "send-keys", "-t", self.sess,
                        "Enter"], check=True)
        deadline = time.time() + 5
        while time.time() < deadline and not out.exists():
            time.sleep(0.1)
        self.assertTrue(out.exists(), "send-keys did not reach the shell")
        self.assertEqual(out.read_text().strip(), "INTEGRATION_OK")

    def test_capture_pane_reads_back(self):
        # capture-pane is how effort-verification & the chat reader inspect a pane
        subprocess.run(["tmux", "-L", self.SOCK, "send-keys", "-t", self.sess,
                        "-l", "echo MARKER_XYZ"], check=True)
        subprocess.run(["tmux", "-L", self.SOCK, "send-keys", "-t", self.sess,
                        "Enter"], check=True)
        time.sleep(0.5)
        r = subprocess.run(["tmux", "-L", self.SOCK, "capture-pane", "-p",
                            "-t", self.sess], capture_output=True, text=True)
        self.assertIn("MARKER_XYZ", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
