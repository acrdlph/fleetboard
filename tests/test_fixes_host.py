#!/usr/bin/env python3
"""The Host allowlist — API.md §2.3 step 2, closing T2 (a fronting proxy).

`auth.check` already passed the `Host` header down and already checked `Origin`,
but it did NOT allowlist `Host` — and `same_origin`'s own docstring names the
hole it leaves: a request where `Origin` and `Host` AGREE on a foreign name
(DNS rebinding) sails through the origin guard, and a proxy that rewrites `Host`
(`tailscale serve`/`funnel`) is invisible to it. This file proves the allowlist
closes both, and that it does so ADDITIVELY: a loopback request, and a request
with no `Host` at all, behave exactly as before.

    python3 -m unittest tests.test_fixes_host
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402

TAILNET = "100.64.0.9"          # a plausible phone that is not this machine
BOUND_TS = "100.113.110.31"     # the tailnet address a --tailnet server binds


class HostCase(unittest.TestCase):
    """Own registry, own audit log, empty budget, and CFG restored after."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fb-host-"))
        self._saved = (fb.auth.REGISTRY, fb.auth.AUDIT_LOG)
        fb.auth.REGISTRY = self.dir / "devices.json"
        fb.auth.AUDIT_LOG = self.dir / "audit.log.jsonl"
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        fb.auth._forget_allowed_hosts()
        self._cfg = dict(fb.CFG)
        # No test here wants the real subprocess probe: the tailnet address is
        # detected by shelling out, and that is exactly what the mock seam is
        # for. Each test that needs a tailnet bind sets this explicitly.
        self._saved_addr = fb.auth.tailnet.address
        fb.auth.tailnet.address = lambda: None

    def tearDown(self):
        fb.auth.REGISTRY, fb.auth.AUDIT_LOG = self._saved
        fb.auth.tailnet.address = self._saved_addr
        fb.auth._forget_registry()
        fb.auth._reset_buckets()
        fb.auth._forget_allowed_hosts()
        fb.CFG.clear()
        fb.CFG.update(self._cfg)
        shutil.rmtree(self.dir, ignore_errors=True)

    def mint(self, label="iPhone"):
        return fb.auth.add_device(label)


# ------------------------------------------------------ the pure predicate

class TestAuthorityHost(HostCase):
    def test_strips_the_port(self):
        self.assertEqual(fb.auth._authority_host("127.0.0.1:4242"), "127.0.0.1")
        self.assertEqual(fb.auth._authority_host("localhost:4242"), "localhost")
        self.assertEqual(fb.auth._authority_host("evil.com:80"), "evil.com")

    def test_keeps_a_bare_name(self):
        self.assertEqual(fb.auth._authority_host("localhost"), "localhost")
        self.assertEqual(fb.auth._authority_host("evil.com"), "evil.com")

    def test_lowercases(self):
        self.assertEqual(fb.auth._authority_host("EVIL.COM:80"), "evil.com")
        self.assertEqual(fb.auth._authority_host("LocalHost"), "localhost")

    def test_bracketed_v6_keeps_its_brackets_and_drops_the_port(self):
        self.assertEqual(fb.auth._authority_host("[::1]"), "[::1]")
        self.assertEqual(fb.auth._authority_host("[::1]:4242"), "[::1]")

    def test_a_bare_v6_has_no_port_to_strip(self):
        # Two or more colons and no brackets: there is no host:port to split.
        self.assertEqual(fb.auth._authority_host("::1"), "::1")


