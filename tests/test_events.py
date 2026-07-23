#!/usr/bin/env python3
"""`GET /api/events` — the state stream (ADR 0005, ENGINE.md §5.2/§5.3).

Two harnesses, because the route has two halves that fail differently.

Most of this file drives `Handler` DIRECTLY, with a fake wire in place of the
socket: the handler object is built by hand, `do_GET` runs on a thread, and
every frame arrives on a queue. Nothing here waits on the clock to decide a
verdict — a frame either arrives before a generous timeout or the test fails,
and the interesting claims ("no frame when the version did not move") are made
as ORDERING assertions, which cannot be satisfied by waiting longer.

The rest boots a real `Server` on a real port, because three of the claims are
about the socket and cannot be faked: that a rude disconnect gives the thread
and the slot back, that a concurrent `GET /api/state` is not starved while
streams are held open, and that the bytes are genuinely incremental rather than
buffered until close.

    python3 -m unittest discover -s tests
"""

import contextlib
import http.client
import io
import json
import queue
import socket
import socketserver
import struct
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402


def state(at, *, cards=(("alpha", 0),), counts=None, other_cpu=0.5):
    """A `collect_state()` result, hand-built. `publish` reads four keys."""
    return {
        "generated_at": at,
        "counts": counts or {"working": len(cards)},
        "worktrees": [
            {"name": name, "availability": "busy",
             "git": {"branch": "main", "dirty": dirty},
             "sessions": [{"sid": f"s-{name}", "status": "working",
                           "last_write_at": 1000.0}],
             "live_procs": []}
            for name, dirty in cards],
        "other_procs": [{"pid": 9, "cpu": other_cpu, "etime": "01:00",
                         "cwd": "/elsewhere"}],
    }


# ------------------------------------------------------------------- the wire

