# APNs setup — exactly what to do at developer.apple.com, in order

The push pipeline is built, tested, and wired. The one thing it cannot contain
is your **APNs auth key** — a `.p8` file only you can create, because it signs
for your whole Apple developer team. This document is the ten minutes of
clicking that produces one, and the four values you paste back.

Until you do this, everything still runs: events are derived, logged, deduped,
coalesced, and the pipeline reports *"no APNs key configured"* at the last hop.
`python3 -m orchestra --send-test-push` will tell you precisely which piece is
missing at every step, so you can do this in any order and check your work as
you go.

> **You need the paid Apple Developer Program membership ($99/yr).** The free
> tier cannot enable the Push Notifications capability and cannot create an
> APNs auth key at all — this is the single biggest thing the membership buys
> for this project. You have confirmed you hold it.

---

## The four values you are collecting

By the end you will paste these into `orchestra.config.json`:

| config key | what it is | where it comes from |
|---|---|---|
| `apns_key_path` | path to the `.p8` file | downloaded once, in step 3 |
| `apns_key_id` | 10 characters | shown beside the key, steps 2–3 |
| `apns_team_id` | 10 characters | top-right of the portal, step 0 |
| `apns_topic` | your app's bundle id | the App ID, step 1 |

---

## Step 0 — your Team ID (30 seconds)

1. Sign in at **<https://developer.apple.com/account>**.
2. Top-right, under your name, is a 10-character **Team ID** (e.g. `A1B2C3D4E5`).
   Copy it — that is `apns_team_id`.

## Step 1 — an App ID with Push enabled (2 minutes)

The bundle id you register here must be **byte-for-byte** the one in the Xcode
project (`PRODUCT_BUNDLE_IDENTIFIER`). A mismatch is delivered by Apple as
`DeviceTokenNotForTopic`, which reads like a device fault and is a naming one.

1. **Certificates, Identifiers & Profiles** → **Identifiers** → **+**.
2. Choose **App IDs** → **App**.
3. **Bundle ID: Explicit**, and enter your app's bundle id.
   - The iOS side of this project uses **`sh.orchestra.app`** (see
     `IOS-APP.md` and `ARCHITECTURE.md`). Use that unless you have already
     shipped a build under another id.
4. Under **Capabilities**, tick **Push Notifications**.
5. **Continue** → **Register.**
6. That bundle id is `apns_topic`.

> **Time Sensitive Notifications.** The P1 tier (a question, a block) uses
> `interruption-level: time-sensitive`, which is what reaches you *through* a
> Focus mode — including Sleep, the 2 a.m. blocked-agent case this feature
> exists for. Add the **Time Sensitive Notifications** capability to the app
> target in Xcode (Signing & Capabilities → + → Time Sensitive Notifications)
> and make sure it is in the provisioning profile. Without it iOS silently
> downgrades P1 to `active`, and any Focus suppresses it, with no server-side
> error. **Do not** request Critical Alerts — that is a separate Apple approval
> with lead time, not plausibly granted for a developer tool, and the pipeline
> never asks for it.

## Step 2 — create the APNs auth key (2 minutes)

One key signs for **every** app on your team, sandbox and production both. You
do this once, ever — not per app, not per environment.

1. **Certificates, Identifiers & Profiles** → **Keys** → **+**.
2. **Key Name:** anything (e.g. `orchestra push`).
3. Tick **Apple Push Notifications service (APNs)**.
4. **Continue** → **Register.**

## Step 3 — download it (and understand you get ONE chance)

1. On the confirmation page, note the **Key ID** — 10 characters (e.g.
   `2X9R4HXF34`). That is `apns_key_id`. It is also shown forever in the Keys
   list, so this one is recoverable.
2. Click **Download**. You get `AuthKey_<KeyID>.p8`.
   - **This download happens exactly once.** Apple never lets you download the
     private key again. If you lose it, you revoke the key and make a new one —
     there is no recovery. Put it somewhere backed up.
3. Move it next to the server and lock it down — the pipeline **refuses** a
   world-readable key, because it signs for your whole team:

   ```sh
   mkdir -p ~/.orchestra/apns
   mv ~/Downloads/AuthKey_*.p8 ~/.orchestra/apns/
   chmod 600 ~/.orchestra/apns/AuthKey_*.p8
   ```

## Step 4 — paste the four values

Edit `orchestra.config.json` (next to the package, or wherever `--config`
points). Add:

```json
{
  "apns_key_path": "/Users/YOU/.orchestra/apns/AuthKey_2X9R4HXF34.p8",
  "apns_key_id": "2X9R4HXF34",
  "apns_team_id": "A1B2C3D4E5",
  "apns_topic": "sh.orchestra.app",
  "apns_environment": "production"
}
```

**`apns_environment`** decides which of Apple's two hosts you talk to, and it is
the single most common cause of *"push just doesn't work"*:

- a build run from Xcode onto a device is **`sandbox`**;
- a TestFlight or App Store build is **`production`**.

The two are **not** interchangeable — a device token registered against one is
meaningless to the other. The server auto-heals a wrong guess once (on Apple's
`400 BadDeviceToken` it retries the other host and remembers the correction),
but set it to match how you build to avoid the extra round trip. The **app**
reads this from its embedded provisioning profile's `aps-environment` at runtime
and sends it up on registration — never trust `#if DEBUG`, because TestFlight
builds are `DEBUG=0`.

## Step 5 — prove it, end to end

With a phone paired and the app having registered its push token:

```sh
python3 -m orchestra --send-test-push
```

- **`200 · apns-id …`** — done. The notification is on its way; you should see
  it on the phone. Push is live.
- **`403 · InvalidProviderToken`** — the credential is wrong. This ONE message
  covers several causes, in rough order of likelihood: a `apns_key_id` that
  does not match the `.p8`; a `apns_team_id` typo; a `.p8` from a different
  team; or a Mac clock more than an hour off (the token embeds `iat`). Re-check
  steps 0–3. *(This is the message you get with a self-made key that Apple has
  never seen — it is what this pipeline returns today, and it means the
  transport is working and only the key is unregistered.)*
- **`400 · BadDeviceToken`** — the environment is wrong, or the device token is
  stale. Confirm `apns_environment` matches your build; re-open the app to
  re-register.
- **`403 · MissingProviderToken`** / **`DeviceTokenNotForTopic`** — `apns_topic`
  does not match the bundle id the token was minted for. Re-check step 1.
- **`no APNs key configured`** — you have not set the four values, or the `.p8`
  path is wrong. `--send-test-push` prints exactly which.
- **`no paired device has registered a push token`** — pair a phone first
  (`/pair`), then let the app register before running this.

---

## What was verified without your key, and what only your key can prove

Everything except Apple accepting the provider token is already proven, on this
machine, against Apple's real sandbox:

- **The ES256 signature is cryptographically valid.** `openssl` emits a DER
  signature; JWS needs raw `r‖s`, 64 bytes. That conversion (`push.der_to_raw`)
  is verified three independent ways in `tests/test_push.py`: a from-scratch
  P-256 verifier that shares no code with it, `openssl dgst -verify`, and frozen
  known-good vectors — including the short-component cases (a signature half
  under 32 bytes, ~1 in 256) that a plausible parser gets wrong and that Apple
  would reject as `InvalidProviderToken` with no other symptom.
- **The transport reaches Apple.** A real HTTP/2 `POST` to
  `api.sandbox.push.apple.com` negotiates HTTP/2 and comes back with an
  `apns-id` and a reason — measured `403 InvalidProviderToken`, the correct
  answer for a key Apple has not registered.

The only thing your `.p8` adds is a key Apple recognises, which turns that
`403` into a `200`. Nothing else in the path changes.

---

## A note on what the docs got wrong (server is the contract)

Two things measured while building this, recorded here because
`METHOD.md` §4 says the docs have been wrong before:

1. **`ARCHITECTURE.md` §6.1's DER length distribution is
   `{70: 96, 71: 194, 72: 110}`** — it never observed a signature with a
   component shorter than 32 bytes. Re-measured here over 400 fresh signatures:
   `{69: 1, 70: 93, 71: 204, 72: 102}`, including a 69-byte DER whose `s` was 31
   bytes. That short-component row is the exact case left-padding exists for,
   and a reader trusting the doc's distribution would conclude `rjust` is
   defensive rather than load-bearing. The conversion and its tests handle it;
   the doc's table understates the risk it warns about.

2. **The bundle id was inconsistent across the docs** — now resolved. The
   Xcode project builds **`sh.orchestra.app`** (verified in
   `project.pbxproj`), and the server config and this guide match it. Three
   stray `com.acrdlph.orchestra` examples in `API.md` and `UX.md` were
   corrected to agree. Register `sh.orchestra.app` in step 1.
