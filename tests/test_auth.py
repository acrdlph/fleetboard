#!/usr/bin/env python3
"""Authentication — the token, the check, and the proof that no route escapes it.

Three harnesses, because the claims are of three different kinds:

* **the decision** — `auth.check` called directly with an explicit peer,
  header, method and path, and an injected clock. Every refusal in the design
  is one row of one table here.
* **the wire** — a real `Server` on a real port, whose handler believes its
  peer is a tailnet address (`setup()` rewrites `client_address`, which is the
  only honest way to test a non-loopback client without one). This is what
  proves the check is in the REQUEST PATH and not merely in a function.
* **the routes** — read back out of `server.py`'s own source with `ast`. A test
  that hard-codes the route list is a test that goes stale the first time
  somebody adds a route, which is precisely the day it needed to fire. This
  one enumerates every string literal beginning with `/` inside every `do_*`
  method, and requires each to be exempt-by-list or to refuse a stranger.

    python3 -m unittest discover -s tests
"""

import ast
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402

TAILNET = "100.64.0.9"          # a plausible peer that is not this machine


class AuthCase(unittest.TestCase):
    """Every case gets its own registry and audit log, and an empty budget.

    Both files are module globals rebound at runtime — the `resume.RESUME_STATE`
    pattern — so pointing them at a temp dir is the whole of the isolation, and
    `_forget_registry()` is what stops the stat-keyed memo carrying one case's
    devices into the next.
    """

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fb-auth-"))
        self._saved = (fb.auth.REGISTRY, fb.auth.AUDIT_LOG)
        fb.auth.REGISTRY = self.dir / "devices.json"
        fb.auth.AUDIT_LOG = self.dir / "audit.log.jsonl"
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        self._cfg = dict(fb.CFG)

    def tearDown(self):
        fb.auth.REGISTRY, fb.auth.AUDIT_LOG = self._saved
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        fb.CFG.clear()
        fb.CFG.update(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def mint(self, label="iPhone"):
        return fb.auth.add_device(label)

    def check(self, peer, header, method="GET", path="/api/state", now=1000.0,
              **kw):
        """`auth.check` with a fixed clock and a well-formed browser request.

        A mutation defaults to `Content-Type: application/json` because that is
        what every real client sends and what the CSRF guard requires; the
        cases that are ABOUT the guard pass their own (see TestCrossSite).
        """
        if method != "GET":
            kw.setdefault("content_type", "application/json")
        return fb.auth.check(peer, header, method, path, now=now, **kw)


# ------------------------------------------------------------------- the token

class TestTheToken(AuthCase):
    def test_shape_is_orc1_devid_secret(self):
        device, token = self.mint()
        version, devid, secret = token.split("_", 2)
        self.assertEqual(version, "orc1")
        self.assertEqual(devid, device["id"])
        self.assertEqual(len(devid), 8)
        # secrets.token_urlsafe(32) — 256 bits, 43 characters
        self.assertGreaterEqual(len(secret), 43)

    def test_the_registry_is_not_itself_a_credential(self):
        """The whole point of hashing: a copy of the file grants nothing."""
        _, token = self.mint()
        raw = fb.auth.REGISTRY.read_text()
        self.assertNotIn(token, raw)
        self.assertNotIn(token.split("_", 2)[2], raw)
        self.assertIn("token_sha256", raw)

    def test_registry_is_private_to_this_user(self):
        self.mint()
        self.assertEqual(os.stat(fb.auth.REGISTRY).st_mode & 0o777, 0o600)

    def test_public_never_carries_the_hash(self):
        self.mint()
        self.assertTrue(all("token_sha256" not in d for d in fb.auth.devices()))

    def test_two_devices_get_different_ids_and_secrets(self):
        a_dev, a = self.mint("phone")
        b_dev, b = self.mint("ipad")
        self.assertNotEqual(a_dev["id"], b_dev["id"])
        self.assertNotEqual(a, b)
        self.assertEqual(len(fb.auth.devices()), 2)

    def test_minting_never_drops_an_existing_device(self):
        first, _ = self.mint("phone")
        self.mint("ipad")
        self.assertIn(first["id"], [d["id"] for d in fb.auth.devices()])

    def test_add_device_refuses_to_overwrite_an_unreadable_registry(self):
        fb.auth.REGISTRY.write_text("{ this is not json")
        fb.auth._forget_registry()
        with self.assertRaises(ValueError):
            self.mint()

    def test_revoke_marks_and_survives_a_reload(self):
        device, _ = self.mint()
        self.assertIsNotNone(fb.auth.revoke_device(device["id"]))
        fb.auth._forget_registry()
        self.assertTrue(fb.auth.devices()[0]["revoked"])

    def test_revoking_an_unknown_device_is_not_an_error(self):
        self.assertIsNone(fb.auth.revoke_device("deadbeef"))


# ----------------------------------------------------------------- the decision

class TestTheRule(AuthCase):
    """LOOPBACK IS TRUSTED; EVERYTHING ELSE MUST PRESENT A VALID TOKEN;
    A CREDENTIAL THAT IS PRESENTED IS ALWAYS CHECKED."""

    # --- clause one: the browser keeps working

    def test_loopback_with_no_token_is_allowed(self):
        v = self.check("127.0.0.1", None)
        self.assertTrue(v.ok)
        self.assertIsNone(v.device)         # allowed, and nobody in particular

    def test_every_loopback_address_is_loopback(self):
        for peer in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(self.check(peer, None).ok, peer)

    def test_a_peer_that_is_not_an_address_is_not_loopback(self):
        # Fail closed: anything unparseable is off-machine, not on it.
        self.assertFalse(self.check("localhost", None).ok)
        self.assertFalse(self.check("", None).ok)

    def test_loopback_trust_can_be_switched_off(self):
        fb.CFG["auth_trust_loopback"] = False
        self.assertFalse(self.check("127.0.0.1", None).ok)
        _, token = self.mint()
        self.assertTrue(self.check("127.0.0.1", f"Bearer {token}").ok)

    # --- clause two: the tailnet must authenticate

    def test_no_header_from_the_tailnet_is_401(self):
        v = self.check(TAILNET, None)
        self.assertFalse(v.ok)
        self.assertEqual((v.status, v.code), (401, fb.auth.NO_TOKEN))

    def test_a_valid_token_from_the_tailnet_is_allowed(self):
        device, token = self.mint("iPhone")
        v = self.check(TAILNET, f"Bearer {token}")
        self.assertTrue(v.ok)
        self.assertEqual(v.device["id"], device["id"])
        self.assertEqual(v.device["label"], "iPhone")
        self.assertNotIn("token_sha256", v.device)

    def test_a_secret_containing_an_underscore_still_authenticates(self):
        """`secrets.token_urlsafe` emits base64url, whose alphabet INCLUDES
        `_`, so roughly half of all real tokens contain one and a three-way
        `split("_")` refuses exactly those. Pinned with a hand-built token
        rather than a minted one: against a random secret this test would be a
        coin flip, and a test that passes half the time is METHOD.md §4's
        "green on the first mutation" waiting to happen.

        Building the record by hand also pins the storage contract from the
        outside — sha256 of the WHOLE token, hex, under `token_sha256`."""
        token = "orc1_abcdef01_aa_bb-cc_dd"
        fb.auth.REGISTRY.write_text(json.dumps({"version": 1, "devices": [
            {"id": "abcdef01", "label": "iPhone", "created": 1.0,
             "last_seen": None, "revoked": None,
             "token_sha256": hashlib.sha256(token.encode()).hexdigest()}]}))
        fb.auth._forget_registry()
        self.assertTrue(self.check(TAILNET, f"Bearer {token}").ok)

    def test_a_minted_token_authenticates_every_time(self):
        for _ in range(25):             # the alphabet is random; the rule is not
            fb.auth._forget_registry()
            fb.auth.REGISTRY.unlink(missing_ok=True)
            _, token = self.mint()
            self.assertTrue(self.check(TAILNET, f"Bearer {token}").ok, token)

    def test_bearer_is_case_insensitive_but_the_scheme_is_not_optional(self):
        _, token = self.mint()
        self.assertTrue(self.check(TAILNET, f"bearer {token}").ok)
        self.assertFalse(self.check(TAILNET, token).ok)
        self.assertFalse(self.check(TAILNET, f"Token {token}").ok)
        self.assertFalse(self.check(TAILNET, f"Basic {token}").ok)

    def test_malformed_headers_are_401_and_never_500(self):
        _, token = self.mint()
        for header in ("Bearer", "Bearer ", "Bearer  ", "Bearer a b",
                       "Bearer orc1", "Bearer orc1_x", "Bearer orc1__",
                       "Bearer orc2_" + token.split("_", 1)[1],
                       "Bearer orc1_TOOSHORT_" + token.split("_", 2)[2],
                       "Bearer " + token + "_extra", "Bearer café", "\x00"):
            v = self.check(TAILNET, header)
            self.assertFalse(v.ok, header)
            self.assertEqual(v.status, 401, header)
            fb.auth._reset_buckets()        # each of these spends a failure

    def test_non_ascii_is_a_refusal_not_a_crash(self):
        # http.client decodes headers as latin-1, so a UTF-8 token arrives as
        # mojibake and would be hashed as the wrong bytes rather than raise.
        v = self.check(TAILNET, "Bearer orc1_abcdef01_ünïcødé")
        self.assertEqual((v.status, v.code), (401, fb.auth.MALFORMED))

    def test_an_unknown_device_is_refused(self):
        self.mint()
        v = self.check(TAILNET, "Bearer orc1_00000000_" + "x" * 43)
        self.assertEqual((v.status, v.code), (401, fb.auth.UNKNOWN))

    def test_a_real_secret_under_another_devices_id_is_refused(self):
        """The hash is of the WHOLE token, so a secret cannot be transplanted."""
        a, token_a = self.mint("phone")
        b, _ = self.mint("ipad")
        forged = f"orc1_{b['id']}_{token_a.split('_', 2)[2]}"
        v = self.check(TAILNET, f"Bearer {forged}")
        self.assertEqual((v.status, v.code), (401, fb.auth.UNKNOWN))

    def test_one_wrong_character_is_refused(self):
        _, token = self.mint()
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        self.assertEqual(self.check(TAILNET, f"Bearer {bad}").code,
                         fb.auth.UNKNOWN)

    # --- clause three: a presented credential is always checked

    def test_a_revoked_device_is_refused_and_told_so(self):
        device, token = self.mint()
        fb.auth.revoke_device(device["id"])
        v = self.check(TAILNET, f"Bearer {token}")
        self.assertEqual((v.status, v.code), (401, fb.auth.REVOKED))
        self.assertIn("pair again", v.message)

    def test_a_revoked_device_is_refused_from_loopback_too(self):
        """The interesting half of the loopback rule. Trust is for a request
        that presents NOTHING; a revoked token presented from 127.0.0.1 — a
        stale tab, or one day a proxy that makes every peer look local — must
        not be laundered into anonymous trust."""
        device, token = self.mint()
        fb.auth.revoke_device(device["id"])
        v = self.check("127.0.0.1", f"Bearer {token}")
        self.assertFalse(v.ok)
        self.assertEqual(v.code, fb.auth.REVOKED)

    def test_a_garbage_token_from_loopback_is_refused(self):
        self.assertFalse(self.check("127.0.0.1", "Bearer nonsense").ok)

    def test_an_empty_authorization_header_is_refused_everywhere(self):
        # `Authorization:` with no value is a credential that failed to arrive,
        # not the absence of one.
        self.assertFalse(self.check("127.0.0.1", "").ok)
        self.assertFalse(self.check(TAILNET, "").ok)

    # --- the registry itself

    def test_an_unreadable_registry_refuses_the_tailnet(self):
        fb.auth.REGISTRY.write_text("{ not json at all")
        fb.auth._forget_registry()
        v = self.check(TAILNET, "Bearer orc1_abcdef01_" + "x" * 43)
        self.assertEqual((v.status, v.code), (503, fb.auth.UNAVAILABLE))

    def test_an_unreadable_registry_does_not_lock_out_the_board(self):
        """Loopback trust is decided before the registry is opened, so a
        corrupt file cannot take the local browser down with it."""
        fb.auth.REGISTRY.write_text("{ not json at all")
        fb.auth._forget_registry()
        self.assertTrue(self.check("127.0.0.1", None).ok)

    def test_no_registry_at_all_refuses_the_tailnet(self):
        self.assertFalse(fb.auth.REGISTRY.exists())
        self.assertFalse(self.check(TAILNET, "Bearer orc1_abcdef01_x").ok)

    def test_a_revoke_in_another_process_is_seen_within_one_stat(self):
        """--revoke-device runs elsewhere; the memo must not outlive it."""
        device, token = self.mint()
        self.assertTrue(self.check(TAILNET, f"Bearer {token}").ok)
        raw = json.loads(fb.auth.REGISTRY.read_text())
        raw["devices"][0]["revoked"] = 999.0
        fb.auth.REGISTRY.write_text(json.dumps(raw))
        os.utime(fb.auth.REGISTRY, ns=(10**18, 10**18))   # a stat that differs
        self.assertEqual(self.check(TAILNET, f"Bearer {token}").code,
                         fb.auth.REVOKED)

    def test_last_seen_is_recorded_and_then_throttled(self):
        device, token = self.mint()
        fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state", now=1000.0)
        first = fb.auth.devices()[0]["last_seen"]
        self.assertEqual(first, 1000.0)
        # Inside the window: no write. This is METHOD.md §6 — a field updated
        # per request is a disk write per SSE keepalive.
        fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state", now=1030.0)
        self.assertEqual(fb.auth.devices()[0]["last_seen"], 1000.0)
        fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state", now=1100.0)
        self.assertEqual(fb.auth.devices()[0]["last_seen"], 1100.0)

    # --- exempt routes

    def test_exactly_two_routes_are_exempt_and_these_are_they(self):
        """The list is pinned whole, so growing it is a deliberate edit here.

        `GET /api/health` is the route you need BEFORE you have a token.
        `POST /api/v1/pair` is the route that GIVES you one. There is no third,
        and the two candidates that keep looking reasonable — the static pages
        and `/api/state` — are refused in `TestTheRule` below.
        """
        self.assertEqual(fb.auth.EXEMPT, frozenset({
            ("GET", "/api/health"), ("POST", "/api/v1/pair")}))

    def test_health_answers_a_stranger(self):
        self.assertTrue(self.check(TAILNET, None, "GET", "/api/health").ok)
        self.assertTrue(self.check(TAILNET, None, "GET", "/api/health?t=1").ok)

    def test_exemption_is_exact_and_does_not_spread_by_prefix(self):
        """`server.Handler` routes by startswith; the exempt list does not."""
        for path in ("/api/healthcheck", "/api/health/../state", "/api/heal"):
            self.assertFalse(self.check(TAILNET, None, "GET", path).ok, path)

    def test_exemption_is_per_method(self):
        self.assertFalse(self.check(TAILNET, None, "POST", "/api/health").ok)

    def test_nothing_that_reads_transcripts_is_exempt(self):
        for method, path in fb.auth.EXEMPT:
            self.assertNotIn(path, ("/api/state", "/api/chat", "/api/events"))

    def test_the_one_exempt_mutation_is_pairing_and_it_is_audited(self):
        """An unauthenticated POST is allowed to exist exactly once.

        `/api/v1/pair` hands out a credential, which is the most consequential
        thing on this server — so the rule is not "exempt routes do not act"
        (that stopped being true) but "the one that does is written down every
        time it is asked". `audited` is what guarantees the log line.
        """
        mutations = [(m, p) for m, p in fb.auth.EXEMPT if m != "GET"]
        self.assertEqual(mutations, [("POST", "/api/v1/pair")])
        for method, path in fb.auth.EXEMPT:
            if method == "GET":
                self.assertFalse(fb.auth.audited(method, path), path)
            else:
                self.assertTrue(fb.auth.audited(method, path), path)

    def test_an_exempt_mutation_still_faces_the_cross_site_guards(self):
        """Exempt means NO TOKEN REQUIRED, not NO CHECKS.

        This is the hole that opened for ten minutes when pairing was added:
        the exempt branch returned before `browser_guards` ran, so a page you
        were merely visiting could have posted a `text/plain` pairing claim
        from your browser with no preflight to stop it.
        """
        self.assertFalse(self.check(TAILNET, None, "POST", "/api/v1/pair",
                                    content_type="text/plain").ok)
        self.assertEqual(self.check(TAILNET, None, "POST", "/api/v1/pair",
                                    content_type="text/plain").code,
                         fb.auth.NOT_JSON)
        self.assertFalse(self.check("127.0.0.1", None, "POST", "/api/v1/pair",
                                    origin="http://evil.example",
                                    host="127.0.0.1:4242").ok)
        # …and with the header a real client sends, it is allowed through.
        self.assertTrue(self.check(TAILNET, None, "POST", "/api/v1/pair").ok)


# ------------------------------------------- a page you are visiting is not you

class TestCrossSite(AuthCase):
    """The third clause: a website you have open speaks from 127.0.0.1 too.

    Loopback trust is a statement about processes on this machine. A browser
    turns it into a statement about every page you visit, and `POST /api/send`
    types into an agent that acts on what it is told. These are the two lines
    that keep the first clause honest.
    """

    ORIGIN = "http://127.0.0.1:4242"
    HOST = "127.0.0.1:4242"

    def post(self, **kw):
        kw.setdefault("content_type", "application/json")
        kw.setdefault("host", self.HOST)
        return fb.auth.check("127.0.0.1", None, "POST", "/api/send",
                             now=1000.0, **kw)

    def test_the_boards_own_post_is_allowed(self):
        self.assertTrue(self.post(origin=self.ORIGIN).ok)

    def test_a_post_with_no_origin_is_allowed(self):
        # `curl`, the phone, and same-origin navigations send none.
        self.assertTrue(self.post(origin=None).ok)

    def test_another_sites_post_is_refused(self):
        v = self.post(origin="https://evil.example")
        self.assertEqual((v.status, v.code), (403, fb.auth.CROSS_ORIGIN))

    def test_an_opaque_origin_is_refused(self):
        self.assertEqual(self.post(origin="null").code, fb.auth.CROSS_ORIGIN)

    def test_same_origin_fails_closed_on_every_degenerate_pair(self):
        self.assertTrue(fb.auth.same_origin(None, self.HOST))
        self.assertTrue(fb.auth.same_origin("", self.HOST))
        self.assertTrue(fb.auth.same_origin("http://a:1", "a:1"))
        for origin, host in (("null", "a:1"), ("a:1", "a:1"), ("http://", ""),
                             ("http://a:1", None), ("http://a:1", ""),
                             ("http://a:1", "a:2"), ("http://a:1", "b:1"),
                             ("http://evil/http://a:1", "a:1")):
            self.assertFalse(fb.auth.same_origin(origin, host),
                             f"{origin!r} must not pass as {host!r}")

    def test_a_mutation_must_announce_json(self):
        """The CSRF guard proper: `text/plain` is a SIMPLE request, so it needs
        no preflight and a page can send it without your knowledge. `do_POST`
        never looked at the media type — it just parsed the body."""
        for ctype in ("text/plain", "application/x-www-form-urlencoded",
                      "multipart/form-data", "", None):
            v = self.post(content_type=ctype, origin=None)
            self.assertEqual((v.status, v.code), (415, fb.auth.NOT_JSON), ctype)

    def test_a_charset_parameter_is_still_json(self):
        self.assertTrue(self.post(content_type="application/json; charset=utf-8").ok)
        self.assertTrue(self.post(content_type="APPLICATION/JSON").ok)

    def test_reads_need_no_content_type(self):
        self.assertTrue(fb.auth.check("127.0.0.1", None, "GET", "/api/state",
                                      now=1000.0, host=self.HOST).ok)

    def test_a_token_is_no_excuse_for_a_cross_site_post(self):
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "POST", "/api/send",
                          now=1000.0, host="100.64.0.1:4242",
                          origin="https://evil.example",
                          content_type="application/json")
        self.assertEqual(v.code, fb.auth.CROSS_ORIGIN)

    def test_these_refusals_do_not_spend_the_failure_budget(self):
        """Nobody is guessing a credential, and a page hammering the board must
        not be able to lock the real phone out."""
        for _ in range(fb.auth.FAIL_BURST * 3):
            self.post(origin="https://evil.example")
            self.post(content_type="text/plain", origin=None)
        _, token = self.mint()
        self.assertTrue(fb.auth.check("127.0.0.1", f"Bearer {token}", "POST",
                                      "/api/send", now=1000.0, host=self.HOST,
                                      content_type="application/json").ok)

    def test_a_cross_site_attempt_leaves_evidence(self):
        self.post(origin="https://evil.example")
        self.assertEqual(fb.auth.read_audit()[0]["outcome"], "refuse")


