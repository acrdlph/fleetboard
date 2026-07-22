"""orchestra.pairing — how a token gets onto a phone without being typed.

Until now a device token was minted at a shell (`--add-device`), printed once,
and carried to the phone by hand. Forty-three base64 characters, typed into a
phone keyboard, exactly once, with no feedback if you get one wrong. That is not
a flow anybody completes twice, and a credential that is annoying to install is
a credential that gets pasted into a notes app.

## The shape, and why the QR does not carry the token

A short-lived **pairing code** is minted on the Mac and shown on the screen. The
phone presents it to `POST /api/v1/pair` and gets a device token back. The QR
carries the code, not the token.

That indirection is the entire security argument, so it is worth stating why it
is not paranoia. A QR on a screen is **visible to the room**. Somebody behind
you, a video call you forgot was sharing, a screenshot in a bug report, a photo
in a Slack thread six months from now — all of them capture whatever is in the
picture, permanently. A token in that picture is a permanent credential to a
server that can type into terminals running `--dangerously-skip-permissions`. A
pairing code in that picture is worthless 120 seconds later, and worthless
immediately if anybody has already used it.

So the properties are chosen against *that* photograph:

| property | why |
|---|---|
| **single use** | the first claim closes the window. A second phone with the same photo gets `pairing_not_open`, and — the part that matters — the user sees one device appear when they expected one |
| **120 s** | long enough to unlock a phone and open the camera, short enough that a photograph is stale before it is out of the room. It is also the number API.md §3 already specified |
| **the code is not the token** | claiming it mints a fresh 256-bit secret that was never on screen |
| **loopback or tailnet only** | a claim from anywhere else is refused before the code is even compared |
| **attempts are budgeted per IP** | 40 bits of code is not brute-forceable, but the budget is what stops the attempt log being flooded, and it is per-IP so one hostile peer cannot lock out the phone that is actually pairing |

## The code alphabet

Eight characters of **Crockford base32** — `0123456789ABCDEFGHJKMNPQRSTVWXYZ`,
which is the ordinary alphabet with `I`, `L`, `O` and `U` removed. That is 40
bits, and the removals are the point: this string has to survive being read off
a screen by a human when the camera does not work, and `I`/`1`, `O`/`0` and
`L`/`1` are where that goes wrong. `U` is dropped for a different reason, which
is that its presence makes accidental profanity possible.

Normalisation is symmetric and generous — strip spaces and dashes, uppercase,
then fold `I`→`1`, `L`→`1`, `O`→`0`, `U`→`V` — so `7k3m-9qp2` and `7K3M9QP2` and
`7K3M 9QP2` are the same code. It is generous about FORM and not at all generous
about VALUE: the comparison is `hmac.compare_digest` over sha256, never `==`.

## The state is in memory, and that is the design

The open window is a module global. It does not survive a restart, and it must
not: a pairing window is a temporarily open door, and a door that reopens by
itself when the server restarts is a door nobody closed. Restarting orchestra
closing every pairing window is the correct behaviour, not a limitation.

Consequently there is nothing to write and nothing to clean up — the only
persistent effect of a successful pairing is the device that `auth.add_device`
appends to the registry.

## What this module deliberately does not do

* **It does not decide who may OPEN a window.** That is `auth.check`'s job, and
  the answer is "this machine, holding no token" — see `auth.ADMIN`. A phone
  that could open a pairing window could pair a second phone.
* **It does not issue scopes.** ADR 0014 defers them whole; one token grants
  everything, and `tokens.read`/`tokens.act` in API.md §3.3 both being the same
  string today would be a lie about what has been built. The response carries a
  single `token`, and API.md says so.
"""

import hashlib
import hmac
import secrets
import socket
import threading
import time
import urllib.parse

from . import auth, config, qr, tailnet

# Crockford base32 minus nothing — this IS Crockford's alphabet. I, L, O and U
# are absent by construction, which is what makes the manual fallback usable.
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
CODE_LEN = 8                      # 32**8 = 2**40

# API.md §3.2. Long enough to unlock a phone and open a camera; short enough
# that a photograph of the screen is worthless by the time it is shared.
WINDOW_S = 120.0

