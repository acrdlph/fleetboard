"""orchestra.push — the APNs transport: an ES256 provider token, and one POST.

This module is TRANSPORT ONLY. It knows how to sign a JWT, how to put bytes on
the wire, and how to read Apple's answer. It has no opinion about when a
notification is worth sending, what it should say, or whether the user is
asleep — that is `notify.py`, and the seam between them is `Sink.send`, one
method taking an already-composed payload.

Zero dependencies is preserved by shelling out rather than by importing: the
stdlib has no ECDSA and no HTTP/2 client, and ADR 0003 traded two macOS
binaries for a pip install. Both go through `shell.run`, which is the seam the
test suite already stands in for.

--------------------------------------------------------------------------
THE SHARP EDGE
--------------------------------------------------------------------------

`openssl dgst -sha256 -sign` emits an ASN.1 **DER** signature —
`SEQUENCE { INTEGER r, INTEGER s }`. JWS/ES256 requires **raw r‖s, 64 bytes**,
each half fixed at 32. They are not the same bytes and one is not a prefix of
the other. Get this wrong and Apple answers `403 InvalidProviderToken`, which
is the SAME answer you get for a wrong Key ID, a wrong Team ID, an expired
token and a `.p8` from another account — five causes, one message, no detail.

DER integers are signed and minimal-width, so the length varies per signature.
Measured on this machine, 400 signatures from one P-256 key:

    total DER length   69: 1     70: 93    71: 204   72: 102
    (len r, len s)     (32,31): 1  (32,32): 93  (32,33): 97
                       (33,32): 107  (33,33): 102

Three consequences, and each one is a bug somebody ships:

  * **A fixed-offset parser is wrong 77 % of the time here.** `der[4:36]` is
    correct only for the (32,32) case, which is under a quarter of them.
  * **A parser that assumes 32-byte integers is wrong 0.25 % of the time** —
    the (32,31) row. `s` came back SHORT because its leading byte was zero, and
    a 31-byte half concatenated raw produces a 63-byte signature that Apple
    rejects. It happened once in 400 and it is the case left-padding exists
    for. ARCHITECTURE.md's measured distribution (`{70: 96, 71: 194, 72: 110}`)
    does not contain this row at all — it never observed a short integer, so a
    reader would conclude `rjust` is defensive rather than load-bearing.
  * **A 33-byte integer is not an error.** It is a 32-byte value whose high bit
    is set, wearing DER's mandatory `0x00` sign pad. 52 % of signatures have at
    least one.

`der_to_raw` is therefore a real parser: it reads lengths, strips the sign pad,
left-pads to the curve width, and REFUSES anything it does not fully
understand. It is the most heavily tested function in this package, and
`tests/test_push.py` verifies its output the only way that proves anything —
by checking the resulting r‖s against the public key with an independent
P-256 implementation that shares no code with it.

--------------------------------------------------------------------------
WHAT WAS MEASURED HERE, AND WHAT COULD NOT BE
--------------------------------------------------------------------------

Against Apple's real sandbox, from this machine, with no key:

    curl -sS --config … https://api.sandbox.push.apple.com/3/device/abc123
    → HTTP/2 403 · apns-id: 030E7BEF-… · {"reason":"InvalidProviderToken"}

So the transport works end to end: HTTP/2 negotiates, the request is accepted,
Apple answers with an `apns-id` and a reason. Everything below the provider
token is proven. The provider token itself cannot be proven without a `.p8`
that only the account holder can create — see `docs/mobile/APNS-SETUP.md`.

The token never rides argv. `ps` is world-readable on macOS, so a
`-H "authorization: bearer eyJ…"` would publish a 40-minute credential to
every process on the machine. It goes in a mode-0600 config file that curl
reads and we delete.

ONE REQUEST PER `curl`. Batching N pushes into one process with `--next`
separators looks obvious and is broken: `--next` resets all LOCAL options, and
`header` is local. Verified here against a local echo server —

    transfer 1 → authorization: bearer GLOBAL
    transfer 2 → None                          ← unauthenticated
    transfer 3 → authorization: bearer THIRD

so every push after the first would go out with no credential and come back
403. Repeating every option inside every block fixes it; not batching avoids
it. At fleet scale (a phone or three) the handshake is not the cost worth
optimising, and the failure mode of getting it wrong is silent.
"""

import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field

from . import config, shell