# ------------------------------------------------------------------ the budget

class TestFailureBudget(AuthCase):
    def refuse(self, n, at=1000.0, peer=TAILNET):
        out = []
        for i in range(n):
            out.append(fb.auth.check(peer, "Bearer wrong", "GET", "/api/state",
                                     now=at))
        return out

    def test_a_burst_of_failures_stops_being_answered(self):
        verdicts = self.refuse(fb.auth.FAIL_BURST + 2)
        self.assertTrue(all(v.status == 401
                            for v in verdicts[:fb.auth.FAIL_BURST]))
        self.assertEqual(verdicts[-1].status, 429)
        self.assertEqual(verdicts[-1].code, fb.auth.RATE_LIMITED)
        self.assertGreaterEqual(verdicts[-1].retry_after, 1)

    def test_the_budget_refills(self):
        self.refuse(fb.auth.FAIL_BURST)
        self.assertEqual(fb.auth.check(TAILNET, "Bearer wrong", "GET", "/api/s",
                                       now=1000.0).status, 429)
        later = 1000.0 + fb.auth.FAIL_WINDOW_S
        self.assertEqual(fb.auth.check(TAILNET, "Bearer wrong", "GET", "/api/s",
                                       now=later).status, 401)

    def test_an_exhausted_peer_does_not_lock_out_another(self):
        self.refuse(fb.auth.FAIL_BURST + 1)
        _, token = self.mint()
        self.assertTrue(fb.auth.check("100.64.0.10", f"Bearer {token}",
                                      "GET", "/api/state", now=1000.0).ok)

    def test_a_success_clears_the_peers_failures(self):
        _, token = self.mint()
        self.refuse(fb.auth.FAIL_BURST - 1)
        self.assertTrue(fb.auth.check(TAILNET, f"Bearer {token}", "GET",
                                      "/api/state", now=1000.0).ok)
        # …so the next fumble is answered rather than throttled
        self.assertEqual(self.refuse(1)[0].status, 401)

    def test_the_board_can_never_be_throttled(self):
        """A loopback request with no header consults no budget, so no flood
        from anywhere can lock the local browser out of its own server."""
        self.refuse(fb.auth.FAIL_BURST * 3, peer="127.0.0.1")
        for _ in range(50):
            self.assertTrue(fb.auth.check("127.0.0.1", None, "GET",
                                          "/api/state", now=1000.0).ok)

    def test_an_exhausted_peer_still_gets_health(self):
        self.refuse(fb.auth.FAIL_BURST + 1)
        self.assertTrue(fb.auth.check(TAILNET, None, "GET", "/api/health",
                                      now=1000.0).ok)

    def test_the_bucket_table_does_not_grow_without_bound(self):
        for i in range(600):
            fb.auth.check(f"100.64.1.{i % 250}", "Bearer wrong", "GET", "/x",
                          now=1000.0 + i * 30)     # 30 s apart: each refills
        self.assertLess(len(fb.auth._buckets), 60)


