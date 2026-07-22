# Claude Code's transcript format, as observed

Everything orchestra knows about an agent it reads off disk. The format is an **undocumented
internal** — this is what was measured, not what is specified, and a CLI release can change any
of it. Counts come from the dev machine's corpus (~18,700 `.jsonl` files across 8 accounts,
July 2026, CLI 2.1.x).

Treat every claim here as *observed with a count*, and re-measure before relying on it. Method in
[METHOD.md](METHOD.md).

---

## Layout

```
<claude-home>/projects/<munged-cwd>/<session-id>.jsonl     the conversation
<claude-home>/projects/<munged-cwd>/<session-id>/**/*.jsonl subagent + workflow transcripts
```

`munge` is `re.sub(r"[^A-Za-z0-9]", "-", path)` — every non-alphanumeric becomes a dash, so
`/Users/a/code/app` → `-Users-a-code-app`. Matching a project dir back to a worktree is a
**longest-prefix** problem: `myapp` must not swallow `myapp-audit`.

Homes are `~/.claude` and `~/.claude-*`, each containing `projects/`. The suffix is the account
label (`~/.claude-work` → `work`; bare `~/.claude` → `main`).

Files are **append-only** in practice. That is what makes a `(size, mtime_ns)` memo viable — but
compaction rewrites, so identity `(st_dev, st_ino)` must be carried alongside.

---

## Entry types

One JSON object per line. `type` is not a closed set — these were observed as the *terminal*
entry of in-window sessions, which is a good sample of what actually gets written:

| terminal entry | count (79 in-window) |
|---|---|
| `last-prompt` | 36 |
| `file-history-snapshot` | 27 |
| `ai-title` | 4 |
| `permission-mode` | 3 |
| `user` | 2 |
| `system` / `turn_duration` | 2 |

Plus `assistant`, `summary` (compaction), `queue-operation`, `attachment`.

Two flags that gate almost everything:

- **`isSidechain`** — the entry belongs to a subagent, not the main conversation. Filter it out or
  a session's own transcript reads as somebody else's work.
- **`isMeta`** — a `user`-typed entry that is not the human. Filter for anything user-facing.

---

## The turn boundary — the strongest signal available

`system` with `subtype: "turn_duration"` marks a turn ending. It carries
`pendingWorkflowCount` and `pendingBackgroundAgentCount`.

**It is only meaningful positionally.** The question that matters is:

> does a `turn_duration` appear **after the last `assistant` message** in the non-sidechain stream?

| question asked | answer |
|---|---|
| is `turn_duration` the literal last line? | **3 %** |
| does it appear after the last assistant message? | **82 %** (66 of 79) |
| (a prior analysis reported) | 56 % |

The gap is `last-prompt` and `file-history-snapshot`, which the CLI appends *after* a turn
closes. Asking "is it last?" throws away the best signal in the system. See METHOD.md §3.

A later **real human prompt** also withdraws the marker — otherwise the board reports "your turn"
during the seconds between the user typing and the agent's first token. A slash-command stub
(`/model opus`) does not.

---

## Background work — invisible unless you know where to look

**A background `tool_use` is RESOLVED the moment it starts.** The `tool_result` is the harness's
*receipt*, not the work. So `pending_tools` empties immediately and every other idleness signal
reads true, while the session is very much not the user's turn.

**`run_in_background: true` is neither necessary nor sufficient:**

- 16 of 485 notified Bash launches carry no such flag — a foreground Bash that outruns its
  timeout is *moved* to the background (`"…was moved to the background (ID: …)"`).
- 337 `Agent` calls look identical in the input yet ran in the foreground and returned inline.

**The reliable signal is a phrase the harness writes into the tool_result:**

> `"you will be notified"` / `"you'll be notified"`

Of **49,115** tool_use records, **2,069** results carry it — Bash 1,017, Workflow 541, Agent 442,
Monitor 36, SendMessage 33 — and **no foreground result does**. Matching the phrase survives new
tool types; matching a list of tool names does not.

