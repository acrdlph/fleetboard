# fleetboard ⌁

**Local mission control for parallel Claude Code agents.**

You're running Claude Code agents in five worktrees at once — maybe across
several accounts. Which agent is working? Which one is stuck waiting for an
answer? Which worktree is free for the next feature? fleetboard puts it on one
board.

![fleetboard](docs/social-preview-v2.png)

It's a **read-only observer**: it never launches, wraps, or touches your
sessions. Point it at your existing chaos and it reports. Zero dependencies —
one python3 stdlib file.

```bash
git clone https://github.com/acrdlph/fleetboard && cd fleetboard
python3 fleetboard.py --root ~/code        # then open http://127.0.0.1:4242
```

## What it answers

- **Which worktree is free?** The header tile names worktrees with no live
  `claude` process and nothing mid-turn — safe targets for the next agent.
- **Who needs me right now?** Cards sort attention-first. Per session:

  | badge | meaning |
  |---|---|
  | `● WORKING` | transcript written < 90 s ago |
  | `▲ NEEDS ANSWER` | live process with a pending question for you |
  | `■ BLOCKED` | live process stuck on an unresolved tool call (permission prompt?) |
  | `◆ YOUR TURN` | live process idle at the prompt — the turn is finished |
  | `○ ENDED` | recent transcript, but no live process behind it |

- **What is each one doing?** Branch, dirty file count, ahead/behind upstream,
  last commit, each session's opening prompt, and the agent's last words.
- **Which account is where?** Multi-account setups (`~/.claude`,
  `~/.claude-work`, `~/.claude-account2`, …) are auto-discovered; each session
  is tagged with its account.

## Multi-account setups

fleetboard follows the same conventions as its companion tool
[cclimits](https://github.com/acrdlph/cclimits) (usage limits across accounts
in one table — see its README for how to set up multiple accounts via
`CLAUDE_CONFIG_DIR`):

1. **Auto-discovery** — any `~/.claude` or `~/.claude-*` directory containing
   `projects/` is picked up; the account is named after the dir suffix, so
   `~/.claude-work` shows as `work` (bare `~/.claude` shows as `main`).
2. **Non-standard locations** — pass `--home DIR` (repeatable), set `"homes"`
   in the config file, or export `CLAUDE_CONFIG_DIRS` as a colon-separated
   list, exactly as cclimits does.