# Apple's two hosts. They are not interchangeable and a token registered
# against one is meaningless to the other: a development build's device token
# is only known to sandbox, a TestFlight or App Store build's only to
# production. This is historically the most common "push just doesn't work",
# which is why the client reads `aps-environment` out of its embedded
# provisioning profile at RUNTIME rather than branching on `#if DEBUG` —
# TestFlight builds are DEBUG=0 and would silently pick the wrong one.
HOSTS = {"production": "api.push.apple.com",
         "sandbox": "api.sandbox.push.apple.com"}
PORT = 443

# How long a provider token is reused. Apple's rules are a floor AND a ceiling:
# a token older than 60 minutes is rejected (`ExpiredProviderToken`), and
# regenerating one more often than every ~20 minutes earns
# `429 TooManyProviderTokenUpdates` — which is a rate limit on the KEY, not on
# the connection, so a process that mints one per push locks itself out of push
# entirely. 40 minutes sits in the middle of the only window both rules allow.
JWT_TTL_S = 40 * 60

# The curve width. ES256 is P-256, always — `alg: ES256` is not a family, it
# names exactly one curve and one hash, and a P-384 key in a `.p8` would sign
# happily here and be rejected by Apple with the usual single word.
COORD_BYTES = 32

# A device token is 32 bytes of hex today (64 chars). Apple has never promised
# that, and the length has changed once already, so the check is a RANGE and a
# character class rather than `len == 64` — the rule is "hex, plausible", and
# anything else is a client bug worth naming rather than a request worth
# sending.
TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64,200}$")

# Apple's own timeouts are generous; ours cannot be. Every one of these calls
# happens on the notifier's thread, and a wedged TLS handshake to a network
# that is down must not hold the pipeline for a minute — the notification is
# already late by then and the next sweep will re-derive it.
SIGN_TIMEOUT_S = 6
POST_TIMEOUT_S = 12


# ------------------------------------------------------------------ base64url

def b64u(raw):
    """base64url, no padding — RFC 7515 §2. The padding is not optional
    decoration: `=` is not in the base64url alphabet and a JWT carrying one is
    rejected by strict parsers, Apple's included."""
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64u_json(obj):
    """A JOSE segment. `separators` because the default `json.dumps` puts a
    space after every `:` and `,` — harmless to a parser, but it inflates every
    segment and makes byte-comparing two encoders needlessly hard."""
    return b64u(json.dumps(obj, separators=(",", ":"),
                           sort_keys=True).encode("utf-8"))


# --------------------------------------------------------------- DER -> r‖s

