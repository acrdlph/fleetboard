# orchestra for iOS — UX & Design Specification

**Status:** design-complete, implementation-ready for v1.
**Scope:** the complete mobile user experience — information architecture, every screen, every flow, the visual system, and accessibility.
**Companions.** The four sibling specifications: `ARCHITECTURE.md` (server observation model), `API.md` (every wire contract named here), `IOS-APP.md` (module layout, state store, transport, extension targets), `ROADMAP.md` (the BE-n sequencing plan summarised in Appendix D). Supporting material: `VERIFIED-FACTS.md` (measurements), `ENGINE.md` / `FRESHNESS.md` (collector internals), `adr/0001`–`0008`.

**Precedence.** `VERIFIED-FACTS.md` outranks this document on any measured number; `API.md` outranks it on any field name, type, or endpoint shape. Where this document names a field, it states a *requirement on* `API.md`, not an independent definition. The branch-map data contract lives in §5 here and in `API.md`; the BE-n list lives in `ROADMAP.md`.

> **Endpoint names in this document are aliases.** It was written against an earlier draft and uses unversioned paths (`/api/state`, `/api/send`, `/api/events`, `/api/hello`, `/api/kill`, `/api/pasteboard`). The shipping contract is `/api/v1/…` with REST-shaped resources. **`API.md` §0.1 is the translation table; read it before implementing anything here.** `API.md` §0.2 lists the fields this document assumes that the contract does not yet define — chiefly five-valued `availability` (§3.1.2), the `confidence`/`provisional`/`liveness` block (§3.1.4), `terminal.attribution` (§3.3.1), the four free-text fields on the board payload (§3.1.4), and the map's axis parameters (§5.3). Each is flagged in place below.

Wireframes are drawn at ~52 characters of inner width, representing a 393 pt iPhone viewport at default Dynamic Type. Glyphs shown as `▲ ● ■ ◆ ⛔ ○` are **stand-ins for SF Symbols** (§9.4) — the shipped app renders no Unicode status glyphs.

---

# 1. Who is using this, and where

## 1.1 Two different jobs

The desktop board is **ambient**. It lives on a second monitor. It can afford to over-report, because the cost of a false signal is a glance. Everything is one hover away, the user's hands are on the machine that runs the agents, and "focus that terminal" is a meaningful verb.

The phone is **episodic and interruptive**. You open it because it buzzed, or because you have thirty seconds in a queue. You are not looking at the Mac. You cannot see the terminals. You may be on cellular, in sunlight, with one thumb, walking.

Three consequences drive every decision in this document.

**A phone answer is a sentence, not a dashboard.** The desktop opens with five stat tiles and a grid. The phone opens with `▲ 2 need you` and two names. Everything else is one tap below that.

**A push is a debt.** The desktop bell fires on any increase in `needs_input + blocked + waiting`. But `waiting` means "the agent finished its turn and is idle at the prompt" — a state that occurs at the end of every single turn, dozens of times a day. Push that and the user disables notifications within a week, taking the tier that actually matters with it. Attention is therefore tiered (§8.4), and the tiers are load-bearing.

**You cannot see the Mac.** Every desktop affordance whose payoff is "a window moves on the machine in front of me" is either dropped or re-motivated as "prepare something for when I arrive."

And one architectural premise, earned rather than assumed: **the server observes continuously, publishes a versioned snapshot, and streams changes.** The phone is a subscriber, not a poller, and it does not run a second copy of the server's triage logic. Most of what a naive port would spend its budget on — poll ladders, client-side diffing, a client-side severity function, client-side freshness heuristics — is deleted by that premise.

## 1.2 The ten principles

Stated as commitments. Every later section is checkable against them.

1. **Answer the question in one second.** The first line of the first screen is the answer. Everything else is progressive disclosure.
2. **Never claim knowledge the app does not have.** A stale number is labelled stale. An inferred status says it is inferred. A dash beats a confident zero. This applies hardest to widgets and notifications, which are read without context.
3. **The server is the single source of truth for triage.** Ordering, status, severity, availability, and every string that carries remediation come from the server, verbatim. The client has no `severity()` function.
4. **Friction is proportional to irreversibility × cost, never to how important the action feels.** Replying to an agent — the app's primary job — has zero dialogs. Launching a mission, which spends money and starts an autonomous process, has a confirmation sheet.
5. **Colour is a reinforcing channel, never the primary one.** Shape and word carry status everywhere: on a row, in a chart, in a notification, in a monochrome-tinted widget. Position (server sort order) is the third channel.
6. **Every actuating control is at least 44 × 44 pt, and that costs real layout space.** Density is recovered by dropping columns at large type sizes, not by shrinking targets.
7. **The board never moves under a finger.** Order and membership freeze on first touch and apply only on an explicit trigger. A tap on a row that just moved is refused, perceptibly.
8. **The app never initiates a retry of a non-idempotent write.** Where the platform can retry beneath us, an idempotency key is a shipping precondition, not an enhancement.
9. **Every failure surfaces the server's own prose.** orchestra's messages already carry the remedy (`"couldn't reach Terminal — Automation permission? (ttys004)"`). Paraphrasing destroys the only actionable part.
10. **Offline, stale, asleep and unpaired are first-class states with designed screens** — not spinners, not blank views, not silent last-known-good.

## 1.3 Where principles trade against each other

