"""orchestra.auth — who is allowed to ask, and what it costs to be wrong.

Until this module existed the server had no authentication of any kind, and
`127.0.0.1` was the whole of its security. That is a real defence and it is
also a ceiling: the board cannot leave the machine. What sits behind the door
is not a dashboard — it is the full text of every prompt and every reply, plus
a keyboard attached to terminals running `--dangerously-skip-permissions`,
plus a dispatch button that spends real usage. So the threat model is not
"someone reads a status page"; it is `POST /api/send` typing an instruction
into an agent that will act on it.

ADR 0013 decided where the security budget goes: **not** TLS (WireGuard has
already encrypted the link, and the realistic attacker inside a tailnet is a
compromised or shared DEVICE, which TLS does nothing about) but tokens. This
module is those tokens.

THE RULE, stated once so it can be quoted and tested:

    LOOPBACK IS TRUSTED. EVERYTHING ELSE MUST PRESENT A VALID TOKEN.
    A CREDENTIAL THAT IS PRESENTED IS ALWAYS CHECKED, WHEREVER IT CAME FROM.
    AND A PAGE FROM ANOTHER SITE IS NOT LOOPBACK, WHATEVER THE SOCKET SAYS.

The first clause is what keeps the existing browser working — it has no token
and never will. It is not a compromise, it is an accurate statement of the
boundary: anything that can open a socket to 127.0.0.1 is already a process
running as you on this Mac, and such a process can read `~/.claude*/projects`
directly, with no server involved. A token would guard a door in a wall that
is not there (API.md §2.6 lists this as *unclosable*, and it is).

The third clause is the one that was nearly missed, and it is the reason the
first is safe to write down at all. A website you have open in a tab also
speaks from 127.0.0.1 — through your browser, with your loopback trust — and
`POST /api/send` types into an agent running `--dangerously-skip-permissions`.
That is CSRF, it predates this module, and it is closed here by requiring
`Content-Type: application/json` on every mutation (which forces a CORS
preflight this server refuses) and by rejecting a cross-site `Origin`.

The second clause is what stops that trust from being a bypass. A request from
loopback carrying a *bad* token is refused rather than waved through as
"anonymous loopback", because the only ways to produce one are a stale app, a
revoked device, or something on this machine guessing — and none of the three
should be answered with 200. It also means the day a proxy is ever put in
front of this server (`tailscale serve` makes every request arrive from
loopback — API.md §2.7 blocks it for exactly this reason), a token that has
been revoked stays revoked instead of being laundered into anonymity.

WHAT IS DELIBERATELY NOT HERE:

* **Scopes.** API.md §2.2 designs `read`/`act`/`admin` and this module
  implements none of it: one token grants everything. A phone that can read
  but cannot act is not the product, so the split buys nothing yet, and a
  half-built scope ladder is worse than an honest absence — it invites the
  belief that a `read` token is safe to hand out. Deferred, whole.
* **Pairing.** Tokens are minted on this machine (`python3 -m orchestra
  --add-device`) and carried to the phone by hand. The QR/pairing-code dance
  of API.md §3 needs a bootstrap route that is exempt by construction, which
  is a second unauthenticated door; it can wait until there is a phone to walk
  through it.
* **Host / Origin allowlists, tailnet whois, lockdown, idempotency** — steps
  2, 3, 7, 8 and 10 of API.md §2.3's guard. Each is a real check and none of
  them is the *missing* one.

Nothing in this module imports anything above `config`: it is a leaf, and it
has to be, because it runs before every route and must not be able to reach
the code it is guarding. `REGISTRY` and `AUDIT_LOG` are rebound at runtime by
tests, so — the `resume.RESUME_STATE` rule, ADR 0010 — they are deliberately
NOT re-exported by the facade. Reach them as `auth.REGISTRY`.
"""

import contextlib
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import threading
import time

from . import config


# The two state files. `config.HERE` is the REPO ROOT, not the package
# directory (ADR 0010 moved it up one) — getting that wrong would put the
# device registry inside `orchestra/`, where a later reinstall silently
# deletes every paired device and nothing announces it.
REGISTRY = config.HERE / "devices.json"
AUDIT_LOG = config.HERE / "audit.log.jsonl"

# `orc1_<devid>_<secret>`, API.md §2.1. Three parts and not one opaque blob
# because the device id has to be readable WITHOUT the secret: it is the audit
# log's identifier, and it is what turns a lookup into a dict hit instead of a
# scan over every registered device comparing hashes.
TOKEN_VERSION = "orc1"
DEVID_BYTES = 4            # 8 hex chars — a name, not a secret
SECRET_BYTES = 32          # 256 bits from `secrets`, urlsafe-encoded to 43 chars

# Refusal codes. A client branches on these; the human message beside each one
# is for the person reading a `curl`. They are all 401 except the bucket,
# because "your credential is not good enough" is one answer however it failed
# — the code says which, the status does not have to.
NO_TOKEN = "unauthorized"
MALFORMED = "token_malformed"
UNKNOWN = "token_unknown"
REVOKED = "device_revoked"
RATE_LIMITED = "rate_limited"
UNAVAILABLE = "auth_unavailable"
CROSS_ORIGIN = "origin_not_allowed"
NOT_JSON = "content_type_required"

# The media type every mutation must announce. This is the CSRF guard, and it
# is two lines because of how the browser's rules happen to fall.
#
# "Loopback is trusted" is a statement about processes on this machine — but a
# WEBSITE YOU VISIT also gets to speak from 127.0.0.1, through your browser,
# with your loopback trust. That is CSRF, it has been live in this server since
# it was written, and `POST /api/send` under `--dangerously-skip-permissions`
# is the worst possible thing to leave behind it.
#
# What stops it is the CORS preflight, which fires for a cross-origin request
# only if the request is not "simple" — and `Content-Type: application/json` is
# exactly what makes it not simple. So: requiring the header turns every
# cross-origin mutation into an OPTIONS this server answers with a refusal and
# no `Access-Control-Allow-Origin`, at which point the browser never sends the
# POST at all. Without it, a plain form or a `text/plain` fetch reaches
# `do_POST`, which never looked at the media type and simply `json.loads`ed the
# body. The attacker cannot read the reply either way; they do not need to —
# typing the instruction IS the attack.
#
# It costs nothing to hold: all six of the board's own POSTs already send this
# header. A `curl -X POST` now needs `-H 'Content-Type: application/json'`.
JSON_TYPE = "application/json"

