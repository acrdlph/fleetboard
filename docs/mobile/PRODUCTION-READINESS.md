# Production-readiness — what's built, and what's left

An honest ledger. "Built" means verified working on real hardware or by a test, not specced.
The app is genuinely useful today; this is the gap between *useful* and *something you'd trust
unattended for a year*.

## Built and proven

- **Backend:** continuous observer (~5% of a core), kqueue-driven, versioned immutable snapshots,
  SSE delta stream, per-device bearer auth, a tailnet bind that fails closed, the APNs pipeline
  (ES256 signature verified three ways, real HTTP/2 to Apple). 912 python tests + a 5,656-case
  characterization net.
- **App:** pairs over the tailnet (QR + token), live board via SSE deltas, acts (chat, dispatch,
  finish, resume), branch map, **real push received on a physical iPhone**, network-path awareness
  for weak links.
- **Auth to the server:** the app authenticates with a per-device token in the Keychain, revocable.

---

## Tier 1 — Security & safety (do first; small, high-stakes)

The server types into terminals running `--dangerously-skip-permissions` and dispatches agents
that spend money. That raises the bar for everything that can reach it.

1. **Biometric gate on the app itself (Face ID / passcode).** *Not built.* Today anyone holding
   your **unlocked** phone can open Orchestra and drive the fleet. Add `LocalAuthentication`:
   require Face ID to reveal the board (or at minimum before any *mutation* — dispatch, send,
   finish). Small: one `LAContext` wrapper + a gate in `RootView` / before the act paths. This is
   the single most important missing piece and it is the one you asked about.
2. **A real security review of the exposed surface.** The `/security-review` treatment on the
   auth path, the request guard, and the actuation layer, before this is trusted long-term.
   `METHOD.md` §7 already names which direction is dangerous.
3. **Token scopes (`read` / `act` / `admin`).** *Not built — deliberately deferred* (`auth.py`
   reserves the design; a half-built scope ladder is worse than an honest absence). Today one
   token = full fleet control. A read-only device, or a fresh-biometric requirement for admin
   actions, is defense in depth.

## Tier 2 — Distribution & stability

4. **TestFlight**, or the **remote OTA install** feature (handoff in
   `HANDOFF-remote-ota-install.md`). The current device build is development-signed and needs the
   Mac; neither is a sustainable way to keep the app on your phone. TestFlight is the normal
   answer; OTA is the "install while traveling" one.
5. **App icon + asset catalog.** *Not built* — no `.xcassets`. Required for TestFlight/App Store,
   and the way brand colours reach out-of-process surfaces (widgets, notification content).
6. **iOS CI.** The python suite runs in CI; the app does not. Add `xcodebuild build` + `swift
   test` on push, so a Swift regression is caught like a python one.
7. **Bundle IBM Plex Mono.** The app uses the system SF Mono, not the brand face — cosmetic, but
   it is the difference between "looks like orchestra" and "looks close".

## Tier 3 — The phone superpowers (specced in UX.md, not built)

8. **Home Screen / Lock Screen widget** — "who needs me" at a glance without opening the app.
9. **Live Activity** — a running mission on the lock screen, updating live.
10. **Notification polish** — inline reply is wired; still wanted: snooze, quiet-hours UI,
    per-event-type preferences, thread-id grouping.

## Tier 4 — Backend loose ends (from the README open-items table)

11. **`resumes` don't ride the stream.** They live in `resume.py`, which the observer doesn't
    watch, so a stream-only client (the phone) learns about auto-resume changes via a side poll,
    not the delta stream. Fine today; name it before it surprises someone.
12. **Transcript-corpus retention.** Orchestra's own inputs grow ~1,000 files/day (~5 GB now).
    There's a backup job; there is no pruning. A disk on a laptop is finite.
13. **The four UX back-ports (UX.md Appendix E)** so the app and the desktop board agree
    pixel-for-pixel on colours and glyphs.

---

## Suggested order

Tier 1 is a few days and removes the scariest gaps — **start with the biometric gate.** Tier 2
makes it something you can keep on your phone without a cable. Tier 3 is where it starts feeling
like a product rather than a tool. Tier 4 is housekeeping that can trail the rest.

Nothing here blocks daily use over the tailnet with the phone in your own pocket — it is the list
that turns "works for me" into "safe to leave running."