# Per-IP claim attempts inside one window, and the aggregate that closes it.
# The code is 40 bits, so this is not what stops it being guessed — 5 attempts
# in 120 s is 5 of 10**12. What it stops is an unauthenticated peer turning the
# pairing route into a sha256 mill or an audit-log flood, and the aggregate cap
# stops a distributed version of the same. PER IP is load-bearing: a shared
# counter would let one hostile peer spend the budget the real phone needs.
ATTEMPTS_PER_PEER = 5
ATTEMPTS_TOTAL = 25

LABEL_MAX = 40                    # API.md §3.3
PLATFORM_MAX = 16

# The URL scheme the iOS client registers in CFBundleURLTypes (API.md §3.2).
# `f=` — the SPKI certificate pin — is GONE, and its absence is a decision, not
# an oversight: ADR 0013 removed TLS from this server entirely, so there is no
# certificate to pin and a pin field would be a field the client must ignore.
SCHEME = "orc://p"

# Error codes, which the Swift client branches on. They are the strings in
# API.md §3.3's error table and nothing here invents a new one.
NOT_OPEN = "pairing_not_open"
CODE_WRONG = "pairing_code_wrong"
ATTEMPTS = "pairing_attempts"
LOCKED = "pairing_locked"
PEER_REFUSED = "peer_not_permitted"
BAD_REQUEST = "pairing_bad_request"

_lock = threading.RLock()
_window = None        # the single open window, or None


def _hash(code):
    return hashlib.sha256(code.encode()).hexdigest()


def normalise(code):
    """A typed or scanned code, reduced to its canonical form.

    Generous about form — whitespace, dashes and case are all noise — and
    Crockford-folding the four ambiguous glyphs, because the manual fallback is
    a human reading a screen. `I` and `L` become `1`, `O` becomes `0`, `U`
    becomes `V`.

    Returns "" for anything that is not a string, so a JSON body carrying
    `{"code": 12345678}` or `{"code": null}` lands on "no code" rather than on a
    TypeError inside the comparison.
    """
    if not isinstance(code, str):
        return ""
    out = []
    for ch in code.strip().upper():
        if ch in " -_\t\n":
            continue
        out.append({"I": "1", "L": "1", "O": "0", "U": "V"}.get(ch, ch))
    return "".join(out)


def peer_permitted(peer):
    """May this address claim a pairing code at all?

    Loopback (the Mac testing its own flow) and the tailnet (`100.64.0.0/10`,
    which is where the phone is). Nothing else, and an address that will not
    parse is nothing else — API.md §3.3's `peer_not_permitted`.

    This is checked BEFORE the code is compared, so a peer that has no business
    pairing never learns whether it guessed right, and never spends the real
    phone's attempt budget.
    """
    return bool(auth.loopback(peer) or tailnet.in_range(peer))


def payload_url(host, port, code):
    """The string that goes in the QR: host, port, code. Never the token.

    The port is omitted when it is the default, which saves five bytes of a
    budget that decides the QR's version and therefore how far away a camera
    can read it from.
    """
    query = {"h": host, "c": code}
    if int(port) != 4242:
        query["p"] = str(port)
    order = [("h", query["h"])]
    if "p" in query:
        order.append(("p", query["p"]))
    order.append(("c", query["c"]))
    return f"{SCHEME}?{urllib.parse.urlencode(order)}"


def grouped(code):
    """`7K3M9QP2` -> `7K3M-9QP2`, for reading aloud. Display only."""
    return f"{code[:4]}-{code[4:]}" if len(code) == 8 else code


def open_window(host=None, port=None, now=None):
    """Mint a pairing code and open the window. Returns the page/API payload.

    Each call REPLACES any window already open (API.md §3.2). That is
    deliberate and it is the safe direction: a user who clicks "pair a device"
    twice has one live code, not two, and the one that is live is the one on the
    screen in front of them.

    `secrets.choice`, never `random.choice`. `random` is a Mersenne Twister
    seeded from the clock; 624 of its outputs reveal every output after, and a
    pairing code is a credential for its whole life however short that is.
    """
    global _window
    now = time.time() if now is None else now
    host = host or config.CFG.get("host") or "127.0.0.1"
    port = config.CFG.get("port", 4242) if port is None else port
    code = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    with _lock:
        _window = {"code_sha256": _hash(code), "opened": now,
                   "expires_at": now + _window_s(), "claimed": None,
                   "attempts": {}, "total": 0, "locked_until": None}
    return _render(code, host, port, now)