# ------------------------------------------------------------------- the audit

class TestAudit(AuthCase):
    def test_an_authenticated_mutation_is_recorded(self):
        device, token = self.mint("iPhone")
        self.check(TAILNET, f"Bearer {token}", "POST", "/api/send",
                      now=1234.0)
        line, = fb.auth.read_audit()
        self.assertEqual(line["device"], device["id"])
        self.assertEqual(line["label"], "iPhone")
        self.assertEqual(line["method"], "POST")
        self.assertEqual(line["path"], "/api/send")
        self.assertEqual(line["at"], 1234.0)
        self.assertEqual(line["outcome"], "allow")
        self.assertEqual(line["peer"], TAILNET)

    def test_a_loopback_mutation_is_recorded_as_loopback(self):
        self.check("127.0.0.1", None, "POST", "/api/dispatch", now=1.0)
        line, = fb.auth.read_audit()
        self.assertEqual((line["device"], line["label"]), (None, "loopback"))

    def test_reads_are_not_recorded(self):
        _, token = self.mint()
        for path in ("/api/state", "/api/events", "/api/chat?sid=x", "/"):
            self.check(TAILNET, f"Bearer {token}", "GET", path, now=1.0)
        self.assertEqual(fb.auth.read_audit(), [])

    def test_the_get_that_acts_is_recorded(self):
        """/api/focus raises a window and steals the keyboard — a side effect
        wearing a GET."""
        _, token = self.mint()
        self.check(TAILNET, f"Bearer {token}", "GET", "/api/focus?pid=1",
                      now=1.0)
        self.assertEqual(fb.auth.read_audit()[0]["path"], "/api/focus?pid=1")

    def test_a_refusal_leaves_evidence(self):
        self.check(TAILNET, "Bearer orc1_abcdef01_nope", "GET",
                      "/api/state", now=7.0)
        line, = fb.auth.read_audit()
        self.assertEqual(line["outcome"], "refuse")
        self.assertEqual(line["code"], fb.auth.UNKNOWN)
        self.assertEqual(line["device"], "abcdef01")   # the id it claimed
        self.assertEqual(line["peer"], TAILNET)

    def test_the_log_never_carries_what_was_said(self):
        """WHO/WHAT/WHEN, never the mission brief or the text typed at an
        agent — the audit file must not become a second copy of the thing the
        tokens exist to protect."""
        _, token = self.mint()
        self.check(TAILNET, f"Bearer {token}", "POST", "/api/send", now=1.0)
        line, = fb.auth.read_audit()
        self.assertEqual(set(line), {"at", "peer", "device", "label", "method",
                                     "path", "outcome"})

    def test_the_log_never_carries_the_token(self):
        _, token = self.mint()
        self.check(TAILNET, f"Bearer {token}", "POST", "/api/finish", now=1.0)
        self.assertNotIn(token.split("_", 2)[2], fb.auth.AUDIT_LOG.read_text())

    def test_the_log_never_carries_a_token_that_arrived_in_the_path(self):
        """The header is not the only place a token can be.

        API.md §2.1 says a token is presented `only` as `Authorization: Bearer`
        — never a query parameter — and this server refuses one that arrives
        any other way. But refusing it is not the end of the story: the refusal
        is AUDITED, and the audit keeps the path whole, query included (ADR
        0008: the query is the identity a mutation was addressed to). So a
        client that got that wrong once wrote its own live 256-bit secret into
        a file whose own docstring says people paste it into bug reports, and
        the token stayed valid.

        The path is still logged. The credential inside it is not.
        """
        _, token = self.mint()
        self.check(TAILNET, None, "GET", f"/api/state?token={token}", now=1.0)
        blob = fb.auth.AUDIT_LOG.read_text()
        self.assertNotIn(token, blob)
        self.assertNotIn(token.split("_", 2)[2], blob)
        self.assertIn("/api/state", fb.auth.read_audit()[0]["path"])

    def test_a_token_is_scrubbed_from_every_field_not_only_the_path(self):
        """The scrub is on the LINE, not on one field, for the same reason the
        auth check is in `parse_request`: a field added next month is covered
        without anybody remembering it was."""
        _, token = self.mint()
        fb.auth.audit(at=1.0, peer=TAILNET, device=None,
                      label=f"phone {token}", method="GET",
                      path="/api/state", outcome="allow", note=token)
        self.assertNotIn(token.split("_", 2)[2],
                         fb.auth.AUDIT_LOG.read_text())

    def test_the_scrub_follows_the_token_version(self):
        """A hard-coded `orc1_` would keep every test green on the day the
        token format bumps, and log the new one in full."""
        self.assertIn(fb.auth.TOKEN_VERSION, fb.auth._TOKENISH.pattern)
        self.assertTrue(fb.auth.REDACTED.startswith(fb.auth.TOKEN_VERSION))
        self.assertEqual(fb.auth.scrub(f"{fb.auth.TOKEN_VERSION}_ab12cd34_sEcRe7"),
                         fb.auth.REDACTED)

    def test_the_scrub_keeps_the_device_id_it_exists_to_record(self):
        """A redaction that ate the audit's own identifier would be a fix that
        broke the file. The device id is not `orc1_`-prefixed and survives."""
        device, token = self.mint()
        self.check(TAILNET, f"Bearer {token}", "POST", "/api/send", now=1.0)
        self.assertEqual(fb.auth.read_audit()[0]["device"], device["id"])

    def test_a_long_path_is_truncated(self):
        self.check("127.0.0.1", None, "POST", "/api/send?x=" + "y" * 5000,
                      now=1.0)
        self.assertLessEqual(len(fb.auth.read_audit()[0]["path"]), 200)

    def test_the_log_is_private_and_appends(self):
        self.check("127.0.0.1", None, "POST", "/api/finish", now=1.0)
        self.check("127.0.0.1", None, "POST", "/api/finish", now=2.0)
        self.assertEqual(len(fb.auth.read_audit()), 2)
        self.assertEqual(os.stat(fb.auth.AUDIT_LOG).st_mode & 0o777, 0o600)

    def test_an_unwritable_log_does_not_take_the_server_down(self):
        fb.auth.AUDIT_LOG = self.dir / "no-such-dir" / "audit.jsonl"
        self.assertTrue(self.check("127.0.0.1", None, "POST", "/api/finish",
                                      now=1.0).ok)


