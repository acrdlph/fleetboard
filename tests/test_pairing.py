#!/usr/bin/env python3
"""Pairing, the admin boundary, and the bind — the second half of step 7.

Three claims, and they are of different kinds, so there are three harnesses:

* **the exchange** — `pairing.claim` called directly with an explicit peer,
  body and clock. Every row of API.md §3.3's error table is one case here.
* **the wire** — a real `Server` on a real port whose handler believes its peer
  is a tailnet address, driven with `http.client`. This is what proves the flow
  works end to end and that `auth.ADMIN` is in the REQUEST PATH rather than
  merely in a function.
* **the bind** — `auth.bind_refusal` and `tailnet`, which decide whether the
  server comes up at all.

The rule the whole file exists to pin, stated once:

    THE QR CARRIES A TICKET, NOT A CREDENTIAL. IT IS SINGLE USE, IT EXPIRES,
    AND DEVICE MANAGEMENT ANSWERS TO THIS MACHINE AND TO NO TOKEN AT ALL.

    python3 -m unittest discover -s tests
"""

import http.client
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402

TAILNET = "100.64.0.9"          # a plausible peer that is not this machine
ELSEWHERE = "203.0.113.7"       # TEST-NET-3: routable, and nowhere near us


class PairCase(unittest.TestCase):
    """Its own registry, its own audit log, an empty budget, no open window."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fb-pair-"))
        self._saved = (fb.auth.REGISTRY, fb.auth.AUDIT_LOG)
        fb.auth.REGISTRY = self.dir / "devices.json"
        fb.auth.AUDIT_LOG = self.dir / "audit.log.jsonl"
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        fb.pairing._reset()
        self._cfg = dict(fb.CFG)

    def tearDown(self):
        fb.auth.REGISTRY, fb.auth.AUDIT_LOG = self._saved
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        fb.pairing._reset()
        fb.CFG.clear()
        fb.CFG.update(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def open(self, now=1000.0, host="100.113.110.31"):
        """Open a window and return `(payload, plain_code)`."""
        w = fb.pairing.open_window(host=host, port=4242, now=now)
        return w, w["code"].replace("-", "")

    def claim(self, code, peer=TAILNET, now=1000.0, **extra):
        body = {"code": code, "label": "Achill's iPhone", "platform": "ios"}
        body.update(extra)
        return fb.pairing.claim(peer, body, now=now)


# ------------------------------------------------------------------- the code

class TestTheCode(PairCase):

    def test_the_alphabet_excludes_every_glyph_that_is_read_wrong(self):
        """Crockford: no I, no L, no O, no U. This is the manual fallback's
        entire reason to be usable."""
        self.assertEqual(fb.pairing.ALPHABET,
                         "0123456789ABCDEFGHJKMNPQRSTVWXYZ")
        for glyph in "ILOU":
            self.assertNotIn(glyph, fb.pairing.ALPHABET)
        self.assertEqual(len(fb.pairing.ALPHABET), 32)

    def test_a_code_is_eight_characters_from_that_alphabet(self):
        for _ in range(50):
            w, code = self.open()
            fb.pairing._reset()
            self.assertEqual(len(code), fb.pairing.CODE_LEN)
            self.assertTrue(set(code) <= set(fb.pairing.ALPHABET), code)

    def test_two_windows_never_produce_the_same_code(self):
        seen = set()
        for _ in range(200):
            _, code = self.open()
            seen.add(code)
        self.assertEqual(len(seen), 200)

    def test_normalisation_is_generous_about_form(self):
        for typed in ("7K3M9QP2", "7k3m9qp2", "7K3M-9QP2", " 7k3m 9qp2 ",
                      "7K3M_9QP2", "7k3m-9qP2"):
            self.assertEqual(fb.pairing.normalise(typed), "7K3M9QP2", typed)

    def test_normalisation_folds_the_four_ambiguous_glyphs(self):
        self.assertEqual(fb.pairing.normalise("ILOU"), "110V")
        self.assertEqual(fb.pairing.normalise("I"), "1")
        self.assertEqual(fb.pairing.normalise("L"), "1")
        self.assertEqual(fb.pairing.normalise("O"), "0")
        self.assertEqual(fb.pairing.normalise("U"), "V")

    def test_normalisation_never_raises_on_a_hostile_body(self):
        for junk in (None, 12345678, [], {}, b"bytes", 3.14):
            self.assertEqual(fb.pairing.normalise(junk), "")

    def test_a_folded_code_still_authenticates(self):
        """A user reading `1` off the screen and typing `I` must still pair."""
        w, code = self.open()
        typed = code.replace("1", "I").replace("0", "O").lower()
        payload, error = self.claim(typed)
        self.assertIsNone(error)
        self.assertTrue(payload["token"].startswith("orc1_"))


# ------------------------------------------------------------------ the window

class TestTheWindow(PairCase):

    def test_the_window_is_two_minutes_and_single_use(self):
        self.assertEqual(fb.pairing.WINDOW_S, 120.0)
        w, code = self.open(now=1000.0)
        self.assertEqual(w["expires_at"], 1120.0)
        self.assertAlmostEqual(w["expires_in"], 120.0)

    def test_a_claim_one_second_late_is_refused(self):
        w, code = self.open(now=1000.0)
        payload, error = self.claim(code, now=1121.0)
        self.assertIsNone(payload)
        self.assertEqual(error[1], fb.pairing.NOT_OPEN)

    def test_a_claim_one_second_early_is_allowed(self):
        w, code = self.open(now=1000.0)
        payload, error = self.claim(code, now=1119.0)
        self.assertIsNone(error)

    def test_the_second_claim_of_one_code_is_refused(self):
        """The photograph-over-the-shoulder case, and the more likely one: a
        user who scans twice must not create two devices."""
        w, code = self.open()
        first, error = self.claim(code)
        self.assertIsNone(error)
        second, error = self.claim(code)
        self.assertIsNone(second)
        self.assertEqual(error[1], fb.pairing.NOT_OPEN)
        self.assertEqual(len(fb.auth.devices()), 1)

    def test_opening_a_second_window_retires_the_first(self):
        """Clicking twice leaves ONE live code, and it is the one on screen."""
        _, first = self.open()
        _, second = self.open()
        self.assertNotEqual(first, second)
        self.assertIsNone(self.claim(first)[0])
        self.assertIsNone(self.claim(second)[1])

    def test_a_claim_with_no_window_open_is_refused(self):
        payload, error = self.claim("7K3M9QP2")
        self.assertIsNone(payload)
        self.assertEqual(error[0], 409)
        self.assertEqual(error[1], fb.pairing.NOT_OPEN)

    def test_closed_expired_and_already_claimed_are_one_answer(self):
        """They are the same fact from the claimant's side, and telling them
        apart would say whether somebody else just paired."""
        codes = []
        # never opened
        codes.append(self.claim("7K3M9QP2")[1])
        # expired
        _, c = self.open(now=1000.0)
        codes.append(self.claim(c, now=2000.0)[1])
        # already claimed
        _, c = self.open(now=1000.0)
        self.claim(c)
        codes.append(self.claim(c)[1])
        self.assertEqual({e[1] for e in codes}, {fb.pairing.NOT_OPEN})

    def test_the_window_state_never_carries_the_code_in_any_of_its_branches(self):
        """`state` is served to the page on every refresh, so a route that
        echoes a live code turns somebody else's open window into yours.

        ALL THREE BRANCHES, and that is not padding: a first version of this
        test only ever looked at the open one, and a mutation that leaked the
        hash from the CLAIMED branch came back green. `state` has three
        returns and a test that reaches one of them proves nothing about the
        other two.
        """
        checks = []
        # open
        w, code = self.open(now=1000.0)
        checks.append(("open", fb.pairing.state(now=1000.0), code))
        # expired
        checks.append(("expired", fb.pairing.state(now=2000.0), code))
        # claimed
        w, code = self.open(now=3000.0)
        self.claim(code, now=3000.0)
        checks.append(("claimed", fb.pairing.state(now=3000.0), code))
        # never opened
        fb.pairing._reset()
        checks.append(("closed", fb.pairing.state(now=3000.0), code))

        for where, blob, secret in checks:
            text = json.dumps(blob)
            self.assertNotIn(secret, text, where)
            self.assertNotIn(secret.lower(), text.lower(), where)
            for leak in ("sha", "code_sha256", "hash"):
                self.assertNotIn(leak, text.lower(), f"{where}: {text}")
            # the plain word "code" must not appear as a key either
            self.assertNotIn("code", [k.lower() for k in blob], where)
        self.assertTrue(checks[0][1]["open"])
        self.assertTrue(checks[1][1]["expired"])
        self.assertTrue(checks[2][1]["claimed"])

    def test_state_reports_the_claim_so_the_page_can_stop_showing_a_dead_qr(self):
        w, code = self.open()
        self.assertTrue(fb.pairing.state(now=1000.0)["open"])
        payload, _ = self.claim(code)
        after = fb.pairing.state(now=1000.0)
        self.assertFalse(after["open"])
        self.assertEqual(after["claimed"], payload["device_id"])

    def test_closing_a_window_is_idempotent(self):
        self.open()
        fb.pairing.close()
        fb.pairing.close()
        self.assertEqual(fb.pairing.state(), {"open": False})


# ------------------------------------------------------------------ the claim

class TestTheClaim(PairCase):

    def test_a_good_claim_mints_a_token_that_actually_authenticates(self):
        """The end of the whole feature: the string that comes back opens the
        door it is supposed to open."""
        w, code = self.open()
        payload, error = self.claim(code)
        self.assertIsNone(error)
        token = payload["token"]
        verdict = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                                now=1000.0)
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.device["id"], payload["device_id"])

    def test_the_token_is_a_real_orc1_token_and_is_stored_hashed(self):
        w, code = self.open()
        payload, _ = self.claim(code)
        version, devid, secret = payload["token"].split("_", 2)
        self.assertEqual(version, "orc1")
        self.assertEqual(len(devid), 8)
        self.assertGreaterEqual(len(secret), 40)
        self.assertNotIn(secret, fb.auth.REGISTRY.read_text())

    def test_the_label_the_phone_sent_is_the_label_you_can_revoke(self):
        w, code = self.open()
        payload, _ = self.claim(code)
        self.assertEqual(payload["label"], "Achill's iPhone")
        self.assertEqual(fb.auth.devices()[0]["label"], "Achill's iPhone")

    def test_a_device_with_no_label_still_gets_one(self):
        """An empty row in `--list-devices` is a row you cannot act on."""
        w, code = self.open()
        payload, _ = self.claim(code, label="")
        self.assertTrue(payload["label"])
        self.assertIn("ios", payload["label"])

    def test_a_label_cannot_be_used_to_grow_the_registry_without_bound(self):
        w, code = self.open()
        payload, _ = self.claim(code, label="x" * 5000)
        self.assertEqual(len(payload["label"]), fb.pairing.LABEL_MAX)

    def test_a_wrong_code_is_refused_and_mints_nothing(self):
        w, code = self.open()
        payload, error = self.claim("AAAAAAAA")
        self.assertIsNone(payload)
        self.assertEqual(error[0], 409)
        self.assertEqual(error[1], fb.pairing.CODE_WRONG)
        self.assertEqual(fb.auth.devices(), [])

    def test_one_wrong_character_is_refused(self):
        w, code = self.open()
        wrong = ("2" if code[0] != "2" else "3") + code[1:]
        self.assertEqual(self.claim(wrong)[1][1], fb.pairing.CODE_WRONG)

    def test_an_empty_or_missing_code_is_refused_not_accepted(self):
        """Rule 6: ambiguity is a refusal. An empty presented code must never
        compare equal to anything."""
        w, code = self.open()
        for junk in ("", None, "   ", "-", 0):
            payload, error = self.claim(junk)
            self.assertIsNone(payload, repr(junk))
            self.assertEqual(error[1], fb.pairing.CODE_WRONG, repr(junk))

    def test_a_body_that_is_not_an_object_is_refused(self):
        self.open()
        for junk in ([], "code", 7, None):
            payload, error = fb.pairing.claim(TAILNET, junk, now=1000.0)
            self.assertIsNone(payload)
            self.assertEqual(error[1], fb.pairing.BAD_REQUEST)

    def test_the_response_carries_no_tls_fields_that_do_not_exist(self):
        """API.md §3.3 lists `spki` and `cert_not_after`. ADR 0013 deleted TLS,
        so sending them as nulls would invite a client to pin against nothing."""
        w, code = self.open()
        payload, _ = self.claim(code)
        self.assertNotIn("spki", payload["server"])
        self.assertNotIn("cert_not_after", payload["server"])
        self.assertIs(payload["server"]["tls"], False)


# ------------------------------------------------------------- who may claim

class TestWhoMayClaim(PairCase):

    def test_the_tailnet_and_loopback_may_pair(self):
        for peer in ("127.0.0.1", "::1", "100.64.0.1", "100.113.110.31",
                     "100.127.255.254"):
            self.assertTrue(fb.pairing.peer_permitted(peer), peer)

    def test_nobody_else_may_pair(self):
        for peer in (ELSEWHERE, "8.8.8.8", "192.168.1.10", "10.0.0.4",
                     "100.63.255.255", "100.128.0.0", "", "not-an-address",
                     None):
            self.assertFalse(fb.pairing.peer_permitted(peer), repr(peer))

    def test_a_stranger_is_refused_before_the_code_is_even_compared(self):
        """It cannot learn whether a window is open, cannot tell a wrong code
        from a closed door, and cannot spend the real phone's attempts."""
        w, code = self.open()
        payload, error = self.claim(code, peer=ELSEWHERE)
        self.assertIsNone(payload)
        self.assertEqual(error[0], 403)
        self.assertEqual(error[1], fb.pairing.PEER_REFUSED)
        self.assertEqual(fb.auth.devices(), [])
        # the window is untouched — the real phone can still use it
        self.assertIsNone(self.claim(code)[1])

    def test_a_stranger_with_the_right_code_still_gets_nothing(self):
        w, code = self.open()
        self.assertEqual(self.claim(code, peer="8.8.8.8")[1][1],
                         fb.pairing.PEER_REFUSED)
        self.assertEqual(fb.auth.devices(), [])