# ---------------------------------------------------------------- exempt routes
#
# The complete list, and it is one entry long.
#
# `GET /api/health` is open because it is the route you need BEFORE you have a
# token: it is how a phone distinguishes "the Mac is asleep" from "my token was
# revoked" from "I typed the address wrong", and a health check that requires
# the credential you are trying to diagnose can only ever report a tautology.
# It is safe to open because it carries no fleet data at all — no worktrees, no
# counts, no hostname, no device list, nothing that varies with what you are
# doing. It answers "a server that speaks this protocol is alive here, and its
# clock says T". Everything an unauthenticated peer learns from it, it already
# learned by completing the TCP handshake, except the clock skew it needs to
# read a timestamp correctly.
#
# NOTHING ELSE IS EXEMPT, and the two candidates that were considered are worth
# writing down because both look harmless:
#
#   * the static pages (`/`, `/stream.js`, `/map`, `/limits`, `/guide`). They
#     contain no transcript text — they are the app shell, and its source is
#     public. But exempting them buys nothing: a browser cannot attach an
#     `Authorization` header to a top-level navigation, so a phone browser
#     would load the shell and then fail every fetch inside it, which is a
#     worse failure than being refused at the door. ADR 0002 puts a native
#     client on the phone; the board is a loopback program. So they stay shut,
#     and the tailnet sees exactly one open route.
#   * `GET /api/state`. It is "just a summary" until you read it: session
#     topics are the first line of what you typed, and the board's whole point
#     is knowing what each agent is doing. It reads transcript text. Shut.
#
# The match is EXACT on the path with its query stripped, while `server.Handler`
# routes by `startswith`. That asymmetry is deliberate and one-directional: a
# path this list does not recognise is refused even if the router would have
# served it, so `/api/healthcheck-for-free` is a 401 and not a hole.
EXEMPT = frozenset({("GET", "/api/health"), ("POST", "/api/v1/pair")})

# The second exempt route, added with pairing, and it is the one that deserves
# the most suspicion — an unauthenticated POST that hands out a credential.
# What guards it is not authentication (there is none to have; that is the
# point) but four things that are all in `pairing.claim`, in this order:
#
#   * the claimant must be on loopback or the tailnet, checked BEFORE the code;
#   * a window must be open, unclaimed and unexpired — 120 s, single use;
#   * attempts are budgeted per source IP, and spend from THIS module's failure
#     budget as well, which is what ADR 0014 promised when it deferred pairing;
#   * the code is compared with `compare_digest` over sha256.
#
# It is exempt from the TOKEN check only. Everything else in `check` still runs
# — the cross-site guards included, so a page you are visiting cannot post a
# pairing claim through your browser.

# Routes that answer to THIS MACHINE ONLY, and to nobody holding a token.
#
# API.md §2.5 puts device management behind an `admin` scope and says phones are
# never issued it. ADR 0014 deferred scopes whole, so `admin` cannot be a scope
# today — and inventing a half-scope for one route is exactly what that ADR
# refused to do. What it can be, and what is strictly stronger, is a rule with
# no ladder in it at all:
#
#     DEVICE MANAGEMENT IS THE ABSENCE OF A TOKEN, FROM THIS MACHINE.
#
# So no token grants it. A phone cannot list devices, cannot open a pairing
# window, and — the one that matters — cannot revoke a device, including the
# device that would have revoked IT. A stolen phone holding a valid token gets
# 403 on all three, which is the same answer it would get if scopes existed and
# it held `act`. When scopes do arrive this rule becomes `scope == admin` and
# nothing else about the surface changes.
#
# Matched by PREFIX, unlike `EXEMPT`, because `/api/v1/devices/<id>/revoke`
# carries an identity in the path. The asymmetry is one-directional in the safe
# direction: a path this list matches too eagerly is a refusal, never a hole.
ADMIN = ("/api/v1/devices",)

# The refusal code for that rule. Distinguishable from `unauthorized` on
# purpose: the holder of a good token learns that the token is fine and the
# ROUTE is not theirs, which is what stops an app retrying pairing forever.
ADMIN_ONLY = "admin_local_only"

# Requests that are written to the audit log when they are ALLOWED. Refusals
# are always logged, whatever the route.
#
# "A mutation" is every non-GET method, plus the one GET that acts: `/api/focus`
# raises a terminal window and steals the user's focus. Reads are deliberately
# not logged, and the reason is volume rather than principle — the board polls
# `/api/state`, so logging reads would write a line every few seconds forever
# and bury the eleven lines that matter in a year of noise. A stolen token that
# only ever reads leaves no evidence here; that is a known gap, and the honest
# way to close it is a counter in `meta`, not a log nobody can grep.
SIDE_EFFECT_GETS = ("/api/focus",)

# …and the one mutation that is NOT logged, which is the same argument running
# the other way.
#
# `POST /api/hook` is a Claude Code hook edge (ADR 0007). It fires several times
# per agent turn from every hooked session on the machine — the comment above
# says logging reads would "write a line every few seconds forever and bury the
# eleven lines that matter", and this route is an order of magnitude louder than
# `/api/state` polling ever was. A busy fleet would push a megabyte a day of
# `POST /api/hook allow` through `audit.log.jsonl` and make the log useless for
# the thing it exists for.
#
# It is safe to leave out because of what the route can DO, which is nothing: it
# writes one entry to an in-memory dict that expires in 90 s and never leaves
# the process. It types at no agent, reads no transcript and returns no fleet
# data. The honest replacement is exactly what that comment prescribes — a
# COUNTER rather than a log — and `observer.stats()` carries it as
# `hook_received` / `hook_live` / `hook_ignored`.
#
# REFUSALS ARE STILL LOGGED. `audited` only governs the allow path, so a peer
# that is refused here appears in the log like any other refusal — which is the
# half that could ever be evidence of anything.
NOT_AUDITED = ("/api/hook",)

# The failure budget. A token is 256 bits from `secrets`, so this is NOT what
# stops it being guessed — nothing can guess it, and 10 tries a minute across a
# long weekend (60 h) is 36,000 of 2**256. What the budget actually buys:
#
#   * the audit log cannot be flooded by an unauthenticated peer — the ceiling
#     is 10 lines/min/IP, ~90 KB a day from one attacker, so evidence of a real
#     theft is still findable beside it;
#   * a sha256 per attempt cannot be turned into CPU load;
#   * and the one that will matter later: the pairing code of API.md §3 is six
#     digits, and it will inherit this bucket. 10/min is 2.9 % of a 10**6 space
#     over a weekend, which is why the pairing window is also 120 s.
#
# Per source IP, token bucket. Only FAILURES spend from it, so no legitimate
# client can be throttled by using the board hard, and a success empties it —
# whoever holds a valid token can already do everything a token permits, so
# charging them for a typo protects nothing and locks out the one device that
# is trying to reconnect.
FAIL_BURST = 10            # consecutive refusals before the door stops answering
FAIL_WINDOW_S = 60.0       # …and how long a full budget takes to refill