⚠️ In the Workflow receipt the phrase sits **nine lines in**, past the transcript dir and resume
instructions. Matching only a headline misses it.

### What comes back

`<task-notification>` carries **both** `<task-id>` and `<tool-use-id>`. Pairing is exact, not
heuristic: of 1,163 distinct tool-use-ids seen in notifications, **1,163 resolved** (100 %).

Terminal `<status>` values: `completed` 5,065 · `failed` 291 · `killed` 81 · `stopped` 4.

⚠️ `Monitor` also streams **interim** `<event>` notifications (397 observed) carrying *no*
tool-use-id and *no* terminal status. Reading one as the report drops the guard mid-stream.

### Three on-disk shapes

The same notification appears as:

| shape | where the text lives | count |
|---|---|---|
| plain `user` entry | `message.content` | 1,303 |
| `queue-operation` | top-level `content` | 2,626 enqueue + 1,168 remove |
| `attachment` | `attachment.prompt` | 898 |

Reading only `message.content` sees about a third of them. A parser gate of
`if not isinstance(msg, dict): continue` skips the other two entirely.

### It does not always come back

**76 of 2,000 background launches (3.8 %) never produce any notification** — Workflow 37, Bash 32,
Agent 7 — killed, or lost to a restart. The transcript then continues for a median **41,956 s**.
Anything treating "launched" as "still running" needs a bound, or one dead task pins a session
forever.

---

## Machine text in `user` entries

The harness writes as the user. Without filtering, a board quotes the plumbing back at you —
measured at **27 of 654** transcripts before the filter landed:

- the compaction preamble — `This session is being continued from a previous conversation…`
- `<teammate-message teammate_id="…">` — another harness talking to the agent
- terminal mouse-tracking escapes leaking from a click in the composer — `<64;58;44M58;44M/exit`
- `<system-reminder>`, `<local-command-stdout>`, `<command-message>`, `[SYSTEM NOTIFICATION`,
  `task-notification`, tool-use ids

Filter conservatively: over-filtering costs a blank line, under-filtering shows the user their own
tooling. A genuine prompt containing the word *"summarize"* must survive.

---

## Timing

**File mtime is a lying clock.** Worst observed: `mtime_age` 1,779 s against a true evidence age
of **219,803 s** — the file claimed 30 minutes when the last real activity was 2.5 days earlier.
Derive age from parsed transcript evidence, not `stat()` alone.

**Inter-entry gaps**, over 3,720 *unexplained* mid-turn silences (no tool pending, turn still
open) — the population any "has it gone quiet?" timer must tolerate:

```
p50 0.9 s    p95 25.9 s    p99 87.3 s    max 1277 s
```

A timeout at 25 s mistakes **5.22 %** of genuine thinking pauses for the end of a turn; at 45 s,
2.71 %; at 90 s, 0.89 %.

**Subagent activity keeps a session alive.** A session running a Workflow writes only under
`<session-id>/` while its main transcript sits untouched. Liveness must be
`max(mtime, subagent_mtime)`, or long multi-agent sessions vanish from view.

---

## Scale, and the disk cost

```
8 accounts · 295 project dirs · ~700 top-level transcripts
~18,700 .jsonl including subagent trees · ~5 GB
growth ~+1,000 files/day, peak +4,123
```

A single transcript can exceed 100 MB. **Never read one whole** — tail the last 128 KB and drop
the leading partial line. Watching every file with `kqueue` is not free either: the binding
ceiling on macOS is `kern.maxfilesperproc` (61,440) and the *system-wide* `kern.maxfiles`
(122,880, ~18k already in use) — **not** `RLIMIT_NOFILE`. Exhausting the global table breaks
other applications. Bound the watch set: project directories for discovery, in-window transcripts
for writes, never the subagent trees.
