# Handoff — remote over-the-air (OTA) install to the phone

**For a fresh agent. Start a new branch off `engine`.** This is a distinct task — iOS
distribution + build tooling, not the observer/app work the rest of `docs/mobile/` covers. You do
not need this repo's full history; you need this document, `METHOD.md`, and the verified facts below.

---

## The goal, in the owner's words

> "When I'm traveling and I make a change to ConfidAI, I want to trigger a build on my Mac and have
> it install on my phone over Tailscale — a local dev build to try a feature, **not** TestFlight."

The end state: from the phone, trigger *"build project X and send it to me"* → the Mac builds and
serves it → a **push notification** arrives with an install button → tap → it installs. Over
cellular, from anywhere on the tailnet.

---

## The one finding that determines the whole design (proven, not assumed)

The obvious approach — "push an `.ipa` to the phone like Xcode does" — **was tested and it fails
remotely.** With the phone on cellular + Tailscale only (Wi-Fi off), `devicectl` install returned:

```
ERROR: CoreDeviceService was unable to locate a device matching the
       requested device identifier. (com.apple.dt.CoreDeviceError error 1011)
```

But a `tailscale ping` to the same phone answered in **162 ms**. So:

- **Raw connectivity over Tailscale is fine.** Tailscale routes to the phone perfectly.
- **Apple's device *discovery* is the blocker.** `devicectl`/Xcode locate a phone via CoreDevice
  (Bonjour/USB), and that mechanism does not cross the tailnet. It is not a connectivity problem;
  it is that Apple only looks on the local network.

The consequence that shapes everything:

> **Development install has the Mac *push* to the phone (Mac must discover phone → fails remotely).
> OTA ad-hoc has the phone *pull* from the Mac (phone opens an HTTPS URL → works, same direction as
> the app already hitting `/api/state`).**

So the mechanism is **over-the-air ad-hoc distribution**, and the "complexity" is not signing
bureaucracy — it is the only model where the phone reaches *out*, which is the only direction that
survives being remote. Do not try to make `devicectl`/Xcode wireless install work over Tailscale;
it was tested and it does not.

---

## How OTA ad-hoc install works

1. Build an **ad-hoc-signed** `.ipa` — the phone's provisioning UDID is baked into the profile.
2. Generate a `manifest.plist` — carries the `.ipa`'s HTTPS URL, the bundle id, version, title.
3. Serve `manifest.plist` **and** the `.ipa` over **HTTPS with a cert iOS trusts**.
4. The phone opens `itms-services://?action=download-manifest&url=https://…/manifest.plist` → iOS
   downloads and installs.

**HTTPS with a *trusted* cert is mandatory and is where OTA silently fails.** iOS rejects plain
HTTP and rejects self-signed certs for this. This is the linchpin — see below.

---

## Verified facts (measured on this machine — do not re-derive, but re-check cheaply if unsure)

| fact | value | how it was checked |
|---|---|---|
| **Tailscale can issue a trusted HTTPS cert** — the linchpin | domain `achills-macbook-pro.tail1205d9.ts.net` | `tailscale cert` is available and names this domain. **You must still actually provision it** (`tailscale cert <domain>` / `tailscale serve`) and confirm the phone trusts it. |
| Mac's tailnet address | `100.113.110.31` | `tailscale status` |
| Phone is a tailnet peer | `iphone172` = `100.121.77.98` | `tailscale status`; `tailscale ping iphone172` → 162 ms over cellular |
| **Team that owns everything** | `4K738RNZAA` (paid) | owns `sh.orchestra.app` + the APNs key |
| Ignore this team | `MWF387CQWG` | the *free personal team* Xcode grabs by default — not where anything real lives |
| **Distribution cert present** (ad-hoc export needs one) | `Apple Distribution: Achill Rudolph (4K738RNZAA)` | `security find-identity -p codesigning -v` |
| Device | iPhone 16 Pro Max, CoreDevice UUID `A008418C-6680-555E-B4F8-D30309411F20` | `xcrun devicectl list devices` |
| **Provisioning UDID** (≠ the CoreDevice UUID above) | **not yet captured** — phone was cellular-only | Get it with the phone on USB/LAN: `xcrun xctrace list devices \| grep 'iPhone 16 Pro Max'` (the `(…)` is the UDID). Register it in the ad-hoc profile. |
| Push works end-to-end | proven today | real device token, `--send-test-push`, banner received |
| APNs config is complete | `orchestra.config.json` (gitignored) | `apns_team_id`/`key_id`/`key_path`/`topic`/`environment` all set and validated |
| The Orchestra app builds + runs on the phone | development-signed | `docs/mobile/README.md`, and it is installed now |

---

## What to build — in milestones, each proven before the next

**Prove the mechanism with the Orchestra app first. Do NOT touch ConfidAI** (the owner's separate
big app) until the whole path is proven — then pointing it at any `.xcodeproj` is a config change.

### M1 — OTA install works at all, driven by hand
- Register the phone's provisioning UDID in an ad-hoc profile for `sh.orchestra.app` (team
  `4K738RNZAA`). Automatic signing with `-allowProvisioningUpdates` and the ad-hoc/"release-testing"
  export method can create it; or the portal.
- Script: `xcodebuild archive` → `xcodebuild -exportArchive` with an `exportOptions.plist`
  (`method: release-testing` — the current name for ad-hoc — `teamID: 4K738RNZAA`) → produces
  `Orchestra.ipa`.
- Write `manifest.plist` (byte-mode template: `software-package` = the ipa URL, `display-image`
  optional, plus `bundle-identifier` / `bundle-version` / `title`). **Both URLs must be the HTTPS
  tailnet URL.**
