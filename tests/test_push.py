#!/usr/bin/env python3
"""`orchestra.push` — the APNs transport, and the one conversion that decides it.

THE CLAIM THIS FILE HAS TO MAKE, and why nothing weaker would do. `openssl`
emits a DER signature; JWS wants raw `r‖s`. A wrong conversion produces 64
bytes that look exactly like a signature, and Apple answers `403
InvalidProviderToken` — the same words it uses for a wrong Key ID, a wrong Team
ID, a `.p8` from another account and a clock an hour out. Five causes, one
message. So "it returned 64 bytes" and "it round-trips through our own
re-encoder" are both worthless: the first tests nothing and the second tests
that a function is its own inverse, which a consistently-wrong pair of
functions passes.

The only claim worth making is **these bytes are a valid P-256 signature of
this message under this public key**, and it is made THREE independent ways:

  1. `P256.verify` below — a from-scratch ECDSA verifier that shares no line
     of code with `push.py`. Its own constants are not trusted either: the
     curve parameters are checked (G is on the curve, and n·G is the point at
     infinity) before any signature is verified with them, so a mistyped
     modulus fails as a mistyped modulus rather than as a signature bug.
  2. `openssl dgst -verify` — the other implementation, on real signatures
     produced in this test run.
  3. The frozen vectors below, which are real openssl output over a real key,
     captured once and independently confirmed by `openssl dgst -verify`.

And the verifier is pinned FROM BOTH SIDES, because a verifier that returns
True unconditionally passes every test above: `test_the_verifier_rejects_*`
mutates one bit of r, of s, of the message and of the public key and requires a
rejection each time.

THE VECTORS ARE THE POINT. Two of the three carry a component SHORTER than 32
bytes — the case that is 1-in-256 per signature, that ARCHITECTURE.md's
measured distribution never observed, and that a plausible implementation gets
wrong. `test_the_naive_parser_this_module_exists_to_avoid` asserts that the
obvious fixed-offset version FAILS on them, which is what makes the passing
tests mean something.

    python3 -m unittest tests.test_push -v
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402
from orchestra import push, shell  # noqa: E402


HAVE_OPENSSL = shutil.which("openssl") is not None


# --------------------------------------------------------- an ECDSA verifier

class P256:
    """NIST P-256 / prime256v1, enough of it to VERIFY. ~60 lines, no imports.

    Jacobian coordinates rather than affine, for one practical reason: affine
    addition needs a modular inverse per step, which is a 256-bit `pow` and
    turns one verification into a third of a second. That is fast enough to be
    correct and slow enough that a test author quietly reduces the sample size
    — so the geometry is chosen to keep the sample honest. One inverse at the
    end, ~4 ms a verification.
    """

    P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
    B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
    GX = 0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296
    GY = 0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
    N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
    # a = -3 for this curve; written as such so the doubling formula reads
    # like the standard one instead of hiding a magic constant.
    A = P - 3

    @classmethod
    def on_curve(cls, x, y):
        return (y * y - (x * x * x + cls.A * x + cls.B)) % cls.P == 0

    @classmethod
    def _double(cls, pt):
        X, Y, Z = pt
        if Z == 0 or Y == 0:
            return (0, 1, 0)
        p = cls.P
        YY = Y * Y % p
        S = 4 * X * YY % p
        M = (3 * X * X + cls.A * pow(Z, 4, p)) % p
        X3 = (M * M - 2 * S) % p
        Y3 = (M * (S - X3) - 8 * YY * YY) % p
        Z3 = 2 * Y * Z % p
        return (X3, Y3, Z3)

    @classmethod
    def _add(cls, a, b):
        X1, Y1, Z1 = a
        X2, Y2, Z2 = b
        if Z1 == 0:
            return b
        if Z2 == 0:
            return a
        p = cls.P
        Z1Z1, Z2Z2 = Z1 * Z1 % p, Z2 * Z2 % p
        U1, U2 = X1 * Z2Z2 % p, X2 * Z1Z1 % p
        S1 = Y1 * Z2 % p * Z2Z2 % p
        S2 = Y2 * Z1 % p * Z1Z1 % p
        if U1 == U2:
            return cls._double(a) if S1 == S2 else (0, 1, 0)
        H, R = (U2 - U1) % p, (S2 - S1) % p
        HH = H * H % p
        HHH = H * HH % p
        U1HH = U1 * HH % p
        X3 = (R * R - HHH - 2 * U1HH) % p
        Y3 = (R * (U1HH - X3) - S1 * HHH) % p
        Z3 = H * Z1 % p * Z2 % p
        return (X3, Y3, Z3)

    @classmethod
    def _mul(cls, k, pt):
        out = (0, 1, 0)
        add = pt
        while k:
            if k & 1:
                out = cls._add(out, add)
            add = cls._double(add)
            k >>= 1
        return out

    @classmethod
    def _affine(cls, pt):
        X, Y, Z = pt
        if Z == 0:
            return None
        p = cls.P
        zi = pow(Z, p - 2, p)
        return (X * zi * zi % p, Y * pow(zi, 3, p) % p)

    @classmethod
    def parameters_are_self_consistent(cls):
        """G on the curve, and n·G = O. Two equations that a mistyped
        constant cannot satisfy by accident."""
        if not cls.on_curve(cls.GX, cls.GY):
            return False
        return cls._affine(cls._mul(cls.N, (cls.GX, cls.GY, 1))) is None

    @classmethod
    def verify(cls, qx, qy, message, raw_sig):
        """FIPS 186-4 §6.4.2 verification over `raw_sig` = r‖s, 64 bytes."""
        if len(raw_sig) != 64:
            return False
        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        if not (1 <= r < cls.N and 1 <= s < cls.N):
            return False
        if not cls.on_curve(qx, qy):
            return False
        e = int.from_bytes(hashlib.sha256(message).digest(), "big")
        w = pow(s, cls.N - 2, cls.N)
        u1, u2 = e * w % cls.N, r * w % cls.N
        pt = cls._add(cls._mul(u1, (cls.GX, cls.GY, 1)),
                      cls._mul(u2, (qx, qy, 1)))
        aff = cls._affine(pt)
        if aff is None:
            return False
        return aff[0] % cls.N == r


# ------------------------------------------------------------ frozen vectors

# Real `openssl dgst -sha256 -sign` output over one throwaway P-256 key,
# captured on this machine and independently confirmed with
# `openssl dgst -sha256 -verify` (all three: "Verified OK").
#
# Only the PUBLIC key is here — verification needs nothing else, and a private
# key in a repository is a private key in a repository even when it is a toy.
#
# Two of the three exist because they are hard to obtain: a component shorter
# than 32 bytes happens for about one signature in 256, so a test that waits
# for one to turn up is a test that usually does not run. These were found by
# signing until they appeared (vector 39 at attempt 39, vector 197 at 197) and
# frozen so the case is covered on every run, deterministically.
VEC_QX = 0x3b72ab737c6935e821bfc6844868384257dc57630d03bd54dff1ae4fa0ebf0bf
VEC_QY = 0x7b2ef478c09ff336c7ef96033e8914cef2f94b4376d56857edf93b14246a0f62

VECTORS = [
    # (label, message, DER hex, len r, len s) — lengths of the TRUE values,
    # after DER's sign padding is stripped.
    ("r is 31 bytes — the short-r case, 69-byte DER",
     b"orchestra-vector-39",
     "3043021f7eca0a059d5e3a2160cfa4757a5b64b0ed68c3f436ec8680ad07b1138eb19c"
     "02205e46cb02c984bb4de54d2a888f3a4ff5f9f422c038c6349a18b8e81312c653a0",
     31, 32),
    ("s is 31 bytes — the short-s case, 70-byte DER",
     b"orchestra-vector-197",
     "3044022100d136b5b928947500e2395eac34ea94526ab5222c34e33af3deaaa0955b96"
     "801b021f604f3d7017a738cdf361afcd5dca2d5f4c31088548d1a7fcf99495281633e8",
     32, 31),
    ("both carry DER's 0x00 sign pad — 72-byte DER",
     b"orchestra-vector-0",
     "3046022100ccd8231bbc58739c7f6563fdbb973a176ea1d1f98ff159baf1286b9af62d"
     "8c9c022100c807b20ef94e4b02c72edd7416ee1d0cd7ad8bdf69af88e135c15bbd46c9182a",
     32, 32),
]


def naive_fixed_offset(der):
    """The implementation this module exists to avoid: assume the two INTEGERs
    both carry a sign pad and live at fixed offsets. Correct for exactly one of
    the three DER shapes."""
    return der[4:36] + der[38:70]


# ------------------------------------------------------------------ the tests

class TestTheVerifierItself(unittest.TestCase):
    """Before the verifier is used to judge anything, it is judged."""

    def test_curve_parameters_are_self_consistent(self):
        self.assertTrue(P256.parameters_are_self_consistent(),
                        "P-256 constants are wrong — every verification below "
                        "would be meaningless")

    def test_the_frozen_public_key_is_on_the_curve(self):
        self.assertTrue(P256.on_curve(VEC_QX, VEC_QY))

    def test_the_verifier_accepts_a_known_good_signature(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = push.der_to_raw(bytes.fromhex(der))
        self.assertTrue(P256.verify(VEC_QX, VEC_QY, msg, raw))

    def test_the_verifier_rejects_a_flipped_bit_in_r(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = bytearray(push.der_to_raw(bytes.fromhex(der)))
        raw[0] ^= 0x01
        self.assertFalse(P256.verify(VEC_QX, VEC_QY, msg, bytes(raw)))

    def test_the_verifier_rejects_a_flipped_bit_in_s(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = bytearray(push.der_to_raw(bytes.fromhex(der)))
        raw[63] ^= 0x01
        self.assertFalse(P256.verify(VEC_QX, VEC_QY, msg, bytes(raw)))

    def test_the_verifier_rejects_the_wrong_message(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = push.der_to_raw(bytes.fromhex(der))
        self.assertFalse(P256.verify(VEC_QX, VEC_QY, msg + b"!", raw))

    def test_the_verifier_rejects_the_wrong_public_key(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = push.der_to_raw(bytes.fromhex(der))
        # the other frozen vector's curve point would do, but any valid point
        # that is not this one proves it: use G.
        self.assertFalse(P256.verify(P256.GX, P256.GY, msg, raw))

    def test_the_verifier_rejects_a_wrong_length_signature(self):
        _, msg, der, _, _ = VECTORS[0]
        raw = push.der_to_raw(bytes.fromhex(der))
        self.assertFalse(P256.verify(VEC_QX, VEC_QY, msg, raw[:63]))


class TestDerToRawAgainstKnownGoodVectors(unittest.TestCase):
    """The conversion, judged by whether its output is a real signature."""

    def test_every_frozen_vector_converts_to_a_valid_signature(self):
        for label, msg, der, _, _ in VECTORS:
            with self.subTest(label):
                raw = push.der_to_raw(bytes.fromhex(der))
                self.assertEqual(len(raw), 64)
                self.assertTrue(
                    P256.verify(VEC_QX, VEC_QY, msg, raw),
                    f"der_to_raw produced 64 bytes that are NOT a valid "
                    f"signature for: {label}")

    def test_the_short_component_is_left_padded_not_right(self):
        """A 31-byte value must arrive as `00 || value`, not `value || 00`.

        Both produce 32 bytes and only one is the number. This is asserted on
        the bytes rather than left to `verify`, because it names the mistake."""
        _, _, der, rlen, slen = VECTORS[0]
        self.assertEqual(rlen, 31)
        raw = push.der_to_raw(bytes.fromhex(der))
        self.assertEqual(raw[0], 0x00, "short r was not LEFT-padded")
        self.assertNotEqual(raw[1], 0x00)

        _, _, der2, _, slen2 = VECTORS[1]
        self.assertEqual(slen2, 31)
        raw2 = push.der_to_raw(bytes.fromhex(der2))
        self.assertEqual(raw2[32], 0x00, "short s was not LEFT-padded")
        self.assertNotEqual(raw2[33], 0x00)

    def test_the_naive_parser_this_module_exists_to_avoid(self):
        """The fixed-offset version must FAIL on two of the three vectors.

        Without this, every test above could be passing for the wrong reason —
        they would look identical if the conversion were trivial. This is the
        assertion that the vectors DISCRIMINATE."""
        wrong = 0
        for label, msg, der, _, _ in VECTORS:
            raw = naive_fixed_offset(bytes.fromhex(der))
            if not P256.verify(VEC_QX, VEC_QY, msg, raw):
                wrong += 1
        self.assertGreaterEqual(
            wrong, 2, "the frozen vectors do not discriminate between a "
                      "correct parser and a fixed-offset one")

    def test_the_sign_pad_is_stripped_not_kept(self):
        """A 33-byte DER INTEGER is a 32-byte value plus DER's mandatory 0x00.
        Keeping the pad gives 33 bytes; refusing the value gives an error."""
        _, msg, der, _, _ = VECTORS[2]
        raw = push.der_to_raw(bytes.fromhex(der))
        self.assertEqual(len(raw), 64)
        self.assertNotEqual(raw[0], 0x00)     # the pad is gone, the value is not
        self.assertTrue(P256.verify(VEC_QX, VEC_QY, msg, raw))


class TestDerToRawRefusals(unittest.TestCase):
    """Everything it must not accept. Each of these is a corrupted signature
    that a lenient parser turns into 64 confident, wrong bytes."""

    GOOD = bytes.fromhex(VECTORS[2][2])

    def test_truncated_is_a_valueerror_not_an_indexerror(self):
        """Slicing past the end of `bytes` returns a SHORT slice rather than
        raising — so a truncated signature parses into a wrong-length integer,
        gets left-padded, and becomes a perfect signature of nothing."""
        for cut in (8, 20, 40, len(self.GOOD) - 1):
            with self.subTest(cut=cut):
                with self.assertRaises(ValueError):
                    push.der_to_raw(self.GOOD[:cut])

    def test_not_a_sequence(self):
        with self.assertRaises(ValueError):
            push.der_to_raw(b"\x31" + self.GOOD[1:])

    def test_second_element_is_not_an_integer(self):
        d = bytearray(self.GOOD)
        d[2 + 2 + d[3]] = 0x03          # the `s` tag
        with self.assertRaises(ValueError):
            push.der_to_raw(bytes(d))

    def test_trailing_bytes_after_the_sequence(self):
        with self.assertRaises(ValueError):
            push.der_to_raw(self.GOOD + b"\x00")

    def test_a_zero_integer_is_not_a_signature(self):
        der = bytes.fromhex("3006020100020100")
        with self.assertRaises(ValueError):
            push.der_to_raw(der)

    def test_an_integer_wider_than_the_curve(self):
        # r declared 40 bytes of nonzero value
        body = b"\x02\x28" + b"\x11" * 40 + b"\x02\x01\x05"
        der = b"\x30" + bytes([len(body)]) + body
        with self.assertRaises(ValueError):
            push.der_to_raw(der)

    def test_a_string_is_refused_by_type(self):
        with self.assertRaises(ValueError):
            push.der_to_raw(self.GOOD.hex())

    def test_empty(self):
        with self.assertRaises(ValueError):
            push.der_to_raw(b"")


@unittest.skipUnless(HAVE_OPENSSL, "openssl not installed")
class TestAgainstRealOpensslSignatures(unittest.TestCase):
    """The frozen vectors prove the shapes. This proves the SHAPES ARE THE
    ONES OPENSSL PRODUCES — on a key generated in this test run, so it cannot
    be passing against a captured artefact of one particular key."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="orchestra-test-apns-")
        cls.key = os.path.join(cls.tmp, "AuthKey_TESTONLY.p8")
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                        "ec_paramgen_curve:P-256", "-out", cls.key],
                       check=True, capture_output=True)
        os.chmod(cls.key, 0o600)
        der = os.path.join(cls.tmp, "pub.der")
        subprocess.run(["openssl", "pkey", "-in", cls.key, "-pubout",
                        "-outform", "DER", "-out", der],
                       check=True, capture_output=True)
        raw = open(der, "rb").read()
        point = raw[-65:]
        assert point[0] == 0x04, "expected an uncompressed SPKI point"
        cls.qx = int.from_bytes(point[1:33], "big")
        cls.qy = int.from_bytes(point[33:], "big")
        cls.pem = os.path.join(cls.tmp, "pub.pem")
        subprocess.run(["openssl", "pkey", "-in", cls.key, "-pubout",
                        "-out", cls.pem], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_sign_es256_produces_signatures_that_verify(self):
        """Twelve real signings. Not one: the DER length varies per signature
        (measured 69/70/71/72 over 400), so a single sample tests one shape."""
        seen = set()
        for i in range(12):
            msg = f"orchestra signing sample {i}".encode()
            raw = push.sign_es256(self.key, msg)
            self.assertEqual(len(raw), 64)
            self.assertTrue(P256.verify(self.qx, self.qy, msg, raw),
                            f"sample {i} did not verify")
            seen.add(raw[:1] == b"\x00")
        # not asserted as a distribution — twelve samples cannot pin one — but
        # the loop is what makes the varying shapes actually get exercised.
        self.assertTrue(len(seen) >= 1)

    def test_openssl_agrees_with_the_hand_rolled_verifier(self):
        """Two implementations, same bytes. The test's own verifier could be
        wrong in a way that matches a wrong conversion; openssl cannot be wrong
        in the same way, because it never sees our conversion at all."""
        msg = b"orchestra cross-check"
        sig = os.path.join(self.tmp, "x.der")
        inp = os.path.join(self.tmp, "x.txt")
        open(inp, "wb").write(msg)
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key,
                        "-out", sig, inp], check=True, capture_output=True)
        der = open(sig, "rb").read()
        out = subprocess.run(["openssl", "dgst", "-sha256", "-verify",
                              self.pem, "-signature", sig, inp],
                             capture_output=True, text=True)
        self.assertIn("Verified OK", out.stdout)
        self.assertTrue(P256.verify(self.qx, self.qy, msg,
                                    push.der_to_raw(der)))

    def test_the_provider_jwt_signature_verifies_over_its_own_signing_input(self):
        """The whole token, end to end — which is the thing Apple checks.

        The signing input is `header.claims`, ASCII, and the third segment must
        be a signature OF THAT STRING. A JWT that signs anything else — the
        JSON before encoding, the claims alone, the string with a trailing
        newline — is well-formed, decodes cleanly, and is rejected by Apple
        with `InvalidProviderToken`."""
        jwt = push.provider_jwt(self.key, "ABCDE12345", "TEAM123456",
                                now=1784636700.9)
        h, c, s = jwt.split(".")
        self.assertEqual(len(jwt.split(".")), 3)

        def unb64(seg):
            import base64
            return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))

        self.assertEqual(json.loads(unb64(h)),
                         {"alg": "ES256", "kid": "ABCDE12345", "typ": "JWT"})
        claims = json.loads(unb64(c))
        self.assertEqual(claims["iss"], "TEAM123456")
        self.assertEqual(claims["iat"], 1784636700)
        self.assertIsInstance(claims["iat"], int)
        self.assertNotIn("exp", claims)

        raw = unb64(s)
        self.assertEqual(len(raw), 64)
        self.assertTrue(
            P256.verify(self.qx, self.qy, f"{h}.{c}".encode("ascii"), raw),
            "the JWT signature does not cover `header.claims` — this is the "
            "403 InvalidProviderToken that has no other symptom")

    def test_no_padding_survives_into_the_jwt(self):
        jwt = push.provider_jwt(self.key, "ABCDE12345", "TEAM123456")
        self.assertNotIn("=", jwt)
        self.assertNotIn("+", jwt)
        self.assertNotIn("/", jwt)

    def test_a_key_on_the_wrong_curve_is_named_not_guessed(self):
        p384 = os.path.join(self.tmp, "p384.p8")
        subprocess.run(["openssl", "genpkey", "-algorithm", "EC", "-pkeyopt",
                        "ec_paramgen_curve:P-384", "-out", p384],
                       check=True, capture_output=True)
        with self.assertRaises(push.SigningError) as cm:
            push.sign_es256(p384, b"x")
        self.assertIn("ES256", str(cm.exception))

    def test_a_missing_key_says_where_to_get_one(self):
        with self.assertRaises(push.SigningError) as cm:
            push.sign_es256(os.path.join(self.tmp, "nope.p8"), b"x")
        self.assertIn("APNS-SETUP", str(cm.exception))

    def test_a_key_that_is_not_a_key(self):
        junk = os.path.join(self.tmp, "junk.p8")
        with open(junk, "w") as _f:
            _f.write("-----BEGIN PRIVATE KEY-----\nnope\n")
        with self.assertRaises(push.SigningError):
            push.sign_es256(junk, b"x")


