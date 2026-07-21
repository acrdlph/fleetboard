#!/usr/bin/env python3
"""orchestra — local mission control for parallel Claude Code agents.

Watches your git worktrees, your Claude Code home directories (multi-account
setups included), and live `claude` processes; serves three views on
http://127.0.0.1:4242 — the board (who's working / who needs you / which
worktree is free), the map (real git topology of every branch), and limits
(per-account usage via cclimits) — plus a click-only control plane: chat with
any agent, resume a limit-stuck one when its session limit resets, dispatch
new tmux-hosted agents into free worktrees, and finish a done mission (an
agent lands the branch; the worktree goes free).

Watching is read-only and touches nothing. Acting (chat/resume/dispatch/
finish) happens only on an explicit request — dispatch spends account usage,
and finish hands a closeout brief to an agent that merges and pushes. When
the branch has already landed, finish skips the agent: it parks the worktree
back on the trunk itself (switch + pull — the one provably-safe case where
the board runs git write commands). Zero dependencies — python3 stdlib only.

    python3 -m orchestra --root ~/code
    python3 -m orchestra --demo          # fictional data, for screenshots

Configuration precedence: CLI flags > orchestra.config.json (next to this
script, else cwd) > defaults. See README.md.
"""

import json
import re
import time                    # unused here, but tests reach time.sleep as
                               # `orchestra.time.sleep` — keep the name bound
from http.server import BaseHTTPRequestHandler

from . import (config, shell, status, gitrepo, procs, transcripts, limits,
               observer, terminal, chat, finish, dispatch, resume)

# ---- public surface (facade). Re-exported so tests, tools and
# tests/characterize.py can keep saying `orchestra.<name>`. DEMO,
# CONFIG_PATH, DISPATCH_LOG and RESUME_STATE are deliberately NOT re-exported:
# they are rebound at runtime, so a facade copy would go stale and
# `orchestra.DEMO = True` would be a patch that lies. Reach them as
# `orchestra.config.DEMO`.
from .config import CFG, HOME, HERE, load_config, account_label
from .shell import run
from .status import classify_session, closeout_step, card_availability
from .gitrepo import (munge, match_worktree, discover_worktrees, git_info,
                      _base_ref, branch_topology, demo_topology,
                      cached_topology, TOPO_TTL_S, _topo)
from .procs import (claude_processes, pair_sessions_with_procs, shell_children,
                    _pid_cwds, _pid_config_dirs, _host_of, _tmux_pane_map)
from .transcripts import (claude_homes, _read_chunk, _clean, _real_prompt,
                          session_topic, last_assistant_text, find_last_user,
                          parse_session_tail, scan_sessions,
                          TAIL_BYTES, HEAD_BYTES)
from .limits import (cached_limits, account_reserve, _model_remaining,
                     model_candidates, set_reserve, limits_by_account,
                     demo_limits, _limit_active_until, _cclimits_bin,
                     LIMITS_TTL_S, _limits)
from .observer import collect_state, cached_state, demo_state, _cache, STATE_TTL_S
from .terminal import focus_process, send_to_process, _osa_escape
from .chat import read_chat
from .finish import (start_finish, _park_on_trunk, _reachable, _closeouts,
                     CLOSEOUT_TEXT, SLIM_CLOSEOUT_TEXT, CLOSEOUT_NUDGE_TEXT)
from .dispatch import (start_dispatch, dispatch_status, read_dispatch_log,
                       deliver_text, kickoff_sent, composer_idle,
                       closeout_shell, _pick_defaults, _run_dispatch,
                       _jobs, FLEET_SOCK)
from .resume import (schedule_resume, cancel_resume, resume_public,
                     demo_resumes, fire_resume, resume_loop, save_resumes,
                     load_resumes, _tmux_resume, _wait_composer_idle,
                     _proven_in_transcript, _session_on_board, _resumes,
                     RESUME_POLL_S, RESUME_MAX_ATTEMPTS, RESUME_READY_S)


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            # schedules ride along so the board needs no second fetch
            body = json.dumps({**observer.cached_state(),
                               "resumes": resume.resume_public()}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/focus"):
            m = re.search(r"pid=(\d+)", self.path)
            result = terminal.focus_process(int(m.group(1))) if m else {"ok": False, "message": "missing pid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/topology"):
            body = json.dumps(gitrepo.cached_topology()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/limits"):
            body = json.dumps(limits.cached_limits(refresh="refresh=1" in self.path)).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatchlog"):
            body = json.dumps(dispatch.read_dispatch_log()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatch/status"):
            m = re.search(r"job=([\w-]+)", self.path)
            body = json.dumps(dispatch.dispatch_status(m.group(1)) if m
                              else {"ok": False, "error": "no job"}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/chat"):
            qa = re.search(r"account=([^&]+)", self.path)
            qs = re.search(r"sid=([0-9a-fA-F-]+)", self.path)
            result = chat.read_chat(qa.group(1), qs.group(1)) if qa and qs else \
                {"ok": False, "error": "need account & sid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] in ("/", "/index", "/index.html"):
            body = (config.HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/map"):
            body = (config.HERE / "map.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/limits"):
            body = (config.HERE / "limits.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/guide"):
            body = (config.HERE / "guide.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, OSError):
            payload = {}
        if self.path.startswith("/api/reserve"):
            result = limits.set_reserve(payload.get("account"), payload.get("percent"))
        elif self.path.startswith("/api/resume/schedule"):
            result = resume.schedule_resume(
                payload.get("worktree"), payload.get("sid"),
                payload.get("account"), model=payload.get("model"),
                delay_s=payload.get("delay_s"),
                resets_at=payload.get("resets_at"), due_at=payload.get("due_at"))
        elif self.path.startswith("/api/resume/cancel"):
            result = resume.cancel_resume(payload.get("worktree"), payload.get("sid"))
        elif self.path.startswith("/api/send"):
            result = terminal.send_to_process(int(payload.get("pid") or 0), payload.get("text") or "")
        elif self.path.startswith("/api/finish"):
            result = finish.start_finish(payload.get("worktree") or "")
        elif self.path.startswith("/api/dispatch"):
            result = dispatch.start_dispatch(
                payload.get("mission"), payload.get("worktree") or None,
                payload.get("account") or None,
                payload.get("model") or None, payload.get("effort") or None,
                bool(payload.get("force_model")))
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass
