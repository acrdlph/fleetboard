# ADR 0007 — Claude Code hooks as a first-class signal source

**Date:** 2026-07-21 · **Status:** Accepted in principle; mechanism to be specified

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

## Open

The exact hook events available, their payloads, and the non-invasive installation mechanism are
still to be specified. This ADR fixes the *direction*; `ENGINE.md` fixes the mechanism.

## Alternatives rejected

- **tmux `capture-pane` polling** to detect a prompt. orchestra already uses `capture-pane`
  elsewhere, so it is proven — but it is a screen-scrape: fragile against CLI output changes,
  only works for tmux-hosted agents, and costs a subprocess per probe. Retained as the lowest
  tier of the fallback ladder, not the primary.
- **Accepting inferred statuses permanently.** The status vocabulary is the product's core value;
  leaving two of six statuses as admitted guesses caps how good it can get.