class Wire(io.RawIOBase):
    """The socket's WRITE side, minus the socket: one write in, one queue item.

    The read side is a real `socketpair` (see `StreamGuard.handler`), because
    the stream also asks whether its peer is still there without writing, and
    that question can only be asked of a real fd.

    Two ways for a client to die, and they are not the same failure:
      * `hang_up` — the write fails, which is what a phone that RSTs looks like
      * `vanish`  — the peer closes but nothing is ever written to it, which is
        what a phone that drops on a QUIET fleet looks like, and the one the
        keepalive alone would take a full interval to notice
    """

    def __init__(self, peer=None):
        self.q = queue.Queue()
        self.dead = threading.Event()
        self.peer = peer

    def writable(self):
        return True

    def write(self, blob):
        if self.dead.is_set():
            raise BrokenPipeError(32, "Broken pipe")
        self.q.put(bytes(blob))
        return len(blob)

    def vanish(self):
        self.peer.close()

    def hang_up(self):
        self.dead.set()
        if self.peer is not None:
            self.peer.close()

    def next(self, timeout=5.0):
        """The next write, decoded. A timeout here IS the failure."""
        return self.q.get(timeout=timeout).decode()

    def nothing_within(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None


def drained(deadline_s=5.0):
    """Wait for the process-wide census to reach zero, and say whether it did.

    The census outlives a test case, so a stream still unwinding when the next
    `setUp` zeroes it decrements past zero and the following assertion reads a
    NEGATIVE open count. That is not a flake to retry — it is one test's
    teardown leaking into another's arithmetic, so every class here drains
    before it hands the process on.
    """
    end = time.time() + deadline_s
    while fb.sse_stats()["open"] and time.time() < end:
        time.sleep(0.01)
    return fb.sse_stats()["open"] == 0


def frame(text):
    """One SSE frame -> (id, event, parsed data). Raises if it is malformed."""
    fields = {}
    for line in text.rstrip("\n").split("\n"):
        key, _, val = line.partition(": ")
        fields[key] = val
    return int(fields["id"]), fields["event"], json.loads(fields["data"])


class StreamGuard(unittest.TestCase):
    """Every test here installs its own Observer and restores the process's.

    `watch` off: an Observer built with the developer's real config would open
    a kqueue on the developer's real fleet, and every assertion below would
    become a function of what their agents happened to be doing. The census in
    `server._subs` is process-wide too, so it is reset per test.
    """

    def setUp(self):
        self._cfg = dict(fb.CFG)
        self._glob = fb.observer._observer
        self._cache = dict(fb._cache)
        fb.CFG["watch"] = False
        fb.CFG["sse_keepalive_s"] = 30.0     # long: no test may pass on a tick
        fb.CFG["sse_liveness_s"] = 0.02      # …and it writes nothing, ever
        fb.server._sse_reset()
        self.obs = fb.Observer()
        fb.observer._observer = self.obs
        self.threads = []
        self._bump = 0

    def bump(self):
        """A publish guaranteed to move the version, whatever came before.

        Every stream still alive is parked in `wait_for`, and `wait_for` is
        woken by a VERSION, not by a publish — which is the property under test
        everywhere else in this file and therefore the property that has to be
        honoured when shutting the streams down. Hanging up a wire and
        republishing the same composed view wakes nobody and leaks the thread.
        """
        self._bump += 1
        self.obs.publish(state(9e9 + self._bump, cards=(("zzz", self._bump),)))

    def tearDown(self):
        for _h, wire, _t in self.threads:
            wire.hang_up()
        if self.threads:
            self.bump()
            for _h, _w, t in self.threads:
                t.join(timeout=5.0)
        left = drained()
        fb.CFG.clear(); fb.CFG.update(self._cfg)
        fb.observer._observer = self._glob
        fb._cache.update(self._cache)
        fb.server._sse_reset()
        self.assertTrue(left, "a stream outlived its test")

    # ------------------------------------------------------------- harness

    def handler(self, path="/api/events", **headers):
        h = fb.Handler.__new__(fb.Handler)
        h.path = path
        h.command = "GET"
        h.requestline = f"GET {path} HTTP/1.1"
        h.request_version = "HTTP/1.1"
        h.client_address = ("127.0.0.1", 54321)
        h.close_connection = True
        h.headers = http.client.HTTPMessage()
        for key, val in headers.items():
            h.headers[key.replace("_", "-")] = str(val)
        # A real socketpair for `connection`: the liveness check selects and
        # peeks on it, and neither can be faked with an object. The write side
        # stays a Wire so frames remain inspectable one at a time.
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        h.connection = ours
        h.wfile = Wire(peer=theirs)
        h.rfile = io.BytesIO()
        return h

    def connect(self, **headers):
        """Open a stream on a thread; return (wire, its response head)."""
        h = self.handler(**headers)
        t = threading.Thread(target=h.do_GET, daemon=True,
                             name="test-sse")
        t.start()
        self.threads.append((h, h.wfile, t))
        return h.wfile, h.wfile.next()

    def refused(self, **headers):
        """A stream that never starts: run it and read what came back.

        That it RETURNS is half the assertion. A refusal is a response, not a
        held socket, so this runs on a thread with a join — a `_sse` that
        streamed where it should have refused would otherwise block the suite
        forever, and a test that hangs cannot tell anyone what it found.
        """
        h = self.handler(**headers)
        t = threading.Thread(target=h.do_GET, daemon=True, name="test-sse")
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive(), "a refusal must not hold the socket open")
        out = h.wfile.q.get(timeout=5.0)
        while True:
            got = h.wfile.nothing_within(0.05)
            if got is None:
                return out.decode(errors="replace")
            out += got


# ----------------------------------------------------------- what arrives