| Tension | Resolution |
|---|---|
| §1 (one second) vs §2 (never overclaim) | The headline shows a count *and* a freshness line when freshness is in doubt. When data is very stale the count dims and actuation blocks — the answer is still one second, it is just "I don't know yet". |
| §4 (no friction on reply) vs §8 (no blind retries) | Reply gets no dialog but does get a server-side `expect_sid` assertion and a visible three-state receipt. Safety moved from the user's attention to the protocol. |
| §6 (44 pt) vs desktop density | Most of what looks tappable on the desktop becomes non-interactive text on iOS. There are exactly two controls in a session row; the rest is display. |
| §7 (never move) vs §1 (show me what's new) | Held updates are counted and announced in a thumb-reachable pill. The user chooses when the board reorders. |

## 1.4 Platform baseline

| | |
|---|---|
| **Minimum iOS** | **Open — `ROADMAP.md` D4, to be closed in M0.** This document assumes **26.0**; `IOS-APP.md` §1.1 argues **18.0** and adopts 26-only APIs behind `if #available`. Only two things here genuinely require 26: `tabViewBottomAccessory` (§2.2) and Icon Composer (§9.9). `onScrollPhaseChange` and Control Center controls are 18, `UITraitOverrides` is 17, `Text(timerInterval:)` predates all of them. **If D4 lands on 18, the accessory becomes the `.safeAreaInset(edge: .bottom)` substitute already specified in §2.2 and nothing else in this document changes.** Distribution is TestFlight to the author's own devices either way — a self-hosted server with a per-user APNs key cannot be App Store distributed (§8.2). |
| **Language** | Swift 6, strict concurrency. State store is one `@MainActor @Observable` class; transport is an `actor`. |
| **Transport** | **HTTPS over Tailscale.** Which certificate — `tailscale cert` or self-signed + SPKI pin — is **`ROADMAP.md` D1, still open**. §1.5. Either way it is a blocker, not a detail. |
| **Realtime** | SSE (`GET /api/v1/stream`) is primary. The fallback is the **conditional-poll / long-poll ladder** of `API.md` §9.3 (`?since=`, `?wait=`), summarised in §3.13 — not long-polling alone, and never an unconditional background poll. |
| **Orientation** | Portrait on iPhone. Landscape works but is not optimised. iPad out of scope for v1. |
| **Handedness** | Hand-neutral by construction: no leading swipe actions, and everything frequently-touched lives in the bottom accessory. |

## 1.5 Transport must be TLS — the first blocker

A design that specifies `http://100.84.12.7:4242` cannot make its first network call. App Transport Security refuses cleartext by default, and `NSExceptionDomains` accepts domain names only — never IP literals. The only escape is `NSAllowsArbitraryLoads`, a blanket ATS disable, on an app whose function is remote command execution. Unacceptable.

**Which certificate is `ROADMAP.md` D1 and it is still open.** The documents currently disagree: this document and `ROADMAP.md` D1 recommend path A; `API.md` §1.1/§3.5 and `ARCHITECTURE.md` §5.4 specify path B in full detail. **Do not implement until D1 is closed** — the choice changes the QR payload, the pairing screen, and whether the client ships a trust delegate at all.

**Path A — `tailscale cert` + MagicDNS.** The Mac runs

```
tailscale cert studio-mac.tailXXXX.ts.net       # .crt + .key
```

and orchestra wraps its listener in ~12 lines of `ssl.SSLContext` (stdlib — the zero-dependency identity survives). Consequences:

- ATS is satisfied with **zero exceptions** and no trust delegate at all.
- The pairing QR carries `https://studio-mac.tailXXXX.ts.net:4242`, which survives the Mac's IP changing.
- **Costs, stated honestly:** the tailnet must have HTTPS certificates enabled in the admin console (ROADMAP S2 verifies this); certificates are **90-day and not self-renewing** — `tailscale cert` must be re-run on a schedule, or the port fronted with `tailscale serve`, which `API.md` §2.7 blocks by `Host` allowlist on purpose; and because the name is the identity, the client **cannot fall back to the raw `100.x.y.z` address** when MagicDNS is slow or off, which is the failure mode path B exists to cover.

**Path B — self-signed P-256 + SPKI pin in the QR.** Specified in `API.md` §3.5–3.6 and `ARCHITECTURE.md` §5.4. It is **also ATS-clean**: a self-signed certificate answered by a `URLSessionDelegate` server-trust challenge is an app's prerogative, not an ATS exception, so "ATS compliance" is not a discriminator between the two paths. It works on both the MagicDNS name and the raw IP, needs nothing enabled on the tailnet, and gives a server identity independent of the Tailscale coordination server. It costs a trust delegate, a pin-rotation story, and the verified LibreSSL trap (`openssl req -newkey ec` emits explicit curve parameters and breaks pinning 100 % of the time — the key must be generated separately with `ecparam -param_enc named_curve`).

`--pair` prints which path it used and why, whichever wins.

---

# 2. Information architecture

## 2.1 The tab bar, and why these four

```
┌────────────────────────────────────────────────────┐
│                                                    │
│                     content                        │
│                                                    │
├────────────────────────────────────────────────────┤
│  ⟳ live           ⌗ 3 updates   Board⇄Branches  ＋ │  ← bottom accessory
├────────────────────────────────────────────────────┤
│    ⌗          ⌁          ◔          ⧉              │
│  Fleet     Activity    Limits     Server           │
│    ②                                 ●             │
└────────────────────────────────────────────────────┘
```

**Four tabs. No centre action button.**

| Tab | The question it answers | Desktop origin |
|---|---|---|
| **Fleet** ⌗ | *Who needs me right now?* | `index.html` board + `map.html` |
| **Activity** ⌁ | *What is running, and what happened while I was away?* | dispatch log + job progress + armed schedules — all currently buried inside the mission composer |
| **Limits** ◔ | *Can I afford to start something? why is that agent parked?* | `limits.html` |
| **Server** ⧉ | *Am I connected? will it actually buzz me? how does any of this work?* | the `sync` dot, pairing (new), push setup (new), `guide.html` |

**The justification.** The desktop rail is `board / map / limits / guide` — four peers, because a 118 px rail is free real estate. On a phone a tab is 25 % of the permanent navigation budget and must be spent on *questions asked repeatedly*, not on *documents*.

- `guide` is read twice in a lifetime. No tab. Its content disperses into contextual **Why** sheets (§3.10) delivered at the moment of confusion, plus a full Manual inside Server.
- `map` is one projection of the same objects the board already shows, not a mode. It becomes a segmented toggle in Fleet's bottom accessory. The map's own analysis found it is the least-opened view and its unique payload is small (`fork_ts` and the trunk grouping); a tab would over-price it. **Nothing about the map's content changes — only its placement.** §5.
- The two questions the desktop has no home for — "what is in flight" and "is this thing even connected" — take the freed slots. Both are phone-specific: on the desktop you can see the terminals and the server's log.

**Multiple Macs.** Pairing stores a list of servers, one active at a time. The switcher lives in **Server → Machines only**. v1 does not aggregate across machines — the headline would become a lie about *which* machine needs you.

## 2.2 The bottom accessory — the thumb zone is not decorative

`tabViewBottomAccessory` floats above the tab bar, is fully thumb-reachable, and stays put while content scrolls.

| Tab | Accessory contents (leading → trailing) |
|---|---|
| Fleet | connection state · `⌗ N updates` (when held) · `Board ⇄ Branches` · **`＋`** |
| Activity | connection state · `＋` |
| Limits | connection state · `↻ refresh all accounts` |
| Server | connection state |

It also hosts transient **Undo** (§6.5) and the very-stale refusal banner (§3.12).

**`＋` is here, not in the nav bar's top-trailing corner.** Placement is not a safety mechanism — opening a composer costs nothing, and the Launch confirmation sheet is the guard. Putting the money-spending entry point in the status-bar undershoot zone would be a mis-tap generator dressed up as caution.

If `tabViewBottomAccessory` proves differently shaped than assumed, the structural substitute is `.safeAreaInset(edge: .bottom)` with a `.glassEffect()` container — same geometry, same contents.

## 2.3 Screen map

```
orchestra
│
├── ONBOARDING  (first launch only, and on 401)
│   ├── Welcome
│   ├── Prerequisites  (Tailscale · orchestra running)
│   ├── QR Scanner ──────────── Manual entry
│   ├── Paired ✓
│   └── Push setup ─── Sink choice ─── Mac steps ─── Verify ─── Permission
│
├── FLEET  ⌗
│   ├── Board  (segment)                          ← launch destination
│   │   ├── ▸ Worktree Detail
│   │   │     ├── ▸ Session Chat
│   │   │     │     ├── ▾ Session info
│   │   │     │     ├── ▾ Why is this?
│   │   │     │     └── ▾ Send to another agent…
│   │   │     ├── ▾ Finish  (two-step)
│   │   │     ├── ▾ Session info
│   │   │     └── ▸ Branch detail
│   │   ├── ▸ Counts  (the desktop's five tiles)
│   │   ├── ▾ Why is this?
│   │   ├── ▾ Finish
│   │   ├── ▾ Auto-resume
│   │   └── ▾ Filters
│   ├── Branches  (segment)                       ← §5
│   │   └── ▾ Branch detail  →  same Finish / Chat
│   └── ▾ Mission composer  (.large sheet)
│         ├── ▾ Launch confirmation
│         ├── ▾ Insufficient headroom
│         └── ⇢ transforms in place into Progress
│
├── ACTIVITY  ⌁
│   ├── In flight · Armed · Today · Earlier
│   ├── ▸ Intent Detail       (live dispatch / closeout)
│   ├── ▸ Dispatch Detail     (historical, full mission text)
│   └── ▾ Auto-resume         (from an armed row)
│
├── LIMITS  ◔
│   ├── Accounts list
│   └── ▸ Account Detail
│         ├── Per-limit bars + countdowns
│         ├── Reserve control
│         └── ▸ Sessions on this account  → deep-links into Fleet
│
└── SERVER  ⧉
    ├── Connection ▸ Server Detail       (version, uptime, clock skew, wake)
    ├── Notifications ▸ state machine · sink · this device · per-kind toggles
    │                  · quiet hours · previews · send a test
    ├── Machines ▸ per-machine · ＋ Pair another Mac
    ├── Appearance ▸ theme · high contrast · haptics
    └── Help ▸ Manual · Status legend · Troubleshoot the connection
```

## 2.4 Screen vs sheet vs inline

| Kind | Used for | Why |
|---|---|---|
| **Pushed screen** | >30 s of dwell, may need returning to: Worktree Detail, **Chat**, Account Detail, Intent Detail, Dispatch Detail, Manual | needs a back stack, must survive a phone call, is a push destination |
| **Sheet (detent-sized)** | a decision with a defined end: Finish, Auto-resume, Why, Insufficient headroom, Filters, Session info | dismissible by drag; context stays visible behind it |
| **Sheet `.presentationDetents([.large])`** | Mission composer | **not** `fullScreenCover`: that has no interactive dismissal, so its only exit is Cancel in the hardest-to-reach corner, on the screen most likely to be entered by accident. Drafts autosave, so free dismissal is safe. `.interactiveDismissDisabled` only while a launch is in flight. |
| **Inline in the row** | status, countdown, delivery receipt, refusal, "+3 more" | anything that must be visible *without* interaction — especially refusals, which on a phone may be read twenty minutes later |

**Chat is a pushed screen, not a drawer.** On desktop it is a 460 px drawer with the live board behind it. On a phone the board behind is invisible, and chat needs full height for keyboard plus transcript. A sheet would fight the keyboard and lose the nav title.

---

# 3. Screens

## 3.1 Fleet — Board

**Purpose.** Answer "who needs me" in under a second, then let one thumb act.

```
╔════════════════════════════════════════════════════╗
║  ⌁  orchestra        achill@fleet             🚀    ║  nav, inline title
╠════════════════════════════════════════════════════╣
║   ▲  2 need you                                    ║  34pt, accent
║      3 busy · 1 limited · 4 free · 1 idle          ║  13pt muted ▸ Counts
║   ⚠ limits unavailable — parked agents may look    ║  conditional
║     idle                                           ║
╟─ NEEDS YOU ────────────────────────────────────────╢
║ ┌────────────────────────────────────────────────┐ ║
║ │ ▲  ConfidAI-auth                               │ │  identity row
║ │    feat/auth-rotation   Δ7   ↑3                │ │  ← swipe: Finish
║ ├────────────────────────────────────────────────┤ │
║ │ ▲ NEEDS ANSWER      [work] opus      2m 04s    │ │  ← tap → Chat
║ │ Rotate refresh tokens without breaking exist…  │ │
║ │ → the JWT one, and keep the old key 24h        │ │
║ │ ⏎ I can take either approach — do you want     │ │
║ │   the JWT rotated per-request or per-session…  │ │
║ ├────────────────────────────────────────────────┤ │
║ │ ● WORKING           [main] fable       12s     │ │
║ │   ⚙ subagents running                          │ │
║ ├────────────────────────────────────────────────┤ │
║ │ ‹ +2 more sessions ›                           │ │
║ └────────────────────────────────────────────────┘ ║
║ ┌────────────────────────────────────────────────┐ ║
║ │ ■  ConfidAI-api                                │ │
║ │    fix/webhook-retries   ↓12                   │ │
║ ├────────────────────────────────────────────────┤ │
║ │ ■ BLOCKED           [acct2] sonnet     6m      │ │
║ │ ⧗ waiting on: Bash, Edit                       │ │
║ │ ⓘ inferred — no permission hook installed      │ │
║ └────────────────────────────────────────────────┘ ║
╟─ YOUR TURN ────────────────────────────────────────╢
║ ┌────────────────────────────────────────────────┐ ║
║ │ ◆  ConfidAi6                                   │ │
║ │    docs/play-beta-approved                     │ │
║ ├────────────────────────────────────────────────┤ │
║ │ ◆ YOUR TURN         [work] opus       41m      │ │
║ │ Draft the Play Store beta notes                │ │
║ └────────────────────────────────────────────────┘ ║
╟─ WORKING (3) ────────────────────────────────────⌄ ╢  collapsed
╟─ WAITING ON LIMITS (1) ──────────────────────────⌃ ╢
║ ┌────────────────────────────────────────────────┐ ║
║ │ ⛔  ConfidAI3                    main    Δ3     │ │
║ ├────────────────────────────────────────────────┤ │
║ │ ⛔ LIMIT · Weekly   [acct8]           41m      │ │
║ │    resets 14:32 ·               2h 38m        │ │  ← ticks, fixed width
║ │    ⏱ auto-resume armed for 14:33               │ │
║ └────────────────────────────────────────────────┘ ║
╟─ FREE (4) ───────────────────────────────────────⌄ ╢
╟─ OTHER AGENTS (1) ───────────────────────────────⌄ ╢
╠════════════════════════════════════════════════════╣
║  ⟳ live                       Board⇄Branches   ＋  ║
╠════════════════════════════════════════════════════╣
║   ⌗ Fleet      ⌁ Activity   ◔ Limits   ⧉ Server    ║
╚════════════════════════════════════════════════════╝
```

### 3.1.1 Structure — one level of Section, and why

SwiftUI `List` does not support nested sections. "A section per worktree under sticky severity headers" is not expressible, and reaching for `LazyVStack` to get two levels loses `.swipeActions` — on which the entire gesture model depends.

> **One `Section` per availability group. Worktrees are visually-grouped runs of rows inside a section.**

```swift
List {
  ForEach(model.groups) { group in            // needs_you, your_turn, busy, limited, free, other
    Section(group.title) {
      ForEach(group.cards) { card in
        WorktreeIdentityRow(card)
          .listRowSeparator(.hidden, edges: .bottom)
          .listRowBackground(CardSurface(position: .top, card: card))
        ForEach(card.visibleSessions) { s in
          SessionRow(s).listRowBackground(CardSurface(position: .middle, card: card))
        }
        if card.hiddenSessionCount > 0 { MoreSessionsRow(card) }
      }
    }
  }
}
.listStyle(.insetGrouped)
```

`CardSurface` draws the rounded card silhouette with radii varying by position. The only thing lost versus true nesting is that the card's corners are faked; `List`, `.swipeActions`, Dynamic Type and VoiceOver are all kept.

### 3.1.2 The section key — one server field

Sections come from a **five-valued `availability`** shipped by the server, which maps 1:1 onto both the sections and the badge. Order *within* a section comes from the server's `order` array, applied verbatim.

> ⚠ **This is a required change to `API.md` §10.2, not a description of it.** The contract currently ships the legacy four (`free` · `attention` · `waiting` · `busy`), where `attention` conflates `needs_input`/`blocked` with `waiting` — the exact defect this section exists to fix — and `waiting` means *limit-parked*, which collides with the session status of the same name. Tracked in `API.md` §0.2. Until it lands, the client would have to re-derive the split from session statuses, which violates principle 3 (no client-side severity); **ship the server change first.**

| `availability` | means | section | glyph | badge word |
|---|---|---|---|---|
| `needs_you` | any `needs_input` or `blocked` | NEEDS YOU | ▲ / ■ | NEEDS YOU |
| `your_turn` | any `waiting`, none working | YOUR TURN | ◆ | YOUR TURN |
| `busy` | any `working` | WORKING | ● | BUSY |
| `limited` | any non-handed-off `limit`, none working | WAITING ON LIMITS | ⛔ | WAITING |
| `free` | no live proc, nothing working | FREE | ◇ | FREE |

Section order is `NEEDS YOU · YOUR TURN · WORKING · WAITING ON LIMITS · FREE · OTHER AGENTS`, matching the server's severity ranking. `WORKING`, `FREE` and `OTHER AGENTS` are collapsed by default with counts; collapse state persists.

Why five and not the desktop's four: the desktop collapses `needs_input`, `blocked` and `waiting` into one `attention` bucket, so a `waiting`-only card renders an accent NEEDS-YOU badge while the headline above says "all clear". Splitting `attention`, and adding `free` and `limited`, fixes the sections, the badges and the tier/badge disagreement in one server-side change.

### 3.1.3 The triage headline

**The whole headline counts worktrees, not sessions,** because that is what the user reasons about on a phone. The server ships it precomputed; the client does no arithmetic.

```json
"counts": {
  "sessions": {"working":3,"needs_input":1,"blocked":1,"waiting":2,"limit":1,"ended":4},
  "cards":    {"needs_you":2,"your_turn":1,"busy":3,"limited":1,"free":4}
}
```

- **Line 1** — `▲ 2 need you`, 34 pt semibold, accent. Zero → `● all clear` (green), or `◇ nothing running` (cyan) when no live processes exist at all.
- **Line 2** — 13 pt muted: `3 busy · 1 limited · 4 free · 1 idle`. Tap ▸ **Counts**, the desktop's five tiles verbatim at session level, including `next reset in …` and `⏱ N armed`. Nothing is lost; it is just not in the way.
- **Line 3** — conditional, yellow, from the server's `signals` block: `⚠ process table not read for 40s — statuses may be stale`, `⚠ limits unavailable — parked agents may look idle`. Not a cold-start warning (the collector fetches limits independently of any client) but a *failure* warning, because `⛔ LIMIT` is unreachable without `cclimits`.

**The headline never scrolls away.** At `.large` type, nav chrome (~110 pt) + tab bar and accessory (~130 pt) + one attention card (~200 pt) already fills a 6.1" viewport. It is a `.safeAreaInset(edge: .top)`, full-size at scroll offset 0, collapsing to a single 28 pt line (`▲ 2 need you · 3 busy · 1 limited`) on scroll. The large nav title is dropped for an inline title, recovering ~50 pt.

### 3.1.4 Rows

**Identity row.** Availability glyph + name (17 pt semibold, **middle**-truncated — worktree names share long prefixes, so head- and tail-truncation both destroy identity). Line 2: `branch  Δdirty  ↑ahead ↓behind`. The `↑↓` pair is **omitted entirely when `git.ahead` is null** — 5 of 9 live worktrees have no upstream, and `↑null` is not a thing. Commit hash and subject live in Worktree Detail only: `git.commit.subject` arrives untruncated (107 chars observed live) and will blow up a row.

Trailing accessories — `no live terminal`, `⌖ 2` loose terminals, `✕ close pending` — are **decorative, never tappable**. Three overlapping targets in a row that also pushes on tap and carries a swipe action cannot all clear 44 pt. Their information is in Worktree Detail and long-press.

**Session rows.** Max **2** rendered inline (the server returns up to `max_sessions = 6`, pre-sorted so the most actionable is first). Row 1 full, row 2 one-line, then `‹ +N more sessions ›` ▸ Worktree Detail. At `.accessibility1`+: **1** inline session.

Row 1 anatomy, in the desktop's exact order so a user of both never re-learns:

```
▲ NEEDS ANSWER      [work] opus       2m 04s   ← status · account · model · age
Rotate refresh tokens without breaking…        ← topic, 1 line
→ the JWT one, and keep the old key 24h        ← last_user, 1 line, cyan
⏎ I can take either approach — do you want     ← last_assistant, 2 lines
   the JWT rotated per-request or per-sess…
⚙ subagents running                            ← subagent tag
↳ continued on [spare] — nothing to do         ← handed_to
⧗ waiting on: Bash, Edit                       ← pending_tools when not working
ⓘ inferred — no permission hook installed      ← confidence line
```

The subagent tag takes the first match, exactly as the desktop: `subagents_active` → `⚙ subagents running`; else `pending_workflows` → `⚙ awaiting N workflow(s)`; else `pending_bg_agents` → `⚙ awaiting N background agent(s)`; else `tool_running` → `⚙ running: {bg_shell ? "background shell" : pending_tools.first ?? "tool"}`. On the wire these are members of `session.flags` (`API.md` §10.3), not separate boolean keys.

> ⚠ **Three of those four text lines are not on the board payload.** `API.md` §9.3 ships a single **`headline`** (80 chars, `last_assistant or topic`); `topic` (140), `last_user` (140), `last_assistant` (240) and `subagent_said` (240) live only on the worktree-detail endpoint, deliberately, because they are ~48 % of the payload. `ARCHITECTURE.md` §7.2 argues the opposite ("no list/detail split — under a delta protocol they are stable and cost nothing after the first snapshot"), and that argument is the stronger one *for a streaming client*: the fields change only when the agent speaks, so the delta cost is zero and the split buys nothing but a partially-loaded-session bug class.
>
> **Resolution required before §3.1 is built.** Either (a) `API.md` §9.3 carries the four fields on the board and drops `headline`, and this wireframe stands; or (b) `headline` stays and the row collapses to **status line + `headline` (2 lines) + tags**, with `topic` / `last_user` / `subagent_said` appearing only in Worktree Detail and Chat. Do not build (a)'s layout against (b)'s payload — it degrades to three blank lines per row.

**The confidence line is new and it is the most valuable thing the phone gains.** It needs `confidence`, `why`, `evidence_source` and `provisional` per session — **none of which `API.md` defines** (§0.2). `liveness` is covered, but as `status: "unknown"` (§10.1) rather than a separate field. Treat this block as a server change to be specified, not as available data:

- `provisional: true` → the row dims 40 % and is **excluded from the headline count**.
- `confidence: "inferred"` → a muted `ⓘ` line carrying `why`. Tap ▾ Why sheet.
- `status: "unknown"` → renders as `? UNKNOWN — process table unreadable`, **never** as `○ ENDED`. Today an `lsof` failure silently manufactures FREE, and FREE gates dispatch. This one is already in the contract and should ship regardless of the rest.

**Age is a client-side animation.** The wire carries **`activity_at`** as an absolute epoch (`API.md` §9.3), never `age_s`. The phone renders elapsed time from `serverNow()` (§3.13) on a 1 s ticker that mutates **text only, in a fixed-width monospaced slot sized for the longest form (`2d 03h`)**. Card contents are therefore stable across time — nothing re-renders on the clock alone, and **rows never change height or width as time passes**. A metadata line re-wrapping from `2h38m` to `59m` under a thumb is a mis-tap.

**Limit rows** carry an absolute reset (`resets 14:32`) plus a ticking countdown, plus the auto-resume chip when a schedule exists. When `limit.resets_at` is null — the transcript-regex fallback, a real and common state — the row reads `⛔ LIMIT · reset time unknown` and the chip reads `⏱ pick a time`.

### 3.1.5 States

| condition | rendering |
|---|---|
| **first load ever** | 3 skeleton sections, breathing (static grey under Reduce Motion). Never a spinner over blank. |
| **any subsequent load** | last-good state stays; the accessory carries connection state. Skeletons never replace live data. |
| **zero worktrees** | `◇ no worktrees found` · *"orchestra watches git repos under `/Users/achill/Downloads`, filtered by `/confid/i`. Nothing matched."* · ‹Show my config› ‹Manual: setup›. Paths come from `/api/hello`'s `config` block — the desktop renders a literally blank grid here. |
| **worktrees, no sessions** | `○ nothing has run in the last 48h` + ‹New mission› |
| **only ended sessions in a card** | `{n} ended session(s) hidden` + inline ‹Show› |
| **no free worktrees** | `FREE (0) — everything busy` |
| **`other_procs` empty** | section omitted entirely |
| **stale / very stale / offline / asleep** | §3.12, §3.14 |

## 3.2 Worktree Detail

```
╔════════════════════════════════════════════════════╗
║ ‹ Fleet          ConfidAI-auth                ⋯    ║
╠════════════════════════════════════════════════════╣
║  ▲ NEEDS YOU                                       ║
║  feat/auth-rotation                                ║
║  Δ7 uncommitted · ↑3 ahead · ↓0 behind             ║
║  a1b2c3d4 · 12m ago                                ║
║  fix(auth): rotate refresh tokens on reuse         ║
║  /Users/achill/Downloads/ConfidAI-auth         ⧉   ║
╟─ SESSIONS (4) ─────────────────────────────────────╢
║  ▲ NEEDS ANSWER    [work] opus · 2m             ›  ║
║  ● WORKING         [main] fable · 12s           ›  ║
║  ◆ YOUR TURN       [acct2] haiku · 41m          ›  ║
║  ○ ENDED           [work] opus · 3h12m          ›  ║
║  showing 4 of 4                                    ║
╟─ TERMINALS (2) ────────────────────────────────────╢
║  ⌖ ttys004 · Terminal · [work]                     ║
║     up 12h43m · 2.1% cpu                           ║
║  ⌖ tmux -L fleet · [acct2]                         ║
║     mission-confidai-auth-091204                   ║
║     ⧉ send attach command to the Mac               ║
╟────────────────────────────────────────────────────╢
║  ┌──────────────────────────────────────────────┐  ║
║  │        ✓   Finish this worktree…             │  ║  safeAreaInset,
║  └──────────────────────────────────────────────┘  ║  ≥16pt above
║        opens a confirmation — never acts           ║  home indicator
╚════════════════════════════════════════════════════╝
```

- `showing 4 of 4` / `showing 6 of 9` comes from a server `session_count` field. Today truncation at `max_sessions` is silent.
- `etime` is a raw `ps` string in three shapes (`15:02`, `12:43:46`, `2-03:14:22`). Parse to `up 12h43m`; fall back to verbatim.
- What makes the pinned Finish footer safe is not that a thumb cannot reach it — it is exactly where a scrolling thumb rests — but that **it opens a sheet and never actuates**, and the caption says so.
- `⋯`: *New mission here* · *Branch detail* · *Copy path* · *Show ended sessions* · **On studio-mac** ▸ *Send attach command to the Mac*, *Open a terminal there*.

## 3.3 Chat (Session)

The most important screen in the app.

```
╔════════════════════════════════════════════════════╗
║ ‹  ConfidAI-auth · [work]                      ⋯   ║
║    Rotate refresh tokens without breaking exis…    ║
╠════════════════════════════════════════════════════╣
║ ▲ NEEDS ANSWER · asked 2m 04s ago              ⌄   ║  tap → Why
╟────────────────────────────────────────────────────╢
║   — earliest of 40 loaded turns —                  ║
║                                                    ║
║                        ┌─────────────────────────┐ ║
║                        │ ❯ rotate the refresh    │ ║
║                        │   tokens on reuse and…  │ ║
║                        └───────────────────── ✓✓┘ ║
║  ┌──────────────────────────────────────┐          ║
║  │ ⏎ I'll add a rotation table and a    │          ║
║  │   grace window. Two options: per-    │          ║
║  │   request or per-session rotation…   │          ║
║  │                        ‹show full›   │          ║
║  └──────────────────────────────────────┘          ║
║                        ┌─────────────────────────┐ ║
║                        │ ❯ per-request, keep the │ ║
║                        │   old key 24h           │ ║
║                        └──────────────── ✓ queued┘ ║
╟────────────────────────────────────────────────────╢
║ ┌──────────────────────────────────────┐  ┌──────┐ ║
║ │ and add a test for the grace window  │  │  ↑   │ ║
║ └──────────────────────────────────────┘  └──────┘ ║
╚════════════════════════════════════════════════════╝
```

### 3.3.1 There is no pid, and no target picker above the keyboard

The desktop's default — *"the session's own pid if reachable, else the first reachable proc"* — means typing into a different session's terminal. That is precisely what the server's own `fire_resume` refuses to do, on the grounds that an unattended message at the wrong agent is an injected instruction. A phone makes it worse: a target-changing control directly above the text field sits in the highest-traffic thumb zone in the app, where an upward overshoot retargets the message.

**`/api/send` takes `{account, sid}`, never `pid`.**

- The client never sees or sends a pid. Addressing is durable identity; the server resolves it to a terminal at send time, immediately before typing, inside the same lock as the pairing.
- **The default target is this session's own terminal, or nothing.** If the session has no reachable terminal, the composer is replaced by read-only. Borrowing another agent's terminal is not a default and is not reachable from the composer — it lives in `⋯ → Send to another agent in this worktree…`, presents its own sheet, and each row reads `[acct2]'s terminal — this session has none; your message goes to a different agent`.
- **`pid_certain` is not what it looks like** and is replaced. The server sets it to `proc.account == session.account`, then pairs *within* an account by freshness order. Two agents on one account in one worktree therefore both report `pid_certain: true` while the actual pairing is a coin flip that re-flips whenever their relative mtimes swap. The server ships a third state:

```json
"terminal": {"attribution": "certain" | "ambiguous" | "guess" | "none",
             "why": "2 sessions and 2 processes share account [work] in this worktree"}
```

`ambiguous` renders the same dashed treatment and caption as `guess`. This is why the server-side `expect_sid` assertion is a ship blocker: no client-side check can disambiguate what the server itself cannot.

### 3.3.2 Send, and a receipt that is actually a receipt

A client-side `✓✓` — diffing the next `/api/chat` poll against the pending bubble — has at least five systematic false-negative paths, each of which would fire a "your message never arrived" warning on a message that arrived perfectly:

1. Claude Code **queues** a message typed mid-turn and submits it at end of turn — and Reply is offered on `WORKING` sessions, so the transcript entry can be minutes away.
2. The server's `_real_prompt` filter drops anything starting `/`, anything starting `Caveat:`, and machine text. `/exit`, `/compact`, `/model opus` can **never** appear.
3. `_clean(t, 900)` truncates at 899 chars + `…`, so any reply over 900 chars can never match exactly.
4. `read_chat` returns the last 40 turns with no ids; a verbose agent turn can push the user's own message out of the window before the next poll.
5. Repeated identical text (`continue`) matches a *previous* occurrence instantly and falsely.

**The receipt is server-proven.** `/api/send` routes through the server's existing `deliver_text()` (atomic `set-buffer` + `paste-buffer -p -d` bracketed paste, avoiding the `[Pasted text #N]` chip failure) and `_proven_in_transcript()` (reads past a recorded byte offset for a `user` entry containing the text). Both already exist in the file and are currently unused on this path.

```jsonc
// POST /api/send
{ "account": "work", "sid": "9b8ef2d1-…", "text": "per-request, keep old key 24h",
  "expect_sid": "9b8ef2d1-…", "idempotency_key": "0f2c…" }

// 202, then phases arrive as intent frames on /api/events
{ "intent_id": "int-7f3a", "phase": "typed" }
{ "intent_id": "int-7f3a", "phase": "delivered", "at": 1784636700.4 }
{ "intent_id": "int-7f3a", "phase": "failed",
  "message": "tmux send-keys failed", "remedy": "tmux -L fleet attach -t mission-…" }
```

| bubble state | glyph | meaning |
|---|---|---|
| sending | `◌` | request in flight |
| typed | `✓` | keystrokes accepted (`rc == 0`) — the only thing today's `ok:true` ever meant |
| **delivered** | `✓✓` | **server-proven**: the text appears as a `user` entry past the recorded offset |
| queued | `✓ queued` | typed while the session was `working`; the CLI submits at end of turn. **No escalation timer.** |
| failed | `⚠` | tap → the server's prose verbatim + ‹Copy text› ‹Try again› |

Escalation attaches to the server's `delivered: false`, and is suppressed for the queued case and for `/`-prefixed text:

> `⚠ typed, but it never reached the conversation — it may have landed in a paste chip.` ‹Send attach command to the Mac›

**The client's leading-dash guard is deleted.** `tmux send-keys -l "-n foo"` exits 1 with `unknown flag`. With `deliver_text`'s `set-buffer` path plus a `--` sentinel server-side, a message beginning with `-` works. Shipping a UI restriction to work around a two-character server bug is the wrong trade.

**Newline honesty.** The server collapses `\s*\n\s*` to a single space. The composer collapses it **as you type** — Return inserts a space — with a one-time footnote *"newlines become spaces on the way to the terminal"*. WYSIWYG or nothing.

**Freshness gate.** With identity addressing the real guard is server-side. The client's honest job is small:

- if the last frame is more than **30 s** behind `serverNow()`, request a resync first (silent, ~150 ms on a warm stream);
- if the session is no longer present in the snapshot, block with `⚠ this session is no longer on the board`;
- otherwise send.

**No confirmation dialog on reply, ever.** It is the primary job; a dialog here kills the app.

**Draft persistence is per-sid**, to the App Group, on a 500 ms debounce. It survives app kill, connection loss and session navigation. Offline, the send button reads `can't send while offline — your text is saved`.

### 3.3.3 Transcript

`GET /api/chat?account=&sid=` returns the last 40 turns, chronological, each `_clean`ed to 900 chars with **all newlines destroyed server-side**. Consequences designed for, not hidden:

- Top marker `— earliest of 40 loaded turns —`. No fake infinite scroll. **No `.refreshable` here**: the universal gesture at the top of a transcript is load-older, and pairing it with plain refresh is a lie; it also needs the keyboard dismissed first. Refresh comes from the stream. When the server grows a `before=` cursor, the top pull becomes load-older.
- A run-on 900-char paragraph is the norm. **Do not build a markdown renderer** — there is no structure left. Agent bubbles collapse at 6 lines with ‹show full›; text selection is enabled so a command can be copied out.
- A bubble ending in `…` gets a `truncated by the server` footnote, so the user does not think the agent stopped mid-sentence.
- `ts` is ISO-8601 `Z`. Date separators between days. Long-press → *Copy* / *Copy with timestamp* / *Quote in reply*.
- **Auto-follow only when the last message is fully visible.** The desktop force-scrolls on every 5 s poll; that is dropped.
- Chat content is not in the snapshot, so `/api/chat` polls at 5 s while foreground with keyboard up or a send in the last 2 minutes, 15 s otherwise, **never backgrounded**. When chat turns join the digest stream this poll is deleted.

**Read-only state:**

```
╟────────────────────────────────────────────────────╢
║  ⌨︎  read-only                                      ║
║  This session has no terminal that can be typed    ║
║  into. Cursor and VS Code terminals can't be       ║
║  scripted; an ended session has no terminal.       ║
║  ‹Why?›       ‹Send to another agent in this wt…›  ║
╚════════════════════════════════════════════════════╝
```

Errors render the server's error verbatim. `unknown account …` gets special copy — it is usually the un-decoded query-param bug rather than a genuinely unknown account.

## 3.4 Activity

```
╔════════════════════════════════════════════════════╗
║  Activity                                          ║
╠════════════════════════════════════════════════════╣
╟─ IN FLIGHT ────────────────────────────────────────╢
║ ⌁ mission → ConfidAI-ci          ③ booting      ›  ║
║   [acct2] · opus · xhigh · 8s ago                  ║
║   ▓▓▓▓▓▓▓▓░░░░░░░░░░  step 3 of 5                  ║
║ ✓ closeout → ConfidAI-auth       brief sent     ›  ║
╟─ ARMED (2) ────────────────────────────────────────╢
║ ⏱ ConfidAI3 · [acct8]           fires 14:33     ›  ║
║   in 2h 39m · re-armed 2×                          ║
║ ⏱ orbital-api · [work]          firing now…     ›  ║
║   cancelling now won't stop a resume in progress   ║
╟─ TODAY ────────────────────────────────────────────╢
║ ✓ 12:10  ConfidAI-ci · [acct2] · opus           ›  ║
║   ● live · effort xhigh                            ║
║   clean up the CI matrix and make the tmux tes…    ║
║ ⛔ 11:02  [work] hit its Weekly cap                 ║
║ ○ 09:14  ConfidAI-auth · closeout · haiku       ›  ║
╟─ EARLIER ──────────────────────────────────────────╢
║ ○ 07-20 18:41  ConfidAi5 · [main] · sonnet      ›  ║
╚════════════════════════════════════════════════════╝
```

**Sources:** server **intents** (the durable record of every in-flight and recent mutation, streamed as intent frames), armed schedules, `GET /api/dispatchlog`, plus client-side events the server does not record (connection losses) so the feed reads as a history of *your* actions.

In-flight jobs are **server-owned**. `Intent` carries `{id, kind, target, phase, created_at, expires_at, payload, result}` with phases `armed | running | brief_sent | closing | done | failed | interrupted`. This survives app kill, backgrounding, and — unlike the in-memory `_jobs` dict, capped at 20 and reset on restart — a server restart.

- `ts` is a **timezone-naive local string**. Rendered verbatim with a `(server local time)` footnote in detail, until the server adds an epoch field.
- Row ▸ **Dispatch Detail**: full untruncated `mission_original` (the only place the user can read what they actually asked for), `kickoff` in a `DisclosureGroup`, the attach command with ‹Send to the Mac›, ‹Chat with this agent› when `alive`, `closeout: true` badged.
- `/api/dispatchlog` is ~36 KB for 25 entries and is read whole-file server-side. **Fetched only when this tab is visible.** Never on the board's path.
- Empty: `nothing dispatched yet` + ‹New mission›.

**Demo safety.** `--demo` does not sandbox this endpoint or `/api/chat`, so real mission prose and real transcripts leak. The app never points at `--demo`; "look around first" (§4.9) loads a **bundled sample fleet**.

## 3.5 Mission Composer

`.sheet` at `.large`.

```
╔════════════════════════════════════════════════════╗
║ Cancel            New mission             Launch   ║  disabled until
╠════════════════════════════════════════════════════╣  model + effort
║ ┌────────────────────────────────────────────────┐ ║
║ │ Clean up the CI matrix. Drop py3.11 from the   │ ║
║ │ test grid, add 3.14, and make the tmux tests   │ ║
║ │ skip instead of fail on a loaded runner.       │ ║
║ │                                            ▌   │ ║
║ │                                                │ ║
║ └────────────────────────────────────────────────┘ ║
║  draft saved                            1,204 ch   ║
╟────────────────────────────────────────────────────╢
║   Worktree    Auto → ConfidAI-ci               ⌄   ║
║               cleanest free (Δ0)                   ║
║   Account     Auto → [acct2] 88% left          ⌄   ║
║   Model       — pick one —                     ⌄   ║
║   Effort      — pick one —                     ⌄   ║
║                                                    ║
║   placement is deterministic. model and effort     ║
║   are your call — nothing guesses difficulty   ⓘ   ║
╚════════════════════════════════════════════════════╝
```

- Editor focused on open, ~55 % of height; pickers pinned below via `.safeAreaInset(edge: .bottom)`.
- **The auto-preview must not lie.** It mirrors the server's `_pick_defaults`: free worktrees ascending by `git.dirty`, first wins. Two divergences a naive mirror misses:
  - **Ties.** Equal-`dirty` worktrees resolve by the server's card order. The client uses the `order` array it already receives; it does not re-derive.
  - **`exclude_accounts`.** Exposed by no endpoint today. Wherever it is set — and it is set on the author's machine, to `["main"]` — the account preview names an account the server will never pick, and every dispatch shows a "picked X" correction. `/api/hello`'s `config` block exposes it, with `reserve_percent` and its `"*"` default.
- Account menu lists `ok` accounts as `{fb_label} · {round(headroom)}% left`, sorted descending, suffixed `🔒 reserve` / `⛔ exhausted`. **Join on `fb_label`, never `slug`** — they differ (`slug: "default"` vs `fb_label: "main"`).
- Model and Effort have **no default**; Launch stays disabled until both are set, mirroring the server's own refusal, so the app never sends a request it knows will bounce. The disabled reason is shown inline, not just as a dimmed button.
- Draft persists to the App Group on a 500 ms debounce; survives app kill and failed launch.
- **Share extension** hosts a minimal composer *inside the extension* — text view, four pickers, Launch — reading credentials from the shared Keychain access group. (`NSExtensionContext.open(_:)` to launch the host app is unsupported/unreliable, so "opens the app prefilled" does not work.) If the tailnet is unreachable it writes a draft and completes with *"saved as a draft in orchestra"*.
- **`orchestra://mission?text=` is invocable by any web page.** The composer **never auto-launches**; Launch always requires an explicit tap, and a URL-opened composer shows `from a link` above the text.

## 3.6 Limits

```
╔════════════════════════════════════════════════════╗
║  Limits                          fetched 4m ago    ║
╠════════════════════════════════════════════════════╣
║ ┌────────────────────────────────────────────────┐ ║
║ │  acct2              MAX      ◇ MOST HEADROOM   │ ║
║ │  ████████████████████████░░░░░░  88% left      │ ║
║ │                          ▏reserve 20%          │ ║
║ │  healthy                                    ›  │ ║
║ └────────────────────────────────────────────────┘ ║
║ ┌────────────────────────────────────────────────┐ ║
║ │  work               MAX      ⛔ EXHAUSTED       │ ║
║ │  ▨▨░░░░░░░░░░░░░░░░░░░░░░░░░░░   4% left       │ ║  hatched
║ │  Weekly · resets 14:32 ·           2h 38m   ›  │ ║
║ └────────────────────────────────────────────────┘ ║
║ ┌────────────────────────────────────────────────┐ ║
║ │  main               PRO      🔒 RESERVE         │ ║
║ │  ███████░░░░░░░░░░░░░░░░░░░░░░  18% left       │ ║
║ │  below its 20% reserve — auto-dispatch won't   │ ║
║ │  use it; you still can                      ›  │ ║
║ └────────────────────────────────────────────────┘ ║
╠════════════════════════════════════════════════════╣
║  ⟳ live                    ↻ refresh all accounts  ║
╚════════════════════════════════════════════════════╝
```

**Refresh is a whole-fleet operation and is labelled as one.** There is no per-account refresh: the server's refresh shells out to `cclimits --json --refresh` for *all* accounts with a 90 s timeout, mutating one global dict. Hiding a 90-second global subprocess behind a swipe — the cheapest gesture in the app — would be a trap. One `↻ refresh all accounts` in the accessory, disabled while in flight, labelled `up to a minute`, `timeoutIntervalForRequest = 120`, never auto-retried.

Three server-side problems fixed upstream so the phone does not inherit them: `_limits` has no lock and the resume daemon refreshes on its own schedule (single-flight it); failures are never cached, so a machine without `cclimits` re-runs a 30 s blocking subprocess on *every* call (negative-cache 60 s); and with the collector owning the limits lane, the phone never blocks on `cclimits` at all — **which deletes the "mandatory prime on foreground" rule** that would otherwise hang app launch for 30 s.

## 3.7 Account Detail

```
╔════════════════════════════════════════════════════╗
║ ‹ Limits              work                         ║
╠════════════════════════════════════════════════════╣
║  MAX · /Users/achill/.claude-work                  ║
║  4% left overall                                   ║
╟─ LIMITS ───────────────────────────────────────────╢
║  Session                                  21% used ║
║  ██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  ║
║  resets in 3h 12m                                  ║
║                                                    ║
║  Weekly                              ⛔  96% used  ║
║  ▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨░░░░░  ║
║  exhausted — resets 14:32 ·            2h 38m      ║
║                                                    ║
║  Fable       [model cap]                 100% used ║
║  ▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨▨  ║
║  exhausted — only sessions running Fable are       ║
║  blocked; the account itself is not                ║
╟─ RESERVE BUFFER ───────────────────────────────────╢
║       ─  ●────────────────────────────  ＋         ║
║                       20 %                         ║
║  auto-dispatch stops at 80% of the weekly limit.   ║
║  you can still launch here by hand.                ║
╟─ SESSIONS ON THIS ACCOUNT ─────────────────────────╢
║  ⛔ ConfidAI3 · fable-5 · 41m ago               ›  ║
║  ○ ConfidAi5 · opus-4-8 · 3h ago                ›  ║
╚════════════════════════════════════════════════════╝
```

- **Every limit that has a reset gets a countdown.** The desktop hides them below 50 % used unless exhausted, so a healthy account shows no reset information at all. On a phone "when does this free up" is the whole question.
- `model_scoped` limits get a full-size `model cap` pill and a sentence, not a 7 pt superscript. A maxed model cap does **not** block the account — collapsing that distinction is an explicit anti-goal in the server.
- **Reserve control**: slider snapped to 5 % over 0–95, with `−`/`+` and a large readout. Posts on release, debounced 600 ms, optimistic with rollback and an **inline** error — never an alert, which is what the desktop does. Send an `Int`.
- The caption states the real semantics, including the one that silently lies today:
  - key present → `20% — auto-dispatch stops at 80% of the weekly limit`
  - key absent → `20% — inherited from the default (*)`
  - set to 0 → `0% — this removes the override; the default (20%) applies again`

  That last is the server's actual behaviour: setting 0 *pops the key*, and the `"*"` wildcard then applies.
- **Whole-page error** (`available: false`): the server's error, the `cclimits` install hint, and the consequence — *"without this, orchestra can't tell a limit-parked agent from an idle one."*

## 3.8 Server

```
╔════════════════════════════════════════════════════╗
║  Server                                            ║
╠════════════════════════════════════════════════════╣
║  ● studio-mac                                      ║
║    studio-mac.tailXXXX.ts.net:4242 · achill        ║
║    stream live · 240 ms · v41207                   ║
║    orchestra 0.10.0 · up 2d 4h · awake           ›  ║
╟─ NOTIFICATIONS ────────────────────────────────────╢
║   ● Delivering · last push 14:02                   ║
║   Sink                    APNs (studio-mac key) ›  ║
║   This iPhone             registered 3d ago     ›  ║
║   Message previews              Fetch on device ›  ║
║   ────────────────────────────────────────────     ║
║   Needs an answer                             ⬤    ║
║   Blocked on a tool                           ⬤    ║
║   Your turn (idle > 10 min)                   ○    ║
║   Limit hit                                   ⬤    ║
║   Mission launched / failed                   ⬤    ║
║   Auto-resume fired                           ⬤    ║
║   Worktree freed                              ○    ║
║   Quiet hours          22:00–08:00 (device tz)  ›  ║
║   Send a test notification                      ›  ║
╟─ MACHINES ─────────────────────────────────────────╢
║   ● studio-mac                          active  ›  ║
║   ○ mbp-16                         unreachable  ›  ║
║   ＋ Pair another Mac                              ║
╟─ APPEARANCE ───────────────────────────────────────╢
║   Theme                              Night ▾       ║
║   High-contrast statuses                      ○    ║
║   Haptics                                     ⬤    ║
╟─ HELP ─────────────────────────────────────────────╢
║   Manual                                        ›  ║
║   Status legend                                 ›  ║
║   Troubleshoot the connection                   ›  ║
╚════════════════════════════════════════════════════╝
```

`awake` reflects the server's wake detection (§3.14). `Manual` renders `guide.html`'s five sections natively (`ol.steps` → numbered cards, `table.vocab` → a grouped list, `.callout` → a bordered box, `<details>` → `DisclosureGroup`). `Status legend` is the vocab table, also reachable from any status chip.

## 3.9 Sheet inventory

| Sheet | Detent | Contents |
|---|---|---|
| **Why** | `.medium` | §3.10 |
| **Finish** | `.medium` → `.large` on outcome | §4.4 |
| **Auto-resume** | `.medium`, `.large` with the exact-time picker | §4.5 |
| **Launch confirmation** | `.height(340)` | deliberately *not* `.medium` — §7.3 |
| **Insufficient headroom** | `.medium` | §4.3 |
| **Send to another agent** | `.medium` | §3.3.1 |
| **Session info** | `.large` | sid (copyable), cwd, subdir, branch (flagged when ≠ card branch), model (raw string — **not an enum**, can be `""` → render `—`), pending tools/workflows/bg agents, terminal attribution + `why`, `evidence_at` as an absolute local time, `evidence_source`, ‹Send attach command to the Mac›, ‹Open a terminal there› |
| **Counts** | pushed screen | §3.11 |
| **Filters** | `.medium` | Show ended · Hide FREE · Only needs-you |

## 3.10 The Why sheet

```
╔════════════════════════════════════════════════════╗
║                      ▁▁▁▁▁                         ║
║  ■  BLOCKED                                        ║
║                                                    ║
║  The agent is stuck on an unresolved tool call —   ║
║  usually a permission prompt waiting at its        ║
║  terminal.                                         ║
║                                                    ║
║  Waiting on:  Bash, Edit                           ║
║                                                    ║
║  ⓘ inferred — no permission hook is installed, so  ║
║    orchestra is reading this from the transcript:   ║
║    two tool calls opened 6 minutes ago and         ║
║    neither has a result.                           ║
║                                                    ║
║  ┌──────────────────────────────────────────────┐  ║
║  │              ✉  Open the conversation        │  ║
║  └──────────────────────────────────────────────┘  ║
║  ┌──────────────────────────────────────────────┐  ║
║  │       ⧉  Send attach command to the Mac      │  ║
║  └──────────────────────────────────────────────┘  ║
║                                                    ║
║  Read more: the status vocabulary  ›               ║
╚════════════════════════════════════════════════════╝
```

One authored answer per status, plus special cases for `handed_to`, null-reset limits, `ambiguous` attribution, `bg_shell`, `pending_workflows`, `provisional`, `liveness: unknown`, `closeout_sent`. **Every status word anywhere in the app is tappable and lands here.** This is how `guide.html` survives losing its tab: instead of a manual read once, the manual arrives one tap from the thing that confused you.

## 3.11 Counts

```
╔════════════════════════════════════════════════════╗
║ ‹ Fleet          Counts                            ║
╠════════════════════════════════════════════════════╣
║  ● WORKING                                     3   ║
║    agents mid-turn                                 ║
║  ▲ NEED YOU                                    2   ║
║    1 question · 1 blocked                          ║
║  ◆ YOUR TURN                                   2   ║
║    finished a turn, idle at the prompt             ║
║  ⛔ WAITING ON LIMITS                           1   ║
║    next reset in 2h 38m · ⏱ 2 armed                ║
║  ◇ FREE WORKTREES                              4   ║
║    ConfidAi2, ConfidAi7, docs, scratch             ║
║  ⌁ LIVE AGENTS                                 7   ║
║    claude processes on studio-mac                  ║
╚════════════════════════════════════════════════════╝
```

Session-level, from `counts.sessions` in §3.1.3 (`needs_input 1 · blocked 1 · waiting 2 · working 3 · limit 1`). `FREE WORKTREES` and `LIVE AGENTS` are card- and process-level, as on the desktop.

**One deliberate divergence from the desktop's tiles.** The desktop's `▲ need you` tile is `needs_input + blocked + waiting` — it would read **4** here — and its sub-line ends `· N idle`. This screen splits `waiting` into its own `◆ YOUR TURN` row instead, because promoting `waiting` into "need you" is the precise defect §3.1.2, §8.4 and Appendix F exist to remove; a Counts screen that re-merged them would contradict every other surface in the app. Six rows, not five. Nothing is lost — it is one tap away instead of occupying the top third of the phone.

## 3.12 Connection states

Rendered in the bottom accessory, never as a full-screen takeover.

**The state machine is `API.md` §6.3; this table is its presentation.** Two rules from there are load-bearing and were got wrong in an earlier draft of this section:

1. **Liveness and recency are separate signals.** *Any* frame — including a 25 s heartbeat and a 5 s keepalive comment — proves the socket is alive. Only `at` (the collector's tick) proves the data is current. A threshold keyed on "last frame" alone at anything under the heartbeat period marks a **perfectly healthy idle fleet** as stale every 25 seconds, and then disables actuation on exactly the free worktree you wanted to dispatch into.
2. **Thresholds are derived from the server's advertised cadence, never hard-coded.** `hb` and `tick_s` arrive in `hello` and in every heartbeat and change with server load.

| state | trigger (`hb`, `tick_s` from the stream) | accessory | actuation |
|---|---|---|---|
| `connecting` | first frame pending | `◐ connecting…` + skeletons | blocked |
| `live` | frame within `hb × 1.6 + 5 s`, `at` within `3 × tick + 10 s`, `collector_ok` | `⟳ live` | **allowed** |
| `slow` | request >5 s in flight | `◐ slow link…` | allowed |
| `lagging` | frames fine, `at` behind, or one missed heartbeat | `◑ data from 42s ago` | **allowed — no functional change** |
| `collector_stuck` | `collector_ok: false`, or `at` behind by >90 s | `⚠ the collector is stuck  ↻` | blocked |
| `stale` | no frame for `hb × 3 + 10 s` | `◑ not live — reconnecting  ↻`, list dims to 55 % | **blocked**, with the reason printed on each control — never hidden (hiding reflows under the thumb) |
| `offline` | 3 failed reconnects, or `NWPath` unsatisfied | `◯ can't reach studio-mac  ‹Troubleshoot›` | blocked |
| `mac_asleep` | `NWPath` satisfied but connect refused, or `wake_gap > 120` | §3.14 | blocked |
| `unauthorized` | 401 | `⚠ this iPhone is no longer paired  ‹Re-pair›` | blocked |

**One offline message, not two.** URLSession does not reliably distinguish "no route" from "connection refused" — both commonly surface as `NSURLErrorCannotConnectToHost`, and over a tunnel it depends on whether Tailscale drops or rejects the packet. `NWPathMonitor` reports `.satisfied` whenever the tunnel is up, regardless of peer reachability. Confidently saying *"studio-mac is up but orchestra isn't running"* when the tailnet actually dropped sends the user to the wrong machine.

**The distinction lives in Troubleshoot**, which is a *sequence*:

```
╔════════════════════════════════════════════════════╗
║ ‹ Server      Troubleshoot                         ║
╠════════════════════════════════════════════════════╣
║  ✓  Tailscale is connected                         ║
║     100.84.12.7 · studio-mac reachable             ║
║  ✓  studio-mac.tailXXXX.ts.net resolves            ║
║  ✗  Port 4242 refused the connection               ║
║     orchestra isn't running on studio-mac.          ║
║                                                    ║
║     On your Mac:                                   ║
║     ┌────────────────────────────────────────────┐ ║
║     │  cd ~/Downloads/orchestr && ./start.sh     │ ║
║     └────────────────────────────────────────────┘ ║
║                              ‹Copy›                ║
║  —  /api/hello                        not reached  ║
║                                                    ║
║  ┌──────────────────────────────────────────────┐  ║
║  │                Run again                     │  ║
║  └──────────────────────────────────────────────┘  ║
╚════════════════════════════════════════════════════╝
```

Only the raw `NWConnection` probe surfaces `ECONNREFUSED` distinctly, and only in this deliberate sequence is it safe to name a cause.

**Swipe actions under `very stale`.** A `.swipeActions` button cannot be meaningfully disabled in SwiftUI, and omitting it changes the drawer's shape as connection state drifts — reintroducing the mutating-target problem §4.5 fixes. **The drawer shape is constant.** Every action in `very stale` opens a one-line sheet: `showing data from 4m ago` + ‹Refresh›.

## 3.13 Realtime and clock skew

**The stream is primary; polling is the fallback.** SSE was measured on the real `ThreadingHTTPServer`: 12 concurrent subscribers → 14 threads, 0.45–0.68 ms broadcast latency, an ordinary GET served in 21 ms while all streams were held, and full thread reclamation after rude disconnects.

**The frame contract is `API.md` §7 and §9.4 — read it there.** An earlier draft of this section sketched a card-level delta (`"worktrees":[…changed cards only…]`) with a bare integer cursor and a `transitions` array riding on the state frame. All three are superseded, and the card-level one was actively wrong: a changed card is ~1 KB, so a card-level delta ships a near-full payload wearing a `"delta"` label. What the client actually receives:

```
GET /api/v1/stream?since=9f2c1a04:4711&sub=<install id>
Authorization: Bearer orc1_…

event: hello
data: {"epoch":"9f2c1a04","seq":4711,"at":…,"dg":"a41f0c93","tick_s":10.0,"hb":25.0,
       "collector_ok":true,"wake_gap":0.0,"features":[…]}

event: delta
data: {"seq":4713,"at":…,"dg":"b7710e42","ops":[
        {"p":"w/3f9a2b1c7d04/s/9d4db7b2-…","f":"status","v":"needs_input"},
        {"p":"w/3f9a2b1c7d04","f":"card_rev","v":"1cc8e40b"},
        {"p":"order","v":["wt_9911aabb2233","wt_3f9a2b1c7d04"]}]}

event: hb       ← every 25 s, carries tick_s / hb / collector_ok / dg / wake_gap
event: resync   ← {"reason":"cursor_too_old"|"epoch_changed"|"digest_mismatch"|…}
:               ← bare keepalive comment every 5 s
```

| rule | why |
|---|---|
| cursor is the opaque `"<epoch>:<seq>"` — store it, echo it, compare for equality, **never parse it for time** | an epoch change means "everything you hold may be wrong"; a bare integer cannot express a restart |
| an `epoch` change or a `resync` frame → refetch a full snapshot | a partial history is incoherent; the backpressure path deliberately produces `resync` for a slow phone |
| ops are **field-addressed** (`{p,f,v,x}`), one level of descent per family | a card-level delta is not a delta |
| never accept a frame whose `at` regresses | out-of-order delivery must not un-do a newer truth |
| `dg` mismatch → resync | silent delta/full divergence is the one failure with no other symptom |
| **attention edges come from `GET /api/v1/events`**, not from the state frame | the event log is durable and replayable, so the same edge drives the foreground haptic, the push, and the badge reconcile — one source. The desktop's `attn > lastAttn` arithmetic is deleted, and replay can no longer ring. |
| `order` and `w/<wid>/order` applied verbatim | no second `severity()` in Swift; the `4.5` handed-off sort weight lives in exactly one place |
| at most one render per frame | `@Observable` batching |
| **no `mode=digest`** | there is no such parameter. Payload size is solved by the delta itself (a real 5 s window is ~131 B), not by a reduced mode. `low=1` exists, and it changes *cadence*, not shape. |

**The stream is dropped when the app backgrounds** and re-opened with `?since=<stored cursor>` on foreground, which replays the ring. Backgrounded, the phone's only input is push.

**Poll ladder (fallback only)** — used when the stream cannot be established, when `features[]` lacks `stream`, or after 3 consecutive stream failures. Every rung is a **conditional** `GET /api/v1/state?since=<cursor>` — an unchanged fleet costs a `304` with no body, so these intervals are wake counts, not payload counts. Fleet visible 5 s (or `&wait=25` long-poll where available); Fleet visible with no touch for 60 s, 20 s; other tab 20 s; Chat 5 s active / 15 s idle; Limits on appear + manual; **Branches on appear + pull-to-refresh only** (§5.11 — never on a timer from a phone; the endpoint is ~90 git subprocesses); Activity on appear + 15 s while an op is in flight; **backgrounded: none**. Single-flight per endpoint — cancel in-flight before reissuing, never stack.

**Clock skew.** Every timestamp on the wire is *server* wall-clock. A few seconds of skew shifts countdowns; a minute — a Mac that has been asleep, a phone that just changed timezone — makes any freshness threshold either permanently block or permanently pass.

```swift
// on every frame and every /api/hello
let sample = frame.generatedAt - Date().timeIntervalSince1970 + rtt/2
skew = skew.map { $0 * 0.8 + sample * 0.2 } ?? sample     // EWMA
func serverNow() -> TimeInterval { Date().timeIntervalSince1970 + skew }
```

Every server-derived instant renders through `serverNow()`. `Server → studio-mac` shows `clock offset +0.4 s` and **warns above ±30 s** — because that also means the auto-resume you armed will fire at a different wall time than the sheet promised.

## 3.14 The Mac being asleep

A closed lid, or an idle iMac past its Energy Saver timeout, stops `serve_forever()`, the collector, the resume loop, the agents, and all push emission. The premise "agent sessions keep running on the user's local Mac" is false by default on any laptop, and the app must say so rather than showing a frozen board.

1. **Prevention.** `./start.sh` runs under `caffeinate -dis`. Pairing verifies it: `⚠ studio-mac isn't caffeinated — closing the lid stops your agents`.
2. **Server-side wake handling.** A sweep gap larger than `max(30, 3×reconcile)` clears memos and hooks, forces a cold pass, and **suppresses push for one full reconcile cycle** — otherwise opening the lid delivers forty notifications about 3 a.m.
3. **Client-side.** A `generated_at` jump larger than the poll interval, or `/api/hello` reporting a wake:

```
╔════════════════════════════════════════════════════╗
║  ⟳ studio-mac was asleep for 9h — catching up  ⏳  ║
╚════════════════════════════════════════════════════╝
```

Actuation is blocked until the first post-wake frame. A resumed board is exactly as dangerous as a stale one.

4. **Drain honesty.** On wake the resume loop finds every overdue schedule and fires them **serially**, each potentially minutes. §4.5's queued/firing distinction keeps that legible instead of showing five simultaneous "firing now".

---

# 4. Flows

Every desktop capability, walked step by step, with what changed and why.

## 4.1 Triage — "who needs me"

1. Buzz, glance at the widget, or open the app.
2. The app renders the **App Group's cached snapshot instantly** — never a blank screen — opens the SSE stream, and requests `since=<lastV>`. A delta if the server's ring covers it, a snapshot otherwise.
3. The headline answers the question. The sections give the names.
4. One swipe or one tap acts.

**Limits are not primed.** The desktop must call `/api/limits` before the board is honest, because `collect_state` reads a cache only that endpoint populates. With the collector owning the limits lane, that dependency is gone — and with it the mandatory 30 s blocking fetch on every foreground.

### The hold rule

> **Order and membership are frozen from the first touch until an explicit apply.** Applies are: tapping the `⌗ N updates` pill, pull-to-refresh, tapping the active Fleet tab (scroll-to-top), returning to foreground, or the list being **at rest and fully off-screen**. There is no time-based auto-apply.
>
> **Content updates are frozen too, for any worktree whose row has an open swipe drawer.** A SwiftUI row whose content changes while its drawer is revealed dismisses the drawer — and with relative ages as the highest-churn field, a revealed action would slam shut under the thumb roughly every frame. The fixed-width age slot (§3.1.4) removes the churn; this removes the rest.
>
> **After any applied reorder, a 700 ms tap shield.** A tap on a row whose frame moved >4 pt in that window flashes the row and is consumed, with the accessory reading `⌗ the board moved — tap again`. (700 ms, matching `API.md` §5.5 and `ARCHITECTURE.md` §7.5; the desktop's is 600 ms and it does not have to survive scroll momentum.)

Why not a timer: lift-to-aim-to-tap is a 500–900 ms arc, so "apply after 400 ms of stillness" reorders precisely inside the mis-tap window the freeze exists to close. It would also make the `⌗ N updates` pill unreachable, since it could only appear during a continuous touch.

```swift
@Observable @MainActor final class Board {
  private(set) var rows: [Row] = []
  private var pending: Snapshot?
  var heldUpdates = 0
  var isTouching = false          // DragGesture(minimumDistance: 0) in .simultaneousGesture
  var isDecelerating = false      // onScrollPhaseChange
  var voFocusInside = false       // @AccessibilityFocusState<Row.ID?>
  var openDrawer: Row.ID?
  var shieldUntil: ContinuousClock.Instant?

  var mayApplyStructure: Bool { !isTouching && !isDecelerating && !voFocusInside }
}
```

Rows never animate out from under a finger. New rows animate in only when their section is visible and the list is at rest. Under Reduce Motion the reorder is a jump-cut plus a 300 ms highlight flash on moved rows.

**Changed from desktop.** The desktop holds re-sorts while the pointer is over the grid and shields clicks on cards that moved within 600 ms. Neither ports: iOS has no hover, so a tap fires `pointerenter` and `pointerleave` may never fire. Touch began/ended is a *cleaner* signal than hover ever was, so the mechanism survives in stronger form.

## 4.2 Read and reply

```
Board                      Chat                       Chat
┌──────────────────┐      ┌──────────────────┐       ┌──────────────────┐
│ ▲ NEEDS ANSWER   │      │ ▲ NEEDS ANSWER   │       │  ❯ per-request…  │
│ Rotate refresh…  │ ───► │ ⏎ I can take     │  ───► │            ✓✓    │
│ ⏎ I can take…    │ tap  │   either…        │ send  │                  │
└──────────────────┘      │ ┌──────────┐ ┌─┐ │       │ ⏎ Got it — I'll  │
                          │ │per-reque…│ │↑│ │       │   do per-request │
                          │ └──────────┘ └─┘ │       └──────────────────┘
                          └──────────────────┘
```

1. Tap the session row. **Chat** pushes.
2. The status strip states the situation. Tap it for **Why**.
3. Read. Long bubbles collapse at 6 lines with ‹show full›.
4. Type. Newlines collapse to spaces as you type.
5. Send. `◌` → `✓` → `✓✓`, or `✓ queued` when the agent is mid-turn.
6. Failure surfaces the server's own prose, which already contains the remedy.

**Changed from desktop.** The desktop's chat is a drawer over a live board with a 5 s full-`innerHTML` replacement and a force-scroll to bottom on every poll. Mobile: a real screen, auto-follow only when already at the bottom, a real receipt instead of `ok:true`, and no target picker.

## 4.3 Dispatch a mission

```
 ＋ accessory                Composer                  Confirmation
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  ⟳ live      ＋  │ ───► │ [ text… ]        │ ───► │ Launch this      │
└──────────────────┘      │ Worktree Auto ⌄  │      │ mission?         │
                          │ Account  Auto ⌄  │      │ → ConfidAI-ci    │
                          │ Model    — ⌄     │      │ → [acct2] 88%    │
                          │ Effort   — ⌄     │      │ → opus · xhigh   │
                          │        [Launch]  │      │  [   Launch   ]  │
                          └──────────────────┘      │                  │
                                                    │  [   Cancel   ]  │
                                                    └──────────────────┘
                                    ▼
                          ┌──────────────────┐      ┌──────────────────┐
                          │ ▸ launching      │ ───► │ ✓ launched       │
                          │ ① picked → …     │      │ attach: tmux -L… │
                          │ ② creating tmux… │      │ on the board in  │
                          │ ③ booting claude…│      │ ~30s             │
                          └──────────────────┘      └──────────────────┘
```

1. `＋` in the bottom accessory → Composer (`.large` sheet), draft restored.
2. Type, or arrive from the share extension.
3. Pick Worktree/Account (Auto resolved from real config), Model and Effort (both required).
4. **Launch** → confirmation sheet:

```
╔════════════════════════════════════════════════════╗
║                      ▁▁▁▁▁                         ║
║   Launch this mission?                             ║
║                                                    ║
║   →  ConfidAI-ci      (cleanest free, Δ0)          ║
║   →  [acct2]          88% left · reserve 20%       ║
║   →  opus · effort xhigh                           ║
║                                                    ║
║   Spends [acct2] usage. The agent runs with        ║
║   --dangerously-skip-permissions and can run       ║
║   commands and push.                               ║
║                                                    ║
║   You can stop it from Activity.               ⓘ   ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │              Launch mission                  │ ║
║   └──────────────────────────────────────────────┘ ║
║                                                    ║
║                (24 pt of dead space)               ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │                  Cancel                      │ ║
║   └──────────────────────────────────────────────┘ ║
╚════════════════════════════════════════════════════╝
```

Three ergonomic rules, all consequences of "a fixed detent means a fixed thumb coordinate": **Cancel is bottom-most**; **24 pt of dead space** separates actions whose consequences differ in kind; and this sheet uses `.height(340)` so its primary button does **not** land at the same y-coordinate as the Finish and Auto-resume sheets.

5. POST with an `Idempotency-Key`. The composer transforms in place into the progress view; a Live Activity starts; the sheet becomes dismissible (the intent continues in Activity).

6. **`needs_decision`** → Insufficient headroom sheet:

```
╔════════════════════════════════════════════════════╗
║  ⚠ No opus headroom on any account — best is       ║
║    [work] at 12% left, below its 20% reserve.      ║
║                                                    ║
║  ┌──────────────────────────────────────────────┐  ║
║  │  ▶ Start with Opus — [spare], 88% left       │  ║  primary
║  └──────────────────────────────────────────────┘  ║
║                (24 pt)                             ║
║  ┌──────────────────────────────────────────────┐  ║
║  │  ⚑ Use fable anyway                          │  ║  tinted warn
║  └──────────────────────────────────────────────┘  ║
║  ┌──────────────────────────────────────────────┐  ║
║  │    Cancel                                    │  ║
║  └──────────────────────────────────────────────┘  ║
╚════════════════════════════════════════════════════╝
```

`⚑ Use anyway` sets `force_model: true`, bypassing the reserve, so it gets **its own second confirm naming the number** — at `.height(280)`, again a different detent. When `can_opus` is false the primary is replaced by *"no account has Opus headroom either"*.

7. **Progress** renders the server's `①②③④⑤` lines verbatim, with two-space-prefixed lines indented as sub-lines. Typical 10–20 s. Foreground: intent frames off the stream. Backgrounded: ActivityKit pushes (§8.3) — **not** polling, which iOS does not grant a backgrounded app.

8. **Terminal states**
   - `ok` → success card with the attach command, `appears on the board in ~30 s`, and a persistent amber note when `effort_confirmed == false`. Draft cleared.
   - `ok:false` → failure card, draft restored, ‹Reopen draft›.
   - **Client deadline 90 s** with no terminal phase → Reconciliation. (`_run_dispatch` has no `try/except`, so a bad payload can strand a job at `done:false, result:null` forever.)

9. **Reconciliation**

```
╔════════════════════════════════════════════════════╗
║   Did it launch?                                   ║
║   The connection dropped before the server          ║
║   confirmed. Checking…                       18s   ║
║                                                    ║
║   ⚠ Don't launch again yet — a retry could start   ║
║     a SECOND agent in the same worktree.           ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │            Open the dispatch log             │ ║
║   └──────────────────────────────────────────────┘ ║
╚════════════════════════════════════════════════════╝
```

Matching is on the **idempotency key**, never on `mission_original` text: dispatch-log timestamps are timezone-naive, Activity offers "Use as a new mission" (making duplicate mission text a designed-in feature), and a false positive here produces the worst outcome available — the user believes it launched, does not relaunch, and nothing is running. The reconciler polls `GET /api/intents/{key}` at 2 s for 25 s, falling back to the dispatch log matched on `client_op_id`. 25 s and not 5, because the log line is written only *after* the tmux session exists (~10–16 s in) and never when tmux fails.

**Kill.** From Activity or Intent Detail: `POST /api/kill {session}` → `tmux -L fleet kill-session`. Confirmation names the session and states that work in progress is lost. This is the only way to stop an agent dispatched by accident.

**Changed from desktop.** Confirmation sheet added (the desktop launches on one click). Progress and history moved out of the composer into Activity, so a dispatch survives closing the composer. Idempotency key and reconciliation added, because the platform can retransmit beneath us. Kill added.

## 4.4 Finish — the two-step

**The arm moves to the server.** The desktop's 6-second client-only `_armFinish` window cannot survive an APNs action button, and two clients desynchronise instantly. Finish becomes an `Intent` with phases; the mobile confirmation sheet is a *presentation* of `phase: armed`, not a private client timer.

```
Board row                Finish sheet             Outcome
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ ▲ ConfidAI-auth  │    │ Close out        │    │ ✓ closeout brief │
│   feat/auth-rot… │ ─► │ ConfidAI-auth?   │ ─► │   sent. When the │
└──────────────────┘    │ ▲ 1 agent live   │    │   agent reports  │
   swipe ← ✓ Finish     │   Δ7 · ↑3        │    │   done, ✕ Close  │
                        │ [Send the brief] │    │   verifies.      │
                        │ [Cancel]         │    └──────────────────┘
                        └──────────────────┘             │
                                                          ▼
                                       Board row now shows "✕ close pending"
```

### Step 1 — send the closeout brief

1. Trailing swipe on the identity row → `✓ Finish` (orange, **full-swipe disabled**), or the Worktree Detail footer, or long-press.
2. Finish sheet:

```
╔════════════════════════════════════════════════════╗
║                      ▁▁▁▁▁                         ║
║   Close out ConfidAI-auth?                         ║
║                                                    ║
║   ▲ 1 agent live · Δ7 uncommitted                  ║
║     ↑3 ahead of origin/main                        ║
║                                                    ║
║   The agent gets a closeout brief: settle          ║
║   background work, commit what matters, land the   ║
║   branch, park on trunk, report. Work that has     ║
║   already landed is never re-merged.               ║
║                                                    ║
║   If no terminal is live, a one-shot closeout      ║
║   agent is launched instead (haiku).               ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │           Send the closeout brief            │ ║
║   └──────────────────────────────────────────────┘ ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │                   Cancel                     │ ║
║   └──────────────────────────────────────────────┘ ║
║   What does the brief say?                     ⓘ   ║
╚════════════════════════════════════════════════════╝
```

The one-shot-agent line is not optional: `mode:"dispatch"` has **no double-fire guard at all** server-side, and the user must not be surprised by an agent appearing. `ⓘ` opens `guide.html` §4's five-step brief verbatim.

3. POST with an `Idempotency-Key` and a **server-side per-worktree lock**. `timeoutIntervalForRequest = 120` — the call runs `git fetch origin` (30 s) plus `claude_processes()` twice (up to 26 s each) plus osascript (10 s), and can exceed 60 s.

**Progress is real or absent.** A staged label (`fetching origin… · checking the landing… · typing the brief…`) on a single synchronous call with no job id is a timed fiction, and it would sit on the wrong stage given wildly asymmetric worst cases. Finish returns an `intent_id` immediately and phases stream (`fetching → checking → typing → brief_sent`). Until that lands, the UI shows an honest indeterminate spinner with an elapsed counter and *"this can take up to a minute"*.

4. **Outcome, by `mode`:**

| `mode` | ok | sheet result | card afterwards |
|---|---|---|---|
| `exit` | ✅ | `✓ already landed — /exit sent. The terminal closes and the card frees itself.` | trends to FREE |
| `brief` | ✅ | `✓ closeout brief sent. When the agent reports done, ✕ Close verifies the landing.` | `✕ close pending` |
| `slim` | ✅ | `✓ already landed — sent the short brief (settle, tidy, park).` | `✕ close pending` |
| `parked` | ✅ | `✓ already landed — parked on main and pulled. No agent needed.` | FREE |
| `noop` | ✅ | `✓ nothing to finish — landed and clean.` | FREE |
| `dispatch` | ✅ | `✓ no live terminal — launched a one-shot closeout agent.` → Live Activity | Activity row |
| `pending` | ❌ | a persistent refusal row (below) | amber row in the card |
| *(none)* | ❌ | server prose verbatim | inline error row |
| timeout | — | reconcile, never retry | |

**Finish reconciliation is not just `closeout_sent`.** That flag is **never set on the dispatch path**. Reconciliation checks, in order: the intent by idempotency key; then `closeout_sent`; then the dispatch log for a `closeout: true` line written immediately after `tmux new-session`.

### Step 2 — ✕ Close

```
╔════════════════════════════════════════════════════╗
║   ✕ Close ConfidAI-auth?                           ║
║                                                    ║
║   Brief sent 6m ago.                               ║
║   This verifies the landing (fetch · merge-base ·  ║
║   clean tree) and types /exit. It never re-sends   ║
║   the brief.                                       ║
║                                                    ║
║   ┌──────────────────────┐  ┌────────────────────┐ ║
║   │   Verify and close   │  │ Chat with the agent│ ║
║   └──────────────────────┘  └────────────────────┘ ║
╚════════════════════════════════════════════════════╝
```

### The refusal, promoted and self-clearing

`mode:"pending"` is a 6-second toast on desktop. On a phone you may look twenty minutes later, so it becomes a persistent amber row inside the card:

```
│ ⚠ can't close yet — 3 leftover file(s).            │
│   brief sent 6m ago · ‹Chat›  ‹Try again›          │
```

**Its condition is recomputed from every frame** (`git.dirty`, `git.ahead`, `closeout_sent`, `availability`) and the row **dissolves itself** when it clears, replaced by `✓ landed — you can close it now`. A client-local refusal with no re-verification would be a *worse* lie than the toast it replaces, because it persists.

### Server-restart hazard

`_closeouts` is in-memory only, so a restart silently reverts `✕ close` to `✓ finish` — and pressing it re-types the whole 600-char brief at an agent mid-closeout. The client remembers locally that it sent a brief for this worktree in the last 30 minutes; if the server stops reporting `closeout_sent` while live procs remain, the sheet warns *"the server restarted — sending again re-types the brief at an agent that may already be closing out"* and demotes the primary button. Durable intents remove this entirely.

**Changed from desktop.** The 6 s arm becomes a sheet with named consequences (a 26 pt chip is not adequate consent for "launch a headless agent that merges and pushes"). The refusal becomes a persistent, self-clearing row. The server-side lock is added, because a client-side guard cannot cover an app relaunch, a second phone, or the desktop board open on the same Mac — exactly the double-fire it was defending against.

## 4.5 Resume and auto-resume

### Manual resume

`▶ resume` exists only when `status == "limit" && limit.group == "session" && limit.resets_at` — session caps only. It sends `POST /api/send {account, sid, text: "continue"}` through the same identity-addressed path and the same receipt states. **No confirmation** — one word, into an agent idle by definition.

**Both swipe actions are always present.** A trailing action that mutates from `⏱ Auto` (opens a sheet) to `▶ Resume` (a no-confirm write) when the clock passes `resets_at` means a swipe begun at 14:32:59 can reveal a different button than the one aimed at. Instead: both actions, fixed positions, `▶ Resume` disabled-with-reason before the reset (`available at 14:32`). Position never moves; meaning never changes.

### Auto-resume sheet

```
╔════════════════════════════════════════════════════╗
║                      ▁▁▁▁▁                         ║
║   Auto-resume · ConfidAI3                          ║
║   [acct8] · Weekly limit                           ║
║   resets 14:32 · in 2h 38m                         ║
╟────────────────────────────────────────────────────╢
║   Resume                                           ║
║    ( 1 min after reset )                           ║
║      5 min      15 min      1 hour                 ║
║      At an exact time…                             ║
║                                                    ║
║    fires 14:33 today                               ║
║    you'll get a Lock Screen countdown              ║
║                                                    ║
║   ┌──────────────────────────────────────────────┐ ║
║   │              Arm auto-resume                 │ ║
║   └──────────────────────────────────────────────┘ ║
║                                                    ║
║   At the armed moment the board re-checks the      ║
║   limit. If it still binds, it re-arms for the     ║
║   next reset (up to 10 times). Then it types       ║
║   "continue" into this session's own terminal.     ║
║   If no terminal can be typed into, the            ║
║   conversation is reopened in tmux with            ║
║   claude --resume.                             ⓘ   ║
╚════════════════════════════════════════════════════╝
```

- Delays mirror the desktop select exactly (60 / 300 / 900 / 3600 + exact). `At an exact time…` expands an inline wheel defaulting to `resets_at + 60`, or `now + 1 h` when no reset is known; the sheet grows to `.large`.
- The resolved fire time is always shown in words, **and which surface you will get** (§8.3c).
- **`need_time` is not an error.** The sheet auto-expands the exact-time picker with *"this limit carries no reset time — the time is yours to pick"*.
- Armed: `⏱ armed for 14:33 · re-armed 2×` + ‹Change› ‹Disarm›. Done: `✓ sent 'continue' — sent via tmux`. Failed: `⚠ {message}` + ‹Re-arm›.

### The firing blind spot

`fire_resume` can block for ~14 minutes (90 s `cclimits --refresh` + 420 s waiting for a composer to go idle + 3 retries) while the schedule still reads `pending` with a past `due_at`. But the resume loop fires due keys **serially in one pass** — if A takes 14 minutes, B and C are merely *queued*, freely cancellable, and marking them all "firing now" denies the user a working action with a false explanation at exactly the moment they most want it.

**Interim rule until the server ships `status: "firing"` + `started_at`:** only the **earliest** overdue pending schedule may be firing.

```
⏱ ConfidAI3 · [acct8]     firing now…      Disarm ✗
   cancelling now won't stop a resume in progress
⏱ orbital-api · [work]    queued behind ConfidAI3   Disarm ✓
```

`_resumes` iterates in **insertion order, not due order**, which is not what a user would predict — so the queued caption names the blocker, making the ordering visible.

**Re-arming while firing** is a lost-update race server-side; `Change` is blocked in the firing window. And **cancel is not an abort**: if `fire_resume` is already executing, the pop removes the key but the side effect still happens and is never reported. The caption says so.

**Changed from desktop.** The drawer becomes a sheet with the fire semantics stated in full. Both swipe actions become permanent. The firing/queued distinction is new — the desktop cannot show it either, but on a desktop you can look at the terminal.

## 4.6 Limits and reserve editing

```
Limits                    Account Detail            After edit
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│ main    🔒RESERVE │ ──► │ RESERVE BUFFER   │ ──► │ ✓ reserve set to │
│ 18% left       › │     │ ─ ●────────── ＋  │     │   25% on main    │
└──────────────────┘     │       20 %       │     └──────────────────┘
                         │ auto-dispatch    │
                         │ stops at 80%…    │
                         └──────────────────┘
```

1. Limits → tap an account → Account Detail.
2. Drag the slider (snapped to 5 %). The readout and caption update live; nothing is sent.
3. Release → optimistic update + POST, debounced 600 ms.
4. Success: an inline confirmation, no toast. Failure: **inline** error with the literal truth — the server mutates its in-memory config *before* the disk write, so `ok:false` means *"saved for now, but the config file couldn't be written; it resets when orchestra restarts"*.

**Refresh all accounts** is a separate, deliberate act in the accessory: disabled while in flight, labelled `up to a minute`, never auto-retried.

**Changed from desktop.** A number input with an `onchange` and an `alert()` on failure becomes a slider with inline errors. Reset countdowns are always shown rather than hidden below 50 % used. The `0 = remove the override` semantics are surfaced instead of silently applying the `"*"` default.

## 4.7 The terminal-focus question

The desktop's `⌖ focus` is: a **GET with a side effect** that for tmux hosts opens a *brand-new Terminal window every call*; retried by URLSession by default on idempotent GETs; paid off on a screen the user is not looking at; and a lie for Cursor/VS Code, where `open -a` returns `{"ok": true}` without focusing anything.

**It is demoted, and its substitute is a server-side pasteboard write.**

```jsonc
// POST /api/pasteboard
{ "text": "tmux -L fleet attach -t mission-confidai-auth-091204" }
// server: subprocess.run(["pbcopy"], input=text.encode())
```

The button is **‹Send attach command to the Mac›**. It works from anywhere on the tailnet, needs no proximity, and puts the command exactly where the user's hands will be. Confirmation: `✓ on studio-mac's clipboard`.

> A local ‹Copy› relying on Universal Clipboard was considered and rejected: Handoff requires the same iCloud account, Bluetooth *and* Wi-Fi, and the devices **within Bluetooth range**. It is a proximity feature with no deferred delivery — so copying on cellular two miles away serves only the already-at-my-desk case, which is the one case where `⌖ focus` was fine.

**Placement, one rule:**

> `Open a terminal there` and `Send attach command to the Mac` live in **exactly two places**: the Session Info sheet, and the `On studio-mac` submenu of a long-press context menu. **Never** in a swipe action, never as a button on any screen, never at a nav-bar overflow's top level.

Plus: rate-limited to one call per session per 60 s; **never retried on timeout** (a timeout may mean it worked); disabled with an explanation when `host ∈ {Cursor, VS Code, kitty, Ghostty, WezTerm, Alacritty}`; success message shown verbatim.

**One sharp edge, stated wherever an attach command is offered:** attaching to a tmux session that already has a client resizes the pane to the smaller client's dimensions, which can garble a running Claude TUI. Caption: `if a window is already attached, this will resize it`.

## 4.8 The map

Full specification in §5. The flow:

1. Fleet → `Board ⇄ Branches` in the bottom accessory.
2. Read the ranked rows: divergence age, landing cost, staleness.
3. Tap a row → **Branch detail** sheet.
4. From there, `✉ chat` and the **same** two-step `✓ finish` / `✕ close` state machine as the board.

**Changed from desktop.** `map.html`'s `✓ finish` is a strictly older one-step implementation that discards `mode` and never reads `closeout_sent`. iOS has exactly one finish state machine, shared. `⌖ focus` is not reproduced at all.

## 4.9 Onboarding and QR pairing

```
   A                      B                       C
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│                  │   │ Before we start  │   │  ┌────────────┐  │
│       ⌁          │   │                  │   │  │            │  │
│    orchestra      │   │ ① Tailscale on   │   │  │   [camera] │  │
│                  │   │   this iPhone ✓  │   │  │            │  │
│ Mission control  │   │ ② orchestra       │   │  └────────────┘  │
│ for the agents   │   │   running     →  │   │                  │
│ on your Mac.     │   │                  │   │ Point at the code│
│                  │   │ On your Mac:     │   │ in your Mac's    │
│ [Pair with my    │   │  ./start.sh      │   │ terminal         │
│  Mac]            │   │      --pair      │   │                  │
│ [Take a look     │   │                  │   │ ‹Enter details   │
│  first]          │   │ [Scan the code]  │   │  manually›       │
└──────────────────┘   └──────────────────┘   └──────────────────┘
                                                       │
   D                      E (push)                     ▼
┌──────────────────┐   ┌──────────────────┐   ┌──────────────────┐
│ ✓ Paired         │   │ How do you want  │   │ ✓ key readable   │
│                  │   │ to be told?      │   │ ✓ JWT signs      │
│ ● studio-mac     │ ─►│ ⬤ APNs           │ ─►│ ✓ APNs accepted  │
│   …ts.net:4242   │   │ ○ ntfy.sh        │   │   a test push    │
│   achill         │   │ ○ Off            │   │                  │
│   9 worktrees    │   │                  │   │ [Allow           │
│                  │   │ [Continue]       │   │  notifications]  │
│ [Continue]       │   └──────────────────┘   └──────────────────┘
└──────────────────┘
```

`./start.sh --pair` prints an ASCII QR encoding:

```
orchestra://pair?url=https%3A%2F%2Fstudio-mac.tailXXXX.ts.net%3A4242
               &name=studio-mac&token=<32-byte base64url>[&pin=<sha256-spki>]
```

Two engineering notes so this is budgeted honestly:

- A stdlib byte-mode QR encoder with GF(256) Reed–Solomon, mask selection and BCH format info is realistically **250–400 lines** for a ~110-char payload needing a version-5-ish 37×37 symbol. Its own module, with test vectors.
- It must render **light-on-dark-safe**: the standard `█`-on-space rendering inverts badly on dark terminal themes and will not scan. Use `█` blocks on an explicitly white background sequence with a 4-module quiet zone, and ship a `--pair --invert` flag.

Validation calls `GET /api/hello` — needed anyway, because `HEAD` and `OPTIONS` both return 501 today, so there is **no cheap liveness probe** and a reachability check otherwise costs a 36 KB payload.

```jsonc
// GET /api/hello
{ "hostname": "studio-mac", "user": "achill", "version": "0.10.0",
  "started_at": 1784400000.0, "now": 1784636700.4,
  "capabilities": ["events","intents","idempotency","identity_send","send_receipt",
                   "kill","pasteboard","push:apns","chat_paging","topology_skipped"],
  "config": { "roots": ["/Users/achill/Downloads"], "pattern": "confid",
              "session_window_h": 48, "max_sessions": 6, "working_s": 90,
              "quiet_s": 25, "resume_message": "continue",
              "exclude_accounts": ["main"],
              "reserve_percent": {"main": 20, "*": 0} } }
```

`capabilities` is the version-skew mechanism, replacing the desktop's hand-written *"the server predates auto-resume; restart it with ./start.sh"*. Missing `idempotency` → dispatch and finish are **disabled** with an explanation, not merely warned about. Missing `identity_send` → chat is read-only. Missing `chat_paging` → the 40-turn marker stays. `config` is what makes the empty states and the auto-preview honest.

**"Take a look first"** loads a **bundled sample fleet**, not `--demo`. `demo_state()` is missing five session fields real state always has, adds a bogus `git_root`, and `--demo` does not sandbox `/api/dispatchlog` or `/api/chat` — real mission prose and real transcripts leak. A demo mode that can put production identifiers into a screenshot is not a preview mode.

**Post-pair connect is parallel, not serialised.** `/api/hello` gates on nothing else; then the SSE stream opens and the device registers **concurrently**. Limits ride the stream and never gate a render.

## 4.10 Push deep links

| kind | destination | actions |
|---|---|---|
| `needs_input` | **Chat**, composer focused | **Reply** (inline) · Open |
| `blocked` | **Chat**, Why auto-presented once | Open |
| `limit` | **Session** + Auto-resume sheet | **Arm auto-resume** (1 min) · Open |
| `dispatch_done` | **Intent Detail** | Send attach cmd |
| `dispatch_failed` | **Composer**, draft restored | Reopen draft |
| `resume_fired` | **Worktree** (not the session — see below) | Open |
| `freed` | **Worktree Detail** | New mission here |
| `closeout_refused` | **Worktree Detail** | Chat |

**Cold start:** the deep link is honoured before the first frame arrives — the destination renders from the App Group's cached snapshot with skeletons for anything missing. Tapping a push never lands on a spinner.

**Stale push:** if on arrival the status no longer matches the notification's reason, a dismissible banner says `this agent moved on — it's working again` rather than silently showing something else.

**Why `resume_fired` targets the worktree:** `_tmux_resume` runs `claude --resume <sid>`, which may surface as a **different** sid, orphaning the schedule key `"{worktree}|{sid}"` and any session-level deep link. Until the server emits `resumed_to_sid`, the notification lands on the worktree.

## 4.11 Reply from the lock screen

`UNTextInputNotificationAction`. The handler runs in a background app launch: tunnel wake → `POST /api/send {account, sid, expect_sid, text, idempotency_key}`. **One round trip**, ~300–800 ms on a warm tailnet, because the identity assertion is server-side.

- Hard budget **8 s**. Beyond it the handler posts `⚠ couldn't reach studio-mac — your reply is saved as a draft` and writes the draft to the App Group.
- Failure — including an `expect_sid` mismatch — arrives as a **second local banner**: `⚠ that session's terminal changed — open orchestra`. A notification-action handler cannot present inline UI; the only channel back is a new notification.
- Success posts nothing; the badge decrement is the acknowledgement.

**This ships enabled.** It is the flagship mobile flow — answering a blocked agent without unlocking the phone.

## 4.12 What the desktop does that mobile deliberately does not

| desktop | mobile | why |
|---|---|---|
| `⌖ focus` — raise a window / open a Terminal | ‹Send attach command to the Mac› | §4.7 |
| bell on any `attn` increase | tiered push; `waiting` off by default | §8.4 |
| toast for everything | inline persistent states; toasts only for transient confirmations | a 6 s toast is unreadable if you look 20 minutes later |
| show-ended checkbox in a permanent controls strip | Filters sheet | one control, not a strip |
| `map.html`'s one-step finish | the board's two-step, shared | the map's version is a stale fork |
| `--demo` as a preview | bundled sample fleet | demo leaks real transcripts |

---

# 5. The branch map on a phone

## 5.1 What the map is actually for

The desktop map draws every worktree's branch as a horizontal lane starting at its true merge-base with the trunk and running rightward to its tip, on a logarithmic time axis whose right edge is `now`. It is a good visualisation and it is 900 px wide by construction.

Cross-referencing it against the board shows the map's genuinely unique payload is small:

| question | data | on the board? |
|---|---|---|
| how long has this diverged from trunk? | `fork_ts` — **topology only** | **no** |
| how expensive is landing it? | `behind` | yes (a tiny `↓N`) |
| is this abandoned work holding a worktree? | old `fork_ts` **and** old `tip_ts` | **no** |
| which worktrees will collide on merge? | group membership — **topology only** | **no** |
| is this worktree safe to dispatch into? | `ahead == 0 && behind > 0` → **no** | **no** |

The last two are why the map is not optional on a phone. `POST /api/finish` in `brief` and `dispatch` modes instructs an agent to merge into trunk and push; two worktrees on the same trunk finishing concurrently collide, and `/api/state` does not expose which worktrees share a trunk. And on the live fleet, `ConfidAI4` reads free-ish on the board while being 51 behind with 20 dirty files, and `ConfidAI-security-audit` reads free while **298 behind**. Dispatching into either from a bus stop is a mistake the board cannot warn you about.

So the drawing's job is narrow and stated precisely:

> **The fork strip exists to show the *cluster structure* of divergence times — that eight worktrees left the trunk within five days and one left five months ago — which a column of `144d / 5d / 2h / 12m` labels does not show without arithmetic.**

One job, worth 20 pt of a row. Not worth a coordinate contract, a level-of-detail ladder, a gesture layer, and a landscape variant.

## 5.2 The recommendation: ranked rows, one fork strip each

```
╔════════════════════════════════════════════════════╗
║  ⌁  Branches                    status ▾    30d ▾  ║
╠════════════════════════════════════════════════════╣
║ CONFIDAI · origin/main · tip 4m ago                ║  sticky
║ ├───────────────────────────────────────────────▽  ║  trunk baseline
╟────────────────────────────────────────────────────╢
║  ConfidAI3 ··                ⛔ LIMIT       ↓2317  ║
║  fix(course): russian lesson 7 audio pipeline +…   ║
║  feat/russian-course · +2045 · Δ3                  ║
║    ⟨ ●━━━━━━━━━━━━━━━━━━━━◉ ·····················▽ ║
║      144d                                          ║
╟────────────────────────────────────────────────────╢
║  ConfidAi8 ····              ● WORKING       ↓147  ║
║  feat(swahili): lesson generator + audio cache     ║
║  feat/swahili · +186 · Δ29                         ║
║       ●━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━◉ ··········▽ ║
║       21h                                          ║
╟────────────────────────────────────────────────────╢
║  ConfidAI4 ·                 ◇ FREE           ↓51  ║
║  ⌁ stale checkout                                  ║
║  feat/server-driven-languages · +0 · Δ20           ║
║                                    ◉ ·············▽║
║                                    2h              ║
╟─ STALLED · 1 ──────────────────────────────────────╢
║  ConfidAi5 ·                 ○ ENDED          ↓16  ║
║  fix(hi): retrofit devanagari renderer             ║
║  fix/hi-retrofit · +53 · Δ36                       ║
║    ●━━━━━━━━━━━━━━━━━━━━━━━━━◉ ··················▽ ║
║    5d                                              ║
╟─ PARKED AT TIP · 2 ──────────────────────────────⌄ ╢
║  ConfidAI2  ConfidAi7                              ║
╟────────────────────────────────────────────────────╢
║  2 worktrees can't be placed — scratch-x (no       ║
║  trunk ref), tmp-y (no merge-base)                 ║
╚════════════════════════════════════════════════════╝
```

**One full-width row per worktree.** Giving each branch its own row eliminates label collision entirely — the desktop map's hardest and most fragile problem, where glyph widths are hand-estimated as `chars × 6.6`. Hit targets go from 4–13 px to a single 393 × 102 pt row. Hover-only detail is either in the row or one tap away.

**Kept from `map.html`:** the log-scaled time axis, the fork point as a real merge-base, the ahead/on-trunk split, status colour + glyph, the pulsing tip for working agents.

**Discarded:** the 900 px floor, the y=lane coordinate frame, the cubic elbow, the rider row-packer, the ±14 px label offsets, the `+220` hit rect — all consequences of drawing many lanes in one coordinate space.

## 5.3 The axis — clamped, per group, computed client-side

**A global anchor destroys the axis.** On live data `t_min = min(all fork_ts)` gives a 144.7-day span; six of nine forks then sit in the last 88 pt of a 360 pt axis and the left 272 pt serves one outlier. The drawing is a smear.

**Fix: a clamped per-group anchor.** The anchor is the oldest fork, but never more than **6 × the 75th percentile of fork ages**, floored at 6 h. Anything older is drawn as a hard `⟨` cap at the left margin with its real age labelled. Measured on the same data: anchor 126.9 h, exactly one branch clipped, fork spread **325 pt** instead of 88.

Quantile-based, so it degrades correctly. **Per group**, because groups never share a trunk — so there was never a reason to share a scale, and a young repo must not be compressed into a sliver by an old one.

**The server ships raw epochs plus two axis scalars; the client computes positions against the device clock.**

> ⚠ `API.md` §9.7 ships `fork_at` / `tip_at` / `trunk_at` (integer epochs) but **not** `axis.s`, `axis.anchor_age_s`, `base_ts`, `commits_capped`, `commits_oldest_ts` or `role`. `role` is derivable client-side from `ahead`/`behind` (§5.6); the axis scalars are **not** — deriving the anchor client-side from the fork set is possible but then the "client and server never disagree" guarantee below is vacuous, and §5.8's stale-`behind` marker has no input at all. Tracked in `API.md` §0.2. Note also the field names: this section's `fork_ts` / `tip_ts` / `trunk_ts` are the legacy spellings; the contract uses `_at`.

```swift
struct GroupAxis: Decodable, Equatable {
    let s: Double            // 900.0 — the log knee
    let anchorAgeS: Double   // clamped span
}

struct AxisScale: Equatable {
    let now: Double, s: Double, denom: Double, anchorAgeS: Double

    init(_ a: GroupAxis, now: Double = Date().timeIntervalSince1970) {
        self.now = now; self.s = a.s
        self.anchorAgeS = max(a.anchorAgeS, 6 * 3600)
        self.denom = log1p(self.anchorAgeS / a.s)
    }
    /// 1.0 = now, 0.0 = the anchor. Negative = older than the anchor → the ⟨ cap.
    func u(_ ts: Double) -> Double { 1.0 - log1p(max(0, now - ts) / s) / denom }
    func x(_ ts: Double, padL: CGFloat, width A: CGFloat) -> CGFloat {
        padL + A * CGFloat(min(1.0, max(0.0, u(ts))))
    }
    func isClipped(_ ts: Double) -> Bool { u(ts) < -0.001 }
}
```

Four reasons the server must **not** precompute normalised positions:

1. Positions anchored at `generated_at` drift under the labels — four minutes is 2.5 % of the axis (~9 pt), worst at the right edge where five of nine tips cluster within 1.7 h.
2. The anchor and denominator are data-dependent: finishing one worktree moves *every* position in *every* row.
3. A `now` field in the body defeats the ETag.
4. The client would recompute `log1p` anyway to animate.

Worst case ~720 `log1p` calls per pass. Free. Client and server still agree because they share `s` and `anchor_age_s` — which is what "never disagree" actually required.

## 5.4 The row

```
┌────────────────────────────────────────────────────┐
│  ConfidAI3 ··              ⛔ LIMIT         ↓2317   │  20pt
│  fix(course): russian lesson 7 audio pipeline +…   │  16pt
│  feat/russian-course · +2045 · Δ3                  │  14pt
│    ⟨ ●━━━━━━━━━━━━━━━━━━━━◉ ·····················▽ │  20pt
│      144d                                          │
└────────────────────────────────────────────────────┘
```

| element | why it is here |
|---|---|
| worktree name | identity; the join key into the board |
| `··` multiplicity dots | one per session, max 4 then `4+`. Distinguishes a 1-agent from a 6-agent worktree |
| status pill | glyph **+ word** + colour — three channels, so colour is never alone |
| `↓2317` chip | landing cost, tier-coloured. Greyed with a `⌁` prefix when this clone's fetch is stale |
| **commit subject** | **the only text the drawing cannot express.** `subject_short`, server-truncated to **72 chars** (`API.md` §9.7). Clamped to 2 lines in the row; the accessibility label carries the full string |
| branch | middle-truncated: both ends carry meaning (`feat/…/arabic-course`) |
| `+2045 · Δ3` | neither is a drawn quantity on either platform |
| fork strip | 20 pt, §5.5 |

**Removed versus a naive port:** the tip's relative age (it *is* the tip donut's x-position — pure restatement), and a "debt segment" from fork to trunk tip. The latter was cut on measurement: `trunk.tip_ts` is a per-group constant, so the segment's length is a deterministic function of the fork dot drawn 0 pt away — the length channel is 100 % redundant, and on live data it renders two worktrees that forked at the same commit as two identical near-full-width bars stating one fact twice. That ink buys the commit subject.

**Debt tiers are a fixed log ladder, not fleet-relative quantiles.** With quantiles a worktree changes colour because an *unrelated* worktree moved; on a glance surface that is disqualifying.

| behind | tier | colour token |
|---|---|---|
| 0 | — | not shown |
| 1–9 | 1 | `textDisabled` |
| 10–99 | 2 | `statusLimit` |
| 100–999 | 3 | `statusTurn` |
| ≥ 1000 | 4 | `statusNeeds` |

**Row height is intrinsic** — no fixed frame, no absolute local-y constants. Only the 20 pt strip is pinned, because it is a diagram. `.layoutPriority(1)` goes to the **status pill and the debt chip**, not the name: at accessibility sizes a long worktree name alone exceeds the screen, and prioritising it would truncate the two triage fields to nothing — exactly the colour-only failure the design exists to avoid.

## 5.5 The fork strip

20 pt band, full row width, `A = width − 16 − 14`, derived from `GeometryReader` **minus safe-area insets** (landscape on notched iPhones reports 44–59 pt symmetric insets).

```
    ⟨ ●━━━━━━━━━━━━━━━━━━━━◉ ·····················▽
    │ │                    │                      │
    │ │                    │                      └─ trunk tip caret (shared
    │ │                    │                         reference, in every row)
    │ │                    └─ tip donut + pulse when working
    │ └─ fork dot (the real merge-base)
    └─ clipped cap: this fork is older than the anchor
```

Primitives, all in one `Canvas`:

1. **baseline hairline**, full width, `textDisabled` at 30 %
2. **trunk tip caret** — the shared reference mark, in every row
3. **lane**, fork → tip, 2 pt, status colour at 85 %
4. **clipped cap `⟨`** or the **fork dot** (7 pt ring, background-filled)
5. **tip donut** (10 pt ring + 4 pt core) with a `r 6 → 12` pulse at `α .7 → 0` over 1.7 s when working — entirely within the 20 pt band
6. **one x-positioned `Text`** carrying this row's own fork age

Rule, stated so it is not violated later: **`Canvas` renders geometry only. X-positioned text is real SwiftUI `Text` in a `ZStack` overlay, at most one per row.** That preserves Dynamic Type, localisation and truncation, and structurally removes the desktop's `chars × 6.6` width-estimation hazard.

**The inline fork label is the design's single most important small decision.** It converts the strip from "a diagram that requires co-visibility" into "a self-describing row that additionally aligns when co-visible". The honest vertical budget is ~6 rows visible on a 393 × 852 pt screen with a tab bar; on a 9-worktree fleet the shared axis is therefore co-visible for two-thirds of it. The label removes the eye-trace to a distant ruler and the dependence on tick labels surviving thinning.

Gridlines still exist for the alignment bonus, but at **α 0.12 on a 0.5 pt hairline** — the desktop's α 0.05 at 1 px composites to ~1.15:1 over `#0d0d0d` and is invisible on a phone outdoors — and they are drawn **once**, in a single full-height `Canvas` behind the scroll content, guaranteeing cross-row pixel alignment that per-row redraws do not.

## 5.6 Three roles, not two

`ahead == 0` is not "nothing to know". Live data: `ConfidAI-security-audit` has `ahead == 0`, **298 behind**, 1 dirty.

```python
def _role(ahead, behind):
    if ahead > 0:  return "diverged"
    return "parked" if behind <= 0 else "stale"
```

| role | rendering |
|---|---|
| `diverged` | full row, main section |
| `stale` | full row, main section, `⌁ stale checkout` note; in the sheet: *"at the trunk's old tip — a mission dispatched here starts 298 commits behind"* |
| `parked` | collapsed into one 44 pt row, **only when** `dirty == 0 && status == free` |

## 5.7 Commit dots are cut from the list, and the cap is made honest

The server runs `git log --format=%ct -40 mb..HEAD` — the **newest** 40, not a sample. Measured: `ConfidAI3` is 2045 ahead and its 40 dots span 13.9 pt of a 147 pt lane (9 %); `ConfidAi5` is 53 ahead and its 40 dots span 96 %. A 2045-commit branch renders as a 91 %-empty lane with a cluster at the tip — visually identical to a branch with 40 recent commits.

1. **v1 draws no commit dots in the list.** The lane is a solid 2 pt stroke fork → tip. On live data only 2 of 9 rows would have had separable dots — itself evidence that commit-level detail is not what this view is for.
2. The payload ships `commits_capped` and `commits_oldest_ts`.
3. The sheet's timeline (v2) draws the lane hard-edged and flat left of `commits_oldest_ts`, labelled `newest 40 of 2045`.

## 5.8 `behind` is per-clone and never fetched

The server computes `behind` in *each worktree's own* checkout, elects the group's trunk from whichever clone had the freshest base, and **never runs `git fetch`**. So `behind` can be stale and inconsistent across a group.

The payload ships `base_ts` per branch. When `group.trunk_ts − branch.base_ts > 900`:

```
↓298 ⌁     accessibilityLabel: "298 behind origin/main, as of 2 hours ago —
           this worktree has not fetched since"
```

On the live fleet 8 of 9 worktrees are **linked worktrees of one repo** sharing `refs/remotes/origin/main`, so their `behind` values *are* mutually consistent. The marker fires only across genuinely separate clones — the case that needed the warning.

## 5.9 Detail sheet

```
╔════════════════════════════════════════════════════╗
║                      ▁▁▁▁▁                         ║
║  ConfidAI3                              ⛔ LIMIT   ║
║  feat/russian-course                               ║
║ ────────────────────────────────────────────────── ║
║  a1b2c3d  fix(course): russian lesson 7 audio      ║
║           pipeline + retry on ttl expiry           ║
║  committed 2d22h ago · forked 144d ago from        ║
║  origin/main                                       ║
║ ────────────────────────────────────────────────── ║
║     +2045            ↓2317            Δ3           ║
║     ahead            behind           uncommitted  ║
║                      as of 2h ago ⌁                ║
║ ────────────────────────────────────────────────── ║
║    ⟨ ●━━━━━━━━━━━━━━━━━━◉ ·····················▽   ║
║      144d                    2d22h            now  ║
║ ────────────────────────────────────────────────── ║
║  SESSIONS                                          ║
║  ⛔ limit   [account8] fable-5  14m   ⏱ 04:31      ║
║  ● working [main]     opus-4-8  40s               ║
║ ────────────────────────────────────────────────── ║
║  ┌──────────────────────┐ ┌─────────────────────┐  ║
║  │       ✉  chat        │ │      ✓  finish      │  ║  48pt
║  └──────────────────────┘ └─────────────────────┘  ║
║                 open on board  ›                   ║
╚════════════════════════════════════════════════════╝
```

`.presentationDetents([.large])` only. `.medium` cannot hold ~490 pt of content at default type, and a scrolling sheet at `.medium` fights its own drag-to-resize.

**`✉ chat` is always enabled when a session exists.** Gating it on terminal reachability would be a regression versus the desktop: the server's transcript reader globs the `.jsonl` and reads a 512 KB tail — **no process is involved at all**. The desktop loads the transcript unconditionally and degrades only the composer. Gating the whole action would disable reading exactly the sessions a phone most wants: limit-stranded, ended, Cursor-hosted.

**`✓ finish` is the board's state machine, unchanged** — same two steps, same `closeout_sent`, same server-side lock, same idempotency key. One implementation, two entry points.

## 5.10 Interaction and states

| gesture | target | effect |
|---|---|---|
| vertical drag | list | scroll — a plain `ScrollView`, nothing competes |
| tap | row (393 × 102 pt) | detail sheet |
| long-press | row | context menu with a 3-line preview — *Chat* · *Finish* · *Pin for compare* (v2) |
| nav-bar `Menu` | sort | `status` (default) · `debt` · `recent` · `name` |
| nav-bar `Menu` | range | `7d` · `30d` · `all` |
| pull down | list | `.refreshable` |

**There is exactly one tappable object per row.** No dot, donut, caret or lane is individually tappable.

**Default sort is `status`, not `debt`.** `behind` grows monotonically for abandoned work and never shrinks without a merge, so a `debt` default pins two ancient forks to rows 1–2 permanently while whatever needs a human falls below the fold. Stall detection — the map's flagship insight — is expressed *structurally*, by moving rows whose `tip_ts` is older than 7 days into a trailing `STALLED · N` section.

**The re-sort hold and tap shield apply here too** (§4.1). Between two measurements a day apart, `ConfidAI4` went `ahead 2188 → 0` and `ConfidAI` went `0 → 1`, each moving a worktree **between sections**. A refresh landing *is* a re-sort under the finger.

| state | rendering |
|---|---|
| loading | header outline + 4 shimmer rows. Never a spinner on blank. This also kills the desktop's "topology arrives before state → every node flashes ◇ FREE" flash, very visible over a tailnet |
| no groups | *"no repos to map — orchestra found worktrees, but none has a trunk ref (origin/HEAD, origin/main, main, master). The board still works."* + ‹open board› |
| unmapped worktrees | muted footer naming them and the reason. The server silently drops these today, so the topology list can be shorter than the board's with nothing surfaced |
| stale | inline amber bar *"map data 4m old · pull to refresh"*. Because geometry is computed against the device clock, tip positions stay truthful as the payload ages — only `behind`/`dirty`/`status` go stale, and only those are marked |
| offline | keep the last payload, dim the strip to 60 %, banner *"not reachable — showing the map from 12:41"*, **both action buttons disabled**. Never blank, never queued for later delivery — a deferred merge-and-push is worse than no merge |
| detached HEAD, `ahead > 0` | amber `⚠ detached · 2188 unmerged` chip. Work on no branch is the strongest stall signal in the payload; styling it below an ordinary branch inverts the priority. Muted italic only when `ahead == 0` |

## 5.11 Refresh cadence

- **Fetch on appear and on `.refreshable`. No timer.** `GET /api/v1/topology` is ~90 git subprocesses server-side with a 45 s cache; `API.md` §9.7 states plainly *"never on a timer from a phone"*, and the desktop map's own 30 s poll against a 30 s TTL is the measured pathology (every refresh pays the full 3.07 s). Suspended on background and on disappear.
- **Status colours ride the state stream**, not the map poll. The payload ships `status` for correct first paint; live board state overrides it thereafter, so tips recolour on every state frame without touching topology.
- The pulse is scoped: `TimelineView(paused: b.status != .working)` means a non-working row does zero per-frame work. Contrast the desktop, which rebuilds the entire SVG via `innerHTML` every 10 s, destroying every `<animate>` and restarting all pulses in lockstep.

## 5.12 Rendering choice

**SwiftUI `Canvas`, one per row, inside `LazyVStack`.**

- **Swift Charts** rejected: the scale is a reversed, clamped, offset log that `.chartXScale(type: .log)` cannot express, so we would pre-transform then fight Charts to relabel the axis in real ages; and `LineMark`/`PointMark` cannot draw the donut-with-core tip or the `⟨` cap. One `Chart` per row also instantiates a plot area and axis renderer per row.
- **UIKit `CAShapeLayer`** rejected: wins only at node counts we never reach (~7 primitives per row, ~42 on screen), and costs a representable, manual trait theming and manual hit-testing.
- **`Canvas`** chosen: immediate-mode; the log transform and clip test are plain Swift; `LazyVStack` means off-screen rows never draw; the pulse scopes cleanly.

## 5.13 v2 backlog and permanent cuts

**v2, gated on telemetry** (`map_opened_from_phone`, `map_row_tapped` instrumented in v1): the detail sheet's commit timeline with a per-branch auto-fitted **linear** window; the compare-pin (long-press → *Pin for compare* locks a row under the sticky header so any two lanes align regardless of scroll distance); a compact 44 pt row mode above 9 worktrees.

**Cut permanently:**

- **Global pinch/pan zoom.** Windowing a log axis magnifies compression rather than relieving it — at the clamped denominator, a 50× window still spans 13.6 hours across 360 pt at the left end and ~11 seconds at the right, so commit-level detail is unreachable exactly where it matters. And the gesture is infeasible on a `ScrollView` without a UIKit representable: an overlay carrying a gesture kills every row tap and `.refreshable`, and `.simultaneousGesture` cannot cancel an in-flight scroll pan. Commit detail moves to the sheet's private linear window, which needs no gesture at all.
- **`⌖ focus`** — §4.7.
- **Per-row commit dots at overview zoom** — §5.7.

---

# 6. Gestures, haptics, and shortcuts

## 6.1 Swipe actions — trailing only

**No leading swipe actions anywhere in the app.** Session rows also appear in Worktree Detail, a pushed screen, so an edge-started rightward swipe collides head-on with `interactivePopGestureRecognizer` — a coin flip between "open the conversation" and "lose the screen you were on", depending on nav-stack depth, which is invisible mid-gesture. Reply is redundant as a swipe anyway: tapping the row already opens Chat.

| Object | Trailing (←) | Full-swipe |
|---|---|---|
| **Worktree identity row** | `✓ Finish` / `✕ Close` (orange) — opens the sheet, never actuates · `＋ Mission here` (cyan) | **disabled** |
| **Session row** — `needs_input`/`blocked`/`waiting`/`working`/`ended` | `ⓘ Why` (grey) | disabled |
| **Session row** — `limit`, no `handed_to` | `⏱ Auto` · `▶ Resume` (both always present; Resume disabled-with-reason before reset) | disabled |
| **Session row** — `limit` with `handed_to` | `ⓘ Why` (explains "nothing to do") | disabled |
| **Branch row** (map) | `✓ Finish` · `✉ Chat` | disabled |
| **Activity: in-flight intent** | `⧉ Send attach cmd` · `✕ Kill` (destructive, confirms) | disabled |
| **Activity: armed schedule** | `✕ Disarm` (single tap; Undo, §6.5) | disabled |
| **Account row** | *(none — refresh is fleet-wide, §3.6)* | — |

**No full-swipe reaches any write.** Every swipe action is duplicated in the long-press menu and in the detail screen — gestures are undiscoverable, and VoiceOver users reach them through the actions rotor.

## 6.2 Taps, drags, presses

| Gesture | Where | Effect |
|---|---|---|
| tap | session row | ▸ Chat |
| tap | identity row | ▸ Worktree Detail |
| tap | status word (anywhere) | ▾ Why |
| tap | headline line 2 | ▸ Counts |
| tap | `⌗ N updates` pill | apply held reorder |
| tap | active Fleet tab | scroll to top **and** apply held reorder |
| tap | clamped text | expand in place |
| double-tap | — | **unused.** Reserved by the system and by VoiceOver |
| pull down | Fleet, Activity, Limits, Branches | `.refreshable` — force-resync, apply held reorders, reset staleness. **Not on Chat** (§3.3.3) |
| long-press | rows, chips, bubbles | context menu with preview (§6.3) |
| pinch | — | **unused.** §5.13 |
| edge swipe → | pushed screens | system back. Nothing competes with it |

## 6.3 Long-press menus

| Object | Menu |
|---|---|
| **Identity row** | *New mission here* · *Finish…* · *Branch detail* · *Copy path* · *Show ended sessions* · **On studio-mac** ▸ *Send attach command to the Mac*, *Open a terminal there* |
| **Session row** | *Reply* · *Why is this?* · *Session info* · *Arm auto-resume* (limit only) · *Copy session id* · **On studio-mac** ▸ same two |
| **Chat bubble** | *Copy* · *Copy with timestamp* · *Quote in reply* · *Delivery detail* (raw server message for a `⚠` bubble) |
| **Account row** | *Copy config dir* · *Set reserve…* |
| **Activity dispatch row** | *Copy mission text* · *Send attach cmd* · *Chat with this agent* · *Use as a new mission* |
| **Branch row** | *Chat* · *Finish…* · *Pin for compare* (v2) · *Copy branch name* |
| **App icon** | *Who needs me* · *New mission* · *Limits* |

Context-menu actions **never present a confirmation dialog directly** — the menu's dismissal animation swallows it. They set state on the parent, which presents on the next runloop turn.

## 6.4 Haptics

**Rate-limited to one haptic per 2 s, globally.** A budget of "selection on every swipe reveal, light impact on every applied reorder, soft impact on every tier-1 arrival" against a live stream buzzes continuously — and the one that matters, tier-1 arrival (the desktop bell's replacement), is lost in the noise.

Implemented with `.sensoryFeedback`, which is trigger-value driven. Naming the trigger is not pedantry — it forces the question "does this fire every 5 s while offline?"

| Feedback | Trigger value | Rule |
|---|---|---|
| `.selection` | picker/segment change, swipe reveal | — |
| `.impact(.light)` | `heldOrderApplied` (Int counter) | user-initiated apply only |
| `.success` | `dispatchResult.id` (UUID, nil until terminal) | once per job, not per re-render. Also `✓✓` delivered, finish `exit`/`parked`/`noop`, auto-resume armed |
| `.warning` | `serverRefusal.id` | once per distinct refusal. Also `needs_decision`, `effort UNCONFIRMED`, `kickoff UNCONFIRMED`, the moved-board shield firing. **Poll failures do not fire.** |
| `.error` | `transportState` transitioning *into* `.failed` | edge-triggered, so an hour offline is one buzz |
| `.impact(.soft)` | tier-1 attention arriving while foregrounded | fires only on the server's `transitions` array, never on a re-render. The desktop's `maybeBell` rule, made replay-proof |

Nothing on scroll, nothing on poll, nothing on a plain navigation tap. All gated on a Settings toggle and the system setting.

## 6.5 Undo

**Undo is a 5-second snackbar in the bottom accessory** — thumb-reachable by construction — with a single `Undo` button.

```
╔════════════════════════════════════════════════════╗
║  ✕ disarmed ConfidAI3                    Undo      ║
╚════════════════════════════════════════════════════╝
```

It covers exactly the reversible-by-re-issue actions: **Disarm** (re-arms with the same parameters; the schedule endpoint is idempotent by key) and **collapse/expand of a section**. Nothing else gets undo, because nothing else can be undone. Shake-to-undo is not used.

## 6.6 Shortcuts and system integration

| Surface | Entries |
|---|---|
| **App icon quick actions** | *Who needs me* · *New mission* · *Limits* |
| **Control Center controls** (iOS 18+) | *New mission* (opens the composer) · *Who needs me* (opens Fleet). **Neither performs a write** — a Control Center tile is one accidental swipe |
| **App Intents / Shortcuts / Spotlight** | `ShowBoard` · `WhatNeedsMe` (spoken summary: *"three agents need you: ConfidAI-auth has a question, …"*) · `DispatchMission(mission:worktree:account:model:effort:)`. `mission`, `model` and `effort` are **required with no defaults** — that part does mirror the server, which refuses a dispatch without model and effort. `worktree` and `account` are *also* required here, which is **stricter than the server** (both accept `null` for Auto): a Shortcut runs unattended, so the deterministic auto-picker choosing a target nobody looked at is a worse failure than an extra parameter |
| **URL scheme** | `orchestra://board`, `orchestra://worktree/<name>`, `orchestra://session/<account>/<sid>`, `orchestra://mission?text=` (never auto-launches), `orchestra://pair?…` |
| **Share extension** | composes and launches a mission in-extension (§3.5) |
| **Hardware keyboard** | not designed for v1 |

---

# 7. Dangerous actions and the confirmation model

## 7.1 Six principles

1. **Friction is proportional to irreversibility × cost, never to how important the action feels.**
2. **The app never *initiates* a retry of a non-idempotent POST — and cannot fully guarantee that alone.** URLSession may retransmit on a fresh connection when the original dies before any response bytes arrive, and that is **not app-configurable**. Therefore `Idempotency-Key` is a *precondition for shipping* dispatch and finish. When `capabilities` lacks `idempotency`, both actions are **disabled**, not merely warned about.
3. **In-flight locks are server-side.** A client-side lock is defeated by two phones, or by one phone plus the desktop board — which has no locks at all — and that is the exact failure it was defending against.
4. **Confirmations name the object and the consequence**, never "Are you sure?".
5. **A refusal is a state, not a toast** — and it re-verifies itself (§4.4).
6. **Never disguise a side-effecting GET as safe.** `/api/focus` and `/api/limits?refresh=1` are excluded from every prefetch, speculative and retry path.

## 7.2 The matrix

| Action | Endpoint | Spends? | Idempotent? | Friction |
|---|---|---|---|---|
| Reply / send | `POST /api/send` | no | **key-guarded** | none visible; server-side `expect_sid`; visible receipts |
| Resume now | `POST /api/send "continue"` | no | key-guarded | none; disabled until `resets_at` |
| **Dispatch** | `POST /api/dispatch` | **yes** | **key-guarded** | confirmation sheet naming worktree/account/model → server lock → no retry → reconciliation |
| **Force model** | same, `force_model:true` | **yes, into the reserve** | key-guarded | second confirm quoting the reserve %, at a different detent |
| **Kill** | `POST /api/kill` | no | yes | confirmation naming the session; states work in progress is lost |
| **Finish (step 1)** | `POST /api/finish` | maybe | key-guarded + server lock | confirmation sheet, 120 s timeout, no retry |
| **Finish (step 2)** | same | no | benign | lighter sheet |
| Arm / change resume | `POST /api/resume/schedule` | no | **yes** by key | none |
| Disarm | `POST /api/resume/cancel` | no | yes | none; blocked only for the one schedule that may be firing |
| Set reserve | `POST /api/reserve` | no | yes | none; debounced, optimistic, rollback |
| Refresh all accounts | `GET /api/limits?refresh=1` | no | yes | disabled while in flight, labelled `up to a minute` |
| Pasteboard | `POST /api/pasteboard` | no | yes | none |
| **Open a terminal there** | `GET /api/focus` | no | **no** (a window per call) | Session Info / long-press only; 1 per session per 60 s; never retried |

## 7.3 Sheet button geometry

```
        Launch confirm            Force-model confirm
        .height(340)              .height(280)
   ┌────────────────────┐     ┌────────────────────┐
   │   …consequence…    │     │  …consequence…     │
   │ ┌────────────────┐ │     │                    │
   │ │ Launch mission │ │ ◄── primary at y₁        │
   │ └────────────────┘ │     │ ┌────────────────┐ │
   │      (24 pt)       │     │ │ Use fable      │ │ ◄── primary at y₂ ≠ y₁
   │ ┌────────────────┐ │     │ └────────────────┘ │
   │ │    Cancel      │ │     │      (24 pt)       │
   │ └────────────────┘ │     │ ┌────────────────┐ │
   └────────────────────┘     │ │    Cancel      │ │
                              │ └────────────────┘ │
                              └────────────────────┘
```

1. **The safe action is bottom-most.** Putting the irreversible money-spending action in the fattest part of the thumb arc and the safe action below it inverts the whole model.
2. **≥24 pt of dead space** between any two rows whose consequences differ in kind.
3. **Consecutive sheets in one chain never share a detent.** Muscle memory drilled on a safe button must not later land on a reserve-burning one.

## 7.4 The finish button never re-enables on timeout

`POST /api/dispatch` and finish-mode-`dispatch` have zero double-fire protection server-side today: tmux session names embed `%H%M%S`, so a retry ≥1 second later launches a **second** agent in the same worktree — two agents merging and pushing the same branch, two accounts burned. Compounding it, the 4-second state cache plus the ~30 s a new agent takes to register as busy means a fast retry's auto-picker re-selects the same "free" worktree.

So: the sheet's action button disables on tap and **does not re-enable on timeout**. Recovery is via reconciliation, never a re-enabled button. And a timeout is never rendered as "failed" — it is rendered as *"no answer in 2 minutes. The closeout brief may already have been sent — check the session before trying again."* Rendering a timeout as failure is the most dangerous possible message, because the brief was very likely typed.

## 7.5 What must NOT have friction

Reading anything · replying · arming or disarming auto-resume · adjusting reserve · switching tabs · expanding sections · pull-to-refresh · dismissing a notification. All reversible; the app's value is seconds-to-act.

---

# 8. Glanceability — widgets, Live Activities, notifications

## 8.1 App component inventory

| Target | Why it exists |
|---|---|
| **App** (SwiftUI) | everything in §3 |
| **Widget extension** | Home Screen + Lock Screen widgets, Live Activities, Control Center controls |
| **Notification Service Extension (NSE)** | **two jobs, both mandatory**: (a) fetch notification body text over the tailnet so transcript prose never touches APNs; (b) write fleet counts into the App Group and call `WidgetCenter.shared.reloadAllTimelines()` *before* the alert renders — this is how widgets stay fresh |
| **Share extension** | composes a mission **inside the extension** (§3.5) |
| **App Group** `group.orchestra.fleet` | shared container: last snapshot, counts, drafts; credentials in the shared Keychain access group |

## 8.2 Push mechanism, and the key-distribution problem

Python's stdlib has no ECDSA and no HTTP/2 client. The server shells out to two binaries macOS ships:

```python
signing_input = b64url(header) + b"." + b64url(claims)
der = subprocess.run(["openssl","dgst","-sha256","-sign",p8,"-"],
                     input=signing_input, capture_output=True).stdout
sig = der_to_jose_p256(der)   # DER SEQUENCE{INTEGER r, INTEGER s} → raw r||s, 64 bytes.
                              # THE sharp edge: strip leading 0x00 padding, left-pad each
                              # to exactly 32 bytes. ~25 lines, fully unit-testable.
jwt = signing_input + b"." + b64url(sig)

subprocess.run(["curl","--http2","-s","-o","/dev/null","-w","%{http_code}",
                "-H", f"authorization: bearer {jwt}",
                "-H", f"apns-topic: {bundle_id}",
                "-H", f"apns-push-type: {push_type}",   # alert | liveactivity | background
                "-H", f"apns-priority: {prio}",
                "-H", f"apns-collapse-id: {collapse}",
                "--data-binary", payload,
                f"https://api.push.apple.com/3/device/{token}"])
```

`der_to_jose_p256` gets its own test vectors. The JWT is cached and **rotated at 40 minutes** — Apple requires reuse for 20–60 minutes and answers faster regeneration with `429 TooManyProviderTokenUpdates`. (40, not 45, to agree with `ARCHITECTURE.md` §6.1 and `ROADMAP.md` M9.)

**The key-distribution question determines whether this app can be distributed at all.**

| model | verdict |
|---|---|
| ship the developer's `.p8` inside a self-hosted python file | **catastrophic.** That key can push to every app on the team, and the file lands on every user's disk. Rejected. |
| a relay service holds the key | breaks the "no public internet exposure" premise. Rejected. |
| **each user supplies their own `.p8` + Team ID + Key ID + bundle ID** | **chosen.** |

Consequence, stated plainly: **this app cannot be App Store distributed.** Each user needs a paid Apple Developer account, their own bundle ID, their own APNs key, and a personal build or TestFlight.

**Therefore ntfy.sh is a first-class sink, not an afterthought.** The sink interface is pluggable; both sinks consume the same `transitions` entries and the same per-device preference records. ntfy loses interactive Reply, Live Activities, badge counts and the NSE body-fetch — so ntfy pushes are title-only by construction, which is the privacy-preferring default anyway. The app detects the sink from `/api/hello` `capabilities` and hides what does not apply.

## 8.3 Live Activities

> **v1 ships exactly one Live Activity: the auto-resume / limit-reset countdown (c).** The mission-launch (a) and closeout (b) activities below are **deferred**, agreeing with `ROADMAP.md` M11 and `IOS-APP.md` §6.6. The reasoning that wins: a dispatch is a 10–20 s job you are usually watching in the foreground; driving `①…⑤` costs six ActivityKit pushes against a scarce budget for a progress bar nobody is looking at, and on a device with no Dynamic Island it is invisible anyway. **Terminal states become one ordinary alert** (`dispatch.succeeded` / `dispatch.failed`, §8.6), and foreground progress renders in the composer from the op's frames. (a) and (b) remain specified here because if the budget question is ever settled the other way, this is the design — but they are not in the v1 cut.
>
> Note the sequencing consequence: dispatch lands in `ROADMAP.md` M7 and Live Activities in M11, so a dispatch Live Activity could not have shipped with dispatch regardless.

**Every Live Activity transition is an ActivityKit APNs push generated by the server.** iOS gives a backgrounded app no periodic execution: there is no 3-second background timer, and `BGAppRefreshTask` is opportunistic on the order of tens of minutes. A polling model freezes `③ booting claude…` the moment the phone locks — the design's own worst case, hit on the ordinary path. This is why (c) uses `Text(timerInterval:)`, which the system renders with **no pushes at all** for the activity's whole life.

```
POST /api/activities
{ "intent_id": "int-7f3a",
  "update_token": "…",     // ActivityKit push token, per-activity
  "start_token": "…" }     // push-to-start token (iOS 17.2+), posted once at launch
```

The server's dispatch already logs `①②③④⑤`. Each log call additionally enqueues an ActivityKit push (`apns-push-type: liveactivity`, `apns-priority: 10`, topic `<bundle>.push-type.liveactivity`). No client polling anywhere.

Foreground: consume intent frames off the stream. **On foreground return**, poll the intent **once**; if it is gone, do **not** report failure — go to Reconciliation.

### (a) Mission launching

```
Dynamic Island compact:     ⌁ │ ③

Lock Screen:
┌──────────────────────────────────────────────┐
│ ⌁  mission → ConfidAI-ci                     │
│    [acct2] · opus · effort xhigh             │
│    ③ booting claude…                         │
│    ▓▓▓▓▓▓▓▓░░░░░░░░░░░░  step 3 of 5         │
└──────────────────────────────────────────────┘

terminal states:
┌──────────────────────────────────────────────┐
│ ✓  launched · on the board in ~30s           │
│              [ ⧉ send attach cmd to Mac ]    │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│ ⚠  launched, kickoff unconfirmed —           │
│    attach and press Enter                    │   ← does NOT auto-dismiss
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│ ✗  not launched — tmux failed                │
│              [ reopen the draft ]            │
└──────────────────────────────────────────────┘
```

The `⚠ unconfirmed` variant is a to-do, not a status, so it persists until dismissed.

### (b) Closeout running

Identical machinery for finish's `dispatch` tier, ending `◇ ConfidAI-auth is free` or `⚠ closeout couldn't verify a clean landing`.

### (c) Auto-resume armed — gated on horizon

```
if due_at - now < 6h  →  Live Activity with Text(timerInterval:)   // ticks locally, no network
else                  →  UNCalendarNotificationTrigger at due_at   // survives force-quit
                         + the widget's ARMED block
```

**iOS ends a Live Activity after ~8 hours of active time (~12 h on the Lock Screen), and weekly caps reset up to 7 days out.** A days-out weekly reset cannot be represented, and the activity would silently vanish hours before firing, reading as "the schedule was lost".

The Auto-resume sheet **says which one you are getting**: `you'll get a Lock Screen countdown` vs `you'll get a notification at 14:33 on Tuesday`. The `⏱ firing now…` state lives in the app and the widget, never only in an activity.

```
┌──────────────────────────────────────────────┐
│ ⛔  ConfidAI3 · [acct8]                       │
│     Weekly limit                             │
│     resumes in            2:38:11            │   ← Text(timerInterval:)
└──────────────────────────────────────────────┘
```

## 8.4 Notification tiers

Tiers are a *presentation* of `API.md` §10.9's `event.type` / `event.level` — they are not a second taxonomy. The mapping, and the two places this document previously invented its own vocabulary:

| Tier | `event.type` (`API.md` §10.9) | Badge | Push default | Interruption level |
|---|---|---|---|---|
| **1 — needs you now** | `session.needs_answer`, `session.blocked`, `resume.failed`, `dispatch.failed` | ✅ (first two only) | ✅ on | `.timeSensitive` |
| **2 — your turn** | `session.your_turn`, `session.idle_nudge` | ❌ | ⛔ off by default; opt-in | `.active` |
| **2 — worth knowing** | `session.died`, `account.limit_hit`, `resume.fired`, `dispatch.stalled` | ❌ | ✅ on, `limit_hit` deduped per `(account, group)` once per episode | `.active` |
| **3 — quiet** | `account.limit_reset`, `dispatch.succeeded`, `finish.landed` | ❌ | ✅ on, no sound | passive |
| **4 / suppressed** | `worktree.free`, `resume.armed`, `session.unstable`; and **any** `limit` carrying `handed_to`, which §10.9 suppresses at source | never | off | — |

Two corrections to earlier drafts of this section. **`closeout_refused` is not an event type** — a `mode:"pending"` refusal is a *state* rendered as the self-clearing amber row in §4.4, and pushing it would alert on something the user just did. And **tier 2's "10 min dwell" is not a server dwell**: `API.md` §10.9 fires `session.your_turn` at the event level with a short dwell so the log is complete, then `session.idle_nudge` after `nudge_min` for an unacknowledged one. The 10 minutes is a **per-device preference** on the nudge, not a delay on the underlying event — which matters because the event log also drives the badge and the foreground haptic, and delaying it there would desynchronise all three.

**Never `.critical`.** This is a dev tool. Tier 1 requires the `com.apple.developer.usernotifications.time-sensitive` entitlement, and it is the difference between reaching the user through a Focus mode and not.

**Tier 2's dwell must not be measured from mtime.** Measured on real data: a session reported `mtime_age` 1,779 s against a true `evidence_age` of 219,803 s — 30 minutes claimed, 2.5 days real. Dwell is computed from `evidence_at` with `evidence_source ∈ {hook, transcript}`. When `evidence_source == "mtime"`, tier 2 does not fire at all and the UI renders `idle ≥ Nh` rather than a precise figure. Same rule for every displayed age.

Edges come from the server's `transitions` array, not client arithmetic — which is what makes the false-bell class structurally impossible and the edge push-able with no client attached. `handed_to` explicitly means "work already continued elsewhere"; alerting on it is the classic false positive.

## 8.5 Notification payload and the privacy trade

**The push payload carries identifiers only; the NSE fetches the body over the tailnet.** The project's premise is "no public internet exposure", and the server itself warns that binding wider "serves your transcript text to the network". Routing that same text through APNs would be a direct contradiction.

```jsonc
{ "aps": { "alert": {"loc-key": "NEEDS_ANSWER", "loc-args": ["ConfidAI-auth"]},
           "mutable-content": 1, "badge": 2, "sound": "default",
           "thread-id": "studio-mac|ConfidAI-auth",
           "interruption-level": "time-sensitive",
           "relevance-score": 1.0 },
  "o": { "server": "studio-mac", "wt": "ConfidAI-auth",
         "sid": "9b8ef2d1-…", "acct": "work",
         "kind": "needs_input", "at": 1784636700.4, "v": 41207 } }
```

The NSE calls **`GET /api/v1/events/{id}`** (`API.md` §9.22), substitutes the real text, and writes counts into the App Group. If the tailnet is down the notification renders title-only — exactly what you want when the tailnet is down.

**Counts ride *in* the payload; only the prose is fetched.** `ARCHITECTURE.md` §7.3 names the case that forces this: with Tailscale in on-demand mode scoped to the app, the NSE **cannot reach the Mac at all**, and §8.9 makes widget freshness depend on the NSE writing counts into the App Group on every push. So the `o` block carries `counts` alongside the identifiers, the NSE writes them unconditionally, and the network call is only ever an *enrichment* that may fail. A design where counts are fetched has a widget that silently stops updating for every user who did not choose always-on VPN.

`Server → Notifications → Message previews`: **Fetch on device** (default) · **Never — titles only** · **Include in the push** (with the trade stated).

`at` is absolute, so the phone renders "asked 40 s ago" correctly even when Apple delayed delivery. `thread-id = "{server}|{worktree}"`, summary `%u more from ConfidAI-auth`.

**Titles carry no leading symbol.** A `▲` in the title makes VoiceOver speak "up-pointing triangle" and Braille displays render the raw codepoint before *every* alert — a mandatory noise token imposed on exactly the population the glyph rule serves, adding nothing, because the word is already in the sentence.

## 8.6 Exact notification copy

| kind | title | subtitle | body (NSE-fetched) |
|---|---|---|---|
| `needs_input` | `ConfidAI-auth needs an answer` | `NEEDS ANSWER · [work] · opus-4-8` | *"I can take either approach — do you want the JWT rotated per-request or per-session?"* |
| `blocked` | `ConfidAI-api is blocked on Bash` | `BLOCKED · [acct2] · sonnet` | `⧗ waiting on: Bash, Edit — usually a permission prompt at its terminal.` |
| `limit` | `[work] hit its Weekly cap` | `3 agents parked · resets 14:32` | `ConfidAI3, ConfidAi5, ConfidAi8 are waiting. Arm auto-resume to continue automatically.` |
| `dispatch_done` | `Mission launched in ConfidAI-ci` | `[acct2] · opus · effort xhigh` | `tmux -L fleet attach -t mission-confidai-ci-121030` |
| `dispatch_done` (unconfirmed) | `Launched, but the kickoff is unconfirmed` | `ConfidAI-ci · [acct2]` | `Attach and press Enter — the brief may not have been submitted.` |
| `dispatch_failed` | `Mission not launched` | `ConfidAI-ci` | `tmux failed: duplicate session. Your draft is saved.` |
| `resume_fired` | `Resumed ConfidAI3` | `[acct8] · sent via tmux` | `"continue" was typed into the session's terminal.` |
| `resume_failed` | `Auto-resume couldn't reach ConfidAI3` | `[acct8]` | `Reopened in tmux but "continue" never reached the conversation — attach and type it.` |
| `freed` | `ConfidAI-auth is free` | `branch landed on origin/main` | `The closeout verified a clean landing and closed the terminal.` |
| `closeout_refused` | `ConfidAI-auth can't close yet` | `3 leftover file(s)` | `The closeout brief went to the agent 6m ago. If it looks stuck, chat with it.` |

**Actions per category:**

| category | actions |
|---|---|
| `orc.needsInput` | **Reply** (`UNTextInputNotificationAction`, placeholder `answer the agent…`) · **Open** |
| `orc.blocked` | **Open** |
| `orc.limit` | **Arm auto-resume** (1 min after reset) · **Open** |
| `orc.dispatch` | **Send attach cmd** · **Open** |
| `orc.freed` | **New mission here** · **Open** |

Banner as rendered:

```
┌──────────────────────────────────────────────────┐
│ ⌁  orchestra                                 now  │
│    ConfidAI-auth needs an answer                 │
│    NEEDS ANSWER · [work] · opus-4-8              │
│    I can take either approach — do you want the  │
│    JWT rotated per-request or per-session?       │
│  ┌────────────────────────────────────────────┐  │
│  │  answer the agent…                         │  │  ← pull down to reply
│  └────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────┘
```

## 8.7 The badge

Pushes fire on increase, so without an explicit rule the badge would sit at 2 forever after you cleared attention at the Mac.

**Every tier-1/tier-3 transition — including decreases — sends an alert-class push carrying `badge`**, with `alert` omitted and `apns-priority: 5` for pure decrements. A payload with a badge and no alert *is* an alert-type push, so it is not subject to silent-push throttling; it simply renders nothing. `collapse-id` is `badge` so decrements coalesce.

Badge value = `counts.sessions.needs_input + counts.sessions.blocked` — tier 1 only. `waiting` is deliberately excluded, unlike the desktop's `attn`.

## 8.8 Notifications state machine

| state | row | cause | remedy |
|---|---|---|---|
| `off` | `○ Notifications off` | user choice | ‹Turn on› |
| `not_configured` | `⚠ No push sink configured` | no `.p8`, no ntfy topic | ‹Set up› |
| `key_invalid` | `✗ APNs key rejected — {reason}` | `InvalidProviderToken`, `ExpiredProviderToken`, unreadable `.p8` | ‹Re-run --push-setup› |
| `unregistered` | `⚠ This iPhone isn't registered` | app reinstalled, token never posted | ‹Register now› |
| `token_rejected` | `✗ Apple stopped accepting this device (410)` | `Unregistered` / `BadDeviceToken` — the server **must** delete the token on 410 or it retries forever | ‹Register now› |
| `delivering` | `● Delivering · last push 14:02` | ≥1 accepted push in 24 h | ‹Send a test› |
| `silent` | `⚠ Nothing pushed in 3 days` | registered, zero sends | ‹Send a test› |

The `silent` state distinguishes "quiet fleet" from "broken pipeline", which is otherwise indistinguishable and is the failure users discover a week late.

**Per-device preferences are server-side state.** Delivered pushes cannot be filtered on-device, so the eight toggles in §3.8 would otherwise be decorative.

```jsonc
// POST /api/devices   (authenticated; idempotent on `token`)
{ "token": "a1b2…", "kind": "apns", "name": "achill's iPhone",
  "bundle_id": "com.acrdlph.orchestra", "tz": "Europe/Berlin",
  "prefs": {"needs_input": true, "blocked": true, "waiting": false,
            "limit": true, "dispatch": true, "resume": true, "freed": false},
  "quiet_hours": {"from": "22:00", "to": "08:00"},
  "previews": "fetch_on_device" }
```

**Quiet hours are evaluated in the device's `tz`**, which the phone posts and re-posts on timezone change. The server's only time formatting is naive local, so evaluating 22:00 against the Mac's clock would be wrong whenever the user travels.

`DELETE /api/devices/{id}`, plus `Server → Machines → studio-mac → Unpair this iPhone`, plus `./start.sh --devices` to list and revoke from the Mac when the phone is lost.

**Permission is asked at the end of push setup, or the first time tier-1 attention appears while the app is open — never at launch.**

## 8.9 Widgets

**Freshness comes from the NSE, not from silent pushes.** Silent `content-available` pushes are budgeted, dropped in Low Power Mode, and **never delivered after a force-quit**; WidgetKit timeline reloads have their own daily budget. The NSE runs on every real push — including badge-only decrements — writes counts into the App Group, and reloads timelines **before** the alert renders. The widget is fresh exactly when something happened. The 15-minute timeline is best-effort decoration on top.

### Small — "Who needs me"

```
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ ⌁ orchestra   │   │ ⌁ orchestra   │   │ ⌁ orchestra   │
│              │   │              │   │              │
│      2       │   │      ●       │   │      —       │
│  need you    │   │  all clear   │   │ can't reach  │
│              │   │              │   │  studio-mac  │
│ ConfidAI-auth│   │ 3 busy       │   │              │
│ ConfidAI-api │   │ 4 free       │   │              │
│ as of 14:02  │   │ as of 14:02  │   │ as of 13:14  │
└──────────────┘   └──────────────┘   └──────────────┘
```

### Medium — "Fleet"

```
┌──────────────────────────────────────────────┐
│ ⌁ orchestra · studio-mac          as of 14:02 │
│                                              │
│   ▲ 2 need you   ● 3 busy   ⛔ 1   ◇ 4 free  │
│ ──────────────────────────────────────────── │
│ ▲ ConfidAI-auth   NEEDS ANSWER   [work] 2m   │
│ ■ ConfidAI-api    BLOCKED        [acct2] 6m  │
│ ⛔ ConfidAI3       LIMIT · 2h38m  [acct8]     │
└──────────────────────────────────────────────┘
```

Each row deep-links to that session's Chat.

### Large

Medium plus up to 6 rows plus an **ARMED** block listing pending auto-resumes and their fire times — also where a >6 h auto-resume lives (§8.3c).

```
│ ──────────────────────────────────────────── │
│ ARMED                                        │
│ ⏱ ConfidAI3 · [acct8]        Tue 14:33       │
│ ⏱ orbital-api · [work]       Fri 09:00       │
```

### Lock Screen accessories

- **Circular** — ring gauge of tier-1 over a ceiling of 5, `▲2` centred; empty ring + `●` when clear.
- **Rectangular** — `▲2 · ●3 · ⛔1` / the freshest needs-you name / `14:02`.
- **Inline** — `▲ 2 agents need you` / `● fleet is clear` / `◯ orchestra unreachable`.

### Two rules

**The honesty rule.** Every widget prints `as of HH:MM`. Older than 10 minutes, **the number is replaced by `—` and `can't reach studio-mac`.** A widget confidently showing `0 need you` from stale data is the worst failure this app can have. Showing a dash is fine.

**The tinted rule.** iOS renders Home Screen widgets in a **forced monochrome tint** on demand, which erases the five-hue code exactly the way protanopia does, on the surface most likely to be glanced at. The tinted variant carries **no colour at all** — symbol + word only, with `.widgetAccentedRenderingMode(.accented)` on the status symbol so it takes the user's tint and the text stays neutral. This is where "colour is never the only channel" pays for itself.

---

# 9. The visual design system

orchestra's identity is **a terminal that grew a face**: near-black canvas, one monospaced face, hairline borders, five hues that *mean* something. iOS's identity is large type, generous targets, Liquid Glass chrome, system gestures, Dynamic Type. Four conflicts, four rules:

| Conflict | Rule |
|---|---|
| Mono everywhere vs. iOS reading comfort | **Mono is for machine tokens; SF for human language.** The desktop already declares two voices (`--mono` / `--sans`) resolving to one stack, with a comment offering the swap. iOS takes the offer; the `--sans` selector list *is* the specification. |
| 10–12 px micro-labels vs. the 11 pt floor and Dynamic Type | Keep the hierarchy, raise the floor: 10/11/12/13/14/18/26 → 11/12/13/15/17/20/34, every step bound to a real text style. Density is recovered by **dropping columns** at large sizes, not by shrinking type. |
| Hairline borders + hover vs. 44 pt targets and no hover | **Borders stay 1 px; the target grows around them — and it costs real layout space.** Most of what looks tappable on the desktop is not a control at all on iOS. |
| Flat near-black surfaces vs. **Liquid Glass** chrome | **Accept glass on chrome, own the content.** Convergence, not conflict: the desktop header is already `backdrop-filter: blur(8px)`. Nav bar, tab bar, accessory and toolbars take the system material; scroll content stays flat. Sheets get `.presentationBackground(Color.orc.surface)`. |

## 9.1 Three themes, one mechanism

| Theme | Mechanism | Purpose |
|---|---|---|
| **Night** (default) | Asset Catalog `Dark` appearance | the product |
| **Daylight** | Asset Catalog `Any` (light) appearance | a *legibility mode*, not a second brand: sunlight, halation relief for astigmatism — and, automatically, every out-of-process surface rendered in light appearance |
| **Contrast+** | `High Contrast` variants of both | driven by the OS Increase Contrast setting **or** forced in-app |

**Daylight is not optional.** Notification banners, notification content extensions, widgets, Live Activities on the Lock Screen, and the launch screen render in the **OS** appearance; `.preferredColorScheme` does not reach them. Once you compute a light status set for those — and you must — a real light theme costs almost nothing. Separately, dark UI degrades far worse under ambient reflection: reflected luminance adds equally to foreground and background, so a measured 15:1 on `#0D0D0D` collapses toward ~2:1 in direct sun while a light canvas barely moves.

```swift
@main struct OrchestraApp: App {
    @AppStorage("appearance") private var appearance: Appearance = .night
    @AppStorage("forceContrast") private var forceContrast = false
    var body: some Scene {
        WindowGroup {
            RootView()
                .preferredColorScheme(appearance.colorScheme)   // .dark / .light / nil
                .orcForceContrast(forceContrast)                // see below
                .accessibilityIgnoresInvertColors(true)         // §10.3
        }
    }
}
```

**`orcForceContrast` is not a stock modifier, because none exists.** There is no SwiftUI `.traitOverride(contrast:)`, and `ColorSchemeContrast`'s cases are `.standard` / `.increased` — there is no `.high`. Setting `.environment(\.colorSchemeContrast, .increased)` changes the value views *read* but does **not** re-resolve Asset Catalog colours, which the OS resolves from the real `UITraitCollection`. The only mechanism that actually flips the catalog is UIKit's `UITraitOverrides` (iOS 17+), so the modifier is a four-line `UIViewControllerRepresentable` wrapper setting `traitOverrides.userInterfaceStyle`-adjacent `accessibilityContrast = .high` (the **UIKit** enum, where `.high` *is* the correct spelling — the two enums do not share names, which is exactly how this got written wrong). Honouring the OS setting needs no code at all; only the in-app force does.

Every colour is `Color("token")` from the Asset Catalog, which carries four variants of each: Any, Dark, Any/High-Contrast, Dark/High-Contrast. **There is no palette struct, no protocol, no environment palette, and no ternary at any call site.** That is what makes the high-contrast variant impossible to apply only 60 %.

**Daylight Auto:** when screen brightness exceeds 0.92 for ≥8 s while in Night, a one-line non-modal banner offers *"bright light — switch to Daylight?"* with Switch / Not now / Never (sticky). **No automatic switching** — a theme that changes under you while you read a limit countdown is worse than glare.

## 9.2 Colour tokens with measured contrast

All ratios are computed (WCAG 2.x relative luminance, sRGB alpha compositing) and **asserted in `ColorContrastTests`**, which resolves each named colour against each trait combination and fails the build on regression.

### Surfaces

| Token | Night | Daylight | Contrast+ Night | Role |
|---|---|---|---|---|
| `canvas` | `#0D0D0D` | `#F4F2EF` | `#000000` | scroll background |
| `sunken` | `#111111` | `#E9E6E2` | `#0A0A0A` | inset wells: session rows, text fields, code blocks |
| `sunkenDim` | `#0E0E0E` | `#EDEBE7` | `#050505` | the `ended` row ground |
| `surface` | `#161616` | `#FCFBFA` | `#141414` | cards, **sheets**, list containers |
| `raised` | `#1C1C1C` | `#FFFFFF` | `#1E1E1E` | card headers, chips, your chat bubble |
| `overlay` | `#202020` | `#FFFFFF` | `#242424` | menus, popovers, toasts — **leaf tier only** |

Two enforceable layering rules:

1. A child surface is lighter than its parent (Night) / whiter (Daylight), except `sunken`/`sunkenDim`, the one inset direction, only ever direct children of `surface` or `raised`.
2. **`overlay` is a leaf: nothing containing a chip, pill or bubble may sit on it.** Sheets therefore sit on `surface`, so `raised` chips inside them still layer correctly. (Sheets on `overlay` make every chip inside *darker* than its parent at 1.05:1 — invisible, in the finish confirmation sheet of all places.)

### The body wash

The desktop's `radial-gradient(1200px 500px at 70% -10%, rgba(217,119,87,.05), transparent 60%)` is a 2.4:1 ellipse. `RadialGradient` is circular and cannot express it; `EllipticalGradient` can, anchored to the screen and not to scroll content, with `.allowsHitTesting(false)`.

Measured consequence: accent at 5 % composites `#0D0D0D` → `#171211`, costing the tightest text tier 0.24 (`textTertiary` 5.44 → **5.20**, still AA). No informational text sits on washed canvas except the empty-state copy, and 5.20 clears it.

### Borders

| Token | Night | Daylight | Use |
|---|---|---|---|
| `hairline` | `#2A2A2A` | `#DCD8D3` | decorative dividers, card outline, row separators — 1.26:1, **decorative only** |
| `control` | `#3A3A3A` | `#B3AEA8` | outline of controls that also carry a text label — 1.59:1 |
| `controlStrong` | `#6A6A6A` | `#8A857F` | outline of any control identified **only** by its border or a symbol |

The rule: *every tappable control is identifiable by (i) a text label at ≥4.5:1, or (ii) a `controlStrong` border, or (iii) a fill at ≥3:1.*

`controlStrong` is validated against the **lightest** surface it may appear on — for a light-on-dark token that is the worst case:

| candidate | on `surface` | on `raised` | on `overlay` |
|---|---|---|---|
| `#636363` | 3.01 | 2.84 ✗ | 2.71 ✗ |
| **`#6A6A6A`** | **3.35** | **3.15** ✓ | **3.01** ✓ |

Because `overlay` is a leaf tier with no controls, the true worst case is `raised` at 3.15. Daylight `#8A857F` on `#FFFFFF` = 3.66 ✓.

### Text

| Token | Night | canvas | sunken | surface | raised | overlay |
|---|---|---|---|---|---|---|
| `textPrimary` | `#E8E6E3` | 15.60 | 15.16 | 14.53 | 13.68 | 13.08 |
| `textSecondary` | `#A8A4A0` | 7.85 | 7.63 | 7.31 | 6.88 | 6.58 |
| `textTertiary` | `#8A8784` | 5.44 | 5.29 | 5.07 | 4.77 | 4.56 |
| `textDisabled` | `#6A6764` | 3.46 | 3.36 | 3.22 | 3.03 | 2.90 |

Daylight `#171614` / `#4E4B47` / `#68645F` / `#8E8A85` → primary 14.5–18.1, secondary 6.97–8.67, tertiary **4.72–5.87**, disabled 2.76–3.43.
Contrast+ Night `#F5F3F1` / `#C9C6C2` / `#B4B0AC` / `#8A8784` → tertiary **7.91 on raised, 7.56 on overlay** (AAA).

`textSecondary` is new — the desktop has no tier between `--fg` and `--muted`, and on a phone the human-voice prose needs to be *readable*, not *quiet*. `textDisabled` is the desktop's `--muted-2` **demoted**: it fails AA on every surface, yet on the desktop it carries the model name and age on every session row at 11 px / 3.0:1. It is named `textDisabled` precisely so reaching for it to style a timestamp feels wrong.

### Status hues

| Token | Night | Daylight | Contrast+ Night | Meaning | on `surface` | on `raised` |
|---|---|---|---|---|---|---|
| `statusNeeds` | `#D97757` | `#9A553E` | `#EB8F6F` | NEEDS ANSWER · BLOCKED · NEEDS YOU · errors | 5.80 | 5.46 |
| `statusTurn` | **`#EDB9AC`** | `#7D615B` | `#F2C7BC` | YOUR TURN (idle @ prompt) · commit hash | 10.46 | 9.85 |
| `statusWorking` | `#87B386` | `#536E53` | `#A3CDA2` | WORKING · BUSY · armed · ok | 7.60 | 7.16 |
| `statusFree` | `#7FB3C8` | `#4D6C79` | `#9CCFE4` | FREE · identifiers: `[account]`, branch, tty, paths | 7.91 | 7.45 |
| `statusLimit` | `#D4B06A` | `#79653D` | `#E6C684` | LIMIT HIT · WAITING · dirty Δ · caution | 8.80 | 8.29 |

Every Night value clears AA as small text on every surface (tightest: `statusNeeds` on `raised` at 5.46). Daylight is validated on its *darkest* ground (`sunken`, 4.51–4.53) and runs 5.4–5.6 on `raised`. Contrast+ Night runs 7.05–11.09 on `raised`, all AAA.

`statusTurn` is **changed from the desktop's `#E8A87C`** on measured colour-vision grounds — §10.3.

### Tint fills

Badge and pill backgrounds are the status colour **composited over the parent surface** — not `.opacity()` on a view, which would fade the border too.

Status text on its own fill, worst pair (always `needs`):

| α | on `surface` | on `raised` | on `overlay` |
|---|---|---|---|
| 0.10 | 5.09 | 4.78 | 4.53 |
| **0.12** | **4.95** | **4.62** ✓ | 4.43 ✗ |
| 0.16 | 4.64 | 4.33 ✗ | 4.13 ✗ |
| 0.25 (the desktop's `.badge.attention`) | **3.99 ✗** | **3.72 ✗** | 3.56 ✗ |

**α = 0.12 is a hard ceiling.** And note the second finding: the desktop currently ships accent at 0.25 behind accent text on its highest-priority badge, at 3.72:1 — a live AA failure worth back-porting.

| theme | tint α | why |
|---|---|---|
| Night | **0.12**, on `surface` and `raised` only | worst pair 4.62 ✓ |
| Night Contrast+ | **0.00 — no fill** | at α 0.20 the needs pair is 4.93, nowhere near the AAA the mode exists for. On bare `raised` the HC hue is 7.05 ✓, and the badge's identity already comes from its solid stroke plus its coloured word |
| Daylight | **0.00 — no fill** | every alpha from 0.10 up yields 3.96–4.45. No safe value exists |

Two glow tokens survive for the level-2 shadow only, never behind text: `glowAccent = #D97757 @ 0.25`, `glowWorking = #87B386 @ 0.18`.

### Opacity is not a channel

Faithful ports of the desktop's de-emphasis CSS, composited:

| desktop state | composited | ratio |
|---|---|---|
| `ended` row: textTertiary @ 0.55 on `sunken` | `#545250` | **2.43** ✗ |
| `handed_to` row @ 0.70 on `sunken` | `#666462` | **3.20** ✗ |
| `.guess` proc chip @ 0.75 on `raised` | `#6E6C6A` | **3.26** ✗ |
| disabled primary: canvas label @ .45 on needs-fill @ .45 | `#121212` on `#6E4233` | **2.22** ✗ |

An `ended` row still carries model, account, age and topic. A `handed_to` row is the one that *explains why an alarm was suppressed*. And the disabled primary is the composer's Launch button, disabled by default — so it is the first thing every user sees in that sheet.

> **Rule: never apply `.opacity()` to a container whose children carry text.**

| state | replacement | ratio |
|---|---|---|
| `ended` row | ground `sunkenDim`, rail `textDisabled`, text `textTertiary` | **5.40** ✓ |
| `handed_to` row | rail `control`, status symbol `textTertiary`, `↳ continued by [x]` at full `statusWorking` | 5.29 ✓ |
| `.guess` / `ambiguous` chip | dashed `control` stroke (`StrokeStyle(lineWidth:1, dash:[3,2])`), text stays `textTertiary` | 4.77 ✓ |
| disabled primary | `raised` fill, `control` stroke, `textTertiary` label | **4.77** ✓ |

Opacity survives in exactly two non-textual places: the pressed state (`.opacity(0.82)` on a whole control, momentary) and the level-1 ring.

## 9.3 Type

### Two voices

**Mono:** worktree name, branch, **commit hash** (the server's `--short` honours `core.abbrev` — 8 and 9 chars observed live, so alignment is only possible in mono), `[account]`, model, `sid`, pid, tty, `etime`, `%cpu`, ages, countdowns, `↑ahead ↓behind`, `Δdirty`, counts, percentages, status words, badges, buttons, progress lines, attach commands.

**SF Pro:** topic, `last_assistant`, `last_user`, `subagent_said`, `handed_to`, chat bubbles, notes, dispatch results, tile subtitles, control labels, mission text, toasts. All natural language.

One carried-over exception: headline numbers use **sans** at display size, because proportional figures read better than tabular mono digits at 34 pt.

`.monospacedDigit()` is **not used** — it applies a font-feature descriptor a custom face may not expose, with known cases of resolving to the system font. The mono face is monospaced by construction.

### The ramp

| Token | Face | Base | Weight | Text style | Tracking | Use |
|---|---|---|---|---|---|---|
| `display` | SF Pro | 34 | semibold | `.largeTitle` | −0.4 | headline numbers only |
| `title` | SF Pro | 20 | semibold | `.title3` | 0 | sheet titles, section heads |
| `cardName` | Mono | 17 | semibold | `.headline` | 0 | worktree name |
| `body` | SF Pro | 17 | regular | `.body` | 0 | mission composer, chat bubbles |
| `bodyCompact` | SF Pro | 15 | regular | `.subheadline` | 0 | topic, last_assistant, last_user, notes |
| `code` | Mono | 15 | regular | `.subheadline` | 0 | branch, path, attach commands, progress lines |
| `codeSm` | Mono | 13 | regular | `.footnote` | 0 | commit subject, session identifiers |
| `meta` | Mono | 12 | regular | `.caption` | 0 | age, model, tty, etime, pid, %cpu |
| `label` | Mono | 11 | semibold | `.caption2` | **.08em** | UPPERCASE micro-labels |
| `status` | Mono | 12 | semibold | `.caption` | **.08em** | status words, availability badges |
| `button` | Mono | 15 | semibold | `.callout` | 0 | all button labels |

`status` is **12 pt, not 10** — the densest, most-glanced element on the board; on a phone it goes up.

**Everything uses `relativeTo:`.** A fixed-size initialiser does not participate in Dynamic Type at all, and at AX5 the 12 pt `meta` would render larger than the 20 pt `title` above it, inverting the hierarchy.

**Tracking scales.** A constant computed from the shipped size means an 11 pt label at AX5 (~28 pt) carries .03em instead of .08em — the tracking vanishing exactly where "uppercase mono without tracking reads as a wall" bites hardest.

**Line height is not in the ramp.** SwiftUI has no line-height API; `.lineSpacing` is additive to the font's own leading and the correct value changes with every Dynamic Type size. A column of numbers nobody can implement is worse than no column. Leading is system default except in two places, both `@ScaledMetric`: chat bubbles (`+3`) and the mission composer (`+4`).

**Clamps** (View modifiers, not font properties): `display` clamps at `.accessibility3`; `status` badges have a *floor* of `.large`; everything else is unbounded.

### What is dropped at each Dynamic Type step

| size | session `row1` carries |
|---|---|
| ≤ `.xxLarge` | status · account · model · age · subdir · branch (if ≠ card's) · subagent tag |
| `.xxxLarge` | drop `model` |
| `.accessibility1–2` | drop `branch`; **1** inline session per card |
| `.accessibility3–5` | status · account · age only; subagent tag on its own line |

`Δ7 ↑3 ↓0` expands to words at `.accessibility1`+: `7 uncommitted · 3 ahead`.

### Bold Text

`Font.custom(_:size:relativeTo:)` does **not** respond to `UIAccessibility.isBoldTextEnabled`. With Bold Text on, the SF human voice goes bold and the entire mono machine voice — worktree names, every status word, every badge, every button, every timestamp — does not. That is not "no benefit"; it is an inverted hierarchy where quiet prose outweighs status words.

Four weights are bundled and `OrchestraType` is resolved from `\.legibilityWeight`:

| token | `.regular` | `.bold` |
|---|---|---|
| `code`, `codeSm`, `meta` | Regular | **Medium** |
| `label`, `status`, `button`, `cardName` | SemiBold | **Bold** |

### Truncation

Server truncation is upstream and invisible: `topic` / `last_user` 140 chars, `last_assistant` / `subagent_said` 240, chat 900, all `…`-suffixed. **Do not add a second ellipsis** — check for a trailing `\u{2026}` first.

Line clamps carry over (topic 1, last_user 1, last_assistant 2, subagent_said 2), and on iOS **every clamped block gets tap-to-expand** — including `git.commit.subject` and topology `branch.subject`, which arrive **completely untruncated** and at AX5 leave about six characters on a 393 pt screen. Both get the expand affordance and an accessibility label carrying the full string.

**Worktree name truncation.** Mono's advance is 0.6em, so at 17 pt a name costs 10.2 pt/char. On a 393 pt phone minus 26 pt padding, a ~100 pt badge and a 44 pt overflow target, ~186 pt ≈ **18 characters** remain. Live names reach 21. Rule: `.lineLimit(1)`, `.truncationMode(.middle)`, `.layoutPriority(1)` on the badge's competitors, not the name. At `.accessibility3`+ the header reflows to two lines.

## 9.4 The symbol channel — SF Symbols, not Unicode

Unicode passthrough is abandoned, for stronger reasons than aesthetics:

- `⛔` U+26D4 has **Emoji_Presentation = Yes**: iOS renders it from Apple Color Emoji as a full-colour bitmap that **ignores `.foregroundStyle`**, ignores weight, does not lift in Contrast+, does not go monochrome in a tinted widget, and carries emoji advance width — breaking the mono column alignment that is the only reason mono was kept. It is also a **disc**, silhouette-identical to `●` WORKING and `○` ENDED at 12 pt, so the shape channel fails for LIMIT — the exact status a protanope most needs separated from WORKING.
- `⌁`, `⌖`, `⌗`, `⧗`, `⏎`, `ᴹ` are Miscellaneous Technical / Supplemental Math / Phonetic Extensions codepoints that IBM Plex Mono almost certainly does not cover. `Font.custom` falls back **per glyph and silently**, to a face with different metrics and weight.
- The desktop gets away with this at 10 px in a browser. Promoting these marks to 12 pt semibold and making them the primary channel converts a cosmetic problem into a structural one.

**Every meaning-bearing mark in the app is an SF Symbol.** SF Symbols is the only glyph channel that stays monochrome and tintable in notification content, tinted widgets, the Dynamic Island and Contrast+, and it inherits weight and optical size from adjacent text.

### Two tables, no collisions

The server explicitly warns that card-level `waiting` is unrelated to session-level `waiting`. Reusing `◆` for both — and `●` for `working`/`busy`, `▲` for `needs_input`/`attention` — collapses exactly the pair hardest to separate by hue, on rows 8 pt apart.

> **Systematic rule: session statuses use `.fill` variants; card availability uses outline variants — and every base shape differs.**

**Session status**

| status | SF Symbol | silhouette | word | detail |
|---|---|---|---|---|
| `working` | `circle.fill` | disc | WORKING | — |
| `needs_input` | `exclamationmark.triangle.fill` | triangle | NEEDS ANSWER | — |
| `blocked` | `square.fill` | square | BLOCKED | pending tool |
| `waiting` | `diamond.fill` | diamond | YOUR TURN | idle @ prompt |
| `limit` | `hourglass` | hourglass | LIMIT HIT | — |
| `ended` | `circle` | ring | ENDED | — |
| `unknown` | `questionmark.circle` | ? in a ring | UNKNOWN | process table unreadable |

**Card availability**

| availability | SF Symbol | silhouette | word |
|---|---|---|---|
| `free` | `circle.dashed` | dashed ring | FREE |
| `busy` | `waveform` | waveform | BUSY |
| `needs_you` | `exclamationmark.triangle` | outline triangle | NEEDS YOU |
| `your_turn` | `diamond` | outline diamond | YOUR TURN |
| `limited` | `pause.circle` | pause bars in a ring | WAITING |

Eleven marks, eleven distinct silhouettes, no cross-table collision.

**Two deliberate corrections to the desktop:**

1. `blocked` takes the **accent** text colour, not yellow. The desktop's split (border accent, text yellow — "border screams, text is calmer") reads as a bug at 3 pt on a phone. `blocked` *is* an attention state; the distinction moves to the channel that survives: the `square.fill` silhouette and the detail line naming the tool.
2. One status table, shared by board and map. `map.html`'s five-key vocabulary folds `blocked` into `needs` and ranks `limit` above `working`, disagreeing with the server. The server's ranking wins.

### The rest of the marks

| Desktop | iOS |
|---|---|
| `⌁` brand / sync | **custom SF Symbol `orc.bolt`** — an SVG template in the Asset Catalog with SF Symbols metadata, so it inherits weight, scale and monochrome rendering |
| `⌗` board | custom SF Symbol `orc.grid` |
| `⌖` pid | `scope` |
| `⧗` waiting on | `clock` |
| `⏎` agent prefix | `arrow.turn.down.left` |
| `❯` your prefix | `chevron.right` |
| `→` last_user prefix | `arrow.right` |
| `⚙` subagent | `gearshape.2.fill` |
| `↳` handed_to | `arrow.turn.down.right` |
| `↑ / ↓` | `arrow.up` / `arrow.down` |
| `Δ` dirty | text `Δ` (basic Greek; covered) |
| `ᴹ` model-scoped | **deleted** — replaced by a real `model cap` pill at `label` size |
| `①②③④⑤` | passthrough — verbatim server prose; the coverage test *warns* rather than fails for server-supplied strings |

**Coverage is verified, not assumed.** `GlyphCoverageTests` enumerates every literal the app renders in a mono style — from a single `OrcLiterals` enum the linter requires such strings to come from — and asserts via `CTFontGetGlyphsForCharacters` that each bundled weight has a real glyph. Any miss fails the build. A second test asserts the PostScript names resolve to non-system fonts, because `Font.custom` fails silently and invisibly in review.

## 9.5 Spacing, targets, radii, elevation

**Spacing** — 4 pt base, seven steps: `xxs 2 · xs 4 · sm 8 · md 12 · lg 16 · xl 24 · xxl 32`. All `@ScaledMetric(relativeTo: .body)` at the point of use for vertical padding and row minimums.

| Context | Desktop | iOS |
|---|---|---|
| screen horizontal inset | 22 px | **16** |
| card ↔ card | 12 px | 12 |
| card head padding | 10 / 14 | 12 / 14 |
| session row padding | 8 / 10 | **12 / 14** |
| session ↔ session | 6 px | 8 |
| chat bubble padding | 8 / 11 | 10 / 14 |
| sheet content inset | 14 / 16 | 16 |

**Touch targets**

1. **Actuating controls take the 44 pt floor, and it costs real height.** That is why session row padding rose from 8/10 to 12/14. Do not "recover" density later by shaving frames — `.frame(minWidth:44, minHeight:44)` participates in layout; it is not invisible padding.
2. **Most of a session row is not a control.** Account, model, age, subdir, branch and the subagent tag are `Text`, not `Button`. This — not spacing — is what removes the overlap problem.
3. Where an expanded hit region genuinely is needed, `.contentShape(.rect(cornerRadius:).inset(by: -8))` extends the hit area without consuming layout. Enlarged shapes *can* overlap and the later view in z-order silently wins, so an 8 pt gap applies there — and only on direct children of an unclipped container, since an ancestor `.clipShape` clips them.
4. Verified with the Accessibility Inspector's hit-region overlay, not by reading code.

**Radii** — `xs 5` chips/badges/pills · `sm 8` rows/bubbles/fields · `md 12` cards/tiles · `lg 16` sheets/toasts. `RoundedRectangle(cornerRadius:style:.continuous)` throughout.

**Elevation — three levels, one shadow**

| Level | Expression |
|---|---|
| 0 — flat | surface fill + `hairline` 1 px stroke. Cards, rows, tiles. **No shadow.** |
| 1 — attention | surface fill + status 1 px stroke + a second 1 px ring at `glowAccent`, drawn as a **background** so an ancestor clip cannot eat it and it does not paint into the inter-card gap |
| 2 — floating | `overlay` fill + `control` stroke + `.shadow(color: .black.opacity(0.55), radius: 24, y: 8)`. Sheets, menus, toasts only |

**Shadows exist only at level 2.** A zero-radius zero-offset "glow shadow" renders exactly behind the view and is invisible; the attention ring is a stroke, not a shadow.

## 9.6 Components and their states

### `StatusStyle` — colour cannot travel alone

```swift
enum StatusRole: String, CaseIterable, Sendable {
    case working, needsInput, blocked, waiting, limit, ended, unknown   // session
    case cardFree, cardBusy, cardNeedsYou, cardYourTurn, cardLimited    // availability
}

struct StatusStyle: Equatable, Sendable {
    let role: StatusRole
    let symbol: String          // SF Symbol name — never empty
    let word: String            // never empty
    let detail: String?

    private init(_ role: StatusRole, _ symbol: String, _ word: String, detail: String? = nil) {
        precondition(!symbol.isEmpty, "StatusStyle requires a symbol")
        precondition(!word.isEmpty,   "StatusStyle requires a word")
        self.role = role; self.symbol = symbol; self.word = word; self.detail = detail
    }

    static func session(_ s: String) -> StatusStyle {
        switch s {
        case "working":     Self(.working,    "circle.fill",                   "WORKING")
        case "needs_input": Self(.needsInput, "exclamationmark.triangle.fill", "NEEDS ANSWER")
        case "blocked":     Self(.blocked,    "square.fill",                   "BLOCKED",   detail: "pending tool")
        case "waiting":     Self(.waiting,    "diamond.fill",                  "YOUR TURN", detail: "idle @ prompt")
        case "limit":       Self(.limit,      "hourglass",                     "LIMIT HIT")
        case "unknown":     Self(.unknown,    "questionmark.circle",           "UNKNOWN",   detail: "process table unreadable")
        default:            Self(.ended,      "circle",                        "ENDED")
        }
    }

    static func availability(_ a: String) -> StatusStyle {
        switch a {
        case "free":      Self(.cardFree,     "circle.dashed",            "FREE")
        case "busy":      Self(.cardBusy,     "waveform",                 "BUSY")
        case "needs_you": Self(.cardNeedsYou, "exclamationmark.triangle", "NEEDS YOU")
        case "your_turn": Self(.cardYourTurn, "diamond",                  "YOUR TURN")
        default:          Self(.cardLimited,  "pause.circle",             "WAITING")
        }
    }

    /// The symbol is decorative; the word (and detail) is the label.
    var a11y: String { detail.map { "\(word), \($0)" } ?? word }
}
```

**No `Color` is stored** — the palette resolves `StatusRole → Color` at render time, which is what makes Asset-Catalog theming work at all. **No `rank` is stored** — re-deriving server ordering violates principle 3, and the desktop's own two rank tables disagree with each other. The private initialiser is what makes "colour never travels without a symbol and a word" true rather than conventional.

### Component states

| Component | States |
|---|---|
| **`StatusBadge`** | default · attention (pulsing ring) · Contrast+ (no fill, solid stroke) · Daylight (no fill) |
| **`WorktreeCard`** | normal · attention (elevation 1) · close-pending (amber refusal row) · collapsed-parked · skeleton |
| **`SessionRow`** | working · needs_input · blocked · waiting · limit · ended (`sunkenDim`) · handed-off (`control` rail) · provisional (dimmed, uncounted) · inferred (`ⓘ` line) · unknown liveness |
| **`AccountChip`** | normal · reserve-blocked (`lock.fill` + `statusLimit`) · exhausted (`hourglass` + `statusLimit`) · error (`xmark` + `statusNeeds`) |
| **`ProcChip`** | certain · **ambiguous / guess** (dashed `control` stroke) · unreachable (no chip; caption instead) |
| **`UsageBar`** | healthy · caution · exhausted (hatched) · model-scoped (`model cap` pill) · unknown reset |
| **`Countdown`** | ticking · reset-unknown (`⌛ limited — reset time unknown`) · stale-source (`· from a 6m old reading`) |
| **`ChatBubble`** | sending `◌` · typed `✓` · **delivered `✓✓`** · queued `✓ queued` · failed `⚠` · truncated (footnote) · collapsed (‹show full›) |
| **Buttons** | `.primary` · `.ghost` · `.armed` · `.destructive` · `.scheduled` (**never pulses** — an armed auto-resume is calm certainty, not an alert) · disabled (`raised`/`control`/`textTertiary`, never `.opacity`) |
| **Toast** | success · failure · refusal · expanded-in-place |
| **Connection element** | connecting · live · slow · stale · very stale · offline · unauthorized · asleep |

### `UsageBar` — the counter-intuitive rule, in a comment

**Width = `percent` (used). Colour = `shade(remaining)`.** This is the desktop's rule and it is deliberately counter-intuitive; the code comment says so, or someone will "fix" it. `shade`: remaining ≥ 50 → `statusWorking`; ≥ 15 → `statusLimit`; else `statusNeeds`. `exhausted_now` overrides to `statusNeeds` plus a hatch.

The layout has **no screen width in it**: label row above (label + `model cap` pill + percentage trailing), full-width bar below, reset line below. The desktop's `112px 1fr 96px` grid leaves ~118 pt of bar on a phone; the fix is the layout, not a number.

### `Countdown` — the platform primitive

```swift
Text(timerInterval: Date.now...deadline, pauseTime: nil, countsDown: true)
    .font(.orc.meta).foregroundStyle(Color.orc.statusLimit)
```

`Text(timerInterval:)` is rendered by the system with **no view updates at all**, and it is the only countdown that works in a widget or a Live Activity. Same component in all three places.

**Deadline derivation.** `resets_in_seconds` is primary; `deadline = fetched_at + resets_in_seconds`. `resets_at` is decoded permissively (Double *or* ISO-8601 String *or* absent) and used only as a fallback — nothing in the repo currently consumes it, and it is passthrough from an external tool whose schema is not pinned. Pin an `/api/limits` capture as a test fixture before shipping.

**Freshness is part of the value.** Under 60 s: bare countdown. Over 60 s: a `textTertiary` suffix `· from a 6m old reading`, updated on the poll, **not** on a 1 s tick.

### `FlowLayout` — a required primitive

**SwiftUI has no wrapping stack**, and `ViewThatFits` cannot wrap — it picks the first child that fits, so using it would mean hand-authoring seven permutations that degrade wrongly at AX2.

```swift
struct FlowLayout: Layout {
    var spacing: CGFloat = 6
    var lineSpacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, lineH: CGFloat = 0, maxX: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > 0, x + spacing + sz.width > width { x = 0; y += lineH + lineSpacing; lineH = 0 }
            x += (x > 0 ? spacing : 0) + sz.width
            lineH = max(lineH, sz.height); maxX = max(maxX, x)
        }
        return CGSize(width: min(maxX, width), height: y + lineH)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize,
                       subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, lineH: CGFloat = 0
        for s in subviews {
            let sz = s.sizeThatFits(.unspecified)
            if x > bounds.minX, x + spacing + sz.width > bounds.maxX {
                x = bounds.minX; y += lineH + lineSpacing; lineH = 0
            }
            if x > bounds.minX { x += spacing }
            s.place(at: CGPoint(x: x, y: y), anchor: .topLeading, proposal: ProposedViewSize(sz))
            x += sz.width; lineH = max(lineH, sz.height)
        }
    }
}
```

Wrap priority is expressed by the §9.3 drop table, applied *before* layout; the layout itself just wraps.

## 9.7 Motion

**One curve** — the desktop's `--ease`:

```swift
extension Animation {
    static let orc      = Animation.timingCurve(0.22, 1, 0.36, 1, duration: 0.30)
    static let orcQuick = Animation.timingCurve(0.22, 1, 0.36, 1, duration: 0.15)
}
```

**One motion primitive, one heartbeat.** Everything that pulses is `RingPulse` — an animated 1 pt stroke ring, phase-driven from a **single app-level `TimelineView`** so all pulsing elements stay in phase.

```swift
struct RingPulse: ViewModifier {
    @Environment(\.accessibilityReduceMotion) private var reduce
    @Environment(\.orcHeartbeat) private var beat: Double      // 0…1, from the root TimelineView
    let role: StatusRole
    let period: Double                                          // 1.6 attention · 1.2 working
    func body(content: Content) -> some View {
        let phase = reduce ? 0.35 : (sin(beat * .pi * 2 / period) + 1) / 2 * 0.5
        content.background(
            RoundedRectangle(cornerRadius: Radius.xs + 1, style: .continuous)
                .strokeBorder(Color.orc.color(role).opacity(phase), lineWidth: 1)
                .padding(-2))
    }
}
```

Visible on near-black (a shadow is not), cheap, and consistent with "elevation is lightness plus a hairline". Reduce Motion changes take effect immediately because `reduce` is read in `body` — and `onAppear` + `repeatForever` is SwiftUI's most reliable source of stuck, duplicated or silently-cancelled animations across cell recycling.

**The WORKING glyph never blinks to zero.** The desktop's `50% { opacity: 0 }` at `steps(1)` means that for 600 ms of every 1200 ms the shape channel for the most common status is **absent**. Dimming to 0.4 is no better: `statusNeeds` at 40 % over `sunken` is **1.93:1**. Instead the disc stays solid and a `RingPulse` at 1.2 s breathes behind it.

| Thing | Motion |
|---|---|
| status change on a row | rail colour + text cross-fade, 0.30 s — a status flip is information; it should be seen changing |
| working | `RingPulse` 1.2 s behind a permanently solid disc |
| attention badge / armed button | `RingPulse` 1.6 s |
| **`.scheduled` (armed auto-resume)** | **none** — calm certainty, not an alert |
| card reorder | `.animation(.orc, value: orderToken)` on `ForEach(id:)` — SwiftUI's native FLIP |
| new card | fade + 8 pt rise, appended at the end |
| button press | scale 0.97 + opacity 0.82, 0.15 s |
| sheet / push | system |
| toast | slide 0.30 s in, fade 0.20 s out |
| skeleton | breathing 0.35 → 0.55 over 1.6 s, heartbeat-driven — **no shimmer** |
| countdown | none — the system renders it |

**`matchedGeometryEffect` is not used.** It requires both endpoints to exist simultaneously, and in a lazy container produces exactly the teleport the reorder rule exists to prevent.

**Scroll anchoring during an applied reorder** uses `ScrollViewReader.scrollTo(_:anchor:)` at a recorded viewport fraction — **not** `.scrollPosition(id:)`, which aligns the identified view to the *leading edge* and would scroll the anchor card to the top, i.e. exactly the jump being prevented.

```swift
let anchorID = topmostVisibleCardID
let fraction = (anchorFrame.minY - viewportFrame.minY) / viewportFrame.height
withAnimation(.orc) { order = pendingOrder }
var t = Transaction(); t.disablesAnimations = true      // do NOT animate the scroll
withTransaction(t) { proxy.scrollTo(anchorID, anchor: UnitPoint(x: 0, y: fraction)) }
```

## 9.8 The SwiftUI extensions

```swift
// OrchestraColor.swift
import SwiftUI

/// All colours come from Colors.xcassets, which carries four variants of each:
///   Any (Daylight) · Dark (Night) · Any/High Contrast · Dark/High Contrast
/// The OS resolves them. There is no ternary at any call site, and UIColor(named:)
/// is available for the launch screen and the notification content extension.
/// Computed WCAG ratios are asserted in ColorContrastTests, not documented in comments.
struct OrchestraTokens: Sendable {
    // surfaces
    let canvas    = Color("canvas")
    let sunken    = Color("sunken")
    let sunkenDim = Color("sunkenDim")
    let surface   = Color("surface")
    let raised    = Color("raised")
    let overlay   = Color("overlay")       // leaf tier: text only, no chips

    // borders
    let hairline      = Color("hairline")        // decorative
    let control       = Color("control")         // labelled controls
    let controlStrong = Color("controlStrong")   // symbol-only controls, >=3:1 everywhere

    // text
    let textPrimary   = Color("textPrimary")
    let textSecondary = Color("textSecondary")
    let textTertiary  = Color("textTertiary")
    let textDisabled  = Color("textDisabled")    // NON-INFORMATIONAL ONLY

    // glow — level-2 shadow only, never behind text
    let glowAccent  = Color("glowAccent")
    let glowWorking = Color("glowWorking")

    /// The only way to obtain a status colour. StatusStyle owns the role, and it
    /// cannot be constructed without a symbol and a word.
    func color(_ role: StatusRole) -> Color {
        switch role {
        case .needsInput, .blocked, .cardNeedsYou:  Color("statusNeeds")
        case .waiting, .cardYourTurn:               Color("statusTurn")
        case .working, .cardBusy:                   Color("statusWorking")
        case .cardFree:                             Color("statusFree")
        case .limit, .cardLimited:                  Color("statusLimit")
        // NOT textDisabled: this colour carries the status WORD ("ENDED", "UNKNOWN"),
        // which is information — and textDisabled fails AA on every surface (§9.2,
        // Appendix A rule 3). The ended row's DE-EMPHASIS comes from its ground
        // (sunkenDim) and its 3 pt rail, which may use textDisabled because a rail
        // is not text.
        case .ended, .unknown:                      Color("textTertiary")
        }
    }

    /// Status tint fill. Night only: 0.12 is a hard ceiling (at 0.16 the accent pair
    /// drops to 4.33:1). In Contrast+ and Daylight the fill is zero — the badge's
    /// identity is its solid stroke plus its coloured word.
    func tint(_ role: StatusRole,
              contrast: ColorSchemeContrast,
              scheme: ColorScheme) -> Color {
        guard scheme == .dark, contrast == .standard else { return .clear }
        return color(role).opacity(0.12)
    }
}

extension Color { static let orc = OrchestraTokens() }
```

```swift
// OrchestraType.swift
import SwiftUI

enum Plex {                       // verified at launch by FontRegistrationTests
    static let regular  = "IBMPlexMono"
    static let medium   = "IBMPlexMono-Medium"
    static let semibold = "IBMPlexMono-SemiBold"
    static let bold     = "IBMPlexMono-Bold"
}

/// Two voices: mono = the machine, SF = the human.
/// Resolved from legibilityWeight so Bold Text moves the mono voice too —
/// Font.custom does not respond to it on its own.
struct OrchestraType: Sendable {
    private let heavy: Bool
    init(bold: LegibilityWeight?) { self.heavy = (bold == .bold) }

    private var monoBody:   String { heavy ? Plex.medium : Plex.regular }
    private var monoStrong: String { heavy ? Plex.bold   : Plex.semibold }

    // human voice — real Dynamic Type styles, never fixed sizes
    var display:     Font { .system(.largeTitle,  design: .default, weight: .semibold) }
    var title:       Font { .system(.title3,      design: .default, weight: .semibold) }
    var body:        Font { .system(.body,        design: .default) }
    var bodyCompact: Font { .system(.subheadline, design: .default) }

    // machine voice
    var cardName: Font { .custom(monoStrong, size: 17, relativeTo: .headline) }
    var code:     Font { .custom(monoBody,   size: 15, relativeTo: .subheadline) }
    var codeSm:   Font { .custom(monoBody,   size: 13, relativeTo: .footnote) }
    var meta:     Font { .custom(monoBody,   size: 12, relativeTo: .caption) }
    var label:    Font { .custom(monoStrong, size: 11, relativeTo: .caption2) }
    var status:   Font { .custom(monoStrong, size: 12, relativeTo: .caption) }
    var button:   Font { .custom(monoStrong, size: 15, relativeTo: .callout) }
}

private struct TypeKey: EnvironmentKey { static let defaultValue = OrchestraType(bold: nil) }
extension EnvironmentValues {
    var orcType: OrchestraType { get { self[TypeKey.self] } set { self[TypeKey.self] = newValue } }
}
// at the app root:  .environment(\.orcType, OrchestraType(bold: legibilityWeight))

/// Tracking as a ratio that resolves at render time, not a shipped-size constant.
struct Tracked: ViewModifier {
    @ScaledMetric(relativeTo: .caption2) private var unit: CGFloat = 1
    let base: CGFloat            // 11 for .label, 12 for .status
    let em: CGFloat              // 0.08
    func body(content: Content) -> some View { content.tracking(base * em * unit) }
}
extension View {
    func orcTracked(base: CGFloat, em: CGFloat = 0.08) -> some View {
        modifier(Tracked(base: base, em: em))
    }
}
```

```swift
// OrchestraMetrics.swift
import CoreGraphics

// One `static let` per line: in a comma-separated binding list the type annotation
// binds only to the FIRST name, so `xs = 4` would silently infer Int.
enum Space {
    static let xxs: CGFloat = 2
    static let xs:  CGFloat = 4
    static let sm:  CGFloat = 8
    static let md:  CGFloat = 12
    static let lg:  CGFloat = 16
    static let xl:  CGFloat = 24
    static let xxl: CGFloat = 32
}
enum Radius {
    static let xs: CGFloat = 5
    static let sm: CGFloat = 8
    static let md: CGFloat = 12
    static let lg: CGFloat = 16
}
enum Rail { static let width: CGFloat = 3 }
enum Hit  { static let min:   CGFloat = 44 }     // costs real layout — see §9.5
```

```swift
// Formatters.swift
enum OrcFormat {
    /// index.html rel() — the board's canonical form. NO zero padding.
    /// limits.html's rel() has no seconds bucket and map.html's differs at the day
    /// boundary; the board's is canonical and is used on every screen, so a
    /// 40-second reset reads "40s", not "0m".
    static func rel(_ s: Int?) -> String {
        guard let s, s >= 0 else { return "—" }
        if s < 60    { return "\(s)s" }
        if s < 3600  { return "\(s / 60)m" }
        if s < 86400 { return "\(s / 3600)h\((s % 3600) / 60)m" }
        return "\(s / 86400)d"
    }

    /// index.html clock() — today "14:05", otherwise "Tue 14:05".
    static func clock(_ ts: TimeInterval) -> String { /* … */ }

    /// THE ONLY correct way to derive an activity instant. Never
    /// Date().addingTimeInterval(-age_s) — age_s is measured against the server's
    /// generated_at, which is seconds stale by the time we read it.
    static func activityDate(evidenceAt: TimeInterval) -> Date {
        Date(timeIntervalSince1970: evidenceAt)
    }
}
```

**Concurrency.** `OrchestraTokens` and `OrchestraType` are structs of `Color`/`Font`, both `Sendable`. `Color.orc` is a `static let` of a `Sendable` value type — legal as a global. `StatusStyle` is `Sendable`. Networking is an `actor`; the board view model is `@MainActor @Observable`.

## 9.9 The mark and the icon

`index.html`'s favicon is already the design: `⌁` on `#0d0d0d` in `#d97757`. In the product it means **live current** — it prefixes `⌁ map`, `⌁ live agents`, `⌁ [pid …]`. It is the glyph for *something is running*.

**Authored as a layered Icon Composer document,** not a flat 1024 PNG (which reads dead under iOS 26's specular pass because there is no layer separation to work with):

1. **Background** — `canvas` flat, plus the elliptical body wash as its own sub-layer.
2. **Mark** — the bolt as a **vector path, not type**. A zigzag between two horizontal terminals, ~46 px stroke on a 1024 canvas, **square caps and miter joins** (Plex's joinery, not SF Symbols' rounded one), ~58 % of the canvas, optically centred (nudge up ~2 %; the glyph is bottom-heavy). Stroke is a gradient along the path `#D97757 → #EDB9AC` — current with a direction, both endpoints brand tokens.
3. **Specular** — left empty; the system's pass owns it.

Appearances: **dark** as above · **tinted** — single-channel mask, gradient flattened to a luminance ramp so the bolt still reads directional · **clear/glass** — 100 % white on transparent · **light** — Daylight `#9A553E` on `#F4F2EF`.

Inside the app: nav-bar leading item at 20 pt (decorative); the connection element (`orc.bolt` solid / outline / `orc.bolt.slash`, with the *text* carrying the state); the empty board state at 48 pt in `textDisabled`. The launch screen takes an image asset and an asset-catalog colour only — it cannot render a glyph — so the mark ships as a PDF asset with `UILaunchScreen.UIImageName`, background `UIColorName: "canvas"`.

---

# 10. Accessibility

## 10.1 VoiceOver — the information architecture

Ungrouped, a `SessionRow` is ~12 elements and a `WorktreeCard` adds a dozen more — roughly 25–50 swipes per card, across nine cards. And principle 5 leans on *sort position* as the colour-blind-safe third channel, which a screen-reader user has **only**. Traversal must be cheap.

| Element | Treatment |
|---|---|
| `WorktreeCard` | container; the **name is `.accessibilityHeading(.h2)`** so the heading rotor becomes the path to "the card that needs you" |
| card header | one element: *"{availability.a11y}. {name}. {branch}, {dirty} uncommitted, {ahead} ahead {behind} behind."* |
| commit line | one element, label = **full untruncated subject** |
| `SessionRow` | **one element.** label = *"{status.a11y}, {account}, {model}, {rel(age)} ago"*; **value** = the body lines joined; **hint** = *"double tap to open the conversation"* |
| `row1` metadata | inside the row element; never separate |
| body-line prefix symbols | never in the tree (row-level `.accessibilityElement(children: .ignore)` handles it) |
| proc chip | its own element: *"terminal, ttys004, Terminal app"* (+ *", ambiguous — two sessions and two processes share account work in this worktree"*) |
| `UsageBar` | one element, label *"acct2, Max plan"*, value *"88 percent remaining, 20 percent reserved"* |
| toast | `.isStaticText` + an announcement |
| held-order pill | *"board order held, {n} cards changed"*, hint *"activate to re-sort"* |
| section headers | `.isHeader`; heading navigation walks NEEDS YOU → YOUR TURN → … — the fastest triage path |

**Concatenated `Text` and accessibility.** Mixed mono-prefix + sans-body lines are built with `Text` concatenation, which is the only construction giving mixed fonts with normal wrapping — and you cannot apply accessibility modifiers to a fragment of a concatenation. Resolution: concatenate for layout, and solve accessibility at the **row** level. Decorative symbols never enter the tree, which is better than hiding them individually: a VoiceOver user hears one coherent row, not eleven fragments.

### Spoken forms

| Visual | VoiceOver |
|---|---|
| `● WORKING [main] fable · 12s` | *"Working. ConfidAI-auth, account main, model fable 5, active 12 seconds ago."* hint *"Double tap to open the conversation."* |
| `■ BLOCKED` + `ⓘ inferred` | *"Blocked, pending tool. Waiting on tools Bash and Edit. Inferred — no permission hook installed."* |
| `◆ YOUR TURN` | *"Your turn, idle at prompt. The agent finished and is waiting."* |
| `⛔ LIMIT · Weekly · 2h 38m` | *"Limit hit. Weekly cap on account account8. Resets in about 2 hours 38 minutes, at 2:32 PM."* |
| `⛔` null reset | *"Limit hit. Reset time unknown."* |
| `↳ continued by [spare]` | *"Work continued on account spare. Nothing to do here."* |
| `? UNKNOWN` | *"Status unknown — the process table could not be read."* |
| availability badges | *"Needs you." / "Your turn." / "Busy." / "Waiting on limits — nothing to do until the reset." / "Free — safe to start a new agent here."* |
| `⏱ 14:33` | *"Auto-resume armed for 2:33 PM."* / *"…queued behind ConfidAI3."* / *"…firing now."* |
| bubble `✓` / `✓✓` / `✓ queued` | *"Typed into the terminal."* / *"Delivered — it appears in the conversation."* / *"Queued — the agent is mid-turn and will receive it at the end of this turn."* |
| branch row (map) | *"ConfidAI3, 2 sessions, branch feat slash russian-course, limit hit. fix parenthesis course: russian lesson 7 audio pipeline. 2045 commits ahead, 2317 behind origin/main, 3 uncommitted files. Last commit 2 days 22 hours ago. Forked 144 days ago."* |

### Actions, not extra focus stops

The session verbs become custom actions on the row rather than five separate stops:

```swift
.accessibilityAction(named: "Reply")           { openChat(session) }
.accessibilityAction(named: "Why is this?")    { presentWhy(session) }
.accessibilityAction(named: "Arm auto-resume") { armResume(session) }     // limit only
.accessibilityAction(named: "Resume now")      { resume(session) }        // when enabled
.accessibilityAction(named: "Session info")    { presentInfo(session) }
```

Card level: `.accessibilityAction(named: "Finish")` — which opens the sheet, never acts. **Every swipe action has a matching rotor action**; gestures are undiscoverable and swipe actions are unreachable to VoiceOver otherwise.

### Focus, escape, live regions

- The finish sheet, the composer and the auto-resume sheet each set `@AccessibilityFocusState` on their title on present, and bind `.accessibilityAction(.escape)` to Cancel.
- The offline banner posts an `AccessibilityNotification.Announcement` on transition in, and carries `.isHeader` so it is rotor-reachable.
- **The hold rule extends to VoiceOver.** With VO driving, the scroll view is at rest essentially always, so a purely scroll-based freeze would hold continuously *and* the board would reorder under the focus cursor — losing or silently transferring focus, so the next Activate lands on a different agent's Finish, which can spawn a headless agent that merges and pushes. Implemented as a per-row `@AccessibilityFocusState<Row.ID?>`: **never reorder while VO focus is inside the list.** On apply, restore focus by card id and post *"board re-sorted"*. Same for Switch Control (`UIAccessibility.isSwitchControlRunning`).
- **The tap shield is disabled under VoiceOver and Switch Control**, where activation is a deliberate double-tap on a focused element, not a mis-aimed thumb; there the shield can only confuse.
- **Live countdowns announce continuously and would make the app unusable.** The ticking text is `.accessibilityHidden(true)`; the parent carries a coarse `.accessibilityValue` refreshed at most once per minute (*"resets in about 2 hours"*). Threshold crossings become **notifications**, which the user controls — not unsolicited announcements that interrupt whatever is being read.
- The map's `Canvas` is a single opaque element by default; `.accessibilityRepresentation` maps it to a plain `List` of branch rows.

## 10.2 Dynamic Type

- Everything is a `List` of text rows; **no fixed row heights**.
- Policy branches on `@Environment(\.dynamicTypeSize)` — **not** `ViewThatFits`, which measures available space and cannot conditionally drop a token.

| element | behaviour |
|---|---|
| worktree name, status word, headline | scale to AX5 uncapped |
| metadata (`[account] model · age`) | one line ≤ `.xxLarge`; wraps above; `model` dropped at `.accessibility1`+ (least load-bearing, and frequently `""`) |
| topic / last_user / last_assistant | scale uncapped; line limits *increase* (2→3→4) rather than truncating harder |
| countdowns | fixed-width slot sized for `2d 03h`; own line at AX sizes |
| chips / badges | full-width labelled rows at `.accessibility1`+ |
| `Δ7 ↑3 ↓0` | expands to words at `.accessibility1`+ |
| inline session rows per card | 2 normally, **1** at `.accessibility1`+ |
| `display` | clamped at `.accessibility3` — a 3-digit count at AX5 blows the tile |
| `status` badges | floor of `.large` — never shrink below shipped size |

Fonts ship in-bundle; SF Mono is the fallback for anything that must scale.

**Bold Text** is handled by resolving the mono weight from `\.legibilityWeight` (§9.3) — without it, Bold Text inverts the hierarchy, leaving quiet prose heavier than every status word.

## 10.3 Colour-blind safety

The desktop language is green (working) · terracotta (needs you) · a second terracotta (your turn) · yellow (limit) · cyan (identifiers). Two collisions: green vs terracotta is the classic deutan/protan confusion, and accent / accent-2 / yellow are three warm tones in a narrow hue band.

**The analysis is done with CIEDE2000 over Viénot-simulated colours**, not with WCAG contrast between two foreground hues — that is a luminance metric and dichromats retain the S-cone axis, so it says nothing about whether two chromatic marks are distinguishable. (Counterexample: protan-simulated green `#ADAD8D` vs cyan `#ACACC6` has a WCAG ratio of 1.04 and a ΔE00 of **30.1** — one of the *most* separable pairs in the set.)

**Original palette, deuteranopia** (~6 % of men): `turn/limit 2.7` · `needs/limit 9.0` · `needs/turn 10.0` · `turn/working 10.1` · `needs/working 10.5` · `working/limit 11.7` · all `free` pairs 31–41.
**Original palette, protanopia:** `turn/working 3.0` · `turn/limit 4.5` · `working/limit 7.5` · `needs/*` 13–15 · all `free` pairs 30–37.

Reading: `free` (cyan) is bulletproof; `needs` is robust under protanopia and marginal under deuteranopia; **the dead cluster is `turn` / `working` / `limit`**.

**The targeted fix.** A search of HLS space for a `turn` replacement maximising worst-case ΔE00 against the other four under both simulations, constrained to ≥4.5:1 on `raised`:

| | worst protan | worst deutan | on `surface` | on `raised` |
|---|---|---|---|---|
| `#E8A87C` (desktop) | **3.0** | **2.7** | 8.90 | 8.38 |
| **`#EDB9AC` (adopted)** | **8.4** | **9.2** | 10.46 | 9.85 |

Worst-case separation improves 3.4×, contrast improves, and it stays a peach. Post-change, **no pair is below 7.5**. No warm hue clears ΔE 11 against all four — the only candidates above 17 are pale mint and blue-violet, which destroy the brand *and* collide semantically with `free`. Tritanopia (~0.008 %) is unfixable here (`working/free` 3.0) and is not designed for.

**What the result licenses — five enforceable consequences:**

1. **Symbol + word are mandatory and inseparable.** `StatusStyle` has no colour-only path and no initialiser that yields one without both.
2. **The symbol is the shape channel**, and it is CVD-invariant — provided it renders, which §9.4 guarantees by using SF Symbols rather than Unicode passthrough.
3. **The left rail is a second shape channel.** 3 pt solid for active statuses; `ended` gets a rail in `textDisabled` on a `sunkenDim` ground rather than an opacity fade.
4. **Position is a third channel.** The board arrives pre-sorted by server severity, so the top is always what needs you. A user who perceives no hue at all reads the board correctly top-to-bottom. **Caveat:** while the re-sort gate holds, that guarantee is suspended — which is why the held state is a first-class accessibility element (§10.1) and not just a pill.
5. **Solid status rails pass non-text contrast on their own** (5.46–9.85 on `raised`). Tint fills do not, and are therefore explicitly decorative.

**High-contrast mode** is offered manually as well as honoured from the OS: Contrast+ lifts every status hue to 7.05–11.09 on `raised` (AAA), drops all tint fills, thickens borders, and adds a hatch pattern to every usage bar. `AXDifferentiateWithoutColor` is honoured automatically.

**Bars carry words**: `88% left · healthy`, `18% left · below reserve`, `4% left · exhausted`.

## 10.4 Reduce Motion, Reduce Transparency, Smart Invert

| Motion | Replacement | Information preserved by |
|---|---|---|
| `RingPulse` (attention, working) | static ring at 0.35 | the ring itself |
| card reorder | jump-cut + 300 ms highlight flash | the flash |
| armed-button pulse | static fill | fill |
| Live Activity progress shimmer | static determinate bar | the bar |
| sheet slide / push | cross-dissolve | — |
| skeleton breathing | static grey blocks | — |
| tap-shield flash | **static 400 ms outline** in `statusLimit` | the outline (120 ms is below many users' change-detection threshold, and a silent refusal is indistinguishable from a dead control) |

Read from `\.accessibilityReduceMotion` **inside `body`**, so runtime toggles take effect immediately — Reduce Motion is frequently toggled by someone getting nauseated *right now*, and an accommodation requiring a relaunch is not one.

**Reduce Transparency:** the bottom accessory and nav bar lose their glass material and become solid `surface`.

**Smart Invert: `.accessibilityIgnoresInvertColors(true)` at the root, nowhere else.** Naive full inversion of the Night palette computes to: `statusNeeds → #2688A8` at **3.17:1 ✗**, `textTertiary → 3.46 ✗`, `textDisabled → 2.26 ✗`. Inversion reflects luminance about 0.5, which flattens exactly the mid-luminance chromatic tokens — so letting Smart Invert run end-to-end produces a light app in which the *most important status colour fails AA*. Selective opt-out is worse than either extreme.

The real accommodation ships instead: **Daylight** is a designed light theme, one tap away in Settings and proactively offered in bright ambient light (§9.1). Smart Invert users wanting light-on-dark relief get a designed answer rather than a computed reflection.

## 10.5 Touch targets and alternative input

**44 × 44 pt minimum for every actuating control, no exceptions.** Every action lives in a list row, a swipe action, a context menu, or a sheet button — all ≥44 pt by construction. **Inline mini-buttons inside a text row are forbidden**, which is why the identity row's trailing pills are decorative (§3.1.4).

- **Voice Control:** every glyph-labelled button carries `.accessibilityLabel` **and** `.accessibilityInputLabels` with at least one plain-English alias — Voice Control users say "tap send", and a glyph has no speakable name.
- **Switch Control:** the tap shield is disabled, the re-sort gate holds on focus, and scanning order follows visual order because the tree is one element per row.
- **Full Keyboard Access:** `.focusable()` on cards and rows; `.onKeyPress(.escape)` mirrors the `.escape` actions.
- `UIAccessibility.buttonShapesEnabled` adds an underline to `.ghost` button labels — the system setting for exactly the problem `controlStrong` solves.

## 10.6 The tests that keep this honest

| Test | Asserts |
|---|---|
| `ColorContrastTests` | resolves every named colour against {light, dark} × {standard, high} and asserts the ratio tables in §9.2. Fails on regression |
| `CVDSeparationTests` | Viénot + CIEDE2000 over the five status roles; asserts no pair below 7.0 under protan/deutan (current worst: 7.5) |
| `GlyphCoverageTests` | every literal in `OrcLiterals` has a real glyph in all four bundled weights via `CTFontGetGlyphsForCharacters` |
| `FontRegistrationTests` | the four PostScript names resolve to non-system fonts |
| `HitTargetTests` | snapshot-measures every `Button` frame ≥ 44 × 44 |
| `DynamicTypeSnapshotTests` | board, card and row at `.large`, `.xxxLarge`, `.accessibility3`, `.accessibility5` on 320 / 393 / 440 pt with a 21-character worktree name |
| `SmartInvertSnapshotTest` | asserts the root carries `accessibilityIgnoresInvertColors` |
| `VoiceOverLabelTests` | every `SessionRow` state produces exactly one element with a non-empty label, value and hint |

---

# Appendix A — twenty rules a developer must not break

Each is grep-checkable or lint-able.

1. Never `Color.primary`, `Color.secondary`, `Color(.systemBackground)`, `.foregroundStyle(.tint)`. Every colour comes from `Color.orc` / the Asset Catalog.
2. Never render a status as colour alone **in rendered UI**. (Notification *titles* are the documented exception — §8.5.)
3. Never use `textDisabled` for information.
4. Never raise a tint above 0.12, and never apply one in Contrast+ or Daylight.
5. Never `.opacity()` a container whose children carry text.
6. Never slice a commit hash to 7 characters.
7. Never `Date().addingTimeInterval(-age_s)`. Use `OrcFormat.activityDate(evidenceAt:)`.
8. Never treat `session.model` as an enum — it can be `""`, `"fable-5"`, `"haiku-4-5-20251001"`.
9. Never treat `live_proc.host` as an enum — it can be `"tmux -L fleet"`, embedding the socket name.
10. Never assume seconds from `etime`. Three shapes.
11. Never join accounts on `slug`. Join on `fb_label`.
12. Never ship a glyph-labelled `Button` without `.accessibilityLabel` **and** `.accessibilityInputLabels`.
13. Never paraphrase a server `message` — it carries the remediation.
14. Never animate the list during a stream frame; only order animates, and only when the §4.1 gate opens.
15. Never put `finish` or `dispatch` behind a swipe, a double-tap, or a re-enabling button. Sheet, or nothing.
16. Never show a skeleton once data has landed.
17. Never auto-present a sheet from a poll or stream response. Auto-presentation is reserved for direct responses to a user action.
18. Never render a Unicode symbol that is not in `OrcLiterals` and covered by `GlyphCoverageTests`.
19. Never read `accessibilityReduceMotion`, `legibilityWeight` or `colorSchemeContrast` outside `body`.
20. Never **address** by `pid`. Addressing is the session (`POST /api/v1/sessions/{sid}/messages`) or the durable `agent_id` — never a pid, which is reused and can name a different agent. A pid may still appear **inside `expect`** (`API.md` §5.2) as an assertion the server checks and rejects on (`409 agent_moved`); that is the opposite of addressing by it, and it is the mechanism that makes a stale board fail loudly.

# Appendix B — desktop → mobile coverage

| # | Desktop affordance | Mobile home | Verdict |
|---|---|---|---|
| 1 | brand `user@fleet` | Fleet nav subtitle + Server | kept |
| 2 | `🚀 new mission` | Fleet accessory `＋`, Activity, quick action, Control Center, share extension | kept, multiplied |
| 3 | `sync` indicator | connection state in the accessory | kept, expanded to 8 states |
| 4 | `⌗ re-sort held` | `⌗ N updates` pill | kept, re-mechanised for touch |
| 5 | `document.title (N!)` | app badge + widgets | kept |
| 6 | left rail ×4 | tab bar (map → segment, guide → Server) | restructured |
| 7 | 5 stat tiles | headline + Counts screen | condensed |
| 8 | card availability badge | section header + identity glyph | kept, extended to 5 values |
| 9–11 | card name, loose terminals, `no live proc` | identity row + Worktree Detail | kept (accessories now decorative) |
| 12 | `✓ finish` 4-state button | swipe + Finish sheet, arm server-side | redesigned (§4.4) |
| 13–14 | gitline, commit line | identity row / Worktree Detail | kept |
| 15 | session rows (≤6) | 2 inline (1 at AX) + `+N more` | condensed |
| 16 | status label + limit suffix | status line + ticking countdown | kept, now ticks |
| 17–18 | account/model/age/subdir/branch/`⚙`, topic/last_user/last_assistant/subagent/handed_to/pending | same lines, same order | kept verbatim |
| — | *(new)* `why` / `confidence` / `provisional` / `liveness` | confidence line + Why sheet | **added** |
| 19 | `⌖ pid` chip + dashed guess | terminal attribution in Session Info; focus demoted | changed (§3.3.1, §4.7) |
| 20 | `✉ chat` | Chat screen | promoted |
| 21 | `▶ resume` + countdown | swipe action, always present, disabled-with-reason | kept |
| 22–23 | `⏱ auto` + drawer | chip + Auto-resume sheet | kept 1:1 |
| 24 | chat drawer | Chat screen | kept + real receipts |
| 25 | mission composer | Composer sheet | kept + confirmation |
| 26 | model-headroom dialog | Insufficient-headroom sheet | kept |
| 27 | dispatch progress ①–⑤ | intent frames + Live Activity | kept verbatim |
| 28 | dispatch log | Activity tab + Dispatch Detail | promoted out of the composer |
| 29 | `↩ edit again` | ‹Reopen draft› | kept |
| 30 | `/?mission=` | `orchestra://mission?text=` + share extension | kept, corrected (never auto-launches) |
| 31–32 | show-ended, bell | Filters sheet; notification tiers + foreground haptic | kept |
| 33 | other live agents | collapsed OTHER AGENTS section | kept |
| 34 | toasts | persistent inline states + accessory banners | upgraded |
| 35 | FLIP / hover hold / click shield | held reorder + pill + 700 ms tap shield | re-mechanised (§4.1) |
| 36–40 | map legend, tooltip, pinned actions, axis, lanes/riders | Branches header, row content, shared finish machine, per-group clamped axis, three roles | kept, redrawn |
| 41 | *(map silently drops worktrees)* | `unmapped` footer with reasons | **fixed** |
| 42–43 | limits reload / force / fetched-ago; account cards | Limits header + accessory; Accounts list | kept, refresh relabelled |
| 44 | reserve number input + `alert()` | slider + inline errors | improved |
| 45 | per-limit bars | Account Detail; countdown always shown | improved |
| 46 | guide §1–§5 | Why sheets + Manual + Status legend | restructured |
| 47 | `GET /api/focus` | Session Info + long-press submenu; ‹Send attach cmd to the Mac› replaces it | demoted (§4.7) |

**Mobile-only additions:** push with lock-screen reply · widgets · Live Activities · server-proven delivery receipts · dispatch and finish reconciliation · self-clearing refusal rows · ticking countdowns · offline / stale / asleep honesty · clock-skew correction · confidence and provenance surfacing · QR pairing · share-extension missions · kill · `unmapped` worktrees · ‹Send attach command to the Mac› · Daylight theme · Contrast+ mode.

# Appendix C — known sharp edges

Small, real, cheap to handle — collected here rather than threaded through, because these are what produce confusing bug reports.

1. **The QR encoder** is 250–400 lines, not ~120; needs a version-5-ish 37×37 symbol and a 4-module quiet zone; must render light-on-dark-safe with a `--pair --invert` escape.
2. **`_tmux_resume` forks the session id.** `claude --resume <sid>` may surface as a *different* sid, orphaning the schedule key `"{worktree}|{sid}"`, the `resume_fired` deep link, and any armed Live Activity. Until the server emits `resumed_to_sid`, the notification deep-links to the **worktree**.
3. **A session aging past `session_window_h` (48 h) vanishes from state entirely.** An open Chat screen shows `○ this session is no longer on the board — its transcript is older than 48 hours`, with the last-loaded transcript still readable; an armed schedule for it renders `⚠ session no longer tracked` and offers Disarm.
4. **`POST /api/send` with a non-numeric pid** raises an uncaught `ValueError` server-side, killing the connection — the client sees a **transport error with an empty reply**, not an HTTP status. Error mapping treats "connection closed with zero bytes after a POST" as a distinct case: `the server rejected that request` + ‹Report›.
5. **Attaching to an already-attached tmux session resizes the pane** and can garble a running TUI. Stated wherever an attach command is offered.
6. **`resets_at` vs `resets_in`.** `resets_at` is computed as `fetched_at + resets_in` — the same data re-anchored, not fresher. Use it because it is **absolute**, so it survives transport delay and app suspension; the real fix for staleness is the collector's limits lane plus clock-skew correction.
7. **`git.commit.subject` and topology `branch.subject` are untruncated**, unlike every session text field. Capped at 3 lines in Worktree Detail; never on the board.
8. **`session.model` is not an enum** and can be `""` (observed live alongside `fable-5`, `opus-4-8`, `haiku-4-5-20251001`). Render `—` for empty.
9. **`live_proc.host` is not an enum either** — it can be `"tmux -L fleet"`. Key off the `tmux` target, never off `host`.
10. **`git.ahead`/`behind` are null with no upstream** (5 of 9 live cards) and can be enormous (`behind: 2030`). Omit the pair entirely when null.
11. **Conditional session keys are absent, not null** — `limit`, `handed_to`, `tool_running`, `bg_shell`, `closeout_sent`. `decodeIfPresent` everywhere; booleans default `false`.
12. **`/api/state` is 36,098 bytes** on the live 9-worktree / 33-session fleet. (9.0 KB is the *gzipped* figure — do not quote it as the payload size.)
13. **The dispatch log is append-only, never rotated**, read whole-file on every call, and contains verbatim mission prose including production identifiers. Fetched only when Activity is visible; never included in any push payload.
14. **`/api/limits?refresh=1` can exceed iOS's default 60 s request timeout** (90 s server-side subprocess). That one call gets `timeoutIntervalForRequest = 120`.
15. **`_closeouts` and `_jobs` are in-memory only.** A server restart reverts `✕ close` to `✓ finish` and turns any in-flight job id into `unknown job` — which means **lost**, not failed.

# Appendix D — backend dependencies, in dependency order

```
   ┌─ BE-0  TLS + bearer token  ──────────────────┐   (nothing works without these)
   └─ BE-1  identity-addressed send + receipt     ┘
                     │
   BE-A  continuous collector  ← the real cost centre
                     │
        ┌────────────┼─────────────┐
        ▼            ▼             ▼
   BE-B  SSE     BE-C  intents   BE-D  transition differ
                     │             │
                     └────────► BE-E  push fan-out (APNs / ntfy)
```

**Ship-gating — nothing meaningful ships without all four:**

| # | Change | Why it gates |
|---|---|---|
| **BE-0** | TLS via `tailscale cert` + bearer token + `Origin`/`Host` allowlist + QR pairing | ATS makes the **first request in the app** fail without TLS. And a tailnet bind today hands every peer unauthenticated RCE. Browser CSRF works on pure loopback today — fix that first, independently. |
| **BE-1** | `/api/send` takes `{account, sid, expect_sid}`; routes through `deliver_text`; returns a `_proven_in_transcript` receipt; `--` sentinel before user text; `Idempotency-Key` on every POST with a `{key: response}` map (TTL 10 min) | Fixes a live bug, makes `✓✓` a fact, deletes the client's leading-dash guard, and — because URLSession's retransmission is not app-configurable — is the **only** thing standing between a dropped connection and a second agent. |
| **BE-A** | continuous collector: git 5 calls → 2, transcript cache, proc facts, publish off the request path | 1641 ms → ~314 ms. Without it, transition detection is a permanent ~33 % subprocess duty cycle, and with the phone asleep and no browser open **nothing looks at all** — push is impossible, not merely late. |
| **BE-E** | pluggable push sink (`apns` via `openssl` + `curl --http2`; `ntfy` fallback), DER→JOSE conversion, JWT rotation at 45 min, 410 token deletion | without push this is a website you have to remember to open. |

**Then, in order** (names as `API.md` spells them; `ROADMAP.md` sequences them into M1–M12): SSE `GET /api/v1/stream` with `?since=<epoch:seq>`, `dg` digest and a subscriber cap · durable **ops** (`op_…`) with phases at `/api/v1/ops/{op_id}` · `POST /api/v1/devices/self/push` + per-device prefs/tz/quiet-hours + `GET /api/v1/meta`'s `device.push` block · `GET /api/v1/health` and `GET /api/v1/meta` (identity, `features[]`, `config{}`) · ActivityKit update + push-to-start tokens · epoch `ts` and the idempotency key in `dispatch.log.jsonl` · `POST /api/v1/agents/{ag_id}/kill` · **five-valued `availability`** + `counts.cards` + `order` · `sessions_total`/`sessions_shown` · terminal **`attribution` + `why`** (§3.3.1) · the **`confidence`/`provisional`/`evidence_source`** block (§3.1.4) · `parse_qs` for all query params · the `before=`/`after=` cursor on `/api/v1/sessions/{sid}/messages` · topology **`dropped[]`**, `base_ts`, `axis.s`/`axis.anchor_age_s`, `commits_capped`/`commits_oldest_ts` (§5.3, §5.7, §5.8) · single-flight + negative-cache on `_limits`, atomic `save_resumes`, locks on `_cache`/`_closeouts` · `--demo` sandboxing + demo/real parity · explicit `0` reserve · `try/except` around dispatch and `do_POST` · server-side per-worktree finish lock · `firing_since` on schedules · a pasteboard endpoint (§4.7 — **undefined in `API.md`**, see §0.2).

**Constraints every item inherits**, from the existing test suite's shape: all functions stay at module scope in `orchestra.py`; all subprocess work goes through the single `run()` seam; new module-level mutable state is added to `ConfigGuard`; every new endpoint gets a `TestHTTPSmoke` case plus a demo-refusal assertion; **zero pip installs**; the CI floor is Python 3.11.

# Appendix E — four corrections to back-port to the desktop

The two clients are meant to be visually identical, so four findings belong upstream in `index.html` / `map.html`:

1. `--accent-2` `#E8A87C` → **`#EDB9AC`** — 3.4× colour-vision separation, and higher contrast (§10.3).
2. `.badge.attention { background: var(--accent-glow) }` at α 0.25 puts accent text on its own fill at **3.72:1** — a live AA failure on the product's highest-priority badge. Standardise all tints on 0.12.
3. `--muted-2` `#6A6764` carries `.meta` — model and age on every session row — at **3.0:1**. Promote to a `#8A8784`-class tier, or stop using it for data.
4. `.sess.ended { opacity: .55 }` puts row text at **2.43:1**. Use a darker ground and a lighter text token instead.

# Appendix F — where this design holds ground

- **The `waiting` tier stays out of push and out of the badge.** It fires at the end of every turn; pushing it costs the user's trust in tier 1. The real objection it answered — a `waiting`-only card rendering an accent NEEDS-YOU badge while the headline said "all clear" — is fixed at the source by five-valued availability, not by promoting the tier.
- **Reply has no confirmation dialog.** It is the app's primary job. Safety comes from server-side identity assertion, not from friction.
- **Dispatch has a confirmation sheet.** Irreversible, spends money, and — even with a kill endpoint — a launched agent has already begun writing files.
- **Chat is a pushed screen, not a sheet.**
- **The map is rewritten, not ported.** Hand-estimated glyph advances, 4–13 px hit targets against a 44 pt floor, a rider row-packer, a hard 900 px floor, and hover-only detail. Rotating to one row per branch eliminates the label-collision problem entirely.
- **`/api/focus` stays demoted**, and its substitute is a server-side pasteboard write rather than a proximity-bound clipboard copy.
- **The widget's honesty rule stays** — a dash beats a confident zero. What the NSE mechanism changes is that the dash becomes rare.
- **Four tabs, no centre `＋`.** `＋` lives in the thumb-reachable bottom accessory, and the confirmation sheet — not the reach distance — is the guard.




