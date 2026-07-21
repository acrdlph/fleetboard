"""orchestra.transcripts — what the agents WROTE: Claude Code's .jsonl files.

Claude Code keeps one transcript per session under
`<home>/projects/<munged-cwd>/<session-id>.jsonl`, append-only, one JSON
object per line, with workflows and subagents writing into a sibling
`<session-id>/` directory. This module is the only place that knows that
shape. It finds the homes (multi-account setups included), reads a bounded
chunk off either end of a file — never the whole thing, transcripts run to
hundreds of megabytes — and turns the last few hundred kilobytes into the
handful of facts a card needs: topic, model, last thing said either way,
which tools are still unresolved.

`_clean` and `_real_prompt` are the filter between a transcript and a human
eye: strip ANSI and tags, collapse whitespace, and refuse the machine text
the harness injects (system reminders, command stubs, tool-use ids) so a card
never shows the plumbing back to you.

`scan_sessions` is the top of this file's stack and the join point of the
whole observe layer: it maps every recent session onto a worktree (gitrepo),
hands the per-worktree session list and process list to `procs` for pairing,
and asks `status` what each one MEANS. Everything is read-only — the board
opens transcripts, it never writes one.
"""

import json
import os
import re
from pathlib import Path

from . import config, gitrepo, procs, status

TAIL_BYTES = 128 * 1024
HEAD_BYTES = 16 * 1024


# ---------------------------------------------------------------- collectors

def claude_homes():
    # Precedence: --home / config "homes" > CLAUDE_CONFIG_DIRS (colon-separated,
    # same convention as cclimits) > auto-discover ~/.claude*
    explicit = config.CFG["homes"] or [
        h for h in os.environ.get("CLAUDE_CONFIG_DIRS", "").split(":") if h]
    if explicit:
        return [Path(h).expanduser() for h in explicit
                if (Path(h).expanduser() / "projects").is_dir()]
    homes = []
    for p in sorted(config.HOME.iterdir()):
        if (p.name == ".claude" or p.name.startswith(".claude-")) and (p / "projects").is_dir():
            homes.append(p)
    return homes