# --------------------------------------------------------------- the attempts

class TestTheAttemptBudget(PairCase):

    def test_five_wrong_codes_from_one_peer_and_it_stops_being_answered(self):
        w, code = self.open()
        for i in range(fb.pairing.ATTEMPTS_PER_PEER):
            self.assertEqual(self.claim("AAAAAAAA")[1][1],
                             fb.pairing.CODE_WRONG, i)
        payload, error = self.claim("AAAAAAAA")
        self.assertEqual(error[0], 429)
        self.assertEqual(error[1], fb.pairing.ATTEMPTS)

    def test_an_exhausted_peer_cannot_pair_even_with_the_right_code(self):
        w, code = self.open()
        for _ in range(fb.pairing.ATTEMPTS_PER_PEER):
            self.claim("AAAAAAAA")
        self.assertEqual(self.claim(code)[1][1], fb.pairing.ATTEMPTS)
        self.assertEqual(fb.auth.devices(), [])

    def test_one_hostile_peer_cannot_lock_out_the_real_phone(self):
        """The reason attempts are counted PER IP. A shared counter would let
        anybody on the tailnet spend the budget the phone needs."""
        w, code = self.open()
        for _ in range(fb.pairing.ATTEMPTS_PER_PEER):
            self.claim("AAAAAAAA", peer="100.64.0.99")
        payload, error = self.claim(code, peer="100.64.0.5")
        self.assertIsNone(error)
        self.assertTrue(payload["token"])

    def test_many_peers_together_close_the_window_and_say_so(self):
        w, code = self.open()
        peer = 0
        while True:
            result = self.claim("AAAAAAAA", peer=f"100.64.1.{peer % 250}")
            peer += 1
            if result[1][1] == fb.pairing.LOCKED:
                break
            self.assertLess(peer, 200, "the aggregate cap never fired")
        self.assertGreaterEqual(peer, fb.pairing.ATTEMPTS_TOTAL)
        # and the real phone is told the window is locked, not that it is wrong
        self.assertEqual(self.claim(code, peer="100.64.0.5")[1][1],
                         fb.pairing.LOCKED)

    def test_a_wrong_code_also_spends_from_the_servers_failure_budget(self):
        """ADR 0014 promised this route would inherit that bucket."""
        w, code = self.open()
        before = fb.auth._budget(TAILNET, 1000.0)
        self.claim("AAAAAAAA")
        self.assertLess(fb.auth._budget(TAILNET, 1000.0), before)

    def test_a_successful_pairing_clears_the_peers_failures(self):
        w, code = self.open()
        self.claim("AAAAAAAA")
        self.claim(code)
        self.assertEqual(fb.auth._budget(TAILNET, 1000.0),
                         float(fb.auth.FAIL_BURST))