def _window_s():
    return float(config.CFG.get("pair_window_s", WINDOW_S))


def _render(code, host, port, now):
    """The open-window payload — everything the page and the app need.

    The QR is built here rather than in the route because the payload and the
    picture must not be able to disagree: one function makes the string and
    immediately encodes THAT string, so there is no path on which the manual
    fields say one thing and the code in the picture says another.
    """
    url = payload_url(host, port, code)
    matrix = qr.encode(url, "M")
    with _lock:
        expires_at = _window["expires_at"] if _window else now
    return {
        "code": grouped(code),
        "url": url,
        "svg": qr.svg(matrix),
        "qr_version": (len(matrix) - 17) // 4,
        "expires_at": expires_at,
        "expires_in": max(0.0, expires_at - now),
        "manual": {"host": host, "port": int(port), "code": grouped(code)},
    }


def close():
    """Shut any open window. Idempotent."""
    global _window
    with _lock:
        _window = None


def state(now=None):
    """What the board should show about pairing right now, with no secrets.

    Never the code and never its hash: this is served to the page on every
    refresh, and a route that can echo the live code back turns a window that
    is open on somebody else's screen into a window that is open on yours.
    """
    now = time.time() if now is None else now
    with _lock:
        w = _window
        if w is None:
            return {"open": False}
        if w["claimed"]:
            return {"open": False, "claimed": w["claimed"],
                    "claimed_at": w.get("claimed_at")}
        if now >= w["expires_at"]:
            return {"open": False, "expired": True}
        return {"open": True, "expires_at": w["expires_at"],
                "expires_in": max(0.0, w["expires_at"] - now),
                "attempts": w["total"]}