def _read_chunk(fp, size, from_end):
    try:
        with open(fp, "rb") as f:
            if from_end:
                f.seek(0, 2)
                n = f.tell()
                f.seek(max(0, n - size))
                data = f.read()
                if n > size:  # drop leading partial line
                    data = data.split(b"\n", 1)[-1]
            else:
                data = f.read(size)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _clean(text, limit=240):
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"<command-name>(.*?)</command-name>", r"\1", text, flags=re.S)
    text = re.sub(r"<[^>]{1,80}>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


_MACHINE_TEXT = re.compile(
    r"<local-command-stdout>|<command-message>|<system-reminder>|"
    r"task-notification|\btoolu_[A-Za-z0-9]|\[SYSTEM NOTIFICATION|"
    # The compaction preamble. The CLI writes it as a *user* entry, so without
    # this the board quotes the harness back to you as "the last thing you told
    # it" — on precisely the long-running sessions you most need to read.
    r"This session is being continued from a previous conversation|"
    # Agent-to-agent messages injected by a teammate harness: another machine
    # talking, not this user.
    r"<teammate-message\b|"
    # Terminal mouse-tracking escapes that leak into the transcript when a
    # click lands in the composer (observed: "<64;58;44M58;44M/exit").
    r"<\d+;\d+;\d+[Mm]")


def _real_prompt(text):
    """A user text that describes the session (not a slash-command stub,
    caveat, or harness-injected machine noise)."""
    if _MACHINE_TEXT.search(text):
        return None
    t = _clean(text, 140)
    if not t or t.startswith("/") or t.startswith("Caveat:"):
        return None
    return t


def session_topic(fp):
    """Label a session: compaction summary if present, else first real user prompt."""
    for line in _read_chunk(fp, HEAD_BYTES, from_end=False).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") == "summary" and e.get("summary"):
            return _clean(e["summary"], 140)
        if e.get("type") == "user" and not e.get("isMeta"):
            c = e.get("message", {}).get("content")
            texts = [c] if isinstance(c, str) else [
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
            for t in texts:
                topic = _real_prompt(t)
                if topic:
                    return topic
    return None


def last_assistant_text(fp, size=TAIL_BYTES):
    """Last assistant text in a transcript, no sidechain filter (for subagent
    files, whose entries are all sidechain from the parent's perspective)."""
    last = None
    for line in _read_chunk(fp, size, from_end=True).splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        c = (e.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    last = _clean(b["text"])
    return last


def find_last_user(fp, size=1024 * 1024):
    """Deeper backward search for the latest real user prompt (fallback when
    the standard tail window is all tool traffic)."""
    for line in reversed(_read_chunk(fp, size, from_end=True).splitlines()):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("isSidechain") or e.get("type") != "user" or e.get("isMeta"):
            continue
        c = e.get("message", {}).get("content")
        texts = [c] if isinstance(c, str) else [
            b.get("text", "") for b in c
            if isinstance(b, dict) and b.get("type") == "text"] if isinstance(c, list) else []
        for t in texts:
            p = _real_prompt(t)
            if p:
                return p
    return None


def parse_session_tail(fp):
    """Tail-parse a transcript: last activity, pending tools, last assistant text."""
    entries = []
    for line in _read_chunk(fp, TAIL_BYTES, from_end=True).splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    main = [e for e in entries if isinstance(e, dict) and not e.get("isSidechain")]

    out = {"cwd": None, "branch": None, "model": None, "pending_tools": [],
           "last_assistant": None, "last_user": None, "pending_workflows": 0,
           "pending_bg_agents": 0}
    pending = {}  # tool_use id -> tool name
    for e in main:
        out["cwd"] = e.get("cwd") or out["cwd"]
        out["branch"] = e.get("gitBranch") or out["branch"]
        if e.get("type") == "system" and e.get("subtype") == "turn_duration":
            # a turn that ended still awaiting workflows or background agents
            # ("✻ Waiting for 1 background agent to finish") is NOT the user's
            # turn — the harness resumes the session when they report back
            out["pending_workflows"] = e.get("pendingWorkflowCount") or 0
            out["pending_bg_agents"] = e.get("pendingBackgroundAgentCount") or 0
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if e.get("type") == "user" and not e.get("isMeta"):
            texts = [content] if isinstance(content, str) else [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"] if isinstance(content, list) else []
            for t in texts:
                prompt = _real_prompt(t)
                if prompt:
                    out["last_user"] = prompt
        if e.get("type") == "assistant":
            model = msg.get("model")
            if model and model != "<synthetic>":
                out["model"] = model
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        pending[b.get("id")] = b.get("name", "?")
                    elif b.get("type") == "text" and b.get("text", "").strip():
                        out["last_assistant"] = _clean(b["text"])
        elif e.get("type") == "user" and isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pending.pop(b.get("tool_use_id"), None)
    out["pending_tools"] = sorted(set(pending.values()))
    return out


def scan_sessions(worktrees, all_procs, now):
    """All recent sessions across every Claude home, mapped to worktrees.

    `all_procs`, not `procs`: the module object of that name is what the
    session↔process pairing now hangs off, and a parameter would shadow it.
    Callers pass it positionally, so the name is local knowledge."""
    by_wt = {w["path"]: [] for w in worktrees}
    wt_prefixes = {w["path"]: gitrepo.munge(w["path"]) for w in worktrees}
    window_s = config.CFG["session_window_h"] * 3600

    for home in claude_homes():
        acct = config.account_label(home)
        for proj in (home / "projects").iterdir():
            wt = gitrepo.match_worktree(proj.name, wt_prefixes)
            if wt is None:
                continue
            for fp in proj.glob("*.jsonl"):
                try:
                    mtime = fp.stat().st_mtime
                except OSError:
                    continue
                # Workflows/subagents write to <session-id>/**/*.jsonl while the
                # main transcript sits untouched — count them toward activity.
                sub_files = []
                sub_dir = fp.with_suffix("")
                if sub_dir.is_dir():
                    for sf in sub_dir.rglob("*.jsonl"):
                        try:
                            sub_files.append((sf.stat().st_mtime, sf))
                        except OSError:
                            continue
                sub_mtime = max((m for m, _ in sub_files), default=0.0)
                # The newest thing the session "said" may be a subagent's
                # report (Claude Code shows those in the terminal too).
                subagent_said = None
                if sub_mtime > mtime:
                    for _, sf in sorted(sub_files, reverse=True)[:2]:
                        subagent_said = last_assistant_text(sf)
                        if subagent_said:
                            break
                age = now - max(mtime, sub_mtime)
                if age > window_s:
                    continue
                tail = parse_session_tail(fp)
                cwd = tail["cwd"] or wt
                by_wt[wt].append({
                    "id": fp.stem[:8],
                    "sid": fp.stem,
                    "account": acct,
                    "age_s": int(age),
                    "cwd": cwd,
                    "subdir": os.path.relpath(cwd, wt) if cwd != wt else None,
                    "branch": tail["branch"],
                    "model": (tail["model"] or "").replace("claude-", ""),
                    "pending_tools": tail["pending_tools"],
                    "pending_workflows": tail["pending_workflows"],
                    "pending_bg_agents": tail["pending_bg_agents"],
                    "topic": session_topic(fp),
                    "last_assistant": tail["last_assistant"],
                    "last_user": tail["last_user"] or find_last_user(fp),
                    "subagent_said": subagent_said,
                    "subagents_active": bool(sub_mtime and now - sub_mtime < config.CFG["working_s"]),
                })

    rank = {"needs_input": 0, "blocked": 1, "working": 2, "waiting": 3, "ended": 4}
    for wt, sessions in by_wt.items():
        sessions.sort(key=lambda s: s["age_s"])
        # A live process proves at most ONE session is really attended.
        # N procs under a worktree vouch for its N freshest sessions —
        # freshness beats cwd matching (recorded cwds drift as agents cd
        # around, and a stale exact match must not outrank the live session).
        wt_procs = [p for p in all_procs if p.get("cwd") and
                    (p["cwd"] == wt or p["cwd"].startswith(wt + "/"))]
        # With --dangerously-skip-permissions there are no approval prompts:
        # an unresolved tool call means a long-running tool, not "blocked".
        skip_perms = bool(wt_procs) and all(
            "--dangerously-skip-permissions" in p["cmd"] for p in wt_procs)

        owner = procs.pair_sessions_with_procs(sessions, wt_procs)
        for s in sessions:
            proc = owner.get(s["sid"])
            alive = proc is not None
            shell_n = proc.get("shells", 0) if proc else 0
            s["pid"] = proc["pid"] if proc else None
            # only an account match is a real attribution; a fallback pairing is
            # a guess and must not be presented as one
            s["pid_certain"] = bool(proc and proc.get("account") == s["account"])
            # `sess_status`, not `status`: the module object of that name is
            # what classify_session now hangs off, and a local would shadow it.
            sess_status, tool_running = status.classify_session(
                s["age_s"], alive, s["pending_tools"],
                s["pending_workflows"] + s["pending_bg_agents"],
                skip_perms, config.CFG["working_s"], shell_n)
            s["status"] = sess_status
            if tool_running:
                s["tool_running"] = True
                if shell_n and not s["pending_tools"]:
                    s["bg_shell"] = True     # transcript idle, shell alive
        sessions.sort(key=lambda s: (rank[s["status"]], s["age_s"]))
        by_wt[wt] = sessions[: config.CFG["max_sessions"]]
    return by_wt