- `tailscale cert achills-macbook-pro.tail1205d9.ts.net`, then serve the ipa + manifest over HTTPS
  (`tailscale serve`, or a small stdlib HTTPS server using the tailscale cert — mirror how the rest
  of orchestra is stdlib-only).
- **Test it for real:** phone on cellular (Wi-Fi off), open the `itms-services://` link in Safari on
  the phone, watch it install. This is the go/no-go for the whole feature. Screenshot it.

### M2 — delivered by notification, tapped from the lock screen
- A push whose tap opens a one-button install page (served over the same tailnet HTTPS) whose button
  is the `itms-services://` link. (`itms-services` only opens in Safari, so a notification cannot
  link to it directly — it deep-links to the page, or opens Safari at it.)
- Reuse the push pipeline (`orchestra/push.py`) and the notification-action plumbing already in the
  app.

### M3 — a first-class orchestra mission
- `POST /api/v1/build-install` (auth'd like everything else): body names a project + scheme +
  device; the server archives, exports ad-hoc, publishes the manifest, and pushes the link. Model it
  as a job with progress, like dispatch.
- A UI affordance in the app: pick a project → "build & send to this phone" → watch progress →
  notification → install.

### M4 — generalize to ConfidAI
- Only now. It is the same script pointed at ConfidAI's `.xcodeproj`/`.xcworkspace` and its own
  team/bundle id. ConfidAI already ships from `4K738RNZAA` (its distribution cert is the one on this
  machine), so its UDID list and ad-hoc profile are a portal step, not new code.

---

## Sharp edges (each of these is a silent failure waiting to happen)

- **The HTTPS cert must be *trusted by the phone*.** Prove it — open `https://achills-macbook-pro.
  tail1205d9.ts.net/…` in Safari on the phone and confirm no cert warning. A self-signed cert or a
  plain-HTTP URL makes iOS refuse the install with a uselessly vague error.
- **The provisioning UDID is not the CoreDevice UUID.** `A008418C-…` is the wrong one; fetch the real
  UDID (above) or the profile will build but the install will fail on-device.
- **`itms-services://` only opens in Safari.** Not in-app, not from a raw notification tap. Deep-link
  to a web page with the button, or open Safari at it.
- **Ad-hoc limits:** the profile expires (~1 year) and caps at 100 devices/year. Fine for a few
  personal devices; not a distribution channel.
- **Manifest URL correctness:** the `.ipa` URL *inside* `manifest.plist` and the manifest's own URL
  must both be the reachable HTTPS tailnet URL. A localhost or LAN URL baked in will fail remotely —
  this is the same "advertise the bound address, not loopback" lesson the pairing QR already learned.
- **Serve the right Content-Types** for `.plist` and `.ipa`.
- **Bundle version:** bump `CFBundleVersion` per build, or iOS may treat a reinstall as a no-op.
- **Do not `git add -A`** in this repo — there is concurrent work. Add paths by name.

---

## Working conventions (this project's, and they are load-bearing — read `docs/mobile/METHOD.md`)

- **Test, don't assume.** This entire feature exists *because* the "push to phone" idea was tested
  and failed with a real error, rather than believed. A claim without a measurement is not a claim.
- **Look at the result.** After an install, screenshot the phone and read it. A thing that "should
  work" is not a thing that worked.
- **Fail closed on anything security-adjacent**, compare secrets with `hmac.compare_digest`, generate
  with `secrets`. The build-install endpoint runs `xcodebuild` — treat "what may I build" as a
  trust boundary.
- **Stdlib-only on the server side** (zero-dependency Python is the project's identity; ADR 0010).
- **Secrets stay gitignored:** the `.p8`, `orchestra.config.json`, `ios/Signing.xcconfig`, and any
  `.ipa`/archive/manifest you produce. Confirm `git check-ignore` before committing anything near
  them. `*.p8`, `.orchestra/`, and `Signing.xcconfig` are already ignored.
- **Commit per milestone**, project voice: lowercase subject stating the user-visible truth, a body
  explaining *why*, and the trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Branch off `engine`** (currently at `33c2808`). Do not work on `main` or `engine` directly.

---

## Orientation — where things are

```
orchestra/            the zero-dependency stdlib server (package; ADR 0010)
  push.py             the APNs pipeline you will reuse for the notification (M2)
  server.py           request routing + auth guard; add the build-install route here (M3)
  auth.py             per-device bearer tokens; every mutating route goes through the guard
ios/                  the SwiftUI app (Swift 6). Orchestra.xcodeproj, Package.swift
  Signing.xcconfig    gitignored; DEVELOPMENT_TEAM = 4K738RNZAA
  Orchestra.entitlements  aps-environment + team-prefixed identifiers (see its own comment)
docs/mobile/
  METHOD.md           READ THIS — how to change this system without shipping a silent bug
  README.md           project status + the development path (steps 0–9, all shipped)
  adr/0013-…          plain HTTP over the tailnet — and the note that `tailscale serve` becomes
                      worth it "if cert management becomes desirable". It just did — this is that.
  VERIFIED-FACTS.md   measured platform facts (fd limits, openssl/curl for APNs, etc.)
```

## First 15 minutes

1. Branch off `engine`. Read `METHOD.md` and this file's "sharp edges".
2. With the phone on USB (or same Wi-Fi), get the provisioning UDID and fill the blank in the facts
   table.
3. `tailscale cert achills-macbook-pro.tail1205d9.ts.net` and serve a trivial file over HTTPS; open
   it in Safari on the phone (cellular, Wi-Fi off) and confirm **no cert warning**. If that fails,
   nothing downstream can work — fix it first.
4. Then M1.