def der_to_raw(der, n=COORD_BYTES):
    """ASN.1 `SEQUENCE { INTEGER r, INTEGER s }` -> raw `r‖s`, `2n` bytes.

    Every branch here rejects rather than guesses, and that is the whole
    design: a signature this function half-understood produces 64 plausible
    bytes that Apple answers with one word. A `ValueError` on this machine at
    least says which byte was wrong.

    THE TRUNCATION CASE IS EXPLICIT, and it is why this reads longer than the
    fourteen lines it could be. Slicing past the end of a `bytes` in Python
    returns a SHORT slice rather than raising, so a truncated DER — a signature
    lost to a full disk, a killed openssl, a half-written file — parses
    cheerfully into a wrong-length integer, gets left-padded to 32 bytes, and
    becomes a syntactically perfect signature of nothing. Each length is
    therefore checked against what remains BEFORE the slice is taken, and the
    error is a `ValueError`, never an `IndexError`.
    """
    if not isinstance(der, (bytes, bytearray)):
        raise ValueError(f"signature must be bytes, not {type(der).__name__}")
    der = bytes(der)
    if len(der) < 8:
        raise ValueError(f"signature too short to be DER ({len(der)} bytes)")
    if der[0] != 0x30:
        raise ValueError(f"not a DER SEQUENCE (tag 0x{der[0]:02x}, want 0x30)")

    # The SEQUENCE length. A P-256 signature is ~70 bytes so this is always the
    # short form, but the long form is read rather than assumed: a parser that
    # silently mis-locates its first INTEGER when handed a longer curve's
    # signature is exactly the class of bug this module exists to avoid.
    ln = der[1]
    if ln & 0x80:
        nbytes = ln & 0x7F
        if nbytes == 0 or 2 + nbytes > len(der):
            raise ValueError("malformed DER SEQUENCE length")
        body_len = int.from_bytes(der[2:2 + nbytes], "big")
        i = 2 + nbytes
    else:
        body_len, i = ln, 2
    if i + body_len != len(der):
        # Trailing bytes are not harmless. openssl writes exactly one
        # signature; anything after it means the file is not what we think it
        # is — two concatenated signatures, or a partially overwritten one.
        raise ValueError(f"DER SEQUENCE says {body_len} bytes of content, "
                         f"buffer has {len(der) - i}")

    out = b""
    for which in ("r", "s"):
        if i + 2 > len(der):
            raise ValueError(f"truncated before INTEGER {which}")
        if der[i] != 0x02:
            raise ValueError(f"expected DER INTEGER for {which}, "
                             f"got tag 0x{der[i]:02x}")
        vlen = der[i + 1]
        if vlen & 0x80:
            raise ValueError(f"INTEGER {which} uses long-form length — no "
                             f"P-256 component needs 128 bytes")
        if vlen == 0:
            raise ValueError(f"INTEGER {which} is empty")
        if i + 2 + vlen > len(der):
            raise ValueError(f"INTEGER {which} claims {vlen} bytes, "
                             f"{len(der) - i - 2} remain")
        v = der[i + 2:i + 2 + vlen]
        i += 2 + vlen
        # DER integers are SIGNED, so a value whose top bit is set carries a
        # mandatory 0x00 pad. Stripping leading zeros is therefore correct and
        # not merely tolerant — but `lstrip` on an all-zero value yields b"",
        # and a zero r or s is not a signature, it is a broken one.
        v = v.lstrip(b"\x00")
        if not v:
            raise ValueError(f"INTEGER {which} is zero — not a signature")
        if len(v) > n:
            raise ValueError(f"INTEGER {which} is {len(v)} bytes, wider than "
                             f"the {n}-byte curve")
        # LEFT-pad. The measured (32,31) case is exactly this: `s` was one byte
        # short because its leading byte was zero, and concatenating it raw
        # would produce a 63-byte signature. Right-padding, or skipping the pad,
        # is a bug that fires on well under 1 % of signatures — often enough to
        # reach a user, rarely enough to survive a test run.
        out += v.rjust(n, b"\x00")
    return out


# ------------------------------------------------------------------- signing

class SigningError(Exception):
    """openssl could not sign. Carries a sentence naming the likely cause,
    because every one of these is a setup problem the user can fix and none of
    them is recoverable by retrying."""


def sign_es256(key_path, signing_input, timeout=SIGN_TIMEOUT_S):
    """Sign `signing_input` (bytes) with the P-256 key at `key_path`.
    Returns raw `r‖s`, 64 bytes — already converted.

    Both the input and the signature travel through FILES rather than through
    pipes, for two unrelated reasons that happen to agree. `shell.run` captures
    stdout as TEXT and `.strip()`s it, so a DER signature read from stdout
    would be mangled by UTF-8 decoding and by any leading or trailing byte that
    happens to be whitespace — silently, and only for some signatures. And the
    key stays a path the whole way: it is never read into this process, so it
    cannot end up in a traceback, a log line, or a core file.

    The temp directory is 0700 and is removed on every path including failure.
    What lands in it is the *signing input* (a public JWT header and claim set)
    and the signature — neither is a secret, but the directory is tight anyway
    because the cost is one octal literal.
    """
    key_path = str(key_path)
    if not os.path.isfile(key_path):
        raise SigningError(f"no APNs key at {key_path} — create one at "
                           f"developer.apple.com (see docs/mobile/APNS-SETUP.md)")
    tmp = tempfile.mkdtemp(prefix="orchestra-apns-")
    try:
        os.chmod(tmp, 0o700)
        inp, sig = os.path.join(tmp, "jwt.in"), os.path.join(tmp, "jwt.der")
        with open(inp, "wb") as f:
            f.write(signing_input)
        rc, out = shell.run(["openssl", "dgst", "-sha256", "-sign", key_path,
                             "-out", sig, inp], timeout=timeout)
        if rc != 0:
            # `shell.run` swallows stderr, so the return code is all we have.
            # It is enough to distinguish the two causes that matter, and
            # guessing between them beats a bare "openssl failed".
            raise SigningError(
                f"openssl could not sign with {key_path} — the file must be "
                f"the unmodified .p8 from developer.apple.com (a PKCS#8 "
                f"'BEGIN PRIVATE KEY' block); a .cer, a .p12 or a re-wrapped "
                f"key will not work (openssl exit {rc})")
        try:
            der = open(sig, "rb").read()
        except OSError as e:
            raise SigningError(f"openssl reported success but wrote no "
                               f"signature: {e}") from None
        try:
            return der_to_raw(der)
        except ValueError as e:
            # Reached only if openssl produced something that is not a P-256
            # DER signature — in practice, a key on another curve. Say so,
            # because "not a DER SEQUENCE" is true and useless.
            raise SigningError(
                f"the signature openssl produced is not a P-256 ECDSA "
                f"signature ({e}) — is {key_path} really an ES256 APNs "
                f"auth key?") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def provider_jwt(key_path, key_id, team_id, now=None):
    """One APNs provider token, signed. `header.claims.signature`, base64url.

    Apple's requirements, each of which is a silent 403 when missed:

      * `alg: ES256` and `kid: <Key ID>` in the HEADER — the key id is not a
        claim, and a provider token carrying it as one authenticates as
        nothing;
      * `iss: <Team ID>` and `iat: <now>` in the CLAIMS, and nothing else is
        required. `exp` is absent on purpose: Apple derives expiry from `iat`
        (one hour) and a token carrying its own `exp` is not more valid, it is
        just longer.
      * `iat` in SECONDS, integer. A float here is a token Apple rejects
        without saying why, and `time.time()` returns a float.
    """
    now = time.time() if now is None else now
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    claims = {"iss": team_id, "iat": int(now)}
    signing_input = f"{b64u_json(header)}.{b64u_json(claims)}".encode("ascii")
    return f"{signing_input.decode('ascii')}.{b64u(sign_es256(key_path, signing_input))}"