class TestTheStream(StreamGuard):

    def test_a_new_subscriber_gets_the_whole_snapshot_before_anything_moves(self):
        self.obs.publish(state(1000.0, cards=(("alpha", 0), ("beta", 1))))
        wire, head = self.connect()
        self.assertIn("HTTP/1.0 200 OK", head)
        self.assertIn("Content-Type: text/event-stream", head)
        fid, event, data = frame(wire.next())
        self.assertEqual((fid, event), (1, "state"))
        self.assertEqual(data["type"], "snapshot")
        self.assertEqual(set(data["cards"]), {"alpha", "beta"})
        self.assertIn("counts", data)
        self.assertIn("freshness", data)

    def test_the_stream_never_sends_a_content_length(self):
        # the shared tail in do_GET ends in an unconditional Content-Length,
        # and a stream carrying one hangs EventSource forever
        self.obs.publish(state(1000.0))
        _, head = self.connect()
        self.assertNotIn("Content-Length", head)
        self.assertIn("Cache-Control: no-store", head)

    def test_one_frame_per_version_bump(self):
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        self.assertEqual(frame(wire.next())[0], 1)
        for i, ver in enumerate((2, 3, 4), start=1):
            self.obs.publish(state(1000.0 + i, cards=(("alpha", i),)))
            fid, _, data = frame(wire.next())
            self.assertEqual(fid, ver)
            self.assertEqual(data["v"], ver)
            self.assertEqual(data["cards"]["alpha"]["git"]["dirty"], i)

    def test_a_publish_that_does_not_move_the_version_sends_nothing(self):
        """§3.2 is the whole point: a sweep that found nothing is silence.

        Asserted by ORDER, not by waiting — the next frame after two no-bump
        publishes must be v=2. A stream that emitted anything for them would
        deliver v=1 twice and fail here however long the test ran.
        """
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        self.assertEqual(frame(wire.next())[0], 1)
        self.obs.publish(state(1001.0))          # same composed view
        self.obs.publish(state(1002.0))          # …and again
        self.assertEqual(self.obs.snapshot().v, 1)
        self.assertIsNone(wire.nothing_within(0.3))
        self.obs.publish(state(1003.0, cards=(("alpha", 7),)))
        self.assertEqual(frame(wire.next())[0], 2)

    def test_an_idle_stream_gets_a_keepalive_comment(self):
        fb.CFG["sse_keepalive_s"] = 0.05
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        self.assertEqual(frame(wire.next())[0], 1)
        self.assertEqual(wire.next(), ": keepalive\n\n")

    def test_a_keepalive_is_not_a_frame(self):
        """It must carry no `id:` — a client that took one as a cursor would
        resume from a version that does not exist."""
        fb.CFG["sse_keepalive_s"] = 0.05
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        beat = wire.next()
        self.assertTrue(beat.startswith(":"))
        self.assertNotIn("id:", beat)
        self.assertNotIn("data:", beat)


# ------------------------------------------------------------------ resume

class TestResume(StreamGuard):

    def wind(self, n):
        for i in range(n):
            self.obs.publish(state(1000.0 + i, cards=(("alpha", i), ("beta", 0))))

    def test_last_event_id_inside_the_ring_resumes_with_a_delta(self):
        self.wind(3)
        wire, _ = self.connect(Last_Event_ID=2)
        fid, _, data = frame(wire.next())
        self.assertEqual(fid, 3)
        self.assertEqual(data["type"], "delta")
        self.assertEqual(data["base"], 2)
        self.assertEqual(set(data["cards"]), {"alpha"})     # beta never moved

    def test_a_resumed_stream_carries_on_from_its_cursor(self):
        self.wind(3)
        wire, _ = self.connect(Last_Event_ID=2)
        self.assertEqual(frame(wire.next())[0], 3)
        self.obs.publish(state(2000.0, cards=(("alpha", 9), ("beta", 0))))
        self.assertEqual(frame(wire.next())[0], 4)

    def test_a_client_already_at_the_current_version_is_answered_at_once(self):
        """A reconnect that missed nothing still gets an answer immediately.

        This is the case the eager first frame exists for, and the only one:
        for any cursor BEHIND the server the loop's own `wait_for` returns at
        once and would send the same frame, so dropping the eager send is
        invisible there. At `cursor == v` it is not — `wait_for` blocks, and a
        client that reconnected across a blip would hear nothing at all until
        the fleet next changed. On a quiet board that is a whole keepalive of
        silence and reads as a stream that never started.
        """
        self.wind(3)
        wire, _ = self.connect(Last_Event_ID=3)
        fid, _, data = frame(wire.next())
        self.assertEqual((fid, data["type"], data["cards"]), (3, "delta", {}))

    def test_a_cursor_older_than_the_ring_gets_a_full_snapshot(self):
        self.wind(fb.observer.HIST + 20)
        wire, _ = self.connect(Last_Event_ID=1)
        _, _, data = frame(wire.next())
        self.assertEqual(data["type"], "snapshot")
        self.assertEqual(set(data["cards"]), {"alpha", "beta"})

    def test_a_cursor_ahead_of_the_server_is_pulled_back_not_stranded(self):
        """A restarted server counts from 1 again. A client holding v=900 would
        otherwise wait forever for a version that is now years away."""
        self.wind(3)
        wire, _ = self.connect(Last_Event_ID=900)
        fid, _, data = frame(wire.next())
        self.assertEqual((fid, data["type"]), (3, "snapshot"))
        self.obs.publish(state(2000.0, cards=(("alpha", 9), ("beta", 0))))
        self.assertEqual(frame(wire.next())[0], 4)   # …and moves on from 3

    def test_a_garbled_cursor_is_an_unknown_cursor(self):
        self.wind(3)
        wire, _ = self.connect(Last_Event_ID="banana")
        self.assertEqual(frame(wire.next())[2]["type"], "snapshot")

    def test_no_cursor_at_all_is_a_snapshot(self):
        self.wind(3)
        wire, _ = self.connect()
        self.assertEqual(frame(wire.next())[2]["type"], "snapshot")


