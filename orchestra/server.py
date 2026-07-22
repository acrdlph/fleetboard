"""orchestra.server — the only door in: one handler, one check, no framework.

THE DOOR IS NOW LOCKED. `parse_request` runs `auth.check` before any `do_*`
method is dispatched, so authentication is not a thing each route remembers to
do — it is a thing no route can reach past. Loopback is trusted (the browser
has no token and never will); everything else presents `Authorization: Bearer
orc1_…`; `GET /api/health` is the single exempt route. The reasoning is in
`auth.py`, which is deliberately a leaf: it cannot import the code it guards.

`Handler` is the whole HTTP surface. GET is the watching half — the board's
state (with the resume schedules riding along so it needs no second fetch),
the map's topology, limits, the dispatch log, a chat drawer — plus the four
static HTML pages, read off disk from `config.HERE` on every request so an
edit shows up on reload. POST is the acting half, and every one of its routes
is a click somebody made: reserve, schedule/cancel a resume, send text to a
session, finish a mission, dispatch a new agent.

`GET /api/events` is the one route that is not a request/response at all: it
is the state STREAM (ADR 0005), a socket held open for the life of the client
that emits one frame per version bump off the observer's publish point. Every
other route answers and hangs up; this one answers and stays.

It is a thin router by design. Nothing here decides anything: each branch
parses the request, hands it to the module that owns the verb, and JSON-dumps
whatever comes back. Errors travel inside the payload (`{"ok": false, …}`)
rather than as HTTP status codes — the board renders the message either way,
and a 500 would tell it less. Only an unknown path is a real 404. The stream
is the exception there too: it has no payload to put an error inside before it
has started, so its two refusals (no sweep running, cap reached) are real 503s.

Top of the import graph: it imports every layer and nothing imports it back.
`log_message` is silenced so the terminal you launched the board from stays
readable.
"""

import json
import re
import select
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (config, auth, gitrepo, limits, observer, terminal, chat,
               dispatch, resume, finish, pairing, tailnet, notify)

MAX_SUBSCRIBERS = 32        # concurrent SSE streams; config key "sse_max_subscribers"
KEEPALIVE_S = 25.0          # silence before a comment frame; key "sse_keepalive_s"
LIVENESS_S = 1.0            # how often a silent stream checks its peer is there

# What one wait in the stream loop came back with. Three outcomes and not a
# bool, because the third one is not the negation of either other: the version
# did not move, so there is nothing to send, AND there is no longer anybody to
# send a keepalive to.
MOVED, QUIET, GONE = "moved", "quiet", "gone"


def _knob(key, default):
    """One SSE setting, resolved: config key > the constant beside it.

    The same shape as `observer._cadence` and `watcher._knob`, minus the
    explicit-argument arm — nothing constructs a stream object to pass one to.
    Read at the START of each connection rather than at import, so a config
    edit reaches the next subscriber without a restart; a running stream keeps
    the value it opened with, which is the same rule the sweep loop follows.
    """
    return config.CFG.get(key, default)


# ------------------------------------------------------------- the SSE census

# One counter for the process, because the cap is a process-wide resource: with
# thread-per-client (ADR 0005) an open subscriber is a thread held for the
# subscriber's whole lifetime. `peak` and `rejected` are here because a cap you
# cannot watch yourself hit is a cap you will misjudge — `open` alone only ever
# tells you about right now, and a stream that was refused is by definition not
# there to be counted.
_subs = {"open": 0, "peak": 0, "opened": 0, "rejected": 0}
_subs_lock = threading.Lock()


def sse_stats():
    """A copy, under the lock — the caller must not be able to hold the census."""
    with _subs_lock:
        return dict(_subs)


def _sse_reset():
    """Tests only: the census is process-wide, so it outlives a test case."""
    with _subs_lock:
        _subs.update(open=0, peak=0, opened=0, rejected=0)


def _v1_match(path, base):
    """A `/api/v1` route matched at a SEGMENT boundary, never by substring.

    `base` itself or `base/<anything>` — so `/api/v1/events` and
    `/api/v1/events/open` both match and `/api/v1/eventsX` does not. This is the
    router half of the rule the guard already enforces (`auth._under_admin`):
    the two disagreeing by one character is exactly how `/api/v1/devicesX`
    served the device inventory a phone should never have seen.
    """
    clean = path.split("?", 1)[0]
    return clean == base or clean.startswith(base + "/")


