"""orchestra.terminal — the actuator: focus a window, type into a shell.

Everything else in the package observes. This module is where the board
touches the outside world, and it has exactly two verbs. `focus_process`
brings the terminal hosting a pid to the front — or, for a tmux-hosted agent,
opens a real Terminal window attached to the session so you can type in it
directly. `send_to_process` types a line plus Enter into the shell a claude
process is running in.

Two mechanisms, picked by how the agent is hosted. tmux panes get
`tmux send-keys`, which is exact and needs no permissions. Terminal.app and
iTerm2 get AppleScript, matched on the tty — which means the user must have
granted Automation permission, so every failure path here says so rather than
failing silently. Anything else (Cursor, VS Code, an unknown host) can't be
scripted at all; we say that too, and offer focus as the fallback.

The AppleScript templates are `%`-formatted with values that came from a
transcript, so `_osa_escape` guards the quoting. `send_to_process` also
collapses every newline to a space before typing: a bare Enter mid-message
would submit half a prompt.

NEITHER VERB TAKES A PID AS ITS ADDRESS. Both take a durable identity — a sid,
a worktree, a tmux pane, a cwd — and hand it to `identity.resolve`, which
re-reads the process table and answers with the process that identity names
*now*; the pid rides along as a hint and is checked against that answer. This
module is the only place in the package that types at an agent, so routing both
verbs through one resolver is what makes the rule unskippable rather than a
convention. ADR 0008, and the reason it exists: the drawer captures a pid when
it opens and the user sends minutes later, so a recycled pid delivers the
message to a stranger running --dangerously-skip-permissions.
"""

import re
import shlex

from . import config, shell, identity


# --------------------------------------------------------------- focus jump

_FOCUS_TERMINAL = '''
tell application "Terminal"
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (tty of t) is "%s" then
          set selected tab of w to t
          set index of w to 1
          set found to true
        end if
      end try
    end repeat
  end repeat
  if found then activate
  return found
end tell'''

_FOCUS_ITERM = '''
tell application "iTerm2"
  set found to false
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if (tty of s) is "%s" then
            tell s to select
            tell t to select
            select w
            set found to true
          end if
        end try
      end repeat
    end repeat
  end repeat
  if found then activate
  return found
end tell'''


def focus_process(pid, **ident):
    """Best-effort: bring the terminal window hosting an agent to the front.

    Identity-addressed like `send_to_process`, and for a smaller reason: focus
    types nothing, so a misdirected focus costs confusion rather than an
    injected instruction. It is still refused rather than guessed, because the
    tmux branch below does not merely raise a window — it OPENS one, attached
    read-write to a session — and because a rule with an exception is a rule
    somebody will copy the exception from.
    """
    proc, refusal = identity.resolve(pid, **ident)
    if refusal:
        return refusal
    pid = proc["pid"]
    tty, host, kind = proc["tty"], proc["host"], proc["host_kind"]
    where = f"pid {pid}" + (f" · {tty}" if tty else "")
    if kind == "tmux":
        # Open a real Terminal window attached to the session (read-write —
        # you can type in it directly). Detach later with Ctrl-b d.
        sock = proc.get("tmux_sock")
        session = (proc.get("tmux_target") or "").split(":", 1)[0]
        if not session:
            return {"ok": False, "message": f"{where}: couldn't resolve tmux session"}
        attach = "tmux" + (f" -L {shlex.quote(sock)}" if sock else "") + \
                 f" attach -t {shlex.quote(session)}"
        script = ('tell application "Terminal"\n  do script "%s"\n  activate\nend tell'
                  % _osa_escape(attach))
        rc, _ = shell.run(["osascript", "-e", script], timeout=8)
        if rc == 0:
            return {"ok": True, "message": f"opened Terminal attached to {session} (Ctrl-b d to detach)"}
        return {"ok": False, "message":
                f"couldn't open Terminal — grant Automation permission, or run:  {attach}"}
    if host in ("Terminal", "iTerm2") and tty:
        script = (_FOCUS_TERMINAL if host == "Terminal" else _FOCUS_ITERM) % f"/dev/{tty}"
        rc, out = shell.run(["osascript", "-e", script], timeout=8)
        if rc == 0 and out.strip() == "true":
            return {"ok": True, "message": f"focused {host} window ({tty})"}
        if rc != 0:
            return {"ok": False, "message":
                    f"couldn't script {host} — grant Automation permission "
                    f"(System Settings → Privacy → Automation), or find {tty} manually"}
        return {"ok": False, "message": f"no {host} tab with {tty} found"}
    if host in ("Cursor", "VS Code"):
        app = "Cursor" if host == "Cursor" else "Visual Studio Code"
        shell.run(["open", "-a", app])
        return {"ok": True, "message":
                f"{where} lives in an embedded terminal inside {host} — "
                f"activated it, check its terminal panel"}
    if host:
        return {"ok": True, "message": f"{where} runs in {host} — look for {tty}"}
    return {"ok": False, "message": f"unknown host for {where}"}