# -------------------------------------------------------------- the refusals

class TestRefusals(StreamGuard):

    def test_the_cap_refuses_the_next_subscriber_with_a_clear_503(self):
        fb.CFG["sse_max_subscribers"] = 2
        self.obs.publish(state(1000.0))
        for _ in range(2):
            wire, _ = self.connect()
            wire.next()
        self.assertEqual(fb.sse_stats()["open"], 2)
        out = self.refused()
        self.assertIn("503", out.split("\n")[0])
        self.assertIn("too many SSE subscribers", out)
        self.assertIn("at most 2", out)
        self.assertIn("/api/state", out)            # …and what to do instead

    def test_a_refused_subscriber_consumes_no_slot(self):
        fb.CFG["sse_max_subscribers"] = 1
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        for _ in range(5):
            self.refused()
        st = fb.sse_stats()
        self.assertEqual((st["open"], st["peak"], st["rejected"]), (1, 1, 5))

    def test_a_slot_freed_by_a_drop_is_reusable(self):
        fb.CFG["sse_max_subscribers"] = 1
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        self.assertIn("503", self.refused())
        _h, w, t = self.threads.pop()
        w.hang_up()
        self.obs.publish(state(1001.0, cards=(("alpha", 4),)))
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(fb.sse_stats()["open"], 0)
        wire2, head = self.connect()                # the slot is free again
        self.assertIn("200 OK", head)

    def test_no_sweep_running_is_a_503_and_not_a_held_socket(self):
        fb.observer._observer = None
        out = self.refused()
        self.assertIn("503", out.split("\n")[0])
        self.assertIn("no observer", out)
        self.assertEqual(fb.sse_stats()["open"], 0)


# ------------------------------------------------------------------ teardown