def claim(peer, body, now=None):
    """`(payload, error)` — exchange a pairing code for a device token.

    Exactly one of the two is None. `error` is `(status, code, message)` in the
    shape `server` already refuses with, so this function decides and the route
    only serialises.

    THE ORDER IS THE SECURITY, and it is the same discipline as `auth.check`:

    1. **peer range** — before anything else. A claimant that is neither on
       this machine nor on the tailnet is refused without touching the window,
       so it cannot spend attempts, cannot learn whether a window is open, and
       cannot tell a wrong code from a closed door.
    2. **a window exists, is unclaimed, and has not expired** — one answer,
       `pairing_not_open`, for all three. They are genuinely the same fact from
       the claimant's side, and distinguishing them would say whether somebody
       else just paired.
    3. **the lock, then the per-IP budget** — before the code is compared, so an
       exhausted peer costs a dict lookup rather than a sha256.
    4. **the code**, with `compare_digest` over sha256.
    5. **mint, close the window, audit.**

    There is no branch that ends in "allow" by falling through, and every
    refusal leaves the window exactly as it found it except for the counters.
    """
    global _window
    now = time.time() if now is None else now

    if not peer_permitted(peer):
        _audit(now, peer, None, "refuse", PEER_REFUSED)
        return None, (403, PEER_REFUSED,
                      f"pairing is answered on this machine and on the tailnet "
                      f"only; {peer or 'that address'} is neither")

    if not isinstance(body, dict):
        return None, (400, BAD_REQUEST, "the body of a pairing request must be "
                                        "a JSON object")
    presented = normalise(body.get("code"))
    label = str(body.get("label") or "")[:LABEL_MAX].strip()
    platform = str(body.get("platform") or "")[:PLATFORM_MAX].strip()

    with _lock:
        w = _window
        if w is None or w["claimed"] or now >= w["expires_at"]:
            _audit(now, peer, None, "refuse", NOT_OPEN)
            return None, (409, NOT_OPEN,
                          "no pairing window is open — open one on the board "
                          "(＋ pair a device) and scan the code it shows")
        if w["locked_until"] and now < w["locked_until"]:
            wait = int(w["locked_until"] - now) + 1
            _audit(now, peer, None, "refuse", LOCKED)
            return None, (429, LOCKED,
                          f"too many wrong pairing codes; this window is "
                          f"closed for {wait}s. Open a new one on the board.")
        spent = w["attempts"].get(peer, 0)
        if spent >= ATTEMPTS_PER_PEER:
            _audit(now, peer, None, "refuse", ATTEMPTS)
            return None, (429, ATTEMPTS,
                          f"{ATTEMPTS_PER_PEER} wrong codes from {peer}; open "
                          f"a new pairing window to try again")

        if not presented or not hmac.compare_digest(_hash(presented),
                                                    w["code_sha256"]):
            w["attempts"][peer] = spent + 1
            w["total"] += 1
            # The aggregate cap. A single peer cannot reach it — it is five
            # attempts past the per-peer limit — so what it closes is many
            # peers at once, which is the only shape of attack a 40-bit code
            # has. The window is LOCKED rather than deleted so that the board
            # can say what happened instead of silently showing an expired one.
            if w["total"] >= ATTEMPTS_TOTAL:
                w["locked_until"] = now + 600.0
            # `auth`'s own failure budget is spent too. It is the same
            # unauthenticated door, counted in the same place, so a peer that
            # burns its pairing attempts also loses the ability to hammer
            # /api/state — ADR 0014 said this route would inherit that bucket.
            auth._budget(peer, now, spend=1.0)
            _audit(now, peer, None, "refuse", CODE_WRONG)
            return None, (409, CODE_WRONG, "that pairing code is not the one "
                                           "on the screen")

        device, token = auth.add_device(label or _default_label(platform, peer),
                                        now=now)
        w["claimed"] = device["id"]
        w["claimed_at"] = now
        _window = w
    auth._forgive(peer)
    _audit(now, peer, device["id"], "paired", None, label=device["label"])
    return {
        "device_id": device["id"],
        "label": device["label"],
        "token": token,
        "server": _server_facts(),
    }, None


def _default_label(platform, peer):
    """A device with no label is still a device that has to be revocable.

    An empty label in `--list-devices` is a row the user cannot act on, so
    something identifying always goes in — the platform if the client sent one,
    otherwise where it came from.
    """
    return (platform or "device") + f" ({peer})"


def _server_facts():
    """What the client needs to talk to this server afterwards.

    No `spki`, no `cert_not_after`: API.md §3.3 lists both, and both belong to
    the TLS design that ADR 0013 replaced. Sending them as nulls would invite a
    client to implement pinning against nothing.
    """
    return {
        "host": config.CFG.get("host", "127.0.0.1"),
        "port": config.CFG.get("port", 4242),
        "hostname": socket.gethostname(),
        "api": "1",
        "tls": False,
    }


def _audit(now, peer, device, outcome, code, label=None):
    """Pairing is written to the same log as everything else.

    It is the one route that hands out a credential, so "somebody asked" is the
    most valuable line in the file. The code is never written — not the one
    presented and not the one expected — because the log is `0600` but it is
    also the file somebody pastes into a bug report.

    TWO LINES PER ATTEMPT, and they are not a duplicate. `auth.check` logs the
    request as it comes through the door (`allow`, device `unauthenticated`),
    which is the unskippable seam — it is written even if every line below is
    deleted. This one logs what pairing then DID with it, which the door cannot
    know. So the outcome here is `paired` rather than `allow`, and the log
    reads as the two facts it is:

        allow   POST /api/v1/pair  unauthenticated   <- the door let it in
        refuse  POST /api/v1/pair  pairing_code_wrong <- and pairing said no

    ADR 0014's note that the audit "records the request, not the outcome" is
    the reason a second write exists at all: this is the one route where the
    outcome is worth its own line, because the outcome is a credential.
    """
    auth.audit(at=now, peer=peer, device=device, label=label,
               method="POST", path="/api/v1/pair", outcome=outcome, code=code)


def _reset():
    """Tests only: the window is process-wide and outlives a test case."""
    close()
