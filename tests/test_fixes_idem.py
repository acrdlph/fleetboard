#!/usr/bin/env python3
"""IDEM fixes — wire idempotency for mutations (ARCHITECTURE §5.6).

    python3 -m unittest tests.test_fixes_idem -v

Wave 1's in-memory locks stop a concurrent double-tap; they do NOT stop a
background retry that lands after `./start.sh` restarted the server. This pins
the persisted, boot-tagged reservation that does:

  * the full response table driven through `idem.begin`/`idem.complete` —
    unseen -> proceed, replay on a settled key, 409 operation_in_flight (same
    boot), 409 operation_indeterminate (a different BOOT_ID), 422
    idempotency_key_reused (same key, different body), 409 expired;
  * persistence across a simulated restart (write a reservation, swap BOOT_ID,
    reload from disk, assert indeterminate) — the exact retry-after-restart the
    feature exists for;
  * `complete` is first-wins, so a success whose socket write failed keeps its
    stored 200;
  * the `server.do_POST` wiring: a keyed mutation replays byte-identically and
    never re-runs its handler, a keyed handler exception settles the key so the
    retry replays the 500, and — the backward-compat contract — a KEYLESS
    request runs exactly as before and persists nothing.
"""

import http.client
import io
import json
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestra as fb  # noqa: E402


T0 = 1_000_000.0        # a fixed clock so the 900 s expiry math is exact


# ----------------------------------------------------------- a wired Handler

def _handler(path, body=b"", command="POST", **headers):
    """A `Handler` on in-memory buffers (the test_fixes_server idiom): built with
    `__new__` so `parse_request`/`auth.check` never runs — every request here is
    one the door already let in."""
    h = fb.Handler.__new__(fb.Handler)
    h.path = path
    h.command = command
    h.requestline = f"{command} {path} HTTP/1.0"
    h.request_version = "HTTP/1.0"
    h.protocol_version = "HTTP/1.0"
    h.client_address = ("127.0.0.1", 54321)
    h.close_connection = False
    h.headers = http.client.HTTPMessage()
    for key, val in headers.items():
        h.headers[key.replace("_", "-")] = str(val)
    h.rfile = io.BytesIO(body)
    h.wfile = io.BytesIO()
    return h


def _parse(h):
    """(status:int, headers:{lower->value}, body:dict) off the response buffer."""
    raw = h.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split(b" ")[1])
    hdrs = {}
    for line in lines[1:]:
        k, _, v = line.partition(b":")
        hdrs[k.decode().strip().lower()] = v.decode().strip()
    try:
        parsed = json.loads(body.decode()) if body else {}
    except ValueError:
        parsed = {}
    return status, hdrs, parsed