# -------------------------------------------------------------- base64url

class TestBase64Url(unittest.TestCase):

    def test_rfc7515_stripping(self):
        self.assertEqual(push.b64u(b"\xff\xff\xff"), "____")
        self.assertEqual(push.b64u(b"\x00"), "AA")
        self.assertEqual(push.b64u(b""), "")

    def test_segments_are_compact_and_stable(self):
        a = push.b64u_json({"b": 1, "a": 2})
        b = push.b64u_json({"a": 2, "b": 1})
        self.assertEqual(a, b, "segment encoding depends on dict order")
        import base64
        raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4)).decode()
        self.assertNotIn(" ", raw)


# ---------------------------------------------------------- the token cache

class TestProviderTokenCache(unittest.TestCase):
    """Apple rate-limits token GENERATION per key. A cache miss per push is not
    a slow fleet, it is a fleet that stops receiving push."""

    def setUp(self):
        self.signed = []
        self.real = push.provider_jwt
        push.provider_jwt = lambda kp, ki, ti, now=None: (
            self.signed.append((kp, ki, ti, now)) or f"jwt-{len(self.signed)}")

    def tearDown(self):
        push.provider_jwt = self.real

    def test_a_second_get_inside_the_ttl_signs_nothing(self):
        t = push.ProviderToken(ttl_s=100)
        self.assertEqual(t.get("k", "K", "T", now=1000.0), "jwt-1")
        self.assertEqual(t.get("k", "K", "T", now=1050.0), "jwt-1")
        self.assertEqual(len(self.signed), 1)
        self.assertEqual(t.mints, 1)

    def test_it_refreshes_at_the_ttl(self):
        t = push.ProviderToken(ttl_s=100)
        t.get("k", "K", "T", now=1000.0)
        self.assertEqual(t.get("k", "K", "T", now=1100.0), "jwt-2")

    def test_the_ttl_is_inside_apples_hour_and_outside_its_floor(self):
        """20 min ≤ TTL ≤ 60 min is the only window both of Apple's rules
        allow, and the constant is the thing that has to sit in it."""
        self.assertGreaterEqual(push.JWT_TTL_S, 20 * 60)
        self.assertLess(push.JWT_TTL_S, 60 * 60)

    def test_changing_the_key_invalidates_without_a_restart(self):
        t = push.ProviderToken(ttl_s=10_000)
        t.get("k", "K", "T", now=1000.0)
        self.assertEqual(t.get("k", "K2", "T", now=1001.0), "jwt-2")
        self.assertEqual(t.get("k2", "K2", "T", now=1002.0), "jwt-3")

    def test_force_discards_it(self):
        t = push.ProviderToken(ttl_s=10_000)
        t.get("k", "K", "T", now=1000.0)
        t.force()
        self.assertEqual(t.get("k", "K", "T", now=1001.0), "jwt-2")

    def test_age_is_none_before_the_first_mint(self):
        t = push.ProviderToken()
        self.assertIsNone(t.age_s())


# ------------------------------------------------------------ the response

class TestResponseClassification(unittest.TestCase):
    """Apple says the same thing several ways. The vocabulary is what the
    caller acts on, so the mapping is pinned rather than inferred."""

    def test_410_drops_the_token(self):
        r = push.Response(status=410, reason="Unregistered")
        self.assertTrue(r.gone)
        self.assertFalse(r.retriable)

    def test_400_baddevicetoken_is_gone_but_first_worth_the_other_host(self):
        r = push.Response(status=400, reason="BadDeviceToken")
        self.assertTrue(r.wrong_environment)
        self.assertTrue(r.gone)

    def test_devicetokennotfortopic_is_also_terminal(self):
        self.assertTrue(push.Response(status=400,
                                      reason="DeviceTokenNotForTopic").gone)

    def test_429_and_5xx_are_retriable_and_400_is_not(self):
        self.assertTrue(push.Response(status=429).retriable)
        self.assertTrue(push.Response(status=503).retriable)
        self.assertTrue(push.Response(status=500).retriable)
        self.assertFalse(push.Response(status=400, reason="BadTopic").retriable)
        self.assertFalse(push.Response(status=200).retriable)

    def test_a_transport_failure_is_retriable_and_not_gone(self):
        """The distinction that matters most: a tailnet blip must never look
        like a dead device token, or one outage unregisters every phone."""
        r = push.Response(status=0, error="curl exit 7")
        self.assertTrue(r.retriable)
        self.assertFalse(r.gone)
        self.assertFalse(r.ok)

    def test_stale_provider_token(self):
        self.assertTrue(push.Response(status=403,
                                      reason="ExpiredProviderToken").stale_provider_token)
        self.assertTrue(push.Response(status=403,
                                      reason="InvalidProviderToken").stale_provider_token)
        self.assertFalse(push.Response(status=403,
                                       reason="Forbidden").stale_provider_token)

    def test_summary_reads(self):
        self.assertIn("200", push.Response(status=200, apns_id="A").summary())
        self.assertIn("no answer", push.Response(status=0, error="x").summary())


