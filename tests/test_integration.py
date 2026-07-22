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


@unittest.skipUnless(HAVE_GIT, "git not available")
class TestCollectPipeline(unittest.TestCase):
    """discover_worktrees + git_info + scan_sessions + collect_state, for real."""

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
        fb.procs.claude_processes = lambda: []                       # no live procs
        fb.limits.cached_limits = lambda refresh=False: {"available": False}
        fb._cache["state"] = None                              # bust the 4s cache

    def tearDown(self):
        for k, v in self._save.items():
            if v is None:
                fb.CFG.pop(k, None)
            else:
                fb.CFG[k] = v
        (fb.config.DEMO, fb.procs.claude_processes,
         fb.limits.cached_limits) = self._demo, self._procs, self._cl
        fb._cache["state"] = None
        shutil.rmtree(self.tmp, ignore_errors=True)

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
        fb.procs.claude_processes = lambda: [{
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
        fb.procs.claude_processes = lambda: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude --dangerously-skip-permissions",
            "account": None, "tmux_target": "s:0", "shells": 0}]
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertEqual(card["closeout_sent"], ts)
        # terminal gone → the flag dies; no stale ✕ close on a freed card
        fb.procs.claude_processes = lambda: []
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertNotIn("closeout_sent", card)
        self.assertNotIn("myapp", fb._closeouts)


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