# --------------------------------------------------------------------- the wire

class RemoteHandler(fb.Handler):
    """The board's handler, believing its peer is on the tailnet.

    `socketserver` sets `client_address` before calling `setup()`, so this is
    the whole of the lie and it changes nothing else — the request is real, the
    socket is real, and `parse_request` runs exactly as it does in production.
    """
    PEER = TAILNET

    def setup(self):
        super().setup()
        self.client_address = (self.PEER, 51000)


class LoopbackHandler(RemoteHandler):
    PEER = "127.0.0.1"


class WireCase(AuthCase):
    handler = RemoteHandler

    def setUp(self):
        super().setUp()
        fb.config.DEMO = True           # no git, no ps, no transcripts
        # /api/reserve persists to the config file, and a test that types at
        # the real one is a test that edits the machine it runs on.
        self._config_path = fb.config.CONFIG_PATH
        fb.config.CONFIG_PATH = self.dir / "orchestra.config.json"
        self.srv = fb.Server(("127.0.0.1", 0), self.handler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        fb.config.DEMO = False
        fb.config.CONFIG_PATH = self._config_path
        super().tearDown()

    def request(self, method, path, token=None, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        head = dict(headers or {})
        if token:
            head["Authorization"] = f"Bearer {token}"
        if body is not None:
            head["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=body, headers=head)
            r = conn.getresponse()
            return r.status, r.read(), dict(r.getheaders())
        finally:
            conn.close()


class TestOnTheWire(WireCase):
    def test_a_stranger_is_refused_at_the_door(self):
        status, body, headers = self.request("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertIn("Bearer", headers.get("WWW-Authenticate", ""))
        payload = json.loads(body)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], fb.auth.NO_TOKEN)

    def test_a_paired_device_is_served(self):
        _, token = self.mint()
        status, body, _ = self.request("GET", "/api/state", token=token)
        self.assertEqual(status, 200)
        self.assertIn("worktrees", json.loads(body))

    def test_health_needs_no_token(self):
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertIn("time", payload)

    def test_health_leaks_nothing_about_the_fleet(self):
        _, body, _ = self.request("GET", "/api/health")
        self.assertEqual(set(json.loads(body)),
                         {"ok", "service", "api", "time"})

    def test_a_refused_post_is_not_read_and_still_answers(self):
        """A refused mutation's body never reaches this process, and the client
        still gets its 401 back — see `Handler._refuse` for the three
        measurements behind that.

        64 KB, an order of magnitude above the largest thing the board sends (a
        mission brief, a pasted chat message) and an order below where the
        measurement says this stops being reliable: 400–800 KB still answers
        every time, 1 MB is a coin flip, 2 MB raises. A 600 KB version of this
        test was written first and flaked once in ten runs — a test that fails
        sometimes is worse than one that cannot fail, because it teaches you to
        re-run instead of to look."""
        blob = json.dumps({"text": "x" * 65536, "pid": 1})
        status, body, _ = self.request("POST", "/api/send", body=blob)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], fb.auth.NO_TOKEN)

    def test_a_refused_post_does_not_act(self):
        before = len(fb.auth.read_audit())
        self.request("POST", "/api/dispatch",
                     body=json.dumps({"mission": "rm -rf"}))
        lines = fb.auth.read_audit()[before:]
        self.assertEqual([l["outcome"] for l in lines], ["refuse"])

    def test_the_stream_is_not_a_way_round_the_door(self):
        # /api/events returns early from do_GET, so a guard written at the top
        # of the elif chain would have missed it.
        status, _, _ = self.request("GET", "/api/events")
        self.assertEqual(status, 401)

    def test_an_unimplemented_method_is_refused_before_it_is_described(self):
        status, _, _ = self.request("PUT", "/api/state")
        self.assertEqual(status, 401)      # not 501: say nothing to a stranger

    def test_brute_force_is_throttled_on_the_wire(self):
        for _ in range(fb.auth.FAIL_BURST):
            self.assertEqual(self.request("GET", "/api/state",
                                          token="orc1_abcdef01_x" * 3)[0], 401)
        status, _, headers = self.request("GET", "/api/state", token="nope")
        self.assertEqual(status, 429)
        self.assertGreaterEqual(int(headers["Retry-After"]), 1)

    def test_a_revoked_phone_stops_working_immediately(self):
        device, token = self.mint()
        self.assertEqual(self.request("GET", "/api/state", token=token)[0], 200)
        fb.auth.revoke_device(device["id"])
        status, body, _ = self.request("GET", "/api/state", token=token)
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body)["error"], fb.auth.REVOKED)