class ProviderToken:
    """The JWT cache, and the reason it is a class rather than a global.

    Apple rate-limits token GENERATION per key, so the cache is not an
    optimisation — a fleet that mints one token per push is a fleet that stops
    receiving push. It is keyed on `(key_path, key_id, team_id)` so that
    changing the key in the config file invalidates it without a restart, and
    it is locked because the notifier thread and a `/api/v1/push/test` request
    can both want a token at once and the loser must WAIT rather than sign a
    second one.

    `force()` exists for exactly one caller: a `403 ExpiredProviderToken`,
    which means our clock and Apple's disagree by more than an hour. Retrying
    with the same cached token would fail identically until the TTL expired,
    so the send path invalidates and retries ONCE. It never loops — a second
    403 is a configuration error, not a stale token.
    """

    def __init__(self, ttl_s=JWT_TTL_S):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._key = None          # (key_path, key_id, team_id)
        self._jwt = None
        self._at = 0.0
        self.mints = 0            # tokens signed, ever — a health counter

    def get(self, key_path, key_id, team_id, now=None):
        now = time.time() if now is None else now
        key = (str(key_path), key_id, team_id)
        with self._lock:
            if (self._jwt is not None and self._key == key
                    and now - self._at < self.ttl_s):
                return self._jwt
            jwt = provider_jwt(key_path, key_id, team_id, now=now)
            self._key, self._jwt, self._at = key, jwt, now
            self.mints += 1
            return jwt

    def force(self):
        """Discard the cached token. The next `get` signs a fresh one."""
        with self._lock:
            self._jwt, self._at = None, 0.0

    def age_s(self, now=None):
        now = time.time() if now is None else now
        return None if self._jwt is None else now - self._at


_TOKENS = ProviderToken()


# ------------------------------------------------------------- credentials