# --------------------------------------------------------------- the POST

class FakeCurl:
    """Stands in for `shell.run`, and records exactly what curl was handed."""

    def __init__(self, code=200, body=b"", headers="HTTP/2 200\r\napns-id: AID\r\n"):
        self.code, self.body, self.headers = code, body, headers
        self.calls = []
        self.config_text = None
        self.config_mode = None
        self.body_sent = None

    def __call__(self, cmd, cwd=None, timeout=6):
        self.calls.append(list(cmd))
        if cmd[0] != "curl":
            return 1, ""
        cfg = cmd[cmd.index("--config") + 1]
        self.config_text = open(cfg).read()
        self.config_mode = os.stat(cfg).st_mode & 0o777
        out = hdr = None
        for line in self.config_text.splitlines():
            if line.startswith("output = "):
                out = line.split('"')[1]
            elif line.startswith("dump-header = "):
                hdr = line.split('"')[1]
            elif line.startswith("data-binary = "):
                self.body_sent = open(line.split('"')[1][1:], "rb").read()
        if out:
            open(out, "wb").write(self.body)
        if hdr:
            open(hdr, "w").write(self.headers)
        return 0, f"{self.code} 2"


class TestPost(unittest.TestCase):

    CREDS = push.Credentials(key_path="/k.p8", key_id="ABCDE12345",
                             team_id="TEAM123456", topic="sh.orchestra.app",
                             environment="production")

    def setUp(self):
        self.real = shell.run
        self.curl = FakeCurl()
        shell.run = self.curl

    def tearDown(self):
        shell.run = self.real

    def cfg(self):
        return self.curl.config_text

    def test_a_200_is_read_with_its_apns_id(self):
        r = push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT")
        self.assertTrue(r.ok)
        self.assertEqual(r.apns_id, "AID")
        self.assertIsNone(r.reason)

    def test_the_reason_is_read_off_the_body(self):
        self.curl.code = 410
        self.curl.body = b'{"reason":"Unregistered"}'
        r = push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT")
        self.assertEqual(r.status, 410)
        self.assertEqual(r.reason, "Unregistered")
        self.assertTrue(r.gone)

    def test_an_empty_body_on_200_is_not_an_error(self):
        """A successful APNs response has NO body at all — so "cannot parse" is
        the normal case and must not become a reason."""
        self.curl.body = b""
        r = push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT")
        self.assertTrue(r.ok)
        self.assertIsNone(r.reason)

    def test_the_provider_token_never_reaches_argv(self):
        """`ps` is world-readable. A 40-minute credential in a command line is
        published to every process on the machine."""
        push.post("ab" * 32, {"aps": {}}, self.CREDS, "SECRET-JWT-VALUE")
        joined = " ".join(self.curl.calls[0])
        self.assertNotIn("SECRET-JWT-VALUE", joined)
        self.assertIn("SECRET-JWT-VALUE", self.cfg())

    def test_the_config_file_is_0600_before_the_token_is_in_it(self):
        push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT")
        self.assertEqual(self.curl.config_mode, 0o600)

    def test_the_required_headers_are_all_present(self):
        push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT",
                  push_type="alert", priority=10)
        c = self.cfg()
        self.assertIn('header = "apns-topic: sh.orchestra.app"', c)
        self.assertIn('header = "apns-push-type: alert"', c)
        self.assertIn('header = "apns-priority: 10"', c)
        self.assertIn("http2", c.split("\n"))
        self.assertIn("api.push.apple.com/3/device/" + "ab" * 32, c)

    def test_sandbox_goes_to_the_other_host(self):
        push.post("ab" * 32, {"aps": {}}, self.CREDS, "JWT",
                  environment="sandbox")
        self.assertIn("api.sandbox.push.apple.com", self.cfg())

    def test_expiration_is_absolute_and_zero_is_kept(self):
        """`apns-expiration: 900` means "expired in 1970" — one attempt, no
        store-and-forward, silently, in exactly the offline case the header
        exists for. And 0 is a legitimate value meaning "now or never", so it
        must survive a falsy default."""
        push.post("ab" * 32, {}, self.CREDS, "J", expiration=1784636700.7)
        self.assertIn('header = "apns-expiration: 1784636700"', self.cfg())
        push.post("ab" * 32, {}, self.CREDS, "J", expiration=0)
        self.assertIn('header = "apns-expiration: 0"', self.cfg())
        push.post("ab" * 32, {}, self.CREDS, "J")
        self.assertNotIn("apns-expiration", self.cfg())

    def test_collapse_id_is_omitted_when_absent_and_capped_at_64_bytes(self):
        push.post("ab" * 32, {}, self.CREDS, "J")
        self.assertNotIn("apns-collapse-id", self.cfg())
        push.post("ab" * 32, {}, self.CREDS, "J", collapse_id="x" * 200)
        line = [l for l in self.cfg().splitlines() if "collapse-id" in l][0]
        value = line.split(": ", 1)[1].rstrip('"')
        self.assertEqual(len(value.encode()), 64)

    def test_the_payload_is_sent_as_compact_json(self):
        push.post("ab" * 32, {"aps": {"alert": "hi"}, "ev": "x"},
                  self.CREDS, "J")
        self.assertEqual(json.loads(self.curl.body_sent),
                         {"aps": {"alert": "hi"}, "ev": "x"})
        self.assertNotIn(b", ", self.curl.body_sent)

    def test_curl_failing_is_a_status_zero_not_an_exception(self):
        shell.run = lambda *a, **k: (7, "")
        r = push.post("ab" * 32, {}, self.CREDS, "J")
        self.assertEqual(r.status, 0)
        self.assertTrue(r.retriable)
        self.assertIn("curl exit 7", r.error)

    def test_retry_after_is_read(self):
        self.curl.code = 429
        self.curl.headers = "HTTP/2 429\r\napns-id: A\r\nretry-after: 30\r\n"
        r = push.post("ab" * 32, {}, self.CREDS, "J")
        self.assertEqual(r.retry_after, 30.0)

    def test_no_temp_directory_is_left_behind(self):
        before = len([d for d in os.listdir(tempfile.gettempdir())
                      if d.startswith("orchestra-apns-")])
        push.post("ab" * 32, {}, self.CREDS, "J")
        shell.run = lambda *a, **k: (7, "")
        push.post("ab" * 32, {}, self.CREDS, "J")
        after = len([d for d in os.listdir(tempfile.gettempdir())
                     if d.startswith("orchestra-apns-")])
        self.assertEqual(before, after)


