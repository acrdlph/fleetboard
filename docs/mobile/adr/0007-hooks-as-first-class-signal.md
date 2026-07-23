# ADR 0007 — Claude Code hooks as a first-class signal source

**Date:** 2026-07-21 · **Status:** Accepted; **mechanism specified and shipped 2026-07-22**
(step 6, `orchestra/hooks.py`)

## Context

orchestra reverse-engineers agent state from file mtimes and the process table. Its own README
concedes the consequence: *"BLOCKED / YOUR TURN are inferred — permission prompts aren't
recorded in transcripts."*

This splits the user's "status is laggy" complaint into three problems that need **different**
fixes, and conflating them is the main way this work goes wrong:

| class | meaning | fix |
|---|---|---|
| **latency** | the truth changed, we have not looked yet | faster collection + push (ADR 0005, 0006) |
| **hysteresis** | we looked, but the rule deliberately holds the old value (`working_s = 90`) | precise write timestamps from a stateful engine (ADR 0006) |
| **ambiguity** | the signal genuinely cannot distinguish two states | **nothing above helps** — needs a better source |

No amount of faster polling resolves ambiguity. `BLOCKED` and `YOUR TURN` look identical from
the outside: a live process, an idle transcript.

## Decision

Ingest **Claude Code hooks** as a first-class, highest-quality signal source. A hook firing on
an agent state transition POSTs directly to orchestra, converting inferred statuses into
**observed** ones.

Signal sources are ranked, and the engine reconciles them when they disagree:

```
hooks (observed)  >  process table  >  precise file writes  >  mtime heuristics  >  tmux capture-pane
```

## Consequences

- `BLOCKED`, `YOUR TURN` and `NEEDS ANSWER` become ground truth rather than heuristics — the
  single largest available improvement in status *quality*, as distinct from status *speed*.
- Detection latency for a hooked transition drops to roughly network-local round-trip.
- **Adoption is the hard part.** Hooks only fire if installed. Required:
  - agents dispatched *by* orchestra can be configured automatically;
  - agents started independently by the user cannot — the system must degrade gracefully to
    inference, and must never present an unhooked session as less trustworthy in a confusing way;
  - an installation flow that does **not** hijack the user's own `settings.json` hooks.
- Introduces an inbound write path to the server, which must be authenticated like any other.

## Resolved — what was actually there (measured 2026-07-22, Claude Code 2.1.218)

This section replaces the "Open" one. Nothing below is remembered; it was captured by wiring all
30 hook events to a logging script and driving real sessions — a headless `-p` run, an interactive
tmux session left to idle, and an interactive session asked for a `Write` outside its cwd.

**The events.** Claude Code 2.1.218 defines **thirty**, not the six that are usually quoted:
`PreToolUse PostToolUse PostToolUseFailure PostToolBatch PermissionDenied Notification
UserPromptSubmit UserPromptExpansion SessionStart SessionEnd Stop StopFailure SubagentStart
SubagentStop PreCompact PostCompact PermissionRequest Setup TeammateIdle TaskCreated TaskCompleted
Elicitation ElicitationResult ConfigChange WorktreeCreate WorktreeRemove InstructionsLoaded
CwdChanged FileChanged MessageDisplay`.

**The two that retire the ambiguity.** `Notification` carries a `notification_type`, and two of its
eight values are exactly the states this ADR called indistinguishable:

| event | payload | means |
|---|---|---|
| `Notification` + `permission_prompt` | *"Claude needs your permission"* | ■ BLOCKED |
| `Notification` + `idle_prompt` | *"Claude is waiting for your input"* | ◆ YOUR TURN |
| `PermissionRequest` | `tool_name`, `tool_input`, `permission_suggestions[]` | ■ BLOCKED (fires with the above) |

**The join is free.** Every payload carries `session_id`, and it is the transcript filename stem —
the key `scan_sessions` already builds sessions on. One dict, no matching, no heuristics.

**Configuration.** Hooks are a `hooks` key in a settings JSON: `{event: [{matcher?, hooks:
[{type: "command", command: …}]}]}`. The command receives the payload on **stdin** and its exit
code is a control channel — `exit 2` on a `PreToolUse` **blocks the tool call**, and `exit 0`
stdout on a `UserPromptSubmit` **is shown to Claude**. An observability sidecar must therefore be
silent and exit 0 unconditionally.

**Installation, and it is the part that could have gone wrong.** `claude --settings <file>` loads
an **additional** layer. Measured: hooks from a `--settings` fragment fired *alongside* the hooks
in the settings the CLI loaded itself, for the same events, in the same session. So orchestra
writes a fragment of its own under `.orchestra/` and hands the path to the agents it launches, and
**never opens a `settings.json` it does not own** — on this machine seven of eight Claude homes
have no `hooks` key and one has two hooks somebody depends on.

**The kill switch nobody had modelled.** `claude --bare` skips hooks entirely (along with LSP,
plugins and CLAUDE.md discovery). Under it no hook ever fires, which is indistinguishable from a
server restart or a dropped POST — and is precisely why the 90 s TTL is not optional.

## Open, still

- Only agents orchestra launches are hooked. There is no non-invasive way to hook an agent the
  user started themselves, and inventing one would mean writing their settings. They degrade to
  rank 2–4, i.e. exactly today's behaviour.
- `Stop` carries `background_tasks[]` and `session_crons[]` — the CLI's own view of what it
  delegated. orchestra reads the same fact off disk and does not yet cross-check the two.

## Alternatives rejected

- **tmux `capture-pane` polling** to detect a prompt. orchestra already uses `capture-pane`
  elsewhere, so it is proven — but it is a screen-scrape: fragile against CLI output changes,
  only works for tmux-hosted agents, and costs a subprocess per probe. Retained as the lowest
  tier of the fallback ladder, not the primary.
- **Accepting inferred statuses permanently.** The status vocabulary is the product's core value;
  leaving two of six statuses as admitted guesses caps how good it can get.