class TestTheBrowserStillWorks(WireCase):
    """The whole point of the loopback rule: the existing board, unchanged,
    with no token, over 127.0.0.1."""
    handler = LoopbackHandler

    def test_every_page_and_read_the_board_uses(self):
        for path in ("/", "/index.html", "/stream.js", "/map", "/limits",
                     "/guide", "/api/state", "/api/topology", "/api/limits",
                     "/api/dispatchlog", "/api/health"):
            self.assertEqual(self.request("GET", path)[0], 200, path)

    def test_a_click_still_acts(self):
        status, body, _ = self.request(
            "POST", "/api/reserve", body=json.dumps({"account": "main",
                                                     "percent": 10}))
        self.assertEqual(status, 200)
        self.assertIn("ok", json.loads(body))

    def test_a_page_from_another_site_cannot_act_through_the_browser(self):
        """On the wire, from 127.0.0.1, exactly as a visited page would."""
        status, body, _ = self.request(
            "POST", "/api/finish", body=json.dumps({"worktree": "x"}),
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], fb.auth.CROSS_ORIGIN)

    def test_a_simple_request_cannot_act_through_the_browser(self):
        # `text/plain` needs no preflight, so this is the shape that reaches a
        # server without the browser asking anybody's permission first.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/finish", body=json.dumps({"worktree": "x"}),
                     headers={"Content-Type": "text/plain"})
        r = conn.getresponse()
        self.assertEqual(r.status, 415)
        conn.close()

    def test_the_server_grants_no_cross_origin_access(self):
        """The preflight the JSON requirement forces must fail — which it does
        by carrying no CORS headers at all, on every response."""
        for method, path in (("OPTIONS", "/api/send"), ("GET", "/api/state"),
                             ("GET", "/api/health")):
            _, _, headers = self.request(method, path)
            self.assertFalse([h for h in headers
                              if h.lower().startswith("access-control")])

    def test_the_board_is_audited_even_though_it_is_trusted(self):
        self.request("POST", "/api/reserve",
                     body=json.dumps({"account": "main", "percent": 10}))
        line = fb.auth.read_audit()[-1]
        self.assertEqual((line["label"], line["path"], line["outcome"]),
                         ("loopback", "/api/reserve", "allow"))


