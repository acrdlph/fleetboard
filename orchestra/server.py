"""orchestra.server — the only door in: one handler, loopback, no framework.

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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import (config, gitrepo, limits, observer, terminal, chat, dispatch,
               resume, finish)

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

    def do_GET(self):
        if self.path.startswith("/api/events"):
            # EARLY RETURN, and it is the whole structural change (ENGINE.md
            # §5.2). Every other route below falls out of the elif chain into a
            # shared `body = …` tail that ends in an unconditional
            # Content-Length — the one header a stream must never send, and the
            # tail that makes all nine of the others work.
            return self._sse()
        if self.path.startswith("/api/state"):
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
        if self.path.startswith("/api/reserve"):
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

    def log_message(self, *args):
        pass


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