class TestAllowedHosts(HostCase):
    def test_a_loopback_bind_is_exactly_the_aliases(self):
        fb.CFG["host"] = "127.0.0.1"
        fb.auth._forget_allowed_hosts()
        self.assertEqual(fb.auth.allowed_hosts(),
                         {"127.0.0.1", "localhost", "::1", "[::1]"})

    def test_a_loopback_bind_never_probes_the_tailnet(self):
        # The perf claim: a loopback-only server pays no subprocess. If it did,
        # this monkeypatched probe would fire and we would see its marker.
        fired = []
        fb.auth.tailnet.address = lambda: fired.append(1) or None
        fb.CFG["host"] = "127.0.0.1"
        fb.auth._forget_allowed_hosts()
        fb.auth.allowed_hosts()
        self.assertEqual(fired, [])

    def test_the_bound_host_joins_the_set(self):
        fb.CFG["host"] = BOUND_TS
        fb.auth._forget_allowed_hosts()
        self.assertIn(BOUND_TS, fb.auth.allowed_hosts())

    def test_the_detected_tailnet_address_joins_the_set(self):
        # `CFG["host"]` was spelled one legitimate way; the detected node
        # address is another, and a phone reaching us by it is still us.
        fb.CFG["host"] = BOUND_TS
        fb.auth.tailnet.address = lambda: "100.64.0.55"
        fb.auth._forget_allowed_hosts()
        self.assertIn("100.64.0.55", fb.auth.allowed_hosts())

    def test_the_probe_is_skipped_on_a_loopback_bind_but_runs_on_a_tailnet_one(self):
        seen = []
        fb.auth.tailnet.address = lambda: seen.append(1) or "100.64.0.55"
        fb.CFG["host"] = "localhost"
        fb.auth._forget_allowed_hosts()
        fb.auth.allowed_hosts()
        self.assertEqual(seen, [])              # loopback name: no probe
        fb.CFG["host"] = BOUND_TS
        fb.auth._forget_allowed_hosts()
        fb.auth.allowed_hosts()
        self.assertEqual(seen, [1])             # tailnet bind: probed once

    def test_the_set_is_memoised_on_the_bound_host(self):
        seen = []
        fb.auth.tailnet.address = lambda: seen.append(1) or "100.64.0.55"
        fb.CFG["host"] = BOUND_TS
        fb.auth._forget_allowed_hosts()
        fb.auth.allowed_hosts()
        fb.auth.allowed_hosts()
        fb.auth.allowed_hosts()
        self.assertEqual(seen, [1])             # probed once, then cached


class TestHostAllowed(HostCase):
    def test_absent_host_passes(self):
        # There is nothing to allowlist, and rejecting it would newly refuse the
        # curls and same-machine callers that send none. Today's behaviour.
        self.assertTrue(fb.auth.host_allowed(None))
        self.assertTrue(fb.auth.host_allowed(""))

    def test_loopback_hosts_pass(self):
        for h in ("127.0.0.1:4242", "localhost:4242", "localhost",
                  "[::1]:4242", "::1"):
            self.assertTrue(fb.auth.host_allowed(h), h)

    def test_a_foreign_host_fails(self):
        for h in ("evil.com", "evil.com:4242", "somenode.ts.net",
                  "somenode.ts.net:443", BOUND_TS + ":4242"):
            self.assertFalse(fb.auth.host_allowed(h), h)


# ---------------------------------------------------- the check, end to end

