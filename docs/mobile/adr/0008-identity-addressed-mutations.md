# ADR 0008 — Mutations are addressed by durable identity, never by pid

**Date:** 2026-07-21 · **Status:** Accepted · **Fixes a live bug**

## Context

This began as a design rule for the mobile client and turned out to describe a **bug that exists
in the shipping code today**.

`/api/send` — the chat reply path — is addressed by raw pid:

- `index.html:703` `sendToPid(pid, text)` POSTs `{pid, text}`. No sid, no account, no worktree.
- The pid is captured into the drawer when it opens (`window._chatPid` / the `chatTarget`
  select) and posted whenever the user finally hits send — possibly many minutes later.
- `send_to_process()` (orchestr.py:1379) validates only:
  ```python
  proc = next((p for p in claude_processes() if p["pid"] == pid), None)
  ```
  i.e. *"is there some claude process with this pid?"* — not *"is it still **that** session?"*

`chatCtx` already holds `{account, sid}` and uses them to **read** the conversation
(`/api/chat?account=…&sid=…`, index.html:692). The **write** path throws that identity away.

**Consequence:** if the original agent exits and the OS recycles its pid to a different claude
process, the reply is typed into the wrong agent's terminal. Those agents run under
`--dangerously-skip-permissions`, so the misdirected text is acted on. Pid recycling is routine
on a machine running many short-lived agents.

`fire_resume`'s docstring (orchestr.py:2113–2116) already names this class of failure — but
guards only the resume path.

## Decision

**Every mutating endpoint is addressed by durable identity** — `sid`, worktree path, or tmux
target — and the server **re-resolves** that identity to a live process at execution time. A pid
may be sent as a *hint*, never as the sole address.

The server must reject a mutation whose supplied identity no longer resolves to the process it
names, with a distinguishable error the client can surface ("that agent is gone") rather than
silently acting on the wrong target.

Applies to: `/api/send`, `/api/finish`, `/api/dispatch`, `/api/resume/*`, `/api/focus`.

## Consequences

- Closes the misdirected-keystroke bug on the desktop today, independent of iOS.
- Required before any mobile client: a phone holds a drawer open across backgrounding, network
  loss and reconnection, so the window between capturing a handle and using it is far wider than
  on desktop. Pid-addressing would be actively unsafe.
- Combines with idempotency keys: identity says *who*, the idempotency key says *which attempt*.
- Some resolution failures become user-visible where they were previously silent. That is the
  point.

## Related

The same class of problem appears in the **finish** flow: its arm step lives only in the browser
(`window._armFinish`, a ~6 s double-click window in `index.html`). With two clients it
desynchronises immediately, and a notification action button has nowhere to hold it. The arm
must move server-side into a durable intent record. Tracked in `ENGINE.md`.