@dataclass(frozen=True)
class Credentials:
    """Everything Apple needs to believe us, and where it comes from.

    All five are the user's to supply and four of them are copied off a web
    page, which is why `problems()` exists and why it names the page. A Key ID
    typed with a transposed character is indistinguishable at the wire from a
    bad signature, so the check that can be done locally is done locally.
    """
    key_path: str = ""
    key_id: str = ""
    team_id: str = ""
    topic: str = ""
    environment: str = "production"

    @classmethod
    def from_config(cls, cfg=None):
        cfg = config.CFG if cfg is None else cfg
        return cls(key_path=str(cfg.get("apns_key_path") or ""),
                   key_id=str(cfg.get("apns_key_id") or ""),
                   team_id=str(cfg.get("apns_team_id") or ""),
                   topic=str(cfg.get("apns_topic") or ""),
                   environment=str(cfg.get("apns_environment") or "production"))

    def problems(self):
        """Everything wrong that can be seen WITHOUT talking to Apple, in the
        order the user must fix it. Empty list = configured.

        The length checks are Apple's own format and have been stable for
        years, but they are checked as SHAPE rather than asserted as truth: a
        10-character Key ID is what the portal issues, and a 9-character one is
        a copy-paste that lost a character — which is worth catching here
        rather than as `InvalidProviderToken` an hour later.
        """
        bad = []
        if not self.key_path:
            bad.append("apns_key_path is not set — the .p8 auth key file "
                       "downloaded from developer.apple.com")
        elif not os.path.isfile(self.key_path):
            bad.append(f"apns_key_path points at nothing: {self.key_path}")
        else:
            try:
                mode = stat.S_IMODE(os.stat(self.key_path).st_mode)
                if mode & 0o077:
                    bad.append(f"{self.key_path} is mode {mode:04o} — the auth "
                               f"key signs for your whole team; chmod 600 it")
            except OSError:
                pass
        if not self.key_id:
            bad.append("apns_key_id is not set — the 10-character Key ID shown "
                       "beside the key in developer.apple.com › Keys")
        elif len(self.key_id) != 10:
            bad.append(f"apns_key_id is {len(self.key_id)} characters, "
                       f"Apple's are 10: {self.key_id!r}")
        if not self.team_id:
            bad.append("apns_team_id is not set — the 10-character Team ID at "
                       "the top right of developer.apple.com")
        elif len(self.team_id) != 10:
            bad.append(f"apns_team_id is {len(self.team_id)} characters, "
                       f"Apple's are 10: {self.team_id!r}")
        if not self.topic:
            bad.append("apns_topic is not set — the app's bundle id, exactly "
                       "as it appears in Xcode (e.g. sh.orchestra.app)")
        if self.environment not in HOSTS:
            bad.append(f"apns_environment is {self.environment!r}, must be "
                       f"'production' or 'sandbox'")
        return bad

    @property
    def configured(self):
        return not self.problems()

    def host(self, environment=None):
        return HOSTS.get(environment or self.environment, HOSTS["production"])


def binaries_missing():
    """Which of the two shelled-out binaries is absent, if either.

    Checked at the point of use rather than at import: this is a macOS-first
    program that must still IMPORT on a box without curl, and a missing binary
    has to reach the user as a sentence about push rather than as an
    ImportError about the board.
    """
    missing = []
    if not shutil.which("openssl"):
        missing.append("openssl")
    if not shutil.which("curl"):
        missing.append("curl")
    return missing


def http2_available():
    """Does this curl actually speak HTTP/2? APNs is HTTP/2-only.

    `curl --version` prints a feature line; nghttp2 in the library list is the
    real evidence. A curl built without it fails every push with a protocol
    error that reads like a network fault.
    """
    rc, out = shell.run(["curl", "--version"], timeout=5)
    return rc == 0 and ("nghttp2" in out or "HTTP2" in out)


# --------------------------------------------------------------- the answer

# Apple's reasons that mean THIS DEVICE TOKEN IS DEAD — stop sending to it and
# forget it. A 410 is the documented one; `BadDeviceToken` on a 400 is the same
# fact arriving through a different door and is the one people miss, so it is
# listed rather than inferred from the status.
GONE_REASONS = frozenset({"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"})

# …and the ones that mean OUR CREDENTIAL is stale, not the device's. One retry
# with a freshly-minted token, then give up: past that it is a wrong key.
STALE_TOKEN_REASONS = frozenset({"ExpiredProviderToken", "InvalidProviderToken"})