# ----------------------------------------------------- talk to agents (send)

_SEND_TERMINAL = '''
tell application "Terminal"
  repeat with w in windows
    repeat with t in tabs of w
      try
        if (tty of t) is "%s" then
          do script "%s" in t
          return true
        end if
      end try
    end repeat
  end repeat
  return false
end tell'''

_SEND_ITERM = '''
tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if (tty of s) is "%s" then
            tell s to write text "%s"
            return true
          end if
        end try
      end repeat
    end repeat
  end repeat
  return false
end tell'''


def _osa_escape(text):
    return text.replace("\\", "\\\\").replace('"', '\\"')


def send_to_process(pid, text, **ident):
    """Type `text` + Enter into the terminal hosting a claude process.

    `pid` is a hint; `ident` is the address (`sid`/`account`, `worktree`,
    `cwd`, `tmux`, `tty` — see `identity.resolve`). The resolve happens HERE,
    immediately before the keystroke, not in the caller and not off a snapshot:
    everything between reading the board and typing is window for the pid to
    become somebody else's.

    The identity is checked after the message is normalised and found non-empty
    — an empty send should cost nothing, least of all a `ps` — and before any
    of the three delivery mechanisms, which is the only ordering that has all
    of them covered.
    """
    if config.DEMO:
        return {"ok": False, "message": "demo mode — no live agents to talk to"}
    text = re.sub(r"\s*\n\s*", " ", text).strip()
    if not text:
        return {"ok": False, "message": "empty message"}
    proc, refusal = identity.resolve(pid, **ident)
    if refusal:
        return refusal
    if proc.get("tmux_target"):
        sock = ["-L", proc["tmux_sock"]] if proc["tmux_sock"] else []
        rc1, _ = shell.run(["tmux"] + sock + ["send-keys", "-t", proc["tmux_target"], "-l", text])
        rc2, _ = shell.run(["tmux"] + sock + ["send-keys", "-t", proc["tmux_target"], "Enter"])
        ok = rc1 == 0 and rc2 == 0
        return {"ok": ok, "message": "sent via tmux" if ok else "tmux send-keys failed"}
    if proc["host"] in ("Terminal", "iTerm2") and proc["tty"]:
        script = (_SEND_TERMINAL if proc["host"] == "Terminal" else _SEND_ITERM) % (
            f"/dev/{proc['tty']}", _osa_escape(text))
        rc, out = shell.run(["osascript", "-e", script], timeout=10)
        if rc == 0 and out.strip() == "true":
            return {"ok": True, "message": f"typed into {proc['host']} ({proc['tty']})"}
        return {"ok": False, "message":
                f"couldn't reach {proc['host']} — Automation permission? ({proc['tty']})"}
    return {"ok": False, "message":
            f"{proc['host'] or 'unknown host'} terminals can't be scripted — focus it instead"}