class TestHostGuardInCheck(HostCase):
    """The predicate wired into `check`, on the paths `browser_guards` runs."""

    def loopback_get(self, **kw):
        # A loopback peer with no token — the board. `browser_guards` runs here.
        return fb.auth.check("127.0.0.1", None, "GET", "/api/state",
                             now=1000.0, **kw)

    def loopback_post(self, **kw):
        kw.setdefault("content_type", "application/json")
        return fb.auth.check("127.0.0.1", None, "POST", "/api/send",
                             now=1000.0, **kw)

    # --- backward compatibility, stated as tests -----------------------------

    def test_a_loopback_host_still_passes(self):
        self.assertTrue(self.loopback_get(host="127.0.0.1:4242").ok)
        self.assertTrue(self.loopback_get(host="localhost:4242").ok)
        self.assertTrue(self.loopback_post(host="127.0.0.1:4242",
                                           origin="http://127.0.0.1:4242").ok)

    def test_no_host_behaves_as_before(self):
        self.assertTrue(self.loopback_get(host=None).ok)
        self.assertTrue(self.loopback_post(host=None, origin=None).ok)

    def test_the_configured_bind_host_passes(self):
        # A real phone on a --tailnet server: valid token, tailnet peer, and a
        # Host that is the bound address.
        fb.CFG["host"] = BOUND_TS
        fb.auth._forget_allowed_hosts()
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                          now=1000.0, host=BOUND_TS + ":4242")
        self.assertTrue(v.ok, v)

    def test_the_detected_tailnet_host_passes(self):
        fb.CFG["host"] = BOUND_TS
        fb.auth.tailnet.address = lambda: "100.64.0.55"
        fb.auth._forget_allowed_hosts()
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                          now=1000.0, host="100.64.0.55:4242")
        self.assertTrue(v.ok, v)

    # --- the refusals --------------------------------------------------------

    def test_a_foreign_host_is_403_host_not_allowed(self):
        v = self.loopback_get(host="evil.com:4242")
        self.assertEqual((v.status, v.code), (403, fb.auth.HOST_NOT_ALLOWED))

    def test_a_funnel_ts_net_host_is_refused_when_it_is_not_the_bound_node(self):
        # `tailscale funnel` fronts the server under a `<node>.ts.net` name that
        # this server never bound. Nothing derives a MagicDNS name into the set,
        # so it is foreign — which is the deliberate §2.7 block.
        fb.CFG["host"] = BOUND_TS
        fb.auth._forget_allowed_hosts()
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                          now=1000.0, host="somenode.ts.net")
        self.assertEqual((v.status, v.code), (403, fb.auth.HOST_NOT_ALLOWED))

    def test_the_exact_hole_same_origin_names_is_now_closed(self):
        """`Origin` and `Host` AGREE on a foreign name — DNS rebinding.

        `same_origin` returns True (they match each other), so the origin guard
        passes it; before the allowlist this reached the route. Now the Host
        check catches it, on both a read and a mutation.
        """
        # The GET the same_origin docstring's `evil.com -> 127.0.0.1` describes.
        v = self.loopback_get(origin="http://evil.com:4242",
                              host="evil.com:4242")
        self.assertTrue(fb.auth.same_origin("http://evil.com:4242",
                                            "evil.com:4242"))   # they AGREE
        self.assertEqual((v.status, v.code), (403, fb.auth.HOST_NOT_ALLOWED))
        # And the mutation half: a page at evil.com posting to /api/send.
        v = self.loopback_post(origin="http://evil.com:4242",
                               host="evil.com:4242")
        self.assertEqual((v.status, v.code), (403, fb.auth.HOST_NOT_ALLOWED))

    def test_a_valid_token_is_no_excuse_for_a_foreign_host(self):
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                          now=1000.0, host="evil.com")
        self.assertEqual((v.status, v.code), (403, fb.auth.HOST_NOT_ALLOWED))

    # --- ordering: Host runs AFTER Origin ------------------------------------

    def test_a_disagreeing_origin_still_wins_with_cross_origin(self):
        """When `Origin` and `Host` DISAGREE, the origin guard answers first —
        it names the offending origin, which is the more useful message, and it
        preserves the exact code the existing suite asserts for that case."""
        v = self.loopback_get(origin="https://evil.example", host="evil.com")
        self.assertEqual((v.status, v.code), (403, fb.auth.CROSS_ORIGIN))

    # --- the budget: a host flood must not lock the real phone out -----------

    def test_a_foreign_host_flood_does_not_spend_the_budget(self):
        for _ in range(fb.auth.FAIL_BURST * 3):
            self.loopback_get(host="evil.com")
        _, token = self.mint()
        v = fb.auth.check(TAILNET, f"Bearer {token}", "GET", "/api/state",
                          now=1000.0, host="127.0.0.1:4242")
        self.assertTrue(v.ok, v)

    # --- the refusal is evidence ---------------------------------------------

    def test_a_foreign_host_refusal_is_audited(self):
        self.loopback_get(host="evil.com")
        self.assertEqual(fb.auth.read_audit()[0]["outcome"], "refuse")


if __name__ == "__main__":
    unittest.main()
