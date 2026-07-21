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

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("orchestra", ROOT / "orchestra.py")
fb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fb)

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
        self._demo, self._procs, self._cl = fb.DEMO, fb.claude_processes, fb.cached_limits
        fb.DEMO = False
        fb.CFG["roots"] = [str(self.root)]
        fb.CFG["homes"] = [str(self.home)]
        fb.CFG["pattern"] = ""
        fb.CFG["exclude_accounts"] = []
        fb.CFG["reserve_percent"] = {}
        fb.claude_processes = lambda: []                       # no live procs
        fb.cached_limits = lambda refresh=False: {"available": False}
        fb._cache["state"] = None                              # bust the 4s cache

    def tearDown(self):
        for k, v in self._save.items():
            if v is None:
                fb.CFG.pop(k, None)
            else:
                fb.CFG[k] = v
        fb.DEMO, fb.claude_processes, fb.cached_limits = self._demo, self._procs, self._cl
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
        fb.claude_processes = lambda: [{
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
        fb.claude_processes = lambda: [{
            "pid": 7, "cpu": 0.0, "etime": "01:00", "tty": None, "host": None,
            "cwd": str(self.repo), "cmd": "claude --dangerously-skip-permissions",
            "account": None, "tmux_target": "s:0", "shells": 0}]
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertEqual(card["closeout_sent"], ts)
        # terminal gone → the flag dies; no stale ✕ close on a freed card
        fb.claude_processes = lambda: []
        fb._cache["state"] = None
        card = fb.collect_state()["worktrees"][0]
        self.assertNotIn("closeout_sent", card)
        self.assertNotIn("myapp", fb._closeouts)


@unittest.skipUnless(HAVE_GIT, "git not available")
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
        self._demo = fb.DEMO
        fb.DEMO = False
        fb.CFG["roots"] = [str(self.root)]; fb.CFG["pattern"] = ""
        fb._topo["data"] = None

    def tearDown(self):
        for k, v in self._save.items():
            fb.CFG[k] = v if v is not None else fb.CFG.pop(k, None)
        fb.DEMO = self._demo
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