# ---------------------------------------------------------------- the sinks

class TestCredentials(unittest.TestCase):

    def test_an_empty_config_names_every_missing_piece(self):
        problems = push.Credentials().problems()
        joined = " ".join(problems)
        for key in ("apns_key_path", "apns_key_id", "apns_team_id",
                    "apns_topic"):
            self.assertIn(key, joined)

    def test_a_short_key_id_is_caught_locally(self):
        """A transposed character in a Key ID is indistinguishable at the wire
        from a bad signature. What can be checked here is checked here."""
        c = push.Credentials(key_path=__file__, key_id="ABCDE123",
                             team_id="TEAM123456", topic="x")
        self.assertTrue(any("apns_key_id is 8 characters" in p
                            for p in c.problems()))

    def test_a_world_readable_key_is_refused(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, "k.p8")
            with open(p, "w") as _f:
                _f.write("x")
            os.chmod(p, 0o644)
            c = push.Credentials(key_path=p, key_id="A" * 10,
                                 team_id="T" * 10, topic="x")
            self.assertTrue(any("chmod 600" in x for x in c.problems()))
            os.chmod(p, 0o600)
            self.assertEqual(push.Credentials(key_path=p, key_id="A" * 10,
                                              team_id="T" * 10,
                                              topic="x").problems(), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_an_unknown_environment(self):
        c = push.Credentials(key_path=__file__, key_id="A" * 10,
                             team_id="T" * 10, topic="x", environment="staging")
        self.assertTrue(any("apns_environment" in p for p in c.problems()))

    def test_from_config_reads_cfg(self):
        c = push.Credentials.from_config(
            {"apns_key_path": "/a", "apns_key_id": "b", "apns_team_id": "c",
             "apns_topic": "d", "apns_environment": "sandbox"})
        self.assertEqual((c.key_path, c.key_id, c.team_id, c.topic,
                          c.environment), ("/a", "b", "c", "d", "sandbox"))


class TestNoopSink(unittest.TestCase):
    """The state the user is in until they create a key — and it must be
    indistinguishable from working to everything upstream."""

    def test_it_records_what_would_have_gone_out(self):
        s = push.NoopSink("no key yet")
        r = s.send("ab" * 32, {"aps": {}})
        self.assertFalse(r.ok)
        self.assertEqual(r.error, "no key yet")
        self.assertEqual(len(s.sent), 1)

    def test_it_never_claims_to_be_ready(self):
        self.assertFalse(push.NoopSink().health()["ready"])

    def test_sink_falls_back_when_nothing_is_configured(self):
        s = push.sink(push.Credentials())
        self.assertIsInstance(s, push.NoopSink)
        self.assertIn("apns_key_path", s.why)


class FakePost:
    """Scripted APNs answers, in order."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, token, payload, creds, jwt, environment=None, **kw):
        self.calls.append({"token": token, "jwt": jwt,
                           "environment": environment or creds.environment,
                           "kw": kw})
        return self.responses.pop(0) if self.responses else push.Response(200)


class TestAPNsSinkRetries(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        keyp = os.path.join(self.tmp, "AuthKey.p8")
        with open(keyp, "w") as _f:
            _f.write("x")
        os.chmod(keyp, 0o600)
        self.CREDS = push.Credentials(key_path=keyp, key_id="A" * 10,
                                      team_id="T" * 10, topic="sh.orchestra.app",
                                      environment="production")
        self.real_post = push.post
        self.real_bin = push.binaries_missing
        push.binaries_missing = lambda: []
        self.tokens = push.ProviderToken(ttl_s=10_000)
        self.minted = [0]
        # every get() hands out a DISTINCT token — force() must produce a fresh
        # one, so the counter only ever climbs.
        self.tokens.get = lambda *a, **k: (
            self.minted.__setitem__(0, self.minted[0] + 1)
            or f"jwt-{self.minted[0]}")
        self.tokens.force = lambda: None

    def tearDown(self):
        push.post = self.real_post
        push.binaries_missing = self.real_bin
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sink(self):
        return push.APNsSink(self.CREDS, tokens=self.tokens)

    def test_a_403_expired_token_mints_once_and_retries_once(self):
        push.post = FakePost(
            push.Response(status=403, reason="ExpiredProviderToken"),
            push.Response(status=200, apns_id="A"))
        r = self.sink().send("ab" * 32, {"aps": {}})
        self.assertTrue(r.ok)
        self.assertEqual(len(push.post.calls), 2)
        self.assertNotEqual(push.post.calls[0]["jwt"], push.post.calls[1]["jwt"])

    def test_it_does_not_loop_on_a_second_403(self):
        push.post = FakePost(
            push.Response(status=403, reason="InvalidProviderToken"),
            push.Response(status=403, reason="InvalidProviderToken"))
        r = self.sink().send("ab" * 32, {"aps": {}})
        self.assertEqual(r.status, 403)
        self.assertEqual(len(push.post.calls), 2)

    def test_a_400_baddevicetoken_tries_the_other_host_once(self):
        push.post = FakePost(
            push.Response(status=400, reason="BadDeviceToken"),
            push.Response(status=200, apns_id="A"))
        s = self.sink()
        r = s.send("ab" * 32, {"aps": {}})
        self.assertTrue(r.ok)
        self.assertEqual(push.post.calls[0]["environment"], "production")
        self.assertEqual(push.post.calls[1]["environment"], "sandbox")
        self.assertEqual(s.healed_environment, "sandbox",
                         "the correction must be reported or every future "
                         "push pays the same double round trip")

    def test_a_400_on_both_hosts_reports_the_first(self):
        push.post = FakePost(
            push.Response(status=400, reason="BadDeviceToken", environment="production"),
            push.Response(status=400, reason="BadDeviceToken", environment="sandbox"))
        s = self.sink()
        r = s.send("ab" * 32, {"aps": {}})
        self.assertEqual(r.environment, "production")
        self.assertIsNone(s.healed_environment)

    def test_a_410_is_not_retried_at_all(self):
        push.post = FakePost(push.Response(status=410, reason="Unregistered"))
        r = self.sink().send("ab" * 32, {"aps": {}})
        self.assertTrue(r.gone)
        self.assertEqual(len(push.post.calls), 1)

    def test_a_malformed_device_token_never_reaches_the_wire(self):
        push.post = FakePost(push.Response(200))
        r = self.sink().send("not-hex", {"aps": {}})
        self.assertEqual(r.status, 400)
        self.assertEqual(len(push.post.calls), 0)

    def test_unconfigured_credentials_short_circuit(self):
        push.post = FakePost(push.Response(200))
        s = push.APNsSink(push.Credentials(), tokens=self.tokens)
        r = s.send("ab" * 32, {"aps": {}})
        self.assertEqual(r.status, 0)
        self.assertEqual(len(push.post.calls), 0)

    def test_a_missing_binary_is_named(self):
        push.binaries_missing = lambda: ["curl"]
        push.post = FakePost(push.Response(200))
        r = self.sink().send("ab" * 32, {"aps": {}})
        self.assertEqual(r.status, 0)
        self.assertIn("curl", r.error)
        self.assertEqual(len(push.post.calls), 0)


class TestBackoff(unittest.TestCase):

    def test_it_doubles_and_caps(self):
        b = push.Backoff(base_s=2.0, cap_s=10.0)
        for expect in (2.0, 4.0, 8.0, 10.0, 10.0):
            b.note(push.Response(status=503), now=100.0)
            self.assertEqual(b.until, 100.0 + expect)

    def test_apples_retry_after_wins_over_our_guess(self):
        b = push.Backoff()
        b.note(push.Response(status=429, retry_after=45.0), now=100.0)
        self.assertEqual(b.until, 145.0)

    def test_a_non_retriable_answer_clears_the_hold(self):
        b = push.Backoff()
        b.note(push.Response(status=503), now=100.0)
        self.assertTrue(b.blocked(now=101.0))
        b.note(push.Response(status=400, reason="BadTopic"), now=101.0)
        self.assertFalse(b.blocked(now=101.0))

    def test_a_delivery_clears_it(self):
        b = push.Backoff()
        b.note(push.Response(status=0, error="down"), now=100.0)
        self.assertTrue(b.blocked(now=100.5))
        b.ok()
        self.assertFalse(b.blocked(now=100.5))
        self.assertEqual(b.consecutive, 0)

    def test_it_is_shared_across_devices_not_per_device(self):
        """A 429 is a statement about the SERVICE. Backing off per device keeps
        hammering with the others and earns a longer ban."""
        b = push.Backoff()
        b.note(push.Response(status=429), now=0.0)
        self.assertTrue(b.blocked(now=1.0))


class TestFacade(unittest.TestCase):

    def test_the_package_exports_the_transport(self):
        self.assertIs(fb.push.der_to_raw, push.der_to_raw)
        self.assertIs(fb.der_to_raw, push.der_to_raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