@dataclass(frozen=True)
class Response:
    """Apple's answer, or our failure to get one.

    `status` is 0 when there was no HTTP exchange at all — curl could not
    resolve, could not connect, timed out. That is a THIRD state and not a
    failure code: a 0 means try again later, where a 400 means never try this
    again. Collapsing them (say, to `ok=False`) is how a transient tailnet
    outage becomes a permanently dropped device token.
    """
    status: int = 0
    apns_id: str = None
    reason: str = None
    retry_after: float = None
    error: str = None          # transport failure, when status == 0
    environment: str = ""
    took_ms: float = 0.0

    @property
    def ok(self):
        return self.status == 200

    @property
    def gone(self):
        """Apple will never deliver to this token again — drop it."""
        return self.status == 410 or (self.reason in GONE_REASONS)

    @property
    def stale_provider_token(self):
        return self.status == 403 and self.reason in STALE_TOKEN_REASONS

    @property
    def wrong_environment(self):
        """A production token sent to sandbox, or the reverse. Apple says
        `400 BadDeviceToken` for both — the same words it uses for a genuinely
        malformed token, which is why this is worth ONE retry against the other
        host before the token is dropped."""
        return self.status == 400 and self.reason == "BadDeviceToken"

    @property
    def retriable(self):
        """Worth trying again later, unchanged. 429 is Apple asking us to slow
        down; 5xx is Apple being unwell; 0 is the network. Everything else is
        about the request, and repeating it changes nothing."""
        return self.status in (0, 429) or 500 <= self.status < 600

    def summary(self):
        if self.ok:
            return f"200 · apns-id {self.apns_id or '—'}"
        if self.status == 0:
            return f"no answer · {self.error or 'transport failed'}"
        return f"{self.status} · {self.reason or '—'}"


# ------------------------------------------------------------------ the POST

