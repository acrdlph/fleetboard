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
                      ("roots", "homes", "pattern", "exclude_accounts", "reserve_percent")}
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
        (d / "a").write_text("changed\n")
        idx = d / ".git" / "index"
        # make the stat cache look stale, which is what provokes the refresh
        old = time.time() - 5
        os.utime(d / "a", (old, old))
        before = idx.stat()
        fb.git_info(d)
        after = idx.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))
        # and the fixture is honest: the plain form DOES rewrite it
        os.utime(d / "a", (old, old))
        subprocess.run(["git", "-C", str(d), "status", "--porcelain=v2",
                        "--branch"], capture_output=True)
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