class TestTeardown(StreamGuard):

    def test_a_dropped_client_releases_its_slot_and_ends_its_thread(self):
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        self.assertEqual(fb.sse_stats()["open"], 1)
        h, w, t = self.threads.pop()
        w.hang_up()                                   # gone, with no goodbye
        self.obs.publish(state(1001.0, cards=(("alpha", 3),)))
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(fb.sse_stats()["open"], 0)

    def test_a_client_that_vanishes_in_silence_is_noticed_without_a_write(self):
        """The quiet fleet is the normal case, and the one that was broken.

        Nothing is published and no keepalive is due for 30 s, so the ONLY way
        this stream can learn its client is gone is by asking. Measured before
        the check existed, 12 rude RSTs: not one slot came back inside 20 s,
        because the bound was the whole `sse_keepalive_s` interval — and every
        one of those slots counts against `sse_max_subscribers` meanwhile.
        """
        fb.CFG["sse_keepalive_s"] = 30.0
        fb.CFG["sse_liveness_s"] = 0.02
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        _h, w, t = self.threads.pop()
        t0 = time.perf_counter()
        w.vanish()                       # the peer goes; the write side is fine
        t.join(timeout=5.0)
        took = (time.perf_counter() - t0) * 1000.0
        self.assertFalse(t.is_alive(), "a silent stream never noticed its client")
        self.assertLess(took, 5000.0)    # not the 30 s keepalive
        self.assertEqual(fb.sse_stats()["open"], 0)
        self.assertIsNone(w.nothing_within(0.05))   # …and it wrote nothing to ask

    def test_a_client_that_speaks_is_not_mistaken_for_a_dead_one(self):
        """Readability is not death. A byte from the client is unexpected —
        EventSource never sends one — but it is not a reason to hang up.

        Asserted through the KEEPALIVE, not through the next frame. Publishing
        and waiting for the frame proves nothing: `wait_for` returns the moment
        the version moves, so the liveness check never runs and a stream that
        treated any readable byte as death would pass anyway. A keepalive can
        only be reached via QUIET, and QUIET requires every liveness slice in
        the interval to have looked at this socket and found somebody there.
        """
        fb.CFG["sse_liveness_s"] = 0.01
        fb.CFG["sse_keepalive_s"] = 0.15       # ~15 liveness checks per beat
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        wire.peer.sendall(b"hello?")
        self.assertEqual(wire.next(), ": keepalive\n\n")
        self.assertEqual(wire.next(), ": keepalive\n\n")
        self.bump()
        self.assertEqual(frame(wire.next())[0], 2)   # …and still streaming

    def test_a_client_that_dies_on_the_keepalive_is_reclaimed_too(self):
        """The quiet path matters more than the busy one: a phone that drops
        overnight is never written to by a frame, only by a heartbeat."""
        fb.CFG["sse_keepalive_s"] = 0.05
        self.obs.publish(state(1000.0))
        wire, _ = self.connect()
        wire.next()
        h, w, t = self.threads.pop()
        w.hang_up()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(fb.sse_stats()["open"], 0)

    @staticmethod
    def _live():
        """Only OUR stream threads. `active_count()` would fold in whatever the
        rest of the suite left dying, and a leak test that can be perturbed by
        an unrelated thread proves nothing about this one."""
        return [t for t in threading.enumerate() if t.name == "test-sse"]

    def test_twelve_rude_disconnects_leave_nothing_behind(self):
        """ADR 0005 measured this; it is re-measured against THIS loop."""
        fb.CFG["sse_max_subscribers"] = 32
        self.obs.publish(state(1000.0))
        self.assertEqual(self._live(), [])
        for _ in range(12):
            wire, _ = self.connect()
            wire.next()
        self.assertEqual(fb.sse_stats()["open"], 12)
        self.assertEqual(len(self._live()), 12)          # one thread per client
        opened = list(self.threads)
        self.threads.clear()
        for _h, w, _t in opened:
            w.hang_up()
        self.obs.publish(state(1001.0, cards=(("alpha", 5),)))
        for _h, _w, t in opened:
            t.join(timeout=5.0)
        self.assertEqual([t.is_alive() for _h, _w, t in opened], [False] * 12)
        self.assertEqual(fb.sse_stats()["open"], 0)
        self.assertEqual(self._live(), [])               # fully reclaimed


# ---------------------------------------------------------- the server class

class TestServerClass(unittest.TestCase):

    @staticmethod
    def _raise_into_handle_error(exc):
        """What socketserver does: call handle_error inside an except block."""
        srv = fb.Server.__new__(fb.Server)
        buf = io.StringIO()
        try:
            raise exc
        except BaseException:
            with contextlib.redirect_stderr(buf):
                srv.handle_error(("sock",), ("127.0.0.1", 1))
        return buf.getvalue()

    def test_a_dropped_peer_prints_no_traceback(self):
        # every tailnet blip raises one of these; without the override each one
        # spams a full stack trace and the log stops being worth reading
        for exc in (ConnectionResetError(54, "Connection reset by peer"),
                    BrokenPipeError(32, "Broken pipe")):
            self.assertEqual(self._raise_into_handle_error(exc), "")

    def test_a_real_error_still_gets_its_traceback(self):
        out = self._raise_into_handle_error(ValueError("not a socket death"))
        self.assertIn("ValueError", out)
        self.assertIn("not a socket death", out)

    def test_the_listen_backlog_is_raised_above_the_stdlib_default(self):
        self.assertEqual(socketserver.TCPServer.request_queue_size, 5)
        self.assertEqual(fb.Server.request_queue_size, 256)

    def test_daemon_threads_stays_on(self):
        # socketserver._Threads.append early-returns on daemon threads; without
        # this, 32 long-lived streams accumulate in the join list server_close
        # walks and shutdown blocks until every phone hangs up
        self.assertTrue(fb.Server.daemon_threads)


# -------------------------------------------------------------- on the wire

class Sub(io.IOBase):
    """One live subscriber on a real socket, with the response head split off
    and everything after it kept for the frame reader."""

    def __init__(self, sock):
        self.sock, self.buf = sock, b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += sock.recv(65536)
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        self.head = head.decode()

    def frame(self):
        while b"\n\n" not in self.buf:
            got = self.sock.recv(65536)
            if not got:
                raise AssertionError("stream closed mid-frame")
            self.buf += got
        one, _, self.buf = self.buf.partition(b"\n\n")
        return frame(one.decode() + "\n")


