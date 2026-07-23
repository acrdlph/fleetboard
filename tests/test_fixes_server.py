"""Fixes for orchestra.server — HTTP substrate hardening.

These drive `Handler` DIRECTLY, calling `do_POST`/`do_GET` on an instance built
with `__new__` (so `parse_request`/`auth.check` never runs — every test here is
about what the handler does with a request the door already let in) and a
`BytesIO` in place of the wire. The response is parsed back out of that buffer.

Every test fails against the pre-fix handler:
  * F1/F5/F9/F10 — a huge/negative/garbled Content-Length was read unbounded;
  * F7 — a chunked body was silently treated as `{}`;
  * F3 — a non-dict payload or a bad `pid` dropped the connection with no reply;
  * F8 — `POST /api/dispatchlog` reached `start_dispatch`;
  * F4 — `_peer_gone` used `select.select`;
  * F2/F6 — `Handler.timeout` was `None`.
"""

import http.client
import io
import socket
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import orchestra as fb  # noqa: E402


def _handler(path, body=b"", command="POST", **headers):
    """A `Handler` wired to in-memory buffers, ready for `do_POST`/`do_GET`."""
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


def _status(h):
    """The numeric status off the response line the handler wrote."""
    line = h.wfile.getvalue().split(b"\r\n", 1)[0]
    return int(line.split(b" ")[1])


class TestBodyGuards(unittest.TestCase):

    def test_oversized_content_length_is_413_without_reading_the_body(self):
        # A body one byte over the cap is refused before a byte is read: the
        # read cursor never moves off zero, so gigabytes are never buffered.
        h = _handler("/api/hook", body=b"x" * 10,
                     Content_Length=str(fb.server.MAX_BODY + 1))
        h.do_POST()
        self.assertEqual(_status(h), 413)
        self.assertEqual(h.rfile.tell(), 0)
        self.assertTrue(h.close_connection)

    def test_a_body_at_the_cap_is_allowed(self):
        # The boundary is inclusive — exactly MAX_BODY is fine (here a tiny
        # object declared with a legal length well under the cap).
        body = b'{"session_id": "s"}'
        fb.observer.hook = lambda *a, **k: None      # mock seam: swallow the edge
        h = _handler("/api/hook", body=body, Content_Length=str(len(body)))
        h.do_POST()
        self.assertEqual(_status(h), 200)

    def test_negative_content_length_is_400_not_a_read_to_eof(self):
        h = _handler("/api/send", body=b"", Content_Length="-1")
        h.do_POST()
        self.assertEqual(_status(h), 400)
        self.assertTrue(h.close_connection)

    def test_garbled_content_length_is_400(self):
        h = _handler("/api/send", body=b"", Content_Length="not-a-number")
        h.do_POST()
        self.assertEqual(_status(h), 400)

    def test_chunked_transfer_encoding_is_411(self):
        h = _handler("/api/hook", body=b"", Transfer_Encoding="chunked")
        h.do_POST()
        self.assertEqual(_status(h), 411)
        self.assertTrue(h.close_connection)


class TestDispatcherGuard(unittest.TestCase):

    def test_a_non_dict_payload_is_coerced_not_a_dropped_connection(self):
        # `[1, 2]` is valid JSON; pre-fix `payload.get(...)` raised AttributeError
        # on every route. Now it is a request with no fields → a normal 200.
        body = b"[1, 2, 3]"
        fb.observer.hook = lambda *a, **k: None
        h = _handler("/api/hook", body=body, Content_Length=str(len(body)))
        h.do_POST()
        self.assertEqual(_status(h), 200)

    def test_a_route_that_raises_gets_a_500_envelope(self):
        # POST /api/send {"pid": "abc"} makes int("abc") raise inside the route;
        # pre-fix that aborted the connection with no response at all.
        body = b'{"pid": "abc", "text": "hi"}'
        h = _handler("/api/send", body=body, Content_Length=str(len(body)))
        h.do_POST()
        self.assertEqual(_status(h), 500)
        self.assertTrue(h.close_connection)

    def test_notify_token_ok_tolerates_a_non_str_token(self):
        self.assertFalse(fb.server.notify_token_ok(123))
        self.assertFalse(fb.server.notify_token_ok(None))
        self.assertTrue(fb.server.notify_token_ok("a" * 64))


class TestLegacyRoutingIsExact(unittest.TestCase):

    def test_post_dispatchlog_is_404_and_launches_nothing(self):
        launched = []
        fb.dispatch.start_dispatch = lambda *a, **k: launched.append(a) or {}
        body = b'{"mission": "x", "model": "m", "effort": "high"}'
        h = _handler("/api/dispatchlog", body=body,
                     Content_Length=str(len(body)))
        h.do_POST()
        self.assertEqual(_status(h), 404)
        self.assertEqual(launched, [])

    def test_post_dispatch_exact_still_reaches_start_dispatch(self):
        launched = []
        fb.dispatch.start_dispatch = lambda *a, **k: launched.append(a) or {"ok": True}
        body = b'{"mission": "x"}'
        h = _handler("/api/dispatch", body=body, Content_Length=str(len(body)))
        h.do_POST()
        self.assertEqual(_status(h), 200)
        self.assertEqual(len(launched), 1)


class TestPeerGoneUsesPoll(unittest.TestCase):

    def test_a_closed_peer_reads_as_gone(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        h = fb.Handler.__new__(fb.Handler)
        h.connection = ours
        theirs.close()                       # a clean FIN from the peer
        self.assertTrue(h._peer_gone())

    def test_a_quiet_live_peer_reads_as_present(self):
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        h = fb.Handler.__new__(fb.Handler)
        h.connection = ours
        self.assertFalse(h._peer_gone())     # nobody wrote, nobody left


class TestTimeoutIsSet(unittest.TestCase):

    def test_handler_declares_a_read_timeout(self):
        self.assertEqual(fb.Handler.timeout, 30)


if __name__ == "__main__":
    unittest.main()