# --------------------------------------------------------------------- the QR

class TestTheQR(PairCase):

    def test_the_payload_carries_host_port_and_code(self):
        w, code = self.open(host="100.113.110.31")
        self.assertEqual(w["url"], f"orc://p?h=100.113.110.31&c={code}")
        self.assertEqual(w["manual"],
                         {"host": "100.113.110.31", "port": 4242,
                          "code": w["code"]})

    def test_a_non_default_port_is_carried_and_the_default_is_not(self):
        """Five bytes of QR budget decide the version, which decides how far
        away a camera can read it."""
        self.assertIn("&p=4243", fb.pairing.payload_url("h", 4243, "AAAA1111"))
        self.assertNotIn("p=", fb.pairing.payload_url("h", 4242, "AAAA1111")
                         .replace("orc://p?", ""))

    def test_the_qr_never_carries_the_token(self):
        """The whole reason for the indirection. A QR is visible to the room,
        to a screenshot, and to a video call you forgot was sharing."""
        w, code = self.open()
        payload, _ = self.claim(code)
        self.assertNotIn(payload["token"], w["url"])
        self.assertNotIn(payload["token"], w["svg"])
        self.assertNotIn(payload["token"].split("_", 2)[2], json.dumps(w))

    def test_the_qr_never_carries_a_certificate_pin(self):
        """API.md §3.2's `f=` field belongs to the TLS design ADR 0013 removed."""
        w, code = self.open()
        self.assertNotIn("f=", w["url"])
        self.assertNotIn("spki", json.dumps(w))

    def test_the_picture_and_the_manual_fields_cannot_disagree(self):
        """One function builds the string and immediately encodes THAT string.

        Decoded here by re-encoding the manual fields and comparing matrices —
        a mismatch means the two halves of the card came from different data,
        which is the failure where a user types a code the QR does not carry.
        """
        w, code = self.open(host="100.113.110.31")
        rebuilt = fb.pairing.payload_url(w["manual"]["host"],
                                         w["manual"]["port"],
                                         w["manual"]["code"].replace("-", ""))
        self.assertEqual(rebuilt, w["url"])
        self.assertEqual(fb.qr.svg(fb.qr.encode(rebuilt, "M")), w["svg"])

    def test_the_svg_is_an_svg_of_a_sane_size(self):
        w, code = self.open()
        self.assertTrue(w["svg"].startswith("<svg "))
        self.assertTrue(w["svg"].endswith("</svg>"))
        self.assertLessEqual(w["qr_version"], 6)

    def test_the_qr_advertises_where_the_phone_should_go_not_where_we_bound(self):
        """A QR saying `127.0.0.1` sends the phone to its own web server.

        So when the board is bound to loopback the advertised host falls back
        to the detected tailnet address; when it is bound to the tailnet the
        bound address already is the answer.
        """
        handler = fb.Handler.__new__(fb.Handler)
        fb.CFG["host"] = "100.113.110.31"
        self.assertEqual(handler._advertised_host(), "100.113.110.31")

        fb.CFG["host"] = "127.0.0.1"
        saved = fb.tailnet.address
        try:
            fb.tailnet.address = lambda: "100.64.7.7"
            self.assertEqual(handler._advertised_host(), "100.64.7.7")
            # …and with no tailnet at all it says what it bound, so a rehearsal
            # on a laptop with Tailscale off looks like a rehearsal instead of
            # failing silently at the phone.
            fb.tailnet.address = lambda: None
            self.assertEqual(handler._advertised_host(), "127.0.0.1")
        finally:
            fb.tailnet.address = saved

    def test_the_longest_realistic_host_still_fits(self):
        """MagicDNS names get long. A payload that does not fit must raise
        rather than render a QR of two thirds of a URL."""
        host = "a" * 63 + ".tail1205d9.ts.net"
        url = fb.pairing.payload_url(host, 4242, "7K3M9QP2")
        matrix = fb.qr.encode(url, "M")
        self.assertLessEqual((len(matrix) - 17) // 4, 10)


# ------------------------------------------------------------------ the audit

class TestTheAudit(PairCase):

    def lines(self):
        return fb.auth.read_audit()

    def test_a_successful_pairing_is_recorded_as_paired_not_as_allow(self):
        """`allow` is the DOOR's word for this route — `auth.check` writes it
        for every pairing attempt, right or wrong, because the door has no
        token to check. `paired` is what actually happened, and it is a
        separate word so the log does not read `allow` beside a refusal of the
        same request."""
        w, code = self.open()
        payload, _ = self.claim(code)
        line = [x for x in self.lines() if x["outcome"] == "paired"][-1]
        self.assertEqual(line["path"], "/api/v1/pair")
        self.assertEqual(line["device"], payload["device_id"])
        self.assertEqual(line["label"], "Achill's iPhone")
        self.assertEqual([x for x in self.lines() if x["outcome"] == "allow"], [])

    def test_every_refusal_is_recorded(self):
        w, code = self.open()
        self.claim("AAAAAAAA")
        self.claim(code, peer=ELSEWHERE)
        codes = {x.get("code") for x in self.lines() if x["outcome"] == "refuse"}
        self.assertIn(fb.pairing.CODE_WRONG, codes)
        self.assertIn(fb.pairing.PEER_REFUSED, codes)

    def test_the_log_never_carries_a_pairing_code_or_a_token(self):
        """It is 0600, and it is also the file somebody pastes into a bug
        report."""
        w, code = self.open()
        self.claim("AAAAAAAA")
        payload, _ = self.claim(code)
        blob = fb.auth.AUDIT_LOG.read_text()
        self.assertNotIn(code, blob)
        self.assertNotIn(payload["token"], blob)
        self.assertNotIn("AAAAAAAA", blob)


# ------------------------------------------------------------ the admin rule

class TestAdminAnswersToNoToken(PairCase):

    def test_the_device_subtree_is_admin_and_pairing_is_not(self):
        self.assertTrue(fb.auth.admin("GET", "/api/v1/devices"))
        self.assertTrue(fb.auth.admin("POST", "/api/v1/devices/pair/open"))
        self.assertTrue(fb.auth.admin("POST", "/api/v1/devices/ab12cd34/revoke"))
        self.assertTrue(fb.auth.admin("GET", "/api/v1/devices?x=1"))
        self.assertFalse(fb.auth.admin("POST", "/api/v1/pair"))
        self.assertFalse(fb.auth.admin("GET", "/api/state"))

    def test_the_prefix_does_not_spread_across_a_segment_boundary(self):
        self.assertFalse(fb.auth.admin("GET", "/api/v1/devices-of-others"))
        self.assertFalse(fb.auth.admin("GET", "/api/v1/deviceslist"))

    def test_this_machine_with_no_token_may_administer(self):
        self.assertTrue(fb.auth.check("127.0.0.1", None, "GET",
                                      "/api/v1/devices", now=1000.0).ok)

    def test_a_perfectly_valid_token_may_not(self):
        """The rule that makes a stolen phone survivable: it cannot revoke the
        device that would have revoked it."""
        _, token = fb.auth.add_device("iPhone")
        for method, path in (("GET", "/api/v1/devices"),
                             ("POST", "/api/v1/devices/pair/open"),
                             ("POST", "/api/v1/devices/ab12cd34/revoke")):
            verdict = fb.auth.check(TAILNET, f"Bearer {token}", method, path,
                                    now=1000.0, content_type="application/json")
            self.assertFalse(verdict.ok, f"{method} {path}")
            self.assertEqual(verdict.status, 403)
            self.assertEqual(verdict.code, fb.auth.ADMIN_ONLY)

    def test_a_token_presented_from_loopback_may_not_either(self):
        """A proxy makes every peer loopback (API.md §2.7). The rule is the
        ABSENCE of a token, not the address, so this stays shut."""
        _, token = fb.auth.add_device("iPhone")
        verdict = fb.auth.check("127.0.0.1", f"Bearer {token}", "GET",
                                "/api/v1/devices", now=1000.0)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.code, fb.auth.ADMIN_ONLY)

    def test_a_stranger_gets_401_not_403(self):
        """Authentication first: an unauthenticated peer learns nothing about
        which routes are special."""
        verdict = fb.auth.check(TAILNET, None, "GET", "/api/v1/devices",
                                now=1000.0)
        self.assertEqual(verdict.status, 401)

    def test_being_told_no_does_not_cost_a_good_device_its_budget(self):
        """A buggy app must not be able to lock its own phone out."""
        _, token = fb.auth.add_device("iPhone")
        for _ in range(fb.auth.FAIL_BURST + 5):
            fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/v1/devices",
                          now=1000.0)
        self.assertTrue(fb.auth.check(TAILNET, f"Bearer {token}", "GET",
                                      "/api/state", now=1000.0).ok)

    def test_reading_the_device_list_is_audited_even_though_it_is_a_get(self):
        """It is the inventory of every credential to this machine, and unlike
        `/api/state` it is not polled, so logging it buries nothing."""
        self.assertTrue(fb.auth.audited("GET", "/api/v1/devices"))
        self.assertFalse(fb.auth.audited("GET", "/api/state"))


