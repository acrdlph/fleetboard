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

import time                    # unused here, but tests reach time.sleep as
                               # `orchestra.time.sleep` — keep the name bound

from . import (config, shell, status, gitrepo, procs, transcripts, limits,
               observer, identity, terminal, chat, finish, dispatch, resume,
               server)

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
                    _pid_cwds, _pid_config_dirs, _host_of, _tmux_pane_map,
                    ProcMemo, proc_memo_stats, proc_memo_drift,
                    proc_memo_clear, PROC_MEMO_CAP)
from .transcripts import (claude_homes, _read_chunk, _clean, _real_prompt,
                          session_topic, last_assistant_text, find_last_user,
                          parse_session_tail, scan_sessions, _subagent_files,
                          StatMemo, memo_stats, memo_drift, memo_clear,
                          TAIL_BYTES, HEAD_BYTES, MEMO_FILES, MEMO_DIRS,
                          MEMO_IDLE_S)
from .limits import (cached_limits, account_reserve, _model_remaining,
                     model_candidates, set_reserve, limits_by_account,
                     demo_limits, _limit_active_until, _cclimits_bin,
                     LIMITS_TTL_S, _limits)
from .observer import (collect_state, cached_state, demo_state, _cache,
                       Observer, Snapshot, GitCadence, start_observer,
                       stop_observer, STATE_TTL_S)
# `resolve` is deliberately NOT re-exported: it is a name generic enough to
# read as anything at the top level, and the two codes are what callers branch
# on. Reach the function as `orchestra.identity.resolve`.
from .identity import GONE, UNADDRESSED, ADDRESSES
from .terminal import focus_process, send_to_process, _osa_escape
from .chat import read_chat
from .finish import (start_finish, _park_on_trunk, _reachable, _closeouts,
                     _prune_closeouts, CLOSEOUT_TTL_S,
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
from .server import Handler