# Bounds the bucket table against a peer that forges source addresses. Entries
# that have refilled completely carry no information and are dropped on every
# touch, so the table only ever holds IPs that are actively failing; the cap is
# the backstop for the case where thousands of them are. Over the cap a new
# address is treated as EXHAUSTED rather than admitted — the failure direction
# is a refusal, never an allow.
MAX_TRACKED_PEERS = 4096

# How stale a device's `last_seen` may be before the registry is rewritten.
# METHOD.md §6: this check runs on EVERY request, and a write per request is
# how a harmless field becomes a disk write per SSE keepalive. 60 s makes
# "when was this phone last here" answerable to the minute and costs one write
# a minute per active device.
LAST_SEEN_S = 60.0


class Verdict:
    """The answer, and everything the seam needs to act on it.

    A class rather than a bool because a refusal has to carry four things: the
    status, a code the client branches on, a sentence a human can act on, and
    (for the bucket) how long to wait. `ok` is the only field the happy path
    reads, and `device` is None for a trusted-loopback request — which is a
    THIRD state, not a failure: allowed, and not anybody in particular.
    """

    __slots__ = ("ok", "status", "code", "message", "device", "retry_after")

    def __init__(self, ok, status=200, code=None, message="", device=None,
                 retry_after=None):
        self.ok = ok
        self.status = status
        self.code = code
        self.message = message
        self.device = device
        self.retry_after = retry_after

    def __repr__(self):
        who = (self.device or {}).get("id") if self.device else "loopback"
        return (f"<Verdict {'allow' if self.ok else 'refuse'} {self.status} "
                f"{self.code or who}>")


ALLOW_LOOPBACK = Verdict(True)


# --------------------------------------------------------------- the registry

_lock = threading.RLock()
_cache = {"key": None, "devices": None, "error": None}