# --------------------------------------------------------------------- the wire

class RemoteHandler(fb.Handler):
    """The board's handler, believing its peer is on the tailnet."""
    PEER = TAILNET

    def setup(self):
        super().setup()
        self.client_address = (self.PEER, 51000)


class LoopbackHandler(RemoteHandler):
    PEER = "127.0.0.1"


class WireCase(PairCase):
    handler = RemoteHandler

    def setUp(self):
        super().setUp()
        fb.config.DEMO = True
        self._config_path = fb.config.CONFIG_PATH
        fb.config.CONFIG_PATH = self.dir / "orchestra.config.json"
        self.srv = fb.Server(("127.0.0.1", 0), self.handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

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
            head.setdefault("Content-Type", "application/json")
        try:
            conn.request(method, path, body=body, headers=head)
            r = conn.getresponse()
            raw = r.read()
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            return r.status, data
        finally:
            conn.close()


class TestTheFlowOnTheWire(WireCase):
    """A loopback handler opens the window; the tailnet handler claims it.

    Two servers, because that is the real topology: the board is on the Mac and
    the phone is not, and the whole point of `auth.ADMIN` is that those two are
    not the same caller.
    """

    def open_window(self):
        board = fb.Server(("127.0.0.1", 0), LoopbackHandler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1",
                                              board.server_address[1], timeout=10)
            conn.request("POST", "/api/v1/devices/pair/open", body="{}",
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            data = json.loads(r.read())
            conn.close()
            return r.status, data
        finally:
            board.shutdown()
            board.server_close()

    def test_the_whole_exchange(self):
        status, window = self.open_window()
        self.assertEqual(status, 200)
        code = window["code"].replace("-", "")

        status, data = self.request("POST", "/api/v1/pair", body=json.dumps(
            {"code": code, "label": "iPhone 15", "platform": "ios"}))
        self.assertEqual(status, 200)
        token = data["token"]

        # the token opens a door it could not open a moment ago
        self.assertEqual(self.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.request("GET", "/api/state", token=token)[0], 200)

        # …and stops the moment it is revoked
        fb.auth.revoke_device(data["device_id"])
        status, refused = self.request("GET", "/api/state", token=token)
        self.assertEqual(status, 401)
        self.assertEqual(refused["error"], fb.auth.REVOKED)

    def test_a_phone_cannot_reach_the_device_routes_with_a_good_token(self):
        _, token = fb.auth.add_device("iPhone")
        for method, path, body in (
                ("GET", "/api/v1/devices", None),
                ("POST", "/api/v1/devices/pair/open", "{}"),
                ("POST", "/api/v1/devices/ab12cd34/revoke", "{}")):
            status, data = self.request(method, path, token=token, body=body)
            self.assertEqual(status, 403, f"{method} {path}")
            self.assertEqual(data["error"], fb.auth.ADMIN_ONLY)

    def test_a_phone_cannot_revoke_the_device_that_would_revoke_it(self):
        device, token = fb.auth.add_device("stolen iPhone")
        status, _ = self.request("POST",
                                 f"/api/v1/devices/{device['id']}/revoke",
                                 token=token, body="{}")
        self.assertEqual(status, 403)
        self.assertIsNone(fb.auth.devices()[0]["revoked"])

    def test_pairing_answers_a_phone_that_has_no_token_at_all(self):
        status, data = self.request("POST", "/api/v1/pair", body="{}")
        self.assertEqual(status, 409)
        self.assertEqual(data["error"], fb.pairing.NOT_OPEN)
        self.assertNotIn("token", data)

    def test_a_pairing_post_without_the_json_header_is_refused(self):
        """The CSRF guard applies to the exempt route too."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/v1/pair", body="{}",
                     headers={"Content-Type": "text/plain"})
        r = conn.getresponse()
        self.assertEqual(r.status, 415)
        conn.close()

    def test_only_the_revoke_verb_revokes(self):
        """The path is parsed, not merely prefix-matched, and the parse has to
        be exact: `/api/v1/devices/<id>` with no verb, or with a verb nobody
        wrote, must be a 404 that changes nothing. A looser check
        (`len(parts) >= 4`) silently turns every path under the subtree into a
        revocation, which is destructive and irreversible."""
        device, _ = fb.auth.add_device("iPhone")
        board = fb.Server(("127.0.0.1", 0), LoopbackHandler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            for path in (f"/api/v1/devices/{device['id']}",
                         f"/api/v1/devices/{device['id']}/",
                         f"/api/v1/devices/{device['id']}/rename",
                         f"/api/v1/devices/{device['id']}/revoke/now",
                         "/api/v1/devices/"):
                conn = http.client.HTTPConnection(
                    "127.0.0.1", board.server_address[1], timeout=10)
                conn.request("POST", path, body="{}",
                             headers={"Content-Type": "application/json"})
                r = conn.getresponse()
                r.read()
                conn.close()
                self.assertEqual(r.status, 404, path)
                self.assertIsNone(fb.auth.devices()[0]["revoked"], path)
            # …and the exact path does revoke.
            conn = http.client.HTTPConnection(
                "127.0.0.1", board.server_address[1], timeout=10)
            conn.request("POST", f"/api/v1/devices/{device['id']}/revoke",
                         body="{}", headers={"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
            self.assertIsNotNone(fb.auth.devices()[0]["revoked"])
        finally:
            board.shutdown()
            board.server_close()

    def test_revoking_an_unknown_device_is_a_404_not_a_500(self):
        board = fb.Server(("127.0.0.1", 0), LoopbackHandler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1",
                                              board.server_address[1], timeout=10)
            conn.request("POST", "/api/v1/devices/nosuchid/revoke", body="{}",
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            self.assertEqual(r.status, 404)
            self.assertEqual(json.loads(r.read())["error"], "device_unknown")
            conn.close()
        finally:
            board.shutdown()
            board.server_close()

    def test_the_pairing_page_is_served_and_is_not_exempt(self):
        self.assertEqual(self.request("GET", "/pair")[0], 401)
        board = fb.Server(("127.0.0.1", 0), LoopbackHandler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1",
                                              board.server_address[1], timeout=10)
            conn.request("GET", "/pair")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            body = r.read()
            self.assertIn(b"pair a device", body)
            conn.close()
        finally:
            board.shutdown()
            board.server_close()

    def test_loading_the_page_does_not_open_a_pairing_window(self):
        """It is a GET; opening a window is a POST. A page that opened a door
        by being looked at is a door that is always open."""
        board = fb.Server(("127.0.0.1", 0), LoopbackHandler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            for _ in range(3):
                conn = http.client.HTTPConnection(
                    "127.0.0.1", board.server_address[1], timeout=10)
                conn.request("GET", "/pair")
                conn.getresponse().read()
                conn.close()
            self.assertEqual(fb.pairing.state(), {"open": False})
        finally:
            board.shutdown()
            board.server_close()


# ------------------------------------------------------------------- the bind

class TestTheBind(PairCase):

    def test_loopback_needs_no_device(self):
        for host in ("127.0.0.1", "localhost", "", "127.0.0.53"):
            self.assertIsNone(fb.auth.bind_refusal(host), host)

    def test_the_tailnet_refuses_with_no_device_and_binds_with_one(self):
        self.assertIn("no device is registered",
                      fb.auth.bind_refusal("100.113.110.31"))
        fb.auth.add_device("iPhone")
        self.assertIsNone(fb.auth.bind_refusal("100.113.110.31"))

    def test_every_interface_is_refused_and_names_the_flag_that_is_not_a_typo(self):
        fb.auth.add_device("iPhone")
        refusal = fb.auth.bind_refusal("0.0.0.0")
        self.assertIn("every interface", refusal)
        self.assertIn("--tailnet", refusal)
        self.assertIn("--bind-every-interface", refusal)

    def test_the_wide_bind_flag_still_requires_a_paired_device(self):
        """Saying it loudly does not make it safe to serve every transcript on
        the machine to a network with nobody registered."""
        fb.CFG["bind_every_interface"] = True
        self.assertIn("no device is registered", fb.auth.bind_refusal("0.0.0.0"))
        fb.auth.add_device("iPhone")
        self.assertIsNone(fb.auth.bind_refusal("0.0.0.0"))

    def test_the_wide_bind_cannot_be_turned_on_from_a_config_file(self):
        """`load_config` overwrites the key from the parsed arguments every
        run, so a leftover line in JSON cannot quietly open the machine."""
        path = self.dir / "orchestra.config.json"
        path.write_text(json.dumps({"bind_every_interface": True,
                                    "roots": [str(self.dir)]}))
        fb.load_config(["--config", str(path)])
        self.assertIs(fb.CFG["bind_every_interface"], False)

    def test_the_flag_turns_it_on_and_selects_the_address(self):
        path = self.dir / "orchestra.config.json"
        path.write_text(json.dumps({"roots": [str(self.dir)]}))
        fb.load_config(["--config", str(path), "--bind-every-interface"])
        self.assertIs(fb.CFG["bind_every_interface"], True)
        self.assertEqual(fb.CFG["host"], "0.0.0.0")

    def test_an_unreadable_registry_refuses_the_bind(self):
        fb.auth.REGISTRY.write_text("{not json")
        fb.auth._forget_registry()
        self.assertIn("cannot be read", fb.auth.bind_refusal("100.113.110.31"))

    def test_a_revoked_device_does_not_count(self):
        device, _ = fb.auth.add_device("iPhone")
        fb.auth.revoke_device(device["id"])
        self.assertIn("no device is registered",
                      fb.auth.bind_refusal("100.113.110.31"))


class TestTheLoopbackCompanion(PairCase):
    """A non-loopback bind must not take the board away from the Mac.

    Found by driving it: a server bound to `100.113.110.31` is not listening on
    `127.0.0.1`, so the board's own bookmark is refused — and worse, a request
    the Mac sends to its own tailnet address arrives from `100.113.110.31`,
    which is not loopback, so `auth.ADMIN` refuses the person at the keyboard.
    Both of those are invisible to every unit test that binds `127.0.0.1`.
    """

    def free_port(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_a_tailnet_bind_keeps_a_loopback_listener(self):
        from orchestra import __main__ as entry
        port = self.free_port()
        addr = fb.tailnet.address()
        if not addr:
            self.skipTest("Tailscale is not up on this machine")
        primary = fb.Server((addr, port), fb.Handler)
        threading.Thread(target=primary.serve_forever, daemon=True).start()
        companion = entry._also_loopback(addr, port)
        try:
            self.assertIsNotNone(companion)
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/health")
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            r.read()
            conn.close()
        finally:
            if companion:
                companion.shutdown()
                companion.server_close()
            primary.shutdown()
            primary.server_close()

    def test_loopback_and_the_wide_bind_get_no_companion(self):
        """127.0.0.1 already is the companion; 0.0.0.0 already includes it, and
        a second bind on either fails with EADDRINUSE at startup.

        `localhost` is in the list because it is where this went wrong: the
        companion spelled "is this loopback" without the name arm that
        `bind_refusal` had, so `--host localhost` bound 127.0.0.1 twice and the
        board would not start. Both now call `auth.loopback_bind`.
        """
        from orchestra import __main__ as entry
        for host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::", ""):
            companion = entry._also_loopback(host, self.free_port())
            if companion is not None:      # do not leak a listener on failure
                companion.shutdown()
                companion.server_close()
            self.assertIsNone(companion, host)

    def test_loopback_bind_and_bind_refusal_agree_about_what_is_loopback(self):
        """The two used to disagree, and the disagreement was the bug."""
        for host in ("127.0.0.1", "localhost", "", "::1", "127.0.0.53"):
            self.assertTrue(fb.auth.loopback_bind(host), host)
            self.assertIsNone(fb.auth.bind_refusal(host), host)
        for host in ("100.113.110.31", "0.0.0.0", "192.168.1.4"):
            self.assertFalse(fb.auth.loopback_bind(host), host)
            self.assertIsNotNone(fb.auth.bind_refusal(host), host)

    def test_the_mac_talking_to_its_own_tailnet_address_is_not_loopback(self):
        """The fact that makes the companion necessary, pinned so nobody
        'simplifies' it away."""
        addr = fb.tailnet.address()
        if not addr:
            self.skipTest("Tailscale is not up on this machine")
        self.assertFalse(fb.auth.loopback(addr))
        self.assertFalse(fb.auth.check(addr, None, "GET", "/api/v1/devices",
                                       now=1000.0).ok)


# ------------------------------------------------- what a request cannot show

class TestTheSourceRules(unittest.TestCase):
    """The same two rules `test_auth.py` pins on `auth.py`, on `pairing.py`.

    They have to be repeated rather than generalised, because the reason they
    exist is that no request can demonstrate them: a `==` on a hash answers
    identically to `hmac.compare_digest` in every test that can be written, and
    `random.choice` emits perfectly plausible pairing codes — what makes it
    unusable is that a Mersenne Twister seeded from the clock is predictable
    from its own output, which no assertion here can see.

    A pairing code is a credential for its whole life however short that is, so
    it gets the same treatment as the token.
    """

    def setUp(self):
        import ast
        self.ast = ast
        self.tree = ast.parse((ROOT / "orchestra" / "pairing.py").read_text())

    def test_the_code_is_never_compared_with_an_operator(self):
        for node in self.ast.walk(self.tree):
            if not isinstance(node, self.ast.Compare):
                continue
            for operand in [node.left, *node.comparators]:
                self.assertFalse(
                    isinstance(operand, self.ast.Call)
                    and getattr(operand.func, "id", "") == "_hash",
                    "a pairing code hash is being compared with an operator")

    def test_the_code_is_compared_with_compare_digest(self):
        calls = [n for n in self.ast.walk(self.tree)
                 if isinstance(n, self.ast.Call)
                 and getattr(n.func, "attr", "") == "compare_digest"]
        self.assertTrue(calls, "nothing calls hmac.compare_digest")
        self.assertTrue(any(isinstance(a, self.ast.Call)
                            and getattr(a.func, "id", "") == "_hash"
                            for c in calls for a in c.args),
                        "compare_digest is called, but not on the code hash")

    def test_the_code_comes_from_secrets_and_never_from_random(self):
        """Checked in the AST, on the CALL — not by grepping the source.

        The first version of this asserted `"secrets.choice" in source`, and it
        could not fail: `open_window`'s own docstring contains the words
        "`secrets.choice`, never `random.choice`", so the substring is present
        however the code is written. A mutation swapping the call for
        `__import__('random').choice(...)` came back green. METHOD.md §3.

        What is asserted now is that every `.choice(` call in this module is a
        call on the NAME `secrets`, which `__import__('random')` is not.
        """
        chooses = [n for n in self.ast.walk(self.tree)
                   if isinstance(n, self.ast.Call)
                   and getattr(n.func, "attr", "") in ("choice", "randint",
                                                       "random", "randrange",
                                                       "shuffle", "sample")]
        self.assertTrue(chooses, "nothing in pairing.py picks a random value")
        for call in chooses:
            self.assertIsInstance(call.func.value, self.ast.Name,
                                  "a random value is drawn from an expression "
                                  "rather than from the `secrets` module")
            self.assertEqual(call.func.value.id, "secrets",
                             f"{call.func.value.id}.{call.func.attr} is not "
                             f"cryptographically random")
        names = set()
        for node in self.ast.walk(self.tree):
            if isinstance(node, self.ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, self.ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertIn("secrets", names)
        self.assertNotIn("random", names)


# --------------------------------------------------------- finding the tailnet

class TestFindingTheTailnet(unittest.TestCase):

    def test_the_range_is_the_cgnat_block_and_nothing_adjacent(self):
        for addr in ("100.64.0.0", "100.64.0.1", "100.113.110.31",
                     "100.127.255.255"):
            self.assertTrue(fb.tailnet.in_range(addr), addr)
        for addr in ("100.63.255.255", "100.128.0.0", "10.0.0.1",
                     "192.168.0.1", "127.0.0.1", "1.1.1.1"):
            self.assertFalse(fb.tailnet.in_range(addr), addr)

    def test_junk_is_not_in_range_rather_than_an_exception(self):
        for junk in ("", "not-an-address", "100.64.0", "::1", None, 7):
            self.assertFalse(fb.tailnet.in_range(junk), repr(junk))

    def test_bindable_answers_the_question_it_claims_to(self):
        """Loopback binds; an address on no interface here does not."""
        self.assertTrue(fb.tailnet.bindable("127.0.0.1"))
        self.assertFalse(fb.tailnet.bindable("100.64.213.99"))

    def test_a_missing_or_broken_source_is_silence_not_a_crash(self):
        saved = fb.tailnet.TAILSCALE
        try:
            fb.tailnet.TAILSCALE = ("/nonexistent/tailscale",)
            self.assertEqual(fb.tailnet.from_cli(), [])
        finally:
            fb.tailnet.TAILSCALE = saved

    def test_address_returns_something_bindable_or_nothing(self):
        addr = fb.tailnet.address()
        if addr is not None:
            self.assertTrue(fb.tailnet.in_range(addr))
            self.assertTrue(fb.tailnet.bindable(addr))

    def test_why_not_distinguishes_the_three_situations(self):
        """Merging them into "no tailnet address found" is the shape of
        unhelpfulness that makes people paste 0.0.0.0 into --host."""
        saved = (fb.tailnet.from_cli, fb.tailnet.from_interfaces)
        try:
            fb.tailnet.from_cli = lambda: []
            fb.tailnet.from_interfaces = lambda: []
            self.assertIn("Is Tailscale installed", fb.tailnet.why_not())

            fb.tailnet.from_cli = lambda: ["100.64.0.5"]
            self.assertIn("backend is not up", fb.tailnet.why_not())

            fb.tailnet.from_interfaces = lambda: ["100.64.0.5"]
            self.assertIn("could not be bound", fb.tailnet.why_not())
        finally:
            fb.tailnet.from_cli, fb.tailnet.from_interfaces = saved


if __name__ == "__main__":
    unittest.main()