class TestOnTheWire(unittest.TestCase):
    """A real socket, because these four claims are about the socket."""

    def setUp(self):
        self._cfg = dict(fb.CFG)
        self._glob = fb.observer._observer
        self._cache = dict(fb._cache)
        self._demo = fb.config.DEMO
        fb.CFG["watch"] = False
        fb.CFG["sse_keepalive_s"] = 30.0
        fb.CFG["sse_liveness_s"] = 0.05
        fb.CFG["sse_max_subscribers"] = 32
        fb.server._sse_reset()
        # DEMO for `/api/state` only: with no sweep thread that route would run
        # a full synchronous collect against the DEVELOPER'S real fleet, which
        # is minutes of git and a different machine's answer every run. The
        # stream reads `observer._observer` directly and is untouched by it.
        fb.config.DEMO = True
        self.obs = fb.Observer()
        fb.observer._observer = self.obs
        self.obs.publish(state(1000.0))
        self.srv = fb.Server(("127.0.0.1", 0), fb.Handler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()
        self.socks = []

    def tearDown(self):
        for s in self.socks:
            try:
                s.close()
            except OSError:
                pass
        left = drained()
        self.srv.shutdown()
        self.srv.server_close()
        fb.CFG.clear(); fb.CFG.update(self._cfg)
        fb.observer._observer = self._glob
        fb._cache.update(self._cache)
        fb.config.DEMO = self._demo
        fb.server._sse_reset()
        self.assertTrue(left, "a stream outlived its test")

    def subscribe(self, last=None):
        """An EventSource, by hand. Returns a `Sub` that keeps its own buffer —
        the header block and the snapshot-on-connect routinely arrive in one
        segment, and a reader that threw the remainder away would silently lose
        the first frame."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.socks.append(s)
        req = "GET /api/events HTTP/1.1\r\nHost: localhost\r\n"
        if last is not None:
            req += f"Last-Event-ID: {last}\r\n"
        s.sendall((req + "\r\n").encode())
        return Sub(s)

    def test_the_bytes_are_incremental_not_buffered_until_close(self):
        sub = self.subscribe()
        self.assertTrue(sub.head.startswith("HTTP/1.0 200 OK"))
        self.assertIn("text/event-stream", sub.head)
        self.assertNotIn("Content-Length", sub.head)
        self.assertEqual(sub.frame()[0], 1)     # the snapshot on connect
        seen = []
        for i in range(1, 4):
            self.obs.publish(state(1000.0 + i, cards=(("alpha", i),)))
            seen.append(sub.frame()[0])         # …read before the next is sent
        self.assertEqual(seen, [2, 3, 4])

    def test_a_state_request_is_not_starved_by_open_streams(self):
        for _ in range(12):
            self.subscribe()
        self.assertEqual(fb.sse_stats()["open"], 12)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        t0 = time.perf_counter()
        conn.request("GET", "/api/state")
        body = conn.getresponse().read()
        ms = (time.perf_counter() - t0) * 1000.0
        conn.close()
        self.assertIn("worktrees", json.loads(body))
        self.assertLess(ms, 2000.0, f"/api/state took {ms:.1f} ms under 12 streams")

    def test_a_rude_disconnect_gives_the_slot_back(self):
        fb.CFG["sse_keepalive_s"] = 0.05
        subs = [self.subscribe() for _ in range(12)]
        self.assertEqual(fb.sse_stats()["open"], 12)
        for sub in subs:
            # RST, not FIN: the phone leaving the tailnet, not a tab closing.
            # A FIN would be a clean shutdown and would prove much less.
            sub.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                struct.pack("ii", 1, 0))
            sub.sock.close()
        deadline = time.time() + 10.0
        while fb.sse_stats()["open"] and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(fb.sse_stats()["open"], 0)

    def test_last_event_id_travels_over_the_wire(self):
        for i in range(1, 4):
            self.obs.publish(state(1000.0 + i, cards=(("alpha", i), ("beta", 0))))
        sub = self.subscribe(last=self.obs.snapshot().v - 1)
        _, _, data = sub.frame()
        self.assertEqual(data["type"], "delta")
        self.assertEqual(set(data["cards"]), {"alpha"})


if __name__ == "__main__":
    unittest.main()