# ------------------------------------------------------------------ the routes

def routes_from_handler():
    """Every route `server.Handler` answers, read out of its own source.

    Any string literal beginning with `/` inside a `do_*` method is a route:
    that is true of `startswith("/api/state")`, of the `in ("/", "/index", …)`
    tuple, and of whatever idiom somebody reaches for next — which is the
    property a hard-coded list does not have. The other literals in those
    methods are content types and error text, and none of them starts with `/`.

    SECOND SOURCE, and it is not a convenience. The device subtree is routed by
    asking the guard — `if auth.admin("GET", self.path)` — rather than by a
    literal, because the router and the guard disagreeing by one character is
    precisely how `/api/v1/devicesX` served the whole device inventory to a
    phone. That fix is right and it costs the literal, so two real routes fell
    out of this enumeration and the loss was invisible: the count merely went
    down. A path the GUARD names is a route this server answers, so `auth.ADMIN`
    is read too, under every method the handler implements. Both landmarks are
    pinned in `test_the_enumeration_actually_enumerates`, so neither source can
    quietly stop producing.
    """
    tree = ast.parse((ROOT / "orchestra" / "server.py").read_text())
    handler = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "Handler")
    found = set()
    methods = []
    for fn in handler.body:
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("do_"):
            continue
        methods.append(fn.name[3:])
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and node.value.startswith("/"):
                found.add((fn.name[3:], node.value))
    for path in fb.auth.ADMIN:
        for method in methods:
            found.add((method, path))
    return found