def post(device_token, payload, creds, jwt, environment=None,
         push_type="alert", priority=10, expiration=None, collapse_id=None,
         timeout=POST_TIMEOUT_S):
    """One notification, one `curl`. Returns a `Response`, never raises.

    Never raising is deliberate and is the same rule `shell.run` follows: this
    is called from a notification pipeline, and a pipeline that can take the
    board down because Apple had a bad minute is worse than no pipeline. Every
    failure that is not a delivery is a `Response` with `status == 0`.

    THE HEADERS, and every one of them has a failure that looks like something
    else:

      `apns-topic`        the bundle id. Wrong, and Apple says
                          `DeviceTokenNotForTopic` — which reads like a device
                          problem and is a configuration one.
      `apns-push-type`    REQUIRED since iOS 13. Missing, and the request is
                          rejected outright rather than delivered silently.
      `apns-priority`     10 delivers immediately; 5 lets iOS bundle it for
                          power. 10 on a `background` push is rejected.
      `apns-expiration`   an ABSOLUTE epoch, and this is the classic. Writing
                          `900` means "expired in 1970", so Apple makes ONE
                          attempt and never stores-and-forwards — silently, and
                          only in the offline case the header exists for. `0`
                          genuinely means "try once, now" and is a legitimate
                          value, so this argument is `None`-defaulted rather
                          than falsy-defaulted.
      `apns-collapse-id`  supersedes an UNDELIVERED push with the same id.
                          ≤64 bytes. Only ever for a state that replaces
                          itself; on a discrete fact it deletes history.
    """
    env = environment or creds.environment
    host = creds.host(env)
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    started = time.time()

    tmp = tempfile.mkdtemp(prefix="orchestra-apns-")
    try:
        os.chmod(tmp, 0o700)
        cfgp = os.path.join(tmp, "curl.cfg")
        bodyp = os.path.join(tmp, "body.json")
        outp = os.path.join(tmp, "out")
        hdrp = os.path.join(tmp, "hdr")
        with open(bodyp, "wb") as f:
            f.write(body)

        lines = [
            f'url = "https://{host}/3/device/{device_token}"',
            'request = "POST"',
            "http2",
            f'header = "authorization: bearer {jwt}"',
            f'header = "apns-topic: {creds.topic}"',
            f'header = "apns-push-type: {push_type}"',
            f'header = "apns-priority: {int(priority)}"',
        ]
        if expiration is not None:
            lines.append(f'header = "apns-expiration: {int(expiration)}"')
        if collapse_id:
            # Truncated rather than refused: a collapse id is an optimisation,
            # and losing the tail of one costs a superseded notification. Losing
            # the NOTIFICATION because its grouping key was long is worse.
            cid = str(collapse_id).encode("utf-8")[:64].decode("utf-8", "ignore")
            lines.append(f'header = "apns-collapse-id: {cid}"')
        lines += [f'data-binary = "@{bodyp}"',
                  f'output = "{outp}"',
                  f'dump-header = "{hdrp}"',
                  "silent", "show-error",
                  f'max-time = {int(timeout)}',
                  'write-out = "%{http_code} %{http_version}"']

        # 0600 BEFORE the token is written into it. Opening with the mode is
        # not the same as chmod-ing afterwards: between `open` and `chmod` the
        # file is world-readable and it already contains the credential.
        fd = os.open(cfgp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")

        rc, out = shell.run(["curl", "--config", cfgp], timeout=timeout + 4)
        took = (time.time() - started) * 1000.0
        if rc != 0:
            return Response(status=0, environment=env, took_ms=took,
                            error=f"curl exit {rc} — {host} unreachable, "
                                  f"or no HTTP/2 in this curl")
        code = 0
        m = re.match(r"\s*(\d{3})", out or "")
        if m:
            code = int(m.group(1))
        headers = _read_headers(hdrp)
        reason = _read_reason(outp)
        retry = headers.get("retry-after")
        try:
            retry = float(retry) if retry is not None else None
        except ValueError:
            retry = None
        return Response(status=code, apns_id=headers.get("apns-id"),
                        reason=reason, retry_after=retry, environment=env,
                        took_ms=took,
                        error=None if code else "curl wrote no status code")
    except OSError as e:
        return Response(status=0, environment=env, error=f"{type(e).__name__}: {e}",
                        took_ms=(time.time() - started) * 1000.0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_headers(path):
    """`dump-header` output -> a lower-cased dict. Never raises."""
    try:
        raw = open(path, "r", errors="replace").read()
    except OSError:
        return {}
    out = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip().lower()] = v.strip()
    return out


def _read_reason(path):
    """Apple's `{"reason": "..."}` body, if there is one.

    A 200 has an EMPTY body — no JSON, no object, nothing — so "cannot parse"
    is the normal case on success and must never be an error. Anything
    unparseable returns None and the status carries the meaning.
    """
    try:
        raw = open(path, "rb").read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return (json.loads(raw.decode("utf-8", "replace")) or {}).get("reason")
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------- the sinks

class Sink:
    """Where a composed notification goes. One method, so that a second
    backend (ADR 0003 keeps ntfy as the documented fallback) is a class rather
    than a branch through the pipeline.

    `send` returns a `Response`. A sink that cannot deliver says so with a
    status; it does not raise, and it does not decide whether the caller should
    try again — `Response.retriable` and `Response.gone` are the vocabulary,
    and `notify.py` acts on them.
    """

    name = "none"

    def send(self, device_token, payload, **kw):
        raise NotImplementedError

    def health(self):
        return {"backend": self.name, "ready": False, "problems": []}


class NoopSink(Sink):
    """No push configured. Every send is a `Response(0)` naming why.

    This is the state the user is in until they create a key, and it must be
    INDISTINGUISHABLE FROM WORKING to everything upstream — the event pipeline
    runs, events are recorded, dedup and coalescing work, and the only
    difference is that the last hop reports `no sink`. That is what makes the
    whole pipeline testable and demonstrable today, and it is what makes adding
    a real key a configuration change rather than a code path that has never
    run.
    """

    name = "none"

    def __init__(self, why="no APNs key configured"):
        self.why = why
        self.sent = []          # every payload that WOULD have gone out

    def send(self, device_token, payload, **kw):
        self.sent.append((device_token, payload, kw))
        return Response(status=0, error=self.why)

    def health(self):
        return {"backend": self.name, "ready": False, "problems": [self.why]}


class APNsSink(Sink):
    """Apple, over HTTP/2, with a provider token this class keeps warm.

    THE RETRY POLICY IS EXACTLY TWO RETRIES AND BOTH ARE FOR A DIFFERENT
    QUESTION THAN "did it work". Neither is a backoff loop — backoff belongs to
    the caller, which knows whether the notification is still worth sending by
    the time the wait is over:

      * `403 Expired/InvalidProviderToken` -> mint a new JWT, send once more.
        The cached token is up to 40 minutes old and Apple's hour is measured
        against ITS clock; a machine whose clock drifted forward hits this
        every time until the TTL rolls, and one retry converts a permanent
        outage into a hiccup.
      * `400 BadDeviceToken` -> try the OTHER host once, and if that works,
        SAY SO (`healed_environment`), because the caller must persist the
        correction or every future push pays the same double round trip.

    A third retry is never worth it. Every remaining failure is either about
    the request (400-class, unchanged by repetition) or about Apple (5xx/429,
    which the caller must space out rather than hammer).
    """

    name = "apns"

    def __init__(self, creds=None, tokens=None):
        self.creds = creds or Credentials.from_config()
        self.tokens = tokens or _TOKENS
        self.healed_environment = None   # set when a 400 retry found the truth
        self.sends = 0
        self.last = None                 # last Response, for /api/v1/push/status

    def health(self):
        problems = list(self.creds.problems())
        problems += [f"{b} is not installed — APNs needs it (ADR 0003)"
                     for b in binaries_missing()]
        return {"backend": self.name, "ready": not problems,
                "problems": problems,
                "environment": self.creds.environment,
                "topic": self.creds.topic,
                "jwt_age_s": self.tokens.age_s(),
                "last": self.last.summary() if self.last else None}

    def send(self, device_token, payload, environment=None, **kw):
        problems = self.creds.problems()
        if problems:
            return Response(status=0, error=problems[0])
        missing = binaries_missing()
        if missing:
            return Response(status=0,
                            error=f"{' and '.join(missing)} not installed")
        if not TOKEN_RE.match(device_token or ""):
            # Refused HERE rather than at the wire: a malformed token is a
            # client bug, and sending it costs a round trip to be told
            # `BadDeviceToken`, which this code would then read as "wrong
            # environment" and retry. One local check removes a whole wrong
            # diagnosis.
            return Response(status=400, reason="BadDeviceToken",
                            error="device token is not 64–200 hex characters")

        env = environment or self.creds.environment
        try:
            jwt = self.tokens.get(self.creds.key_path, self.creds.key_id,
                                  self.creds.team_id)
        except SigningError as e:
            return Response(status=0, error=str(e))

        self.sends += 1
        r = post(device_token, payload, self.creds, jwt, environment=env, **kw)

        if r.stale_provider_token:
            self.tokens.force()
            try:
                jwt = self.tokens.get(self.creds.key_path, self.creds.key_id,
                                      self.creds.team_id)
            except SigningError as e:
                self.last = r
                return Response(status=0, error=str(e))
            r = post(device_token, payload, self.creds, jwt, environment=env, **kw)

        if r.wrong_environment:
            other = "sandbox" if env == "production" else "production"
            r2 = post(device_token, payload, self.creds, jwt,
                      environment=other, **kw)
            if r2.ok:
                self.healed_environment = other
                self.last = r2
                return r2
            # Keep the FIRST answer. The second host's `BadDeviceToken` is not
            # new information and reporting it would name the wrong host in the
            # error the user reads.
        self.last = r
        return r


def sink(creds=None):
    """The configured sink, or a `NoopSink` that says exactly what is missing.

    One function, because "is push set up" is a question three call sites ask
    and none of them should answer it by reimplementing the ladder.
    """
    creds = creds or Credentials.from_config()
    problems = list(creds.problems()) + [
        f"{b} is not installed" for b in binaries_missing()]
    if problems:
        return NoopSink(problems[0])
    return APNsSink(creds)


# ------------------------------------------------------------------ backoff

@dataclass
class Backoff:
    """When may we talk to Apple again? One per sink, shared by every device.

    A 429 or a 503 is a statement about the SERVICE, not about one device
    token, so backing off per device would keep hammering with the other
    devices and earn a longer ban. `retry_after` is honoured when Apple sends
    one — it is the only number in this system that is not a guess.

    Doubling from 2 s to a 5-minute ceiling: the pipeline is edge-triggered, so
    a notification that waits five minutes is usually stale anyway and the
    NEXT edge will carry the current truth. Waiting longer buys nothing and
    delays the recovery probe.
    """
    base_s: float = 2.0
    cap_s: float = 300.0
    until: float = 0.0
    consecutive: int = 0

    def blocked(self, now=None):
        return (time.time() if now is None else now) < self.until

    def wait_s(self, now=None):
        now = time.time() if now is None else now
        return max(0.0, self.until - now)

    def note(self, response, now=None):
        """Fold one answer in. Returns True when the caller should hold off."""
        now = time.time() if now is None else now
        if not response.retriable:
            self.consecutive, self.until = 0, 0.0
            return False
        self.consecutive += 1
        if response.retry_after is not None:
            self.until = now + max(0.0, response.retry_after)
        else:
            self.until = now + min(self.cap_s,
                                   self.base_s * (2 ** (self.consecutive - 1)))
        return True

    def ok(self):
        """A delivery. Clears the hold — Apple is answering again."""
        self.consecutive, self.until = 0, 0.0