def _stat_key(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load_registry(force=False):
    """`({devid: device}, error)` — the registry, memoed on the file's stat.

    Keyed on `(mtime_ns, size, ino)` like every other memo here, and for one
    concrete reason: `--revoke-device` runs in a SEPARATE PROCESS, so a running
    server has to notice a file it did not write. It does, within one `stat`.

    An unreadable or unparseable registry is an ERROR, never an empty registry.
    The difference is the whole of rule 6: an empty registry means "no device
    is allowed in", which is a refusal; a corrupt one silently *becoming*
    empty would be a refusal too — but a corrupt one silently becoming
    `{}` on the WRITE path would delete every paired device, so the two must
    never be the same value.
    """
    with _lock:
        key = _stat_key(REGISTRY)
        if not force and key is not None and key == _cache["key"]:
            return _cache["devices"], _cache["error"]
        devices, error = {}, None
        if key is None:
            # No file at all is not an error — it is a machine where nothing
            # has been paired yet. Loopback keeps working; the tailnet has
            # nobody to let in, which is correct.
            pass
        else:
            try:
                raw = json.loads(REGISTRY.read_text())
                for d in raw["devices"]:
                    if d.get("id") and d.get("token_sha256"):
                        devices[d["id"]] = d
            except (OSError, ValueError, KeyError, TypeError,
                    AttributeError) as e:
                # AttributeError is the structurally-corrupt case rule 6 missed:
                # `{"devices": ["x"]}`, or a string, or a dict, are all valid
                # JSON one bad hand-edit or partial write away, and `d.get(...)`
                # on a non-dict raises here rather than in `json.loads`. Without
                # it the exception escapes `check` into `parse_request` and every
                # authenticated request dies with a per-request traceback and a
                # dropped connection instead of the designed 503 — fail CLOSED,
                # loudly, as a broken machine that says so.
                devices, error = {}, f"{type(e).__name__}: {e}"
        _cache.update(key=key, devices=devices, error=error)
        return devices, error


def _write_registry(devices):
    """Whole file, 0600, atomically replaced.

    0600 because the registry is not a credential (it stores sha256 of the
    token, so a copy grants nothing) but it IS a list of every device you own
    and when each was last near the machine. `os.replace` because a truncated
    write here loses every pairing at once, and the failure would only surface
    the next time the phone tried to connect.
    """
    blob = json.dumps({"version": 1, "devices": list(devices.values())},
                      indent=1, sort_keys=True) + "\n"
    tmp = REGISTRY.with_name(REGISTRY.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(blob)
    os.replace(tmp, REGISTRY)
    with _lock:
        _cache.update(key=_stat_key(REGISTRY), devices=devices, error=None)


@contextlib.contextmanager
def _registry_flock():
    """A cross-process exclusive lock, held across one load-modify-write.

    ARCHITECTURE.md §5.5 promises `--devices` / `--revoke ID` are file
    operations under an flock. The in-process writers serialise on `_lock`, but
    `--revoke-device` runs in a SEPARATE PROCESS, and every writer here is a
    load-modify-whole-file-replace. Without a shared lock a server write whose
    `load_registry()` ran BEFORE the CLI's `os.replace` and whose
    `_write_registry()` lands AFTER it clobbers the revoked file with a
    pre-revoke copy — the user sees 'revoked iPad' printed while the token keeps
    working, the exact failure §5.5 names as the reason the flock exists. So a
    writer takes this lock and re-reads with `load_registry(force=True)` inside
    it, merging its change onto whatever the other process just wrote. `fcntl`
    is stdlib, so this costs no dependency.

    The lock lives on a sidecar `devices.lock` so it is never itself the file
    being replaced. A registry we cannot even lock (its directory is gone, say)
    is still written best-effort rather than raising — the callers that swallow
    a write `OSError` must keep working, not die on the lock instead of the
    write.
    """
    fd = None
    try:
        fd = os.open(REGISTRY.with_suffix(".lock"),
                     os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        if fd is not None:
            os.close(fd)
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def add_device(label, now=None):
    """Mint a device. Returns `(public_record, token)` — the ONLY time the
    token exists in this process, and the only time it can ever be read.

    `secrets`, never `random`: `random` is a Mersenne Twister seeded from the
    clock, and 624 outputs of it reveal every output after. Stored as
    sha256(full token), so the registry file is not itself a credential —
    someone who copies it gets a list of labels, not a way in.
    """
    now = time.time() if now is None else now
    with _lock, _registry_flock():
        devices, error = load_registry(force=True)
        if error:
            raise ValueError(f"refusing to write over an unreadable registry "
                             f"({REGISTRY}): {error}")
        devices = dict(devices)
        while True:
            devid = secrets.token_hex(DEVID_BYTES)
            if devid not in devices:
                break
        token = f"{TOKEN_VERSION}_{devid}_{secrets.token_urlsafe(SECRET_BYTES)}"
        devices[devid] = {"id": devid, "label": label or devid,
                          "created": now, "last_seen": None, "revoked": None,
                          "token_sha256": _hash(token)}
        _write_registry(devices)
        return public(devices[devid]), token


def revoke_device(devid, now=None):
    """Mark a device revoked. Irreversible on purpose: `revoked` is a
    timestamp, not a flag, and un-revoking is minting a new token — which is
    what you want a stolen phone to cost.
    """
    now = time.time() if now is None else now
    with _lock, _registry_flock():
        devices, error = load_registry(force=True)
        if error:
            raise ValueError(f"refusing to write over an unreadable registry "
                             f"({REGISTRY}): {error}")
        if devid not in devices:
            return None
        devices = {k: dict(v) for k, v in devices.items()}
        devices[devid]["revoked"] = now
        _write_registry(devices)
        return public(devices[devid])


def public(device):
    """A device without its hash. The hash is not a secret in the sense the
    token is, but it is a target, and nothing outside this module needs it."""
    return {k: v for k, v in device.items() if k != "token_sha256"}


def devices():
    """Every registered device, newest first, hashes stripped."""
    known, _ = load_registry()
    return sorted((public(d) for d in known.values()),
                  key=lambda d: d.get("created") or 0, reverse=True)


def _touch(devid, now):
    """Record that a device was here — at most once a `LAST_SEEN_S`."""
    with _lock:
        known, error = load_registry()
        d = known.get(devid)
        if error or d is None:
            return
        if (d.get("last_seen") or 0) + LAST_SEEN_S > now:
            return
        # The window check above runs on every authenticated request, so it is
        # deliberately OUTSIDE the flock — taking a cross-process file lock per
        # request just to decide "nothing to write" is the disk-write-per-request
        # this throttle exists to avoid. Only once a write is actually due do we
        # lock, re-read under it, and merge onto whatever `--revoke-device` may
        # have just written — otherwise this `last_seen` bump would clobber a
        # revocation of the very device it is recording.
        with _registry_flock():
            known, error = load_registry(force=True)
            d = known.get(devid)
            if error or d is None:
                return
            if (d.get("last_seen") or 0) + LAST_SEEN_S > now:
                return
            devices_ = {k: dict(v) for k, v in known.items()}
            devices_[devid]["last_seen"] = now
            try:
                _write_registry(devices_)
            except OSError:
                pass    # a registry that cannot be written still authenticates


# The push sub-object's writable keys. A device may register ONLY its own push
# endpoint and preferences (API.md §9.23) — never its scope, its id, its
# created stamp, or anything else the registry holds. The allow-list is here,
# beside the store, because "the server writes only these keys" is a security
# statement and a security statement belongs where it can be enforced rather
# than trusted: a caller who put `revoked: null` in the body must not be able
# to un-revoke themselves through the push route.
PUSH_KEYS = frozenset({"backend", "token", "environment", "topic",
                       "app_version", "tz", "tz_offset_min", "settings",
                       "quiet_hours", "rules", "muted_until", "nudge_min",
                       "privacy", "registered_at", "last_push_at",
                       "last_push_status"})


def set_push(devid, fields, now=None):
    """Write the calling device's `push` sub-object — and ONLY its push keys.

    Returns the stored push object, or None for an unknown device. The device
    id comes from the authenticated token (`Handler.device`), never from the
    body, so a device can only ever write its OWN endpoint — registering push
    is not an escalation, but writing ANOTHER device's endpoint would be, and
    the id not being a parameter is what makes that unreachable rather than
    merely disallowed.
    """
    now = time.time() if now is None else now
    clean = {k: v for k, v in (fields or {}).items() if k in PUSH_KEYS}
    with _lock, _registry_flock():
        known, error = load_registry(force=True)
        if error or devid not in known:
            return None
        devices_ = {k: dict(v) for k, v in known.items()}
        push = dict(devices_[devid].get("push") or {})
        push.update(clean)
        push["registered_at"] = now
        devices_[devid]["push"] = push
        try:
            _write_registry(devices_)
        except OSError:
            pass
        return push


def get_push(devid):
    """The device's stored push object, or None."""
    known, _ = load_registry()
    d = known.get(devid)
    return dict(d["push"]) if d and d.get("push") else None


def note_push(devid, status, now=None):
    """Record the outcome of the last push to this device — the 'last delivered
    4m ago (200)' the settings screen shows. A push that silently stopped after
    a restore is otherwise indistinguishable from a quiet fleet, discovered a
    week late."""
    now = time.time() if now is None else now
    with _lock, _registry_flock():
        known, error = load_registry(force=True)
        if error or devid not in known or not known[devid].get("push"):
            return
        devices_ = {k: dict(v) for k, v in known.items()}
        devices_[devid]["push"] = dict(devices_[devid]["push"])
        devices_[devid]["push"]["last_push_at"] = now
        devices_[devid]["push"]["last_push_status"] = str(status)
        try:
            _write_registry(devices_)
        except OSError:
            pass


def forget_push(devid):
    """Drop a device's push endpoint after APNs says the token is gone —
    `410 Unregistered`, `BadDeviceToken`, `DeviceTokenNotForTopic` (`push.Result.
    is_gone`). The DEVICE stays paired (its bearer token is untouched); only the
    dead push token is removed, so it falls out of `push_devices()` and the
    fan-out stops wasting a POST on it every notification — and Apple stops
    seeing a client that repeatedly pushes to a token it already rejected.

    Without this the registry only grows: a reinstalled app, a wiped simulator,
    a stale token all linger forever. The app re-registers a fresh token on its
    next launch, which lands as a new endpoint on the same device.
    """
    with _lock, _registry_flock():
        known, error = load_registry(force=True)
        if error or devid not in known or not known[devid].get("push"):
            return False
        devices_ = {k: dict(v) for k, v in known.items()}
        devices_[devid].pop("push", None)
        try:
            _write_registry(devices_)
            return True
        except OSError:
            return False


def push_devices():
    """Every registered device that has a push endpoint, hashes stripped —
    the fan-out set for the notifier."""
    known, _ = load_registry()
    return [public(d) for d in known.values()
            if d.get("push") and (d["push"].get("token"))
            and not d.get("revoked")]


# ------------------------------------------------------------- the rate budget

_buckets = {}      # ip -> [tokens, last_touched]


def _budget(peer, now, spend=0.0):
    """Tokens left for `peer`, after refilling and optionally spending.

    A plain token bucket, and the reason it is not a fixed window: a window
    hands an attacker a free burst at every boundary, which for an attempt
    limit is the only part that matters.
    """
    with _lock:
        rate = FAIL_BURST / max(FAIL_WINDOW_S, 0.001)
        for ip, b in list(_buckets.items()):
            # A refilled entry says nothing. Dropping it here is what keeps the
            # table proportional to the number of peers actively failing rather
            # than to the number that ever have.
            if ip != peer and b[0] + (now - b[1]) * rate >= FAIL_BURST:
                del _buckets[ip]
        b = _buckets.get(peer)
        if b is None:
            if len(_buckets) >= MAX_TRACKED_PEERS:
                return 0.0                  # fail closed: no room to track you
            b = _buckets[peer] = [float(FAIL_BURST), now]
        b[0] = min(float(FAIL_BURST), b[0] + max(0.0, now - b[1]) * rate)
        b[1] = now
        if spend:
            b[0] = max(0.0, b[0] - spend)
        return b[0]


def _forgive(peer):
    """A successful authentication clears the peer's failures."""
    with _lock:
        _buckets.pop(peer, None)


def _retry_after(peer, now):
    """Seconds until one more attempt is affordable — always at least 1, so a
    client that honours `Retry-After` cannot busy-loop on a rounded-down 0."""
    with _lock:
        b = _buckets.get(peer)
    if b is None:
        return 1
    need = max(0.0, 1.0 - b[0])
    return max(1, math.ceil(need / (FAIL_BURST / max(FAIL_WINDOW_S, 0.001))))


def _reset_buckets():
    """Tests only — the budget is process-wide and outlives a test case."""
    with _lock:
        _buckets.clear()
        _audit_suppress.clear()


def _forget_registry():
    """Tests only: drop the memo when REGISTRY is REBOUND rather than written.

    The memo key is the file's `(mtime_ns, size, ino)`, which is the right key
    for a file that changes and the wrong one for a pointer that changes — two
    different paths can present the same key. Rebinding `auth.REGISTRY` without
    this is the memo bug of METHOD.md §10, spelled a new way.
    """
    with _lock:
        _cache.update(key=None, devices=None, error=None)


# ------------------------------------------------------------------ the audit

_audit_lock = threading.Lock()

# Anything shaped like a token, wherever it turns up in an audit line.
#
# The header is not the only place a token can be. API.md §2.1 says a token is
# presented ONLY as `Authorization: Bearer` — never a query parameter, never a
# cookie — and `parse_bearer` refuses one that arrives any other way. But the
# refusal is AUDITED, and the audit keeps the path whole with its query,
# because the query is the identity a mutation was addressed to (ADR 0008). So
# a client that got that wrong once wrote its own live 256-bit secret into this
# file, in the clear, and the token stayed valid: driven, not reasoned —
# `GET /api/state?token=orc1_…` produced a 401 and a log line containing all 43
# characters. The file is 0600, and it is also the file somebody pastes into a
# bug report, which is exactly the argument `pairing._audit` already makes
# about the code it refuses to write down.
#
# The substitution is on the SERIALISED LINE rather than on the `path` field,
# for the same reason `auth.check` lives in `parse_request`: a field added next
# month is covered without anybody remembering that it had to be. The device id
# survives — it is eight hex characters with no `orc1_` in front of it, so the
# identifier this log exists to carry is never what gets eaten.
# Built FROM `TOKEN_VERSION` rather than spelling `orc1_` again: the day this
# becomes `orc2` the scrub has to move with it, and a hard-coded prefix here
# would keep passing every test while silently logging the new format in full.
_TOKENISH = re.compile(re.escape(TOKEN_VERSION) + r"_[0-9A-Za-z][0-9A-Za-z_\-]*")
REDACTED = TOKEN_VERSION + "_<redacted>"


def scrub(text):
    """Every token-shaped run in `text`, replaced. Idempotent."""
    return _TOKENISH.sub(REDACTED, text)


def audit(**fields):
    """One JSON object per line, appended, 0600, never rotated.

    WHO / WHAT / WHEN and deliberately not WHAT WAS SAID. The body of
    `/api/send` is the text typed at an agent and the body of `/api/dispatch`
    is a mission brief; logging either would make this file a second copy of
    everything you have ever said to a fleet, which is precisely the asset the
    tokens exist to protect. The path is kept whole (query included — it is the
    identity a mutation was addressed to, ADR 0008) and truncated, because
    "who typed at agent X" is the question this file exists to answer.

    It records the request, not the outcome. That is a real limitation, stated
    plainly: a refused dispatch and a successful one look the same here. The
    seam is *before* the route by construction — that is what makes it
    impossible for a route to forget — and a second write afterwards would be
    a second thing to forget. What this file proves is that somebody ASKED,
    which is the evidence a stolen token leaves.

    A failure to write is swallowed. An audit log that can take the server down
    is a denial of service wearing a security hat.
    """
    line = scrub(json.dumps(fields, sort_keys=True)) + "\n"
    try:
        with _audit_lock:
            fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line)
    except OSError:
        pass


# The audit log is append-only forever (there is no rotation), and its comment
# above and ADR 0014 both promise the failure budget is what keeps that safe:
# "the audit log cannot be flooded by an unauthenticated peer — the ceiling is
# 10 lines/min/IP". That promise was NOT delivered — a 429-throttled peer still
# wrote one line per request, and an unauthenticated pairing flood wrote two —
# so an exhausted peer could pour tens of GB/day into `audit.log.jsonl` and bury
# the eleven lines the log exists to preserve. This gate is the missing half.
#
# `outcome: "throttled"` is the one marker line a flooding peer buys: it is
# written ONCE when the peer crosses from "has budget" to "exhausted", carries
# the running count of what has been suppressed since, and then the peer is
# silent in the log until its bucket refills enough to afford a real attempt
# again — at which point the next line is preceded by the final suppressed count.
AUDIT_THROTTLED = "audit_throttled"
_audit_suppress = {}   # peer -> lines suppressed since its throttle marker


def _audit_gate(peer, now):
    """Whether a refusal/allow line for `peer` may be written to the log now.

    Called wherever a line an off-machine peer can trigger at will is about to
    be written — every `refuse`, the one unauthenticated `allow` (pairing), and
    every `pairing.claim` refusal. It consults the SAME failure budget the
    refusal spends from, so the two stay in step: while the peer has budget its
    lines are written (that is the ~10 the ceiling allows), the moment it is
    exhausted it gets one `throttled` marker and nothing after, and when the
    bucket has refilled the next line flushes the suppressed count. `/api/health`
    never reaches here — it is exempt ABOVE the budget on purpose.
    """
    with _lock:
        if _budget(peer, now) >= 1.0:
            n = _audit_suppress.pop(peer, None)
            if n:
                audit(at=now, peer=peer, outcome="throttled",
                      code=AUDIT_THROTTLED, suppressed=n)
            return True
        if peer not in _audit_suppress:
            # The crossing: one marker so a reader knows the silence that
            # follows is a throttle and not the peer going away.
            _audit_suppress[peer] = 0
            audit(at=now, peer=peer, outcome="throttled",
                  code=AUDIT_THROTTLED, suppressed=0)
            return False
        _audit_suppress[peer] += 1
        return False


def read_audit(limit=200):
    """The last `limit` audit lines, parsed. For tests and for a human."""
    try:
        lines = AUDIT_LOG.read_text().splitlines()
    except OSError:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


# -------------------------------------------------------------------- the check

def loopback(peer):
    """Is this address on this machine? Anything unparseable is NOT.

    `ipaddress` rather than a literal set, because 127.0.0.0/8 is loopback in
    its entirety and `::ffff:127.0.0.1` — what a dual-stack listener reports
    for an IPv4 client — is not `is_loopback` to Python, so it has to be
    unwrapped by hand. Both directions were checked; the mapped case is the one
    that silently breaks the board on a v6-enabled box.
    """
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool((mapped or ip).is_loopback)


def loopback_bind(host):
    """Would binding `host` already put us on 127.0.0.1?

    Not the same question as `loopback(peer)`, which is about a source ADDRESS
    and therefore always an address. A bind target can also be a NAME
    (`localhost`) or empty (which `socket.bind` reads as every interface but
    which this project has always meant as "the default, loopback").

    It exists because the two callers got out of step: `bind_refusal` spelled
    it `loopback(host) or host in ("localhost", "")` and `__main__`'s companion
    listener spelled it without the `localhost` arm — so `--host localhost`
    tried to bind 127.0.0.1 twice and died with EADDRINUSE at startup. One
    function, one answer.
    """
    return bool(loopback(host)) or host in ("localhost", "")


def exempt(method, path):
    """Does this route need no token at all? Exact match on `(method, path)`
    with the query stripped — see `EXEMPT`."""
    return (method, path.split("?", 1)[0]) in EXEMPT


def audited(method, path):
    """Is this request written to the audit log when it is allowed?"""
    clean = path.split("?", 1)[0]
    if any(clean == p or clean.startswith(p + "/") for p in NOT_AUDITED):
        # Segment-exact, like `_under_admin`, and for the mirror-image reason:
        # this list SUPPRESSES logging, so matching too eagerly is the unsafe
        # direction and a substring match on `/api/hook` would also silence
        # `/api/hooked-up-to-something-else`. Write it correctly once.
        return False
    if method != "GET":
        return True
    # Device management is audited even when it only reads. `GET /api/v1/devices`
    # is the inventory of every credential to this machine, which is a very
    # different thing to read than a status page — and unlike `/api/state`, it
    # is not polled, so logging it buries nothing.
    return clean.startswith(SIDE_EFFECT_GETS) or _under_admin(clean)


# The one carve-out from the admin surface: a device's OWN self-service routes.
# `/api/v1/devices/self/*` is under `/api/v1/devices` by path, but it is `read`
# scoped, not admin — a phone registers its own push endpoint and preferences
# (API.md §9.23), and scoping that to admin would make push structurally
# impossible on a phone, silently and permanently, because APNs tokens rotate
# on reinstall and restore. The id in these routes is always the literal
# `self`, resolved to the CALLER's device from its token — never a parameter —
# so "manage myself" cannot become "manage another device" by editing the path.
SELF_SUBTREE = "/api/v1/devices/self"


def _under_admin(path):
    """Is this path inside the admin surface? Exact segment, not substring.

    `startswith("/api/v1/devices")` alone would also match
    `/api/v1/devices-of-other-people`, which is the direction that is safe here
    (more refusals) but would be the wrong direction if this list were ever
    reused for an allowance. So it is written correctly once.

    `/api/v1/devices/self/*` is explicitly NOT admin — see `SELF_SUBTREE`. The
    check is segment-exact for the same reason the ADMIN check is: a substring
    test would let `/api/v1/devices/selfish` masquerade as self-service.
    """
    if path == SELF_SUBTREE or path.startswith(SELF_SUBTREE + "/"):
        return False
    return any(path == p or path.startswith(p + "/") for p in ADMIN)


def admin(method, path):
    """Does this route answer only to this machine holding no token?"""
    return _under_admin(path.split("?", 1)[0])


def parse_bearer(header):
    """`Authorization: Bearer orc1_<devid>_<secret>` -> `(devid, token)`.

    None for anything else, and "anything else" is generous on purpose: a
    scheme that is not Bearer, a missing token, extra words, a token whose
    shape is not this version's, a byte that is not ASCII. Every one of them is
    a REFUSAL rather than an attempt to be helpful — a header this function
    half-understood is exactly how a parser turns into a bypass.

    Non-ASCII deserves its own note: `http.client` decodes headers as latin-1,
    so a UTF-8 token arrives here as mojibake and `.encode()` in `_hash` would
    hash the wrong bytes rather than raise. API.md §2.1 says that is a 401, not
    a 500, and this is where that happens.
    """
    if not header or not isinstance(header, str) or not header.isascii():
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1]
    # maxsplit=2, and it is not a detail: `secrets.token_urlsafe` emits the
    # base64url alphabet, which INCLUDES `_`. A plain three-way split rejects
    # roughly half of all valid tokens — a bug that reads as "authentication
    # works" right up until the phone that fails is not yours to test with.
    bits = token.split("_", 2)
    if len(bits) != 3 or bits[0] != TOKEN_VERSION:
        return None
    devid, secret = bits[1], bits[2]
    if len(devid) != DEVID_BYTES * 2 or not devid.isalnum() or not secret:
        return None
    return devid, token


def same_origin(origin, host):
    """Is this request's `Origin` this server's own?

    `Origin` is `scheme://host[:port]`; `Host` is `host[:port]`. Comparing the
    authority of the first against the whole of the second is the comparison
    that needs no configuration and no knowledge of what address we were bound
    to — which matters, because the tailnet address, `localhost` and
    `127.0.0.1` are all legitimately us and only one of them is in `CFG`.

    It does NOT stop DNS rebinding: `evil.com` resolved to 127.0.0.1 sends
    `Origin: http://evil.com` and `Host: evil.com`, which agree with each
    other. That needs a Host ALLOWLIST (API.md §2.3 step 2), which needs the
    bound address, and it is deferred — with the note that the preflight rule
    above already blocks the mutation half of a rebinding attack, because a
    JSON POST cannot reach `do_POST` without an OPTIONS this server refuses.
    """
    if not origin:
        return True                     # absent is same-origin, per fetch
    scheme, sep, authority = origin.partition("://")
    # `null` — the opaque origin a sandboxed iframe or a `data:` URL sends —
    # needs no case of its own: it has no `://`, so it has no authority to
    # match and lands on False. Neither does a request with no `Host`, which
    # has nothing to match AGAINST. Both fail closed, which is the whole rule.
    return bool(sep) and bool(host) and authority == host


def check(peer, header, method, path, now=None, origin=None, host=None,
          content_type=None):
    """THE check. One call, one answer, and it has side effects on purpose.

    `server.Handler.parse_request` is its only caller inside the package, which
    is what makes it unskippable: every route in the process is dispatched
    *after* `parse_request` returns True, so a route cannot opt out, forget, or
    be added without it. See `tests/test_auth.py::TestEveryRouteIsGuarded`,
    which reads the routes back out of the handler's own source rather than
    from a list that would rot.

    The order below is load-bearing; each step is where it is for a reason:

    1. **Exempt route** — before the budget, after the browser guards are in
       scope. `/api/health` must answer when the credential is the thing being
       diagnosed, and `POST /api/v1/pair` must answer a phone that has no token
       yet; neither validates a token, so there is nothing here to brute-force.
       Exempt means *no token required*, NOT *no checks*: the cross-site guards
       run on both, because one of them is a mutation.
    2. **Loopback with no credential** — before the budget is consulted. This
       is what makes the browser un-throttleable: a request that presents
       nothing from a machine that is already inside cannot fail, so it can
       never spend from a bucket, so no flood from anywhere else can lock the
       board out of its own server.
    3. **The budget** — before the token is looked at, so an exhausted peer
       costs a dict lookup rather than a sha256, and learns nothing from the
       timing of a refusal it was never going to pass.
    4. **Shape**, then **identity**, then **revocation**. Each refuses; each
       spends one from the budget.
    4a. **Admin** — after the token is known good, so the answer is "this route
       is not yours" rather than "who are you". No token reaches it; see
       `ADMIN`.
    5. **The browser guards** — `Origin`, and `Content-Type` on a mutation.
       LAST, on BOTH allow paths, which is `browser_guards`' own docstring:
       they are about the browser rather than the caller, so they must not
       pre-empt a 401, and they must still run before anything is permitted.

    Everything unrecognised lands on a refusal. There is no branch here that
    ends in "allow" by falling through.

    What it costs, since it now runs on every request (20,000 calls each,
    against a one-device registry):

        exempt /api/health                    0.5 us
        loopback, no header — the board       1.8 us
        tailnet, valid token — the phone     14.9 us   (stat + sha256 + memo)
        tailnet, refused                     99.6 us   (an audit line, on disk)

    `/api/state` answers in ~800 us, so the board pays 0.2 % and a phone 1.9 %.
    A stream pays it ONCE, at open: `/api/events` is dispatched after this
    returns and then holds the socket for its whole life, so a keepalive costs
    nothing here. The expensive row is the refusal, and it is expensive because
    it writes to disk — which is the one row an attacker controls, and the
    reason the failure budget exists.
    """
    now = time.time() if now is None else now
    presented = parse_bearer(header)
    home = loopback(peer) and bool(config.CFG.get("auth_trust_loopback", True))

    def refuse(status, code, message, retry_after=None, spend=1.0):
        if spend:
            _budget(peer, now, spend=spend)
        # Gated on the budget: an exhausted peer that keeps knocking is refused
        # with a dict lookup, and MUST NOT buy a disk write per knock, or the
        # "append-only forever" log becomes a disk-fill DoS that buries the
        # evidence it exists for. `_audit_gate` writes one `throttled` marker at
        # the crossing and then falls silent (ADR 0014's 10 lines/min/IP).
        if _audit_gate(peer, now):
            audit(at=now, peer=peer, device=(presented or (None, None))[0],
                  label=None, method=method, path=path[:200], outcome="refuse",
                  code=code)
        return Verdict(False, status, code, message, retry_after=retry_after)

    def browser_guards():
        """The two that are about the BROWSER rather than the caller.

        They run after identity is settled and before anything is allowed —
        the order matters both ways round. After, because a stranger's POST
        deserves "you are not authenticated", not a lecture about media types,
        and because `refuse` is more useful when the earlier failure wins.
        Before, because the request they stop arrives WITH a good identity: a
        page you are visiting borrows the board's loopback trust, and a web
        client would one day borrow its token.
        """
        if not same_origin(origin, host):
            return refuse(403, CROSS_ORIGIN,
                          f"this server answers its own pages only; "
                          f"'{origin}' is not one of them", spend=0)
        if method != "GET" and \
                (content_type or "").split(";")[0].strip().lower() != JSON_TYPE:
            # See JSON_TYPE: the CSRF guard, not a nicety about media types.
            # Without it a page you are merely VISITING can post a `text/plain`
            # body that `do_POST` happily parses as JSON and types at an agent.
            return refuse(415, NOT_JSON,
                          f"a mutation must be sent as {JSON_TYPE}; this is "
                          f"what stops another site's page reaching your "
                          f"agents through your browser", spend=0)
        return None

    if exempt(method, path):
        # No token needed — and the browser guards STILL RUN. That is the
        # change pairing forced, and it was a real hole for the ten minutes it
        # existed: `POST /api/v1/pair` is an unauthenticated mutation, so a
        # `return` here would have let a page you are merely visiting post a
        # `text/plain` pairing claim from your browser with no preflight. It
        # could only guess at the code, but "it would probably fail anyway" is
        # not how rule 6 reads.
        #
        # `GET /api/health` stays ABOVE the failure budget, which is what it
        # needs: the route you use to diagnose a credential must answer even to
        # a peer that has burned its budget presenting the broken one.
        #
        # `POST /api/v1/pair` does NOT get that exemption. It is an
        # unauthenticated mutation an off-machine peer (T1, the threat model's
        # primary attacker) can hammer, and ADR 0014 promised it would "inherit
        # this bucket". So it consults the budget here — an exhausted peer is
        # refused at the door with the same 429 as any other route, its own
        # `pairing.claim` refusals spend from the bucket, and the two audit
        # writes a claim used to cost per request stop the moment it is out of
        # budget. That is what actually closes the pairing door at ~10/min/IP.
        blocked = browser_guards()
        if blocked:
            return blocked
        if method != "GET" and _budget(peer, now) < 1.0:
            wait = _retry_after(peer, now)
            return refuse(429, RATE_LIMITED,
                          f"too many failed attempts from {peer}; try again in "
                          f"{wait}s", retry_after=wait, spend=0)
        if audited(method, path) and _audit_gate(peer, now):
            audit(at=now, peer=peer, device=None, label="unauthenticated",
                  method=method, path=path[:200], outcome="allow")
        return ALLOW_LOOPBACK

    if header is None and home:
        blocked = browser_guards()
        if blocked:
            return blocked
        if audited(method, path):
            audit(at=now, peer=peer, device=None, label="loopback",
                  method=method, path=path[:200], outcome="allow")
        return ALLOW_LOOPBACK
    if _budget(peer, now) < 1.0:
        wait = _retry_after(peer, now)
        return refuse(429, RATE_LIMITED,
                      f"too many failed authentication attempts from {peer}; "
                      f"try again in {wait}s", retry_after=wait, spend=0)
    if presented is None:
        if header is None:
            return refuse(401, NO_TOKEN,
                          "this server serves the text of your agents' "
                          "conversations and can type into their terminals; "
                          "it authenticates every request that is not from "
                          "the machine it runs on. Send "
                          "`Authorization: Bearer orc1_…`.")
        return refuse(401, MALFORMED,
                      "that Authorization header is not a `Bearer orc1_…` "
                      "token this server can read")
    devid, token = presented
    known, error = load_registry()
    if error:
        # Rule 6, the literal case: an unparseable registry is a refusal. It is
        # NOT an empty registry — "nobody is registered" and "I cannot tell who
        # is registered" get different answers, because the second one is a
        # broken machine and saying so is what gets it fixed.
        return refuse(503, UNAVAILABLE,
                      f"the device registry at {REGISTRY} cannot be read, so "
                      f"no request can be authenticated: {error}")
    device = known.get(devid)
    # The hash is computed and compared even when the id is unknown. A
    # short-circuit would answer an unknown device FASTER than a known one with
    # a wrong secret, which is a device-id oracle; the id is public, so this is
    # tidiness rather than a hole, but it costs one sha256 on a path that was
    # going to refuse anyway.
    matched = hmac.compare_digest(str((device or {}).get("token_sha256") or ""),
                                  _hash(token))
    if device is None or not matched:
        # ONE answer for "no such device" and "wrong secret", and one code path
        # to produce it: a caller that could tell them apart could enumerate
        # device ids for free. `compare_digest` and never `==` — string
        # comparison returns at the first differing byte, and over enough
        # samples that is the hash, one byte at a time.
        return refuse(401, UNKNOWN, "that token is not a device this server "
                                    "knows")
    if device.get("revoked"):
        # Distinguishable from UNKNOWN on purpose. The holder learns nothing
        # they did not already know — they were holding the token — and the app
        # learns to stop retrying and ask to be paired again, instead of
        # hammering a door that will never open.
        return refuse(401, REVOKED,
                      f"device '{device.get('label')}' was revoked; pair again "
                      f"to get a new token")
    if admin(method, path):
        # A GOOD token, refused. This is the whole of `ADMIN`: authentication
        # succeeded and the route still is not theirs, because the thing behind
        # it is the ability to revoke devices — including this one. It is
        # spend=0 deliberately: the caller is a known device making a legitimate
        # mistake, not somebody guessing, and charging it would let a buggy app
        # lock its own phone out of the API it is entitled to.
        return refuse(403, ADMIN_ONLY,
                      f"device management answers to the Mac itself and to no "
                      f"token — not even a valid one, because a device that "
                      f"could revoke devices could revoke the device that "
                      f"would have revoked it. Use the board, or "
                      f"`python3 -m orchestra --list-devices`.", spend=0)
    blocked = browser_guards()
    if blocked:
        return blocked
    _forgive(peer)
    _touch(devid, now)
    if audited(method, path):
        audit(at=now, peer=peer, device=devid, label=device.get("label"),
              method=method, path=path[:200], outcome="allow")
    return Verdict(True, device=public(device))


# ---------------------------------------------------------------- the bind

def bind_refusal(host):
    """Why this host must not be bound yet — or None if it may be.

    ADR 0013's first consequence: *"the server refuses to bind beyond loopback
    unless a token is configured. Silent wide exposure must be impossible."*
    Binding a tailnet address with an empty registry would serve every
    transcript on the machine to every device on the tailnet, and — this is the
    part that makes it a refusal rather than a warning — it would do it
    SILENTLY, because the failure looks exactly like success.

    `0.0.0.0` is refused unless the user asked for it by a DIFFERENT NAME.
    It is not a tailnet bind; it is every interface including the coffee-shop
    wifi, and ADR 0013 is explicitly scoped to the tailnet ("if the server is
    ever exposed … TLS stops being optional and this ADR must be superseded").
    Making that transition loud rather than silent is the ADR's own instruction.

    So `--host 0.0.0.0` remains a refusal — that spelling is one slip of muscle
    memory away from a tailnet address, and its error message now names the
    flag that does what the user probably meant (`--tailnet`). The escape hatch
    is `--bind-every-interface`, which cannot be typed by accident and cannot
    be set from the config file at all (`config.load_config` overwrites the key
    from the parsed arguments every time, so a stale line in a JSON file cannot
    quietly open the machine). It does NOT bypass the registry check below: a
    wide bind with nobody registered is refused however loudly it was asked
    for.
    """
    if loopback_bind(host):
        return None
    if host in ("0.0.0.0", "::", "*") and not config.CFG.get("bind_every_interface"):
        return (f"refusing to bind {host}: that is every interface, not the "
                f"tailnet. This server has no TLS by design (ADR 0013, which "
                f"is scoped to WireGuard) and serves your transcript text. "
                f"Use --tailnet to bind the tailnet address, or say what you "
                f"mean with --bind-every-interface.")
    known, error = load_registry()
    if error:
        return (f"refusing to bind {host}: the device registry at {REGISTRY} "
                f"cannot be read ({error}), so nothing off this machine could "
                f"be authenticated.")
    if not any(not d.get("revoked") for d in known.values()):
        return (f"refusing to bind {host}: no device is registered, so every "
                f"request from off this machine would be refused anyway — and "
                f"if the check were ever wrong, the whole fleet's transcripts "
                f"would be readable. Run `python3 -m orchestra --add-device "
                f"<label>` first.")
    return None