class IdemBase(unittest.TestCase):
    """Point the store at a throwaway file and forget every record between
    tests — the store is process-wide and would otherwise leak across cases."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._store = fb.idem.IDEM_STORE
        self._boot = fb.idem.BOOT_ID
        fb.idem.IDEM_STORE = Path(self._tmp) / "idem.store.json"
        fb.idem._reset()

    def tearDown(self):
        fb.idem.IDEM_STORE = self._store
        fb.idem.BOOT_ID = self._boot
        fb.idem._reset()


# ------------------------------------------------------- the response table

class TestResponseTable(IdemBase):
    """`idem.begin`/`idem.complete` driven directly — every row of §5.6."""

    PAY = {"mission": "land the branch", "worktree": "alpha"}

    def _begin(self, key, payload=None, issued_at=None, now=T0):
        return fb.idem.begin(key, "POST", "/api/dispatch",
                             self.PAY if payload is None else payload,
                             issued_at, now)

    def test_first_sighting_proceeds_and_persists_write_ahead(self):
        verdict, data = self._begin("k1")
        self.assertEqual((verdict, data), ("proceed", None))
        # The reservation is on disk BEFORE any side effect — the whole point.
        on_disk = json.loads(fb.idem.IDEM_STORE.read_text())
        rec = on_disk["records"]["k1"]
        self.assertFalse(rec["done"])
        self.assertEqual(rec["boot"], fb.idem.BOOT_ID)

    def test_done_same_fingerprint_replays_stored_response(self):
        self._begin("k1")
        fb.idem.complete("k1", 200, {"ok": True, "job": "j7"})
        self.assertEqual(self._begin("k1"),
                         ("replay", (200, {"ok": True, "job": "j7"})))

    def test_in_flight_same_boot_is_operation_in_flight_with_retry_after(self):
        self._begin("k1")                     # in flight, never completed
        verdict, data = self._begin("k1")
        status, code, _msg, extra = data
        self.assertEqual(verdict, "reject")
        self.assertEqual((status, code), (409, "operation_in_flight"))
        self.assertEqual(extra.get("Retry-After"), "1")

    def test_in_flight_different_boot_is_operation_indeterminate(self):
        self._begin("k1")                     # in flight under this boot
        fb.idem.BOOT_ID = "restarted-boot"    # the server came back different
        verdict, data = self._begin("k1")
        status, code, _msg, _extra = data
        self.assertEqual(verdict, "reject")
        self.assertEqual((status, code), (409, "operation_indeterminate"))

    def test_done_different_fingerprint_is_key_reused(self):
        self._begin("k1")
        fb.idem.complete("k1", 200, {"ok": True})
        verdict, data = self._begin("k1", payload={"mission": "something else"})
        status, code, _msg, _extra = data
        self.assertEqual(verdict, "reject")
        self.assertEqual((status, code), (422, "idempotency_key_reused"))

    def test_issued_at_older_than_900s_is_expired(self):
        verdict, data = self._begin("k1", issued_at=T0 - 901, now=T0)
        status, code, _msg, _extra = data
        self.assertEqual(verdict, "reject")
        self.assertEqual((status, code), (409, "expired"))

    def test_issued_at_within_the_window_proceeds(self):
        verdict, _ = self._begin("k1", issued_at=T0 - 899, now=T0)
        self.assertEqual(verdict, "proceed")

    def test_complete_is_first_wins(self):
        # A success whose socket write later failed must keep its stored 200,
        # never be overwritten by a follow-on 500.
        self._begin("k1")
        fb.idem.complete("k1", 200, {"ok": True, "job": "kept"})
        fb.idem.complete("k1", 500, {"ok": False, "error": "internal"})
        self.assertEqual(self._begin("k1"),
                         ("replay", (200, {"ok": True, "job": "kept"})))

    def test_distinct_keys_do_not_contend(self):
        self.assertEqual(self._begin("k1")[0], "proceed")
        self.assertEqual(self._begin("k2")[0], "proceed")


class TestPersistenceAcrossRestart(IdemBase):
    """The reservation must survive the process, or the restart hole is open."""

    def test_reload_from_disk_then_different_boot_is_indeterminate(self):
        fb.idem.begin("k1", "POST", "/api/dispatch", {"m": "x"}, None, T0)
        # Simulate `./start.sh`: a new process, new BOOT_ID, records reloaded
        # from the file the old process wrote.
        fb.idem.BOOT_ID = "a-different-boot"
        fb.idem._reset()
        verdict, data = fb.idem.begin("k1", "POST", "/api/dispatch",
                                      {"m": "x"}, None, T0 + 5)
        self.assertEqual(verdict, "reject")
        self.assertEqual(data[1], "operation_indeterminate")

    def test_a_missing_store_loads_empty_and_a_corrupt_one_does_not_crash(self):
        fb.idem._reset()
        self.assertEqual(fb.idem.begin("k9", "POST", "/api/dispatch",
                                       {}, None, T0)[0], "proceed")
        fb.idem.IDEM_STORE.write_text("{ this is not json")
        fb.idem._reset()
        # corrupt file -> start empty, never raise
        self.assertEqual(fb.idem.begin("k9", "POST", "/api/dispatch",
                                       {}, None, T0)[0], "proceed")

    def test_eviction_keeps_the_store_bounded(self):
        fb.idem.begin("old", "POST", "/api/dispatch", {}, None, T0)
        # A begin far past the TTL evicts the stale record on access, so the
        # store stays bounded without a sweeper and the key is unseen again.
        verdict, _ = fb.idem.begin("fresh", "POST", "/api/dispatch", {}, None,
                                   T0 + fb.idem.IDEM_TTL_S + 10)
        self.assertEqual(verdict, "proceed")
        self.assertNotIn("old", fb.idem._records)


# ------------------------------------------------------ the do_POST wiring

class TestHandlerWiring(IdemBase):
    """server.do_POST: keyed mutations gated, keyless requests untouched."""

    def setUp(self):
        super().setUp()
        self._start_dispatch = fb.dispatch.start_dispatch
        self._send = fb.terminal.send_to_process

    def tearDown(self):
        fb.dispatch.start_dispatch = self._start_dispatch
        fb.terminal.send_to_process = self._send
        super().tearDown()

    def test_keyless_dispatch_runs_normally_and_persists_nothing(self):
        # THE backward-compat contract: no key => the handler runs, the response
        # carries no replay header, and not a byte is written to the store.
        calls = []
        fb.dispatch.start_dispatch = lambda *a, **k: calls.append(a) or {"ok": True}
        body = b'{"mission": "x"}'
        h = _handler("/api/dispatch", body=body, Content_Length=str(len(body)))
        h.do_POST()
        status, hdrs, payload = _parse(h)
        self.assertEqual(status, 200)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("idempotent-replay", hdrs)
        self.assertFalse(fb.idem.IDEM_STORE.exists())
        self.assertEqual(fb.idem._records, {})

    def test_keyed_dispatch_proceeds_then_replays_without_re_executing(self):
        calls = []
        fb.dispatch.start_dispatch = \
            lambda *a, **k: calls.append(a) or {"ok": True, "job": "j1"}
        body = b'{"mission": "x"}'

        h1 = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                      Idempotency_Key="KEY-A")
        h1.do_POST()
        s1, hd1, p1 = _parse(h1)
        self.assertEqual(s1, 200)
        self.assertEqual(hd1.get("idempotent-replay"), "false")
        self.assertEqual(len(calls), 1)

        # The retry: same key, same body. The handler must NOT run again, and
        # the stored body comes back byte-identical under Idempotent-Replay:true.
        h2 = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                      Idempotency_Key="KEY-A")
        h2.do_POST()
        s2, hd2, p2 = _parse(h2)
        self.assertEqual(s2, 200)
        self.assertEqual(hd2.get("idempotent-replay"), "true")
        self.assertEqual(len(calls), 1)          # never re-executed
        self.assertEqual(p1, p2)

    def test_keyed_handler_exception_settles_the_key_and_replays_the_500(self):
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise RuntimeError("dispatch blew up")
        fb.dispatch.start_dispatch = boom
        body = b'{"mission": "x"}'

        h1 = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                      Idempotency_Key="KEY-B")
        h1.do_POST()
        s1, _hd1, p1 = _parse(h1)
        self.assertEqual(s1, 500)
        self.assertEqual(len(calls), 1)

        # The retry replays the settled failure — it does NOT re-run the handler
        # that already had its side effect go wrong.
        h2 = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                      Idempotency_Key="KEY-B")
        h2.do_POST()
        s2, hd2, p2 = _parse(h2)
        self.assertEqual(s2, 500)
        self.assertEqual(hd2.get("idempotent-replay"), "true")
        self.assertEqual(len(calls), 1)          # never re-executed
        self.assertEqual(p1, p2)

    def test_keyed_in_flight_is_refused_without_running_the_handler(self):
        calls = []
        fb.dispatch.start_dispatch = lambda *a, **k: calls.append(a) or {"ok": True}
        # Seed an in-flight reservation for this exact request, never completed.
        # Real clock: the Handler's own begin runs at time.time(), and a T0 seed
        # would age past the TTL and be evicted before it could match.
        fb.idem.begin("KEY-C", "POST", "/api/dispatch", {"mission": "x"},
                      None, fb_now())
        body = b'{"mission": "x"}'
        h = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                     Idempotency_Key="KEY-C")
        h.do_POST()
        status, hdrs, payload = _parse(h)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "operation_in_flight")
        self.assertTrue(payload["retriable"])
        self.assertEqual(hdrs.get("retry-after"), "1")
        self.assertEqual(calls, [])              # the handler never ran

    def test_keyed_indeterminate_is_refused_and_marked_not_retriable(self):
        fb.dispatch.start_dispatch = lambda *a, **k: {"ok": True}
        fb.idem.begin("KEY-D", "POST", "/api/dispatch", {"mission": "x"},
                      None, fb_now())
        fb.idem.BOOT_ID = "server-restarted"     # the reservation is from before
        body = b'{"mission": "x"}'
        h = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                     Idempotency_Key="KEY-D")
        h.do_POST()
        status, _hdrs, payload = _parse(h)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "operation_indeterminate")
        self.assertFalse(payload["retriable"])

    def test_keyed_expired_issued_at_is_refused_before_the_handler(self):
        calls = []
        fb.dispatch.start_dispatch = lambda *a, **k: calls.append(a) or {"ok": True}
        body = b'{"mission": "x"}'
        h = _handler("/api/dispatch", body=body, Content_Length=str(len(body)),
                     Idempotency_Key="KEY-E",
                     Idempotency_Issued_At=str(fb_now() - 1000))
        h.do_POST()
        status, _hdrs, payload = _parse(h)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "expired")
        self.assertEqual(calls, [])

    def test_a_keyed_read_or_hook_route_is_not_gated(self):
        # `/api/hook` is a mutation-shaped POST but has no side effect a retry
        # doubles, so a key on it changes nothing: it runs, no reservation, no
        # replay header.
        fb.observer.hook = lambda *a, **k: None
        body = b'{"session_id": "s"}'
        h = _handler("/api/hook", body=body, Content_Length=str(len(body)),
                     Idempotency_Key="KEY-F")
        h.do_POST()
        status, hdrs, _payload = _parse(h)
        self.assertEqual(status, 200)
        self.assertNotIn("idempotent-replay", hdrs)
        self.assertFalse(fb.idem.IDEM_STORE.exists())


def fb_now():
    import time
    return time.time()


if __name__ == "__main__":
    unittest.main()
