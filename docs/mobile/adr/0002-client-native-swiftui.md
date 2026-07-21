# ADR 0002 — The mobile client is native SwiftUI, iOS only

**Date:** 2026-07-21 · **Status:** Accepted

## Context

The user asked for a "highly performant and also great looking & functional" mobile app, iOS
only for now, with UX quality called out as the top priority.

## Decision

**Swift 6 + SwiftUI, native, iOS only.** No cross-platform layer.

## Consequences

- Full access to the platform features that make this product work on a phone: Live Activities
  for running missions, Home Screen and Lock Screen widgets, notification actions with inline
  reply, Dynamic Type, haptics, 120 Hz scrolling.
- iOS-only means no cross-platform abstraction tax — the codebase can use platform idioms
  directly.
- Requires Xcode and a Mac. The user has both.
- Android would be a separate codebase later. Accepted; not a goal.
- Swift 6 strict concurrency is a real cost — actor isolation and `Sendable` conformance will
  shape the networking and store layers. This is a known sharp edge, not a surprise.

## Alternatives rejected

| option | why rejected |
|---|---|
| React Native / Expo | Faster to iterate and opens Android, but costs native polish. Widgets and Live Activities need native modules anyway, so the cross-platform saving is smaller than it looks for exactly the features that matter here. |
| Installable PWA | Ships fastest and reuses `index.html`, but no Live Activities, no widgets, unreliable background behaviour, and it would never feel native — directly contrary to the stated priority. |