The page auto-refreshes every 5 s (collectors are cached at 4 s so the browser
can't hammer git/lsof), flags the tab title with `(N!)` when agents need you,
and can optionally ring a terminal bell.

**Lost the window an agent runs in?** Every process chip shows its tty and
hosting app (`⌖ 38627 ttys024 Terminal`) — click it to bring that window to
the front. Works via AppleScript for Terminal.app and iTerm2 on macOS (grant
the Automation permission when asked); editor-embedded terminals (VS Code,
Cursor) get the app activated with a pointer to the right tty; tmux-hosted
agents get the attach command.

The board itself (`--demo` data):

![the fleetboard dashboard](docs/screenshot.png)

## The map — where your branches really are

`/map` draws the fleet's actual git topology: one trunk per repo
(`origin/main`), and every worktree placed where it truly is — branches leave
the trunk at their real merge-base and run to their tip; worktrees whose HEAD
sits on main appear as riders on the trunk itself, at the exact commit they're
parked on.

![the branch map](docs/map.png)

Reading it: **time runs log-scaled into "now"** at the right edge, so the last
hour gets room and last month compresses. A tip that stops short of the edge
is a branch that stopped moving. A long flat arc that forked far left and is
`↓137` behind is drift you should rebase. Dots are commits; tips take the live
status color from the fleet state (a working agent's tip pulses). Hover any
line for details; click a node to jump to that agent's terminal.

## Fleet orchestration — handoffs across accounts

The pattern that makes a multi-account fleet work: an agent burns its account
down, writes a handoff doc (drop to a cheaper model for that), and an agent
on a **different account** picks the branch up and keeps going.

![one branch, three accounts, zero downtime](docs/orchestration.png)

fleetboard understands this succession. A limit-hit session whose worktree
has a fresher live session is annotated **"↳ work continued by [account] —
this terminal can be closed"**, drops out of the need-you counts, and stops
speaking for the branch on the map — the successor's status takes over. Only
a stranded agent with *no* successor keeps demanding your attention.

## Acting on the fleet — chat, resume, dispatch

fleetboard is also a control plane (loopback-only; every action is an explicit
click):

- **✉ chat** — every session row opens a chat drawer: the conversation is read
  from the transcript, and your reply is typed straight into the agent's
  terminal (tmux `send-keys`; AppleScript for Terminal.app/iTerm2 — grant the
  Automation permission; editor-embedded terminals are read-only).
- **▶ resume** — a session-limit-stuck agent gets a resume button with a live
  countdown that arms itself the moment the limit resets, then types
  `continue` for you. Weekly limits never show it (they won't heal soon).
- **🚀 new mission** — describe a feature; fleetboard picks the cleanest free
  worktree and the account with most headroom (or lets a one-shot
  `claude -p --model haiku` router choose and write the kickoff brief), then
  launches a tmux-hosted agent: `tmux -L fleet` sessions, attachable from any
  terminal, visible on the board like any other agent.

Dispatched agents run `--dangerously-skip-permissions` — nobody is at their
prompt to approve tools. Nothing dispatches, resumes, or spends usage on its
own; you are always the trigger.

## Limits — is the agent stuck, or out of juice?

An agent parked at the prompt on an exhausted account isn't "your turn" — it's
out of usage. fleetboard shells out to
[cclimits](https://github.com/acrdlph/cclimits) and joins per-account limit
state into every session: exhausted accounts flip their parked sessions to
`⛔ LIMIT HIT · Weekly · resets in 4d2h`, and the CLI's own "out of usage
credits" transcript notice is detected as a fallback even when the limits
cache is cold.

The `/limits` view shows every account side by side — headroom, per-limit
bars, reset countdowns, and which account has the most room for your next
agent:

![the limits view](docs/limits.png)

Polling discipline: results are cached server-side for 5 minutes on top of
cclimits' own cache, and a **network refetch only ever happens when you click
"force refetch"** — nothing polls the Anthropic API on a timer.

## Usage

```bash
python3 fleetboard.py [--root DIR]... [--pattern REGEX] [--home DIR]...
                      [--port N] [--window-h H] [--demo]
./start.sh            # restart + open browser (extra args passed through)
```

| flag | default | meaning |
|---|---|---|
| `--root DIR` | cwd | directory whose git-repo children are watched (repeatable) |
| `--pattern REGEX` | all | only watch child dirs matching this (case-insensitive) |
| `--home DIR` | auto | Claude home dirs; default finds `~/.claude*` |
| `--port N` | 4242 | also `FLEETBOARD_PORT` env |
| `--window-h H` | 48 | ignore transcripts idle longer than this |
| `--demo` | — | serve fictional data (screenshots, kicking the tires) |

Persistent settings go in a `fleetboard.config.json` next to the script
(gitignored — see `.gitignore`):

```json
{ "roots": ["/Users/you/code"], "pattern": "myproject" }
```

Worktrees are discovered as immediate children of each root that are git
repositories; a `<dir>/repo` checkout layout is also recognized.

## How it works

- **Sessions** — tail-parses the last 128 KB of each
  `<claude-home>/projects/<munged-cwd>/*.jsonl` transcript, skipping subagent
  sidechains. The topic is the compaction summary or the first real user
  prompt; slash-command stubs and ANSI noise are filtered out. Each card shows
  both the latest prompt (`→` what the agent was told to do) and the agent's
  last reply (`⏎`).
- **Subagents & workflows** — a session running a Workflow or subagents writes
  to `<session-id>/**/*.jsonl` while its main transcript sits untouched;
  fleetboard counts that activity toward liveness and shows
  "⚙ subagents running" instead of misreporting the session as idle.
- **Liveness** — `ps` for `claude` processes, then their cwds via one `lsof`
  call (macOS/BSD) or `/proc/<pid>/cwd` (Linux). A live process vouches for at
  most one session per directory (freshest first), so stale transcripts don't
  masquerade as waiting agents.
- **Mapping** — transcript project dirs are matched to worktrees by munged
  path prefix, longest prefix wins (so `myapp` doesn't swallow
  `myapp-security-audit`).

## Caveats

- The transcript format is an undocumented Claude Code internal (tested
  against v2.1.x) — a CLI update can break parsing. Statuses are heuristics:
  transcripts don't record permission prompts explicitly, so BLOCKED /
  YOUR TURN are inferred.
- **The board serves your prompts and your agents' replies.** It binds to
  127.0.0.1 by default; don't expose it to the network.
- Usage/limit tracking is deliberately out of scope — that's what
  [cclimits](https://github.com/acrdlph/cclimits) is for (and the limits API
  shouldn't be polled every 5 s) — though an agent announcing "you've hit your
  weekly limit" shows up in its last-words snippet anyway.

## License

[MIT](LICENSE)
