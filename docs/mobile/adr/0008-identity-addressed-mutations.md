# ADR 0008 — Mutations are addressed by durable identity, never by pid

**Date:** 2026-07-21 · **Status:** Accepted · **Implemented** (`orchestra/identity.py`)

## Context

This began as a design rule for the mobile client and turned out to describe a **bug that exists
in the shipping code today**.

`/api/send` — the chat reply path — is addressed by raw pid:

- `index.html:703` `sendToPid(pid, text)` POSTs `{pid, text}`. No sid, no account, no worktree.
- The pid is captured into the drawer when it opens (`window._chatPid` / the `chatTarget`
  select) and posted whenever the user finally hits send — possibly many minutes later.
- `send_to_process()` (orchestra.py:1379) validates only:
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

`fire_resume`'s docstring (orchestra.py:2113–2116) already names this class of failure — but
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

## As implemented

One resolver, `orchestra/identity.py`, and `terminal` is its only caller inside the package — so
there is no path to typing at an agent that skips it. Two shapes of address:

* **by session** (`sid`) — re-runs the pairing the board itself ran (`scan_sessions` →
  `pair_sessions_with_procs`) over a process table read *now*, and answers with whichever process
  serves that sid at this instant. A pid hint that disagrees is the recycle case, and is refused.
* **by place** (`worktree` / `cwd` / `tmux`) — for a target that is a terminal rather than a
  conversation. The pid must still be a live `claude` **and** still be where the client said.

`account`, `tty` and `tmux` ride along on either shape as corroborators; all three are already on
the wire, so they cost nothing and each one more thing a recycled pid would have had to reproduce.
Refusals carry `error: "identity_gone"` or `"unaddressed"` beside the human message.

The audit of the other endpoints found one more instance of the same shape and two non-instances:

| endpoint | before | after |
|---|---|---|
| `/api/send` | `{pid, text}`, validated as "is there *some* claude with this pid" | addressed by the same `{account, sid}` the drawer already used to *read*; pid is a hint |
| `resume.fire_resume` | typed at `proc["pid"]` taken from `observer.cached_state()` — an **advisory** snapshot (`Snapshot`'s own docstring: "never a mutation precondition"), up to a sweep old | addressed by the sid the schedule was armed for; a refusal falls through to the tmux path, which targets the sid exactly |
| `/api/finish` | worktree-addressed at the door, but then typed at a pid found before a `git fetch`, a merge-base and a status | the worktree and pane travel down to the send, which re-resolves |
| `/api/focus` | pid only | same resolver — focus types nothing, but its tmux branch *opens* an attached window, and a rule with an exception is a rule somebody copies the exception from |
| `/api/dispatch` | — | already durable: names a worktree, creates a new agent, addresses no pid |
| `/api/resume/schedule` `/cancel` | — | already durable: keyed `worktree\|sid` |

The legacy pid-only wire form stays callable and now **refuses** (`unaddressed`) rather than
guessing. A sid resolve costs 155 CPU-ms on this fleet (getrusage SELF+CHILDREN, 9 worktrees,
12 live agents) — one `ps` plus a memo-warm transcript scan, paid per click, never per sweep.

## Related

The same class of problem appears in the **finish** flow: its arm step lives only in the browser
(`window._armFinish`, a ~6 s double-click window in `index.html`). With two clients it
desynchronises immediately, and a notification action button has nowhere to hold it. The arm
must move server-side into a durable intent record. Tracked in `ENGINE.md`.