class TestEveryRouteIsGuarded(WireCase):
    """The claim is NOT "these routes refuse" — it is "no route can forget".

    So the routes are enumerated from the handler rather than listed here, and
    each one is asked, over a real socket, from a peer that is not this
    machine. A route added tomorrow is in this test tomorrow.
    """

    def test_the_enumeration_actually_enumerates(self):
        """A vacuous loop passes every assertion. Pin the extractor first."""
        routes = routes_from_handler()
        self.assertGreaterEqual(len(routes), 15)
        for landmark in (("GET", "/api/state"), ("GET", "/api/events"),
                         ("GET", "/api/chat"), ("POST", "/api/send"),
                         ("POST", "/api/dispatch"), ("GET", "/stream.js"),
                         # the second source: routed by asking `auth.admin`, so
                         # there is no literal in `do_GET` to find. Pinned here
                         # because when it stopped producing, the only symptom
                         # was a smaller number.
                         ("GET", "/api/v1/devices"),
                         ("POST", "/api/v1/devices")):
            self.assertIn(landmark, routes)

    def test_every_route_is_exempt_by_list_or_refuses_a_stranger(self):
        """The claim, per route: refused, or exempt and answering on its merits.

        An exempt route is not asserted to return 200 — `POST /api/v1/pair`
        legitimately answers `409 pairing_not_open` when no window is open,
        which is it working. What is asserted is that the answer is not an
        AUTHENTICATION refusal: no 401, no `WWW-Authenticate`, and no code from
        `auth`'s own refusal vocabulary. That keeps the test strong while
        letting the route say something true.
        """
        refusals = {fb.auth.NO_TOKEN, fb.auth.MALFORMED, fb.auth.UNKNOWN,
                    fb.auth.REVOKED, fb.auth.RATE_LIMITED, fb.auth.UNAVAILABLE,
                    fb.auth.CROSS_ORIGIN, fb.auth.NOT_JSON,
                    fb.auth.ADMIN_ONLY}
        for method, path in sorted(routes_from_handler()):
            fb.auth._reset_buckets()       # the budget is not what is on trial
            body = "{}" if method != "GET" else None
            status, blob, headers = self.request(method, path, body=body)
            if fb.auth.exempt(method, path):
                self.assertNotEqual(status, 401, f"{method} {path}")
                self.assertNotIn("WWW-Authenticate", headers, f"{method} {path}")
                try:
                    code = json.loads(blob).get("error")
                except ValueError:
                    code = None
                self.assertNotIn(code, refusals, f"{method} {path}")
            else:
                self.assertEqual(status, 401, f"{method} {path}")

    def test_no_v1_route_can_be_reached_by_a_suffix(self):
        """`/api/v1` resolves by EXACT match — API.md §2.3 step 5, "there is no
        prefix routing".

        This is the generic form of the `/api/v1/devicesX` hole, which existed
        because the router said `startswith("/api/v1/devices")` and the guard
        said "this segment or one below it": a valid token was refused at the
        exact path and served the whole device inventory one character to the
        right of it. Both halves had a test and neither could see the gap,
        because neither asked the other one anything.

        So the claim is made against the ROUTES THEMSELVES rather than against
        the one that broke: for every v1 route the handler answers, the same
        path with a character glued on must not reach it. A v1 route added next
        month is in this test next month, and it is asked while holding a VALID
        TOKEN — the credential the whole admin rule is about — because a
        stranger is refused by the door and would prove nothing.
        """
        victim, _ = fb.auth.add_device("the device that gets revoked")
        _, token = fb.auth.add_device("iPhone")
        v1 = [(m, p) for m, p in routes_from_handler()
              if p.startswith("/api/v1")]
        self.assertGreaterEqual(len(v1), 3)     # the loop must not be vacuous
        probes = 0
        for method, path in sorted(v1):
            # THE SUFFIX GOES IN THE FIRST SEGMENT AND THE TAIL IS KEPT, which
            # a bare `path + "X"` does not do and which is the difference
            # between a test and a green light. `/api/v1/devicesX` alone lands
            # on a 404 from the id parser and looks refused; it is
            # `/api/v1/devicesX/<id>/revoke` that parses into five parts, is not
            # under the guard's segment, and REVOKES A DEVICE for anybody
            # holding any token. Caught by mutation, not by reading.
            for tail in ("", f"/{victim['id']}/revoke", "/pair/open"):
                fb.auth._reset_buckets()
                probe = path.rstrip("/") + "X" + tail
                body = "{}" if method != "GET" else None
                status, blob, _ = self.request(method, probe, token=token,
                                               body=body)
                probes += 1
                self.assertIn(status, (401, 403, 404),
                              f"{method} {probe} answered {status}: {blob[:120]}")
                self.assertNotIn(b'"devices"', blob, f"{method} {probe}")
                self.assertNotIn(b'"token"', blob, f"{method} {probe}")
                self.assertNotIn(b'"svg"', blob, f"{method} {probe}")
        self.assertGreaterEqual(probes, 9)
        # …and nothing ACTED. A 404 that revoked a device on the way past is
        # the failure this whole test exists for.
        fb.auth._forget_registry()
        by_id = {d["id"]: d for d in fb.auth.devices()}
        self.assertIsNone(by_id[victim["id"]]["revoked"])
        self.assertFalse(fb.pairing.state()["open"])

    def test_the_exempt_list_names_routes_that_exist(self):
        routes = routes_from_handler()
        for method, path in fb.auth.EXEMPT:
            self.assertTrue(any(m == method and path.startswith(p)
                                for m, p in routes), f"{method} {path}")

    def test_every_mutating_route_is_audited(self):
        for method, path in sorted(routes_from_handler()):
            if method != "GET":
                self.assertTrue(fb.auth.audited(method, path),
                                f"{method} {path}")

    def test_a_route_added_tomorrow_is_guarded_by_construction(self):
        """The seam is `parse_request`, which runs before `do_*` is even
        looked up — so a method this handler does not have today is refused
        without anybody adding it to a list."""

        class WithANewRoute(RemoteHandler):
            def do_PUT(self):
                body = b"the crown jewels"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        srv = fb.Server(("127.0.0.1", 0), WithANewRoute)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1",
                                              srv.server_address[1], timeout=10)
            conn.request("PUT", "/api/anything")
            r = conn.getresponse()
            self.assertEqual(r.status, 401)
            self.assertNotIn(b"crown", r.read())
            conn.close()
        finally:
            srv.shutdown()
            srv.server_close()