def _query(path):
    """Query string -> {key: first value}, percent-decoded.

    The older routes here read one parameter each with a `re.search`, which is
    fine for one. `/api/focus` now carries a whole identity (ADR 0008) — a
    worktree name, a cwd, a tmux target — and those contain slashes, spaces and
    non-ASCII, none of which survive being matched out of a raw path.
    """
    qs = urllib.parse.urlsplit(path).query
    return {k: v[0] for k, v in urllib.parse.parse_qs(qs).items() if v}


# ------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    # PINNED — and it is already BaseHTTPRequestHandler's default, which is the
    # entire reason to write it down. SSE over HTTP/1.1 without chunked framing
    # hangs EventSource forever: a stream can carry no Content-Length (it never
    # ends), and a 1.1 client waits for a body length that will never arrive
    # instead of reading to EOF. Under 1.0 the connection close IS the framing.
    #
    # It is also what BaseHTTPRequestHandler already defaults to, so this line
    # changes nothing today and exists to stop somebody changing it tomorrow.
    # Measured against this Handler on a real socket: `HTTP/1.0 200 OK`, no
    # Content-Length, and a version bump reaching a client's recv in 0.051 ms
    # (p50, one stream) — genuinely incremental. Do not "modernise" this.
    protocol_version = "HTTP/1.0"


    # ------------------------------------------------------------ the door
    #
    # `parse_request` is where authentication happens, and it is the ONLY place
    # in this process where it happens. That is not a stylistic choice — it is
    # the one seam a route cannot get past. `handle_one_request` does exactly
    # this, in this order, and has since Python 2:
    #
    #     if not self.parse_request():   # <- we return False here and the
    #         return                     #    request is over
    #     method = getattr(self, 'do_' + self.command)
    #     method()
    #
    # So there is no `do_*` that can forget the check, no branch inside `do_GET`
    # that can skip it, and no route somebody adds next month that starts out
    # unguarded — including a `do_PUT` that does not exist yet, and including
    # `/api/events`, which returns early from `do_GET` and would slip past a
    # guard written at the top of the elif chain. A method this handler does not
    # implement is also refused BEFORE it is told it is not implemented, which
    # is the right order: an unauthenticated peer learns nothing about the
    # surface.
    #
    # Putting the check in a decorator on each `do_*`, or a first line in both,
    # was the alternative. Both are one edit away from being wrong, and the edit
    # is invisible in review.

    def parse_request(self):
        if not super().parse_request():
            return False            # malformed request line; already answered
        peer = self.client_address[0] if self.client_address else ""
        verdict = auth.check(peer, self.headers.get("Authorization"),
                             self.command, self.path,
                             origin=self.headers.get("Origin"),
                             host=self.headers.get("Host"),
                             content_type=self.headers.get("Content-Type"))
        if verdict.ok:
            # The device that just authenticated, for a route that ever needs
            # to know WHO is asking (API.md's `devices/self/*` will). Nothing
            # reads it today; it is None for a trusted-loopback request, which
            # is the state that would otherwise have to be invented later.
            self.device = verdict.device
            return True
        self._refuse(verdict)
        return False

    def _refuse(self, verdict):
        """The refusal, in the same shape as every other error the board reads.

        `{"ok": false, "error": …}` is this server's error convention (module
        docstring), so a refusal renders in the board's existing message path
        rather than as a blank tab. The status is real, though — unlike the
        in-payload errors, a 401 is something an HTTP client must be able to
        act on without parsing anything, and `WWW-Authenticate` is what makes
        it a legal one.

        THE BODY OF A REFUSED POST IS NOT READ, and that took three
        measurements to settle. Answering while the client is still writing can
        leave it blocked in `sendall`, raising BrokenPipe INSTEAD OF READING
        THE 401 — a revoked phone that cannot tell "revoked" from "the Mac is
        asleep" retries forever. A draft of this handler therefore drained the
        body first, and a probe against a hand-rolled server said the guard was
        needed from 512 KB up.

        It is not, and that probe was the wrong harness: it closed the socket
        outright, where `socketserver.shutdown_request` half-closes it
        (`SHUT_WR`) first — which lets the client's remaining bytes drain into
        the kernel instead of earning an RST that discards the response.
        Re-measured against THIS server, a refused POST whose body is never
        read:

            400 KB · 600 KB · 800 KB   401, every time
            1 MB                       401, mostly — flaky with the drain and
                                       without it
            2 MB and up                BrokenPipe / ConnectionReset

        The board's own POSTs are kilobytes — a chat message, a mission brief —
        so the whole band a drain could rescue is one no client reaches, and
        the price was reading a megabyte on behalf of somebody who has not
        authenticated. Removed; the test that "proved" it was flaky in exactly
        the band the measurement says is flaky. What is left is this note, so
        that the next person to probe it on a toy server finds the answer
        before writing the code.
        """
        body = json.dumps({"ok": False, "error": verdict.code,
                           "message": verdict.message}).encode()
        self.send_response(verdict.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if verdict.status == 401:
            self.send_header("WWW-Authenticate", 'Bearer realm="orchestra"')
        if verdict.retry_after:
            self.send_header("Retry-After", str(int(verdict.retry_after)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, status, payload):
        """One JSON answer with a real status code, then hang up.

        The legacy routes share a tail that always sends 200 and puts the error
        inside the body. `/api/v1` does not — see the note in `do_POST` — so it
        needs a writer of its own, and this is it. `Cache-Control: no-store`
        because two of its callers carry a live pairing code and one carries a
        token: a proxy or a disk cache holding either is a credential left on
        the floor.
        """
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _advertised_host(self):
        """The address to put in the QR — where the PHONE should connect.

        `CFG["host"]` is what we bound, and when that is a tailnet address it
        is exactly right. When it is loopback it is exactly wrong: a QR saying
        `127.0.0.1` sends the phone to its own web server. So loopback falls
        back to the detected tailnet address, and only if there is none does it
        emit the bound address — a pairing window opened on a loopback-only
        server is a rehearsal, and it should look like one rather than fail
        silently at the phone.
        """
        bound = config.CFG.get("host") or "127.0.0.1"
        if auth.loopback(bound):
            return tailnet.address() or bound
        return bound

    def do_GET(self):
        if self.path.startswith("/api/events"):
            # EARLY RETURN, and it is the whole structural change (ENGINE.md
            # §5.2). Every other route below falls out of the elif chain into a
            # shared `body = …` tail that ends in an unconditional
            # Content-Length — the one header a stream must never send, and the
            # tail that makes all nine of the others work.
            return self._sse()
        if auth.admin("GET", self.path):
            # The versioned surface starts here (ADR 0009). Everything else in
            # this handler is the legacy unversioned board API and stays where
            # it is; the routes born with pairing are born on `/api/v1`, which
            # is what the Swift client will be written against.
            #
            # `auth.ADMIN` already refused every token holder before this line
            # ran — `parse_request` is the seam — so reaching this branch means
            # the caller is the Mac itself. It reads a list of every credential
            # to this machine, which is why `auth.audited` logs it even though
            # it is a GET.
            #
            # THE CONDITION IS `auth.admin` AND NOT A `startswith`, and that is
            # the whole of a real hole. It used to read
            # `self.path.startswith("/api/v1/devices")`, which is one character
            # less strict than the guard: `_under_admin` stops at a SEGMENT
            # boundary, so `/api/v1/devicesX` was not admin to `auth.check` and
            # WAS the device list to this line. A phone with a perfectly valid
            # token read the inventory of every credential to this machine, and
            # `auth.audited` — asking the same segment-aware question — did not
            # log it. Each half had a test and neither could see the gap.
            #
            # So the router and the guard now consult the SAME function. They
            # cannot drift, because there is only one answer to drift from.
            # API.md §2.3 step 5 said this all along: `/api/v1` resolves by
            # exact match on the path without its query, no prefix routing.
            return self._json(200, {"ok": True, "devices": auth.devices(),
                                    "pairing": pairing.state()})
        if self.path.startswith("/api/health"):
            # The one route that answers without a token (auth.EXEMPT), so it
            # is also the one route whose payload is a security decision. It
            # says a server that speaks this protocol is alive here and what
            # its clock reads — and NOTHING that varies with what the fleet is
            # doing: no counts, no worktrees, no hostname, no device list, not
            # even whether any device is registered. The clock is the point:
            # every other route's timestamps are unreadable to a client whose
            # own clock is wrong, and diagnosing that must not require the
            # credential you are trying to diagnose.
            body = json.dumps({"ok": True, "service": "orchestra",
                               "api": "1.0", "time": time.time()}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/state"):
            # schedules ride along so the board needs no second fetch
            body = json.dumps({**observer.cached_state(),
                               "resumes": resume.resume_public()}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/focus"):
            q = _query(self.path)
            m = re.search(r"pid=(\d+)", self.path)
            # the pid is a hint; wt/sid/cwd/tmux/tty are the address (ADR 0008)
            result = terminal.focus_process(
                int(m.group(1)) if m else None,
                sid=q.get("sid"), account=q.get("account"),
                worktree=q.get("wt"), cwd=q.get("cwd"),
                tmux=q.get("tmux"), tty=q.get("tty"))
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/topology"):
            body = json.dumps(gitrepo.cached_topology()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/limits"):
            body = json.dumps(limits.cached_limits(refresh="refresh=1" in self.path)).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatchlog"):
            body = json.dumps(dispatch.read_dispatch_log()).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/dispatch/status"):
            m = re.search(r"job=([\w-]+)", self.path)
            body = json.dumps(dispatch.dispatch_status(m.group(1)) if m
                              else {"ok": False, "error": "no job"}).encode()
            ctype = "application/json"
        elif self.path.startswith("/api/chat"):
            qa = re.search(r"account=([^&]+)", self.path)
            qs = re.search(r"sid=([0-9a-fA-F-]+)", self.path)
            result = chat.read_chat(qa.group(1), qs.group(1)) if qa and qs else \
                {"ok": False, "error": "need account & sid"}
            body = json.dumps(result).encode()
            ctype = "application/json"
        elif _v1_match(self.path, "/api/v1/events"):
            # The durable side of push (API.md §9.22). Push is lossy; this is
            # not, so the phone reconciles against it on every foreground — and
            # /events/open is the withdrawal route that stops a resolved
            # question sitting on the lock screen forever. Matched at a SEGMENT
            # boundary, never by `startswith` — API.md §2.3 "no prefix routing",
            # so `/api/v1/eventsX` is a 404 and not this route (the same hole
            # `/api/v1/devicesX` was, pinned by test_no_v1_route_can_be_reached
            # _by_a_suffix).
            body = json.dumps(self._events_get()).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] == "/api/v1/push/status":
            # What the settings screen shows: is push configured, what did the
            # last send return, when. `push.sink().health()` says exactly which
            # piece is missing when it is not ready — a NoopSink names the
            # config key, an APNsSink names a bad Key ID or a missing binary.
            from . import push as pushmod
            dev = getattr(self, "device", None)
            body = json.dumps({"ok": True, "push": pushmod.sink().health(),
                               "registered": bool(dev and auth.get_push(dev["id"]))
                               if dev else False}).encode()
            ctype = "application/json"
        elif self.path.split("?", 1)[0] in ("/", "/index", "/index.html"):
            body = (config.HERE / "index.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.split("?", 1)[0] == "/stream.js":
            # The SharedWorker that holds the board's ONE EventSource, and the
            # reason it is a route rather than a Blob URL: a SharedWorker's
            # identity is (origin, script URL, name), and a Blob URL is unique
            # per document — every tab would get its OWN worker, its own stream,
            # and the six-connection ceiling this exists to stay under
            # (ENGINE.md §5.4). A stable path is what makes it shared.
            body = (config.HERE / "stream.js").read_bytes()
            ctype = "application/javascript; charset=utf-8"
        elif self.path.startswith("/map"):
            body = (config.HERE / "map.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/limits"):
            body = (config.HERE / "limits.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/guide"):
            body = (config.HERE / "guide.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        elif self.path.startswith("/pair"):
            # Served like every other page: from disk, on every request, so an
            # edit shows up on reload. It carries no secret of its own — the
            # code and the QR arrive from `/api/v1/devices/pair/open`, which is
            # a POST, so merely LOADING this page cannot open a pairing window.
            # That distinction is the reason it is not a GET.
            body = (config.HERE / "pair.html").read_bytes()
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # --------------------------------------------------------- the state stream

    def _sse(self):
        """GET /api/events — one frame per version bump (ADR 0005, §5.3).

        Two refusals, both 503, both before a byte of stream is written:

        * **no sweep running.** `observer._observer` is None in demo mode and on
          the documented rollback (not calling `start_observer`). There is no
          version to stream and nothing would ever be pushed, so holding the
          socket open would be a promise this process cannot keep. Say so.
        * **the cap.** `sse_max_subscribers`, taken and released around the
          whole stream. The slot is claimed BEFORE the 200 goes out, so a
          refused client never sees a stream begin and then stop.

        The `finally` is the load-bearing line: every way out of `_stream` —
        the client closing, a write failing, an exception nobody predicted —
        goes through it, and it is what makes a dropped phone give back its
        thread and its slot rather than leaking one of each per tailnet blip.
        """
        obs = observer._observer
        if obs is None:
            self.send_error(503, "no observer",
                            "the sweep is not running, so there is nothing to "
                            "stream; poll /api/state instead")
            return
        cap = int(_knob("sse_max_subscribers", MAX_SUBSCRIBERS))
        with _subs_lock:
            over = _subs["open"] >= cap
            if over:
                _subs["rejected"] += 1
            else:
                _subs["open"] += 1
                _subs["opened"] += 1
                _subs["peak"] = max(_subs["peak"], _subs["open"])
        if over:
            self.send_error(503, "too many SSE subscribers",
                            f"this server streams to at most {cap} concurrent "
                            f"subscribers; poll /api/state instead")
            return
        try:
            self._stream(obs)
        finally:
            with _subs_lock:
                _subs["open"] -= 1

    def _stream(self, obs):
        """The stream proper: a snapshot, then one frame per version bump.

        The loop never polls the OBSERVER. `Observer.wait_for` blocks on the
        same Condition that `publish` notifies, so a version that does not move
        produces no wakeup, no frame and no bytes — which is the whole point of
        §3.2. A board that re-rendered on a heartbeat would be back to polling
        with extra steps.
        """
        keep = float(_knob("sse_keepalive_s", KEEPALIVE_S))
        # `Last-Event-ID` is EventSource's own reconnect cursor: the browser
        # sends back the last `id:` it saw, unprompted. It is also the entire
        # resync path, because `delta_since` already decides what a cursor
        # deserves — a delta when it is inside the 512-version ring, a full
        # snapshot when it is unknown, too old, or AHEAD of us (a restarted
        # server whose version counter began again). A tab that never went away
        # and a phone that was suspended for an hour take the same three lines
        # and neither has to know which of the two it is.
        try:
            cursor = int(self.headers.get("Last-Event-ID") or 0)
        except ValueError:
            cursor = 0                  # a garbled cursor is an unknown cursor
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # explicit, though HTTP/1.0 already implies it: the close is the framing
        self.send_header("Connection", "close")
        # Nothing sits between this server and the browser today. A proxy that
        # buffers the stream would turn a 1 s board into a 60 s one, and the
        # symptom — updates arriving in bursts — looks exactly like a bug here.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        # Immediately, before any wait: a client must never be blank while it
        # holds an open, healthy connection. `delta_since(0)` is a snapshot by
        # construction; a resuming client gets whatever its cursor has earned.
        first = obs.delta_since(cursor)
        if first is not None:
            if not self._write_frame(first):
                return
            cursor = first["v"]         # never max(): a client AHEAD of the
                                        # server must be pulled back, not left
                                        # waiting for a version that cannot come
        while True:
            verdict = self._await_change(obs, cursor, keep)
            if verdict == GONE:
                return
            if verdict == QUIET:
                # Silence, and it means the composed view has not changed —
                # not that anyone here is asleep. Three bytes of comment frame,
                # which no conformant SSE parser surfaces to the page, tell the
                # NAT and the proxy that this socket is still worth keeping.
                if not self._write(b": keepalive\n\n"):
                    return
                continue
            frame = obs.delta_since(cursor)
            if frame is None:           # cannot happen after a wait that saw a
                return                  # version, but None is not a frame
            if not self._write_frame(frame):
                return
            cursor = frame["v"]

    def _await_change(self, obs, cursor, keep):
        """Wait out one keepalive interval: MOVED, QUIET, or GONE.

        The waiting is still done by `Observer.wait_for` on the publish
        Condition — this never polls the observer, and a version that does not
        move still produces no frame. The wait is SLICED only so that a silent
        stream can check its peer is still there, because measured, a death
        that is never written to is never noticed. Measured, 12 rude RSTs
        against this loop, time until every slot came back:

            discovered by the next write only  →  > 20,000 ms  (the probe's own
                                                  cap; the real bound is
                                                  `sse_keepalive_s`, 25 s)
            discovered by this check at 1.0 s  →       198 ms

        That window is the cap's problem, not a cosmetic one. `sse_max_sub-
        scribers` is the resource; EventSource retries about every 3 s, so one
        flapping tailnet client parks ~8 dead slots inside a 25 s keepalive and
        four of them park all 32 — the cap would then refuse live clients on
        behalf of clients that had already left, which is precisely the silent
        degradation it exists to prevent.

        GONE is its own answer rather than a fall-through to the write path:
        the version has NOT moved, so `delta_since(cursor)` here would be an
        empty delta, and writing it to a FIN-closed socket succeeds — the loop
        would spin, emitting empty frames into a closed pipe forever.
        """
        live = float(_knob("sse_liveness_s", LIVENESS_S))
        waited = 0.0
        while waited < keep:
            slice_s = min(live, keep - waited)
            if obs.wait_for(cursor, timeout=slice_s) is not None:
                return MOVED
            waited += slice_s
            if self._peer_gone():
                return GONE
        return QUIET

    def _peer_gone(self):
        """Has the client left, asked without writing a byte to find out?

        A subscriber never speaks again after its request, so ANY readability
        on the connection is the end of it: an empty peek is a clean FIN, an
        exception is an RST. A non-empty peek means the client sent something
        unexpected, which is not death — it is left alone rather than guessed
        at, and costs one more peek per interval.

        `MSG_PEEK` and not `recv`: this runs on every slice and must consume
        nothing, because `handle_one_request` still owns the stream.
        """
        try:
            if not select.select([self.connection], [], [], 0)[0]:
                return False
            return not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True

    def _write_frame(self, payload):
        """`id:` carries the version, and that is what makes reconnect free —
        it is the value the browser hands back as `Last-Event-ID`. `event:` is
        constant today; `data` carries the §5.3 envelope, whose `type` field is
        present from the first release so deltas need no version negotiation."""
        return self._write(f"id: {payload['v']}\nevent: state\n"
                           f"data: {json.dumps(payload)}\n\n".encode())

    def _write(self, blob):
        """One frame, one write, and a dead client is a False rather than a raise.

        The socket is the only thing in the loop that can fail and it fails
        routinely — a phone leaving the tailnet, a tab closing. `False` unwinds
        to `_sse`'s `finally`, which releases the slot and lets the thread end;
        `Server.handle_error` then keeps socketserver's traceback off stderr.
        `OSError` rather than the two named subclasses because that is the
        family the socket layer raises and a stream is not the place to be
        surprised by ETIMEDOUT.
        """
        try:
            self.wfile.write(blob)
            self.wfile.flush()
            return True
        except OSError:
            return False

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode() or "{}")
        except (ValueError, OSError):
            payload = {}
        # ---- /api/v1, the versioned surface (ADR 0009) ----
        #
        # These four answer with REAL STATUS CODES rather than this handler's
        # in-payload `{"ok": false}` convention, and the exception is
        # deliberate. The legacy routes are read by one client — the board —
        # which renders the message either way. These are read by a Swift
        # client written from API.md, which has to branch on "the window is
        # closed" (409) versus "you are not allowed here" (403) versus "slow
        # down" (429) before it has parsed anything. A 200 carrying a failure
        # is a contract that only works when the reader is a browser you also
        # wrote.
        # `/api/v1` resolves by EXACT match on the path without its query
        # (API.md §2.3 step 5, "there is no prefix routing"), and after
        # `/api/v1/devicesX` that is a rule rather than a preference. `startswith`
        # here made the router looser than the guard on GET; on POST it happened
        # to fail safe — `/api/v1/pairX` is not in `auth.EXEMPT`, so it demanded
        # a token it would then have handed to `pairing.claim` — but "safe by
        # luck this time" is what the GET was too. One shape, no luck.
        route = self.path.split("?", 1)[0]
        if route == "/api/v1/pair":
            # The bootstrap. `auth.EXEMPT` lets it through with no token —
            # every other guard in `pairing.claim` still applies, starting with
            # the peer range and ending with a constant-time code comparison.
            # `auth.EXEMPT` matches this path exactly, and now so does this
            # line: the exempt list and the router say the same word.
            result, error = pairing.claim(
                self.client_address[0] if self.client_address else "", payload)
            if error:
                status, code, message = error
                return self._json(status, {"ok": False, "error": code,
                                           "message": message})
            return self._json(200, {"ok": True, **result})
        if route == "/api/v1/devices/pair/open":
            w = pairing.open_window(host=self._advertised_host())
            return self._json(200, {"ok": True, **w})
        if route == "/api/v1/devices/pair/close":
            pairing.close()
            return self._json(200, {"ok": True, "pairing": pairing.state()})
        # A device's OWN push endpoint and preferences. `read`-scoped, not admin
        # (auth.SELF_SUBTREE) — a phone registers its own token, which rotates
        # on reinstall and restore, and gating that behind the Mac-only admin
        # scope would make push structurally impossible on a phone. The device
        # is the AUTHENTICATED one (`self.device`), never a path parameter, so a
        # token can only ever write its own endpoint.
        if route in ("/api/v1/devices/self/push",
                     "/api/v1/devices/self/settings",
                     "/api/v1/push/test", "/api/v1/push/mute"):
            return self._push_self(route, payload)
        if auth.admin("POST", route):
            # `/api/v1/devices/<id>/revoke`. Parsed rather than matched so the
            # path shape is API.md §2.5's, which is what the Swift client and
            # the docs both say — and `auth.admin` is the guard's own predicate,
            # so an id this parse does not recognise is refused for everyone but
            # the Mac by the same rule that let the Mac in.
            parts = route.strip("/").split("/")
            if len(parts) == 5 and parts[4] == "revoke":
                device = auth.revoke_device(parts[3])
                if device is None:
                    return self._json(404, {"ok": False,
                                            "error": "device_unknown",
                                            "message": f"no device {parts[3]}"})
                return self._json(200, {"ok": True, "device": device})
            self.send_error(404)
            return

        if self.path.startswith("/api/hook"):
            # ENGINE.md §7.1: one route, one dict. The hottest POST this server
            # has — several per agent turn, from every hooked session on the
            # machine — and the only one an AGENT IS BLOCKED ON while it is
            # served. Claude Code runs its hooks synchronously; a slow answer
            # here is a slow agent, and a 500 here is a red line in somebody's
            # terminal in the middle of their work.
            #
            # So it does exactly two things — record the edge, nudge the loop —
            # and it cannot fail: `observer.hook` swallows everything, and this
            # branch answers 200 for an unknown event, a malformed session id
            # and a board running with no Observer at all. `status` in the reply
            # is what the board UNDERSTOOD (null for the ~22 events that assert
            # nothing), which is the only thing that makes a broken install
            # debuggable without reading this file.
            #
            # AUTHENTICATION is `parse_request`, like every other route, and for
            # a hook that means loopback trust: the agent runs on this Mac and
            # posts to 127.0.0.1 (see `hooks.SCRIPT`). Nothing weaker would be
            # possible — a hook has no credential to present and we will not
            # mint one per session — and nothing stronger is bought: a local
            # process that wanted to lie about a status can already type at the
            # agent through `/api/send`, which is a far worse power than
            # mislabelling a card. The cross-site guards still run above, so a
            # page you are visiting cannot post one through your browser.
            result = {"ok": True, "status": observer.hook(
                payload.get("session_id"), payload.get("hook_event_name"),
                notification_type=payload.get("notification_type"))}
        elif self.path.startswith("/api/reserve"):
            result = limits.set_reserve(payload.get("account"), payload.get("percent"))
        elif self.path.startswith("/api/resume/schedule"):
            result = resume.schedule_resume(
                payload.get("worktree"), payload.get("sid"),
                payload.get("account"), model=payload.get("model"),
                delay_s=payload.get("delay_s"),
                resets_at=payload.get("resets_at"), due_at=payload.get("due_at"))
        elif self.path.startswith("/api/resume/cancel"):
            result = resume.cancel_resume(payload.get("worktree"), payload.get("sid"))
        elif self.path.startswith("/api/send"):
            # {pid, text} was the whole request once, and that was the bug: the
            # drawer captured a pid on open and posted it minutes later, so a
            # recycled pid typed the reply into a different agent (ADR 0008).
            # The identity the drawer already uses to READ the conversation now
            # travels with the write, and the pid is only a hint.
            result = terminal.send_to_process(
                int(payload.get("pid") or 0), payload.get("text") or "",
                sid=payload.get("sid"), account=payload.get("account"),
                worktree=payload.get("worktree"), cwd=payload.get("cwd"),
                tmux=payload.get("tmux"), tty=payload.get("tty"))
        elif self.path.startswith("/api/finish"):
            result = finish.start_finish(payload.get("worktree") or "")
        elif self.path.startswith("/api/dispatch"):
            result = dispatch.start_dispatch(
                payload.get("mission"), payload.get("worktree") or None,
                payload.get("account") or None,
                payload.get("model") or None, payload.get("effort") or None,
                bool(payload.get("force_model")))
        else:
            self.send_error(404)
            return
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------------------------------------------------- push, events

    def _push_self(self, route, payload):
        """The device self-service routes (API.md §9.23). The device is the
        AUTHENTICATED one — a loopback request has none, which is refused here
        because these routes are ABOUT a device and loopback is nobody."""
        dev = getattr(self, "device", None)
        if not dev:
            return self._json(403, {"ok": False, "error": "device_required",
                                    "message": "these routes identify the "
                                    "calling device by its token — loopback "
                                    "holds none"})
        devid = dev["id"]
        if route == "/api/v1/devices/self/push":
            token = payload.get("token")
            if payload.get("backend", "apns") == "apns" and not \
                    notify_token_ok(token):
                return self._json(422, {"ok": False,
                                        "error": "push_token_invalid",
                                        "message": "token must be 64–200 hex "
                                        "characters"})
            stored = auth.set_push(devid, payload)
            if stored is None:
                return self._json(404, {"ok": False, "error": "device_unknown"})
            warnings = []
            st = payload.get("settings") or {}
            if st.get("time_sensitive_allowed") is False:
                warnings.append("time_sensitive_allowed is false — P1 alerts "
                                "will be suppressed by any Focus, including Sleep")
            return self._json(200, {"ok": True, "backend": stored.get("backend"),
                                    "environment": stored.get("environment"),
                                    "warnings": warnings})
        if route == "/api/v1/devices/self/settings":
            stored = auth.set_push(devid, {
                k: payload[k] for k in ("quiet_hours", "rules", "privacy",
                                        "nudge_min") if k in payload})
            if stored is None:
                return self._json(404, {"ok": False, "error": "device_unknown"})
            return self._json(200, {"ok": True, "settings": {
                k: stored.get(k) for k in ("quiet_hours", "rules", "privacy",
                                           "nudge_min")}})
        if route == "/api/v1/push/mute":
            try:
                mins = float(payload.get("minutes") or 0)
            except (TypeError, ValueError):
                mins = 0
            until = time.time() + max(0.0, min(mins, 7 * 24 * 60)) * 60
            auth.set_push(devid, {"muted_until": until})
            return self._json(200, {"ok": True, "muted_until": until})
        if route == "/api/v1/push/test":
            # The real thing end to end: composes a notification, signs a real
            # JWT, does the real HTTP/2 POST. The only step it cannot complete
            # without a .p8 is the 200 — and it says exactly which piece is
            # missing, which is the whole point of a test button.
            return self._json(200, notify.send_test(devid))
        return self._json(404, {"ok": False, "error": "unknown_route"})

    def _events_get(self):
        """`/api/v1/events`, `/events/{id}`, `/events/open` (API.md §9.22)."""
        route = self.path.split("?", 1)[0]
        log = notify.service().log
        if route == "/api/v1/events/open":
            return {"ok": True, "open": log.open_keys(), "as_of": time.time()}
        if route.startswith("/api/v1/events/") and route != "/api/v1/events/":
            eid = route.rsplit("/", 1)[1]
            ev = log.get(eid)
            if ev is None:
                return {"ok": False, "error": "event_not_found"}
            return ev
        q = _query(self.path)
        try:
            limit = max(1, min(200, int(q.get("limit", 50))))
        except ValueError:
            limit = 50
        return log.since(q.get("since"), limit=limit)

    def log_message(self, *args):
        pass


def notify_token_ok(token):
    """An APNs device token is 64–200 hex — the same check `push` uses, reached
    here without importing `push` on the hot path."""
    from . import push as pushmod
    return bool(pushmod.TOKEN_RE.match(token or ""))


class Server(ThreadingHTTPServer):
    """The listening half. Three lines, and every one of them is about SSE.

    `daemon_threads` is already ThreadingHTTPServer's default and is restated
    because the stream depends on it and would break silently without it:
    `socketserver._Threads.append` early-returns on a daemon thread, so 32
    long-lived subscribers never accumulate in the join list that
    `server_close` walks. Flip it off and shutdown blocks until every phone
    hangs up.
    """

    daemon_threads = True
    # socketserver's default is 5 and was never overridden here. Five is the
    # LISTEN BACKLOG, not a connection limit: the excess is dropped by the
    # kernel before any thread sees it, so the browser shows a hung tab rather
    # than an error and nothing on this side ever logs a thing. Measured, 120
    # simultaneous connects against a busy server, three runs each:
    #
    #     backlog    5   →  2 client timeouts, slowest run 5.00 s
    #     backlog  128   →  0 timeouts,        slowest run 0.44 s
    #     backlog  256   →  0 timeouts,        slowest run 0.45 s
    #
    # 128 and 256 measure identically here because the kernel clamps to
    # `kern.ipc.somaxconn`, which is 128 on this machine — the effective value
    # is `min(this, somaxconn)`. 256 is set anyway so that THIS number is not
    # the limit on a host tuned higher; it costs nothing where it is clamped.
    request_queue_size = 256

    def handle_error(self, request, client_address):
        """A dropped subscriber is routine, not an incident.

        socketserver prints a full traceback for any exception out of a
        handler. A client that goes away mid-stream raises ConnectionReset or
        BrokenPipe, and on a tailnet that happens every time the phone changes
        network — so without this, normal operation spams stderr with
        stack traces and the log stops being worth reading (ADR 0005's first
        mandatory mitigation). Anything else still gets its traceback: this
        swallows two specific socket deaths, not errors in general.
        """
        if isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)