# ---------------------------------------------------- what a request cannot show

class TestTheSourceRules(unittest.TestCase):
    """Two rules that no request can demonstrate, pinned at the source instead.

    A `==` on a hash answers identically to `hmac.compare_digest` in every test
    that can be written — the difference is a timing side channel, and timing a
    Python string compare across a network reliably enough to assert on it in a
    unit suite is not a test, it is a coin flip with a stopwatch. Likewise a
    Mersenne Twister emits perfectly plausible-looking tokens; what makes it
    unusable is that 624 of them reveal the rest, which no assertion here can
    see either.

    So these two are asserted against the AST. That is a weaker kind of test
    and it is the honest one available: it cannot prove the code is
    constant-time, only that the one comparison that must be has not quietly
    become an `==` — which is the actual regression this file exists to catch.
    """

    def setUp(self):
        self.tree = ast.parse((ROOT / "orchestra" / "auth.py").read_text())

    def test_the_secret_is_never_compared_with_an_operator(self):
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Compare):
                continue
            for operand in [node.left, *node.comparators]:
                self.assertFalse(
                    isinstance(operand, ast.Call)
                    and getattr(operand.func, "id", "") == "_hash",
                    "a token hash is being compared with an operator; "
                    "string comparison returns at the first differing byte")

    def test_the_secret_is_compared_with_compare_digest(self):
        calls = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "compare_digest"]
        self.assertTrue(calls, "nothing calls hmac.compare_digest")
        self.assertTrue(any(isinstance(a, ast.Call)
                            and getattr(a.func, "id", "") == "_hash"
                            for c in calls for a in c.args),
                        "compare_digest is called, but not on the token hash")

    def test_nothing_here_reaches_for_random(self):
        """Imports AND the call site.

        An import check alone is defeated by `__import__('random')`, which is
        not a hypothetical: the same test on `pairing.py` was written that way
        first and a mutation walked straight past it. So every call that draws
        a random value is required to be an attribute of the NAME `secrets`.
        """
        names = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertIn("secrets", names)
        self.assertNotIn("random", names)

        draws = [n for n in ast.walk(self.tree)
                 if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") in
                 ("token_hex", "token_urlsafe", "token_bytes", "choice",
                  "randint", "random", "randrange", "shuffle", "sample")]
        self.assertTrue(draws, "nothing in auth.py draws a random value")
        for call in draws:
            self.assertIsInstance(call.func.value, ast.Name)
            self.assertEqual(call.func.value.id, "secrets",
                             f"{call.func.attr} is not drawn from `secrets`")


# -------------------------------------------------------------------- the bind

class TestTheBind(AuthCase):
    def test_loopback_binds_with_no_devices(self):
        for host in ("127.0.0.1", "localhost", "::1", ""):
            self.assertIsNone(fb.auth.bind_refusal(host), host)

    def test_the_tailnet_refuses_to_bind_with_no_device(self):
        why = fb.auth.bind_refusal("100.113.110.31")
        self.assertIsNotNone(why)
        self.assertIn("--add-device", why)

    def test_the_tailnet_binds_once_a_device_exists(self):
        self.mint()
        self.assertIsNone(fb.auth.bind_refusal("100.113.110.31"))

    def test_a_revoked_device_does_not_count(self):
        device, _ = self.mint()
        fb.auth.revoke_device(device["id"])
        self.assertIsNotNone(fb.auth.bind_refusal("100.113.110.31"))

    def test_every_interface_is_refused_whatever_the_registry_says(self):
        self.mint()
        for host in ("0.0.0.0", "::", "*"):
            self.assertIsNotNone(fb.auth.bind_refusal(host), host)

    def test_an_unreadable_registry_refuses_the_bind(self):
        fb.auth.REGISTRY.write_text("{ not json")
        fb.auth._forget_registry()
        self.assertIsNotNone(fb.auth.bind_refusal("100.113.110.31"))


if __name__ == "__main__":
    unittest.main()
