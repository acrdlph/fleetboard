/* stream.js — ONE EventSource for the whole browser, held in a SharedWorker.
 *
 * THE CEILING IS THE BROWSER, NOT THE SERVER (ENGINE.md §5.4). The server was
 * measured to 32 concurrent streams at 1.4 ms p50 and survives far more; a
 * browser allows six connections per origin, full stop. One EventSource per TAB
 * would spend the whole budget at six tabs and starve every POST before that —
 * so the stream lives HERE, in a SharedWorker, and the tabs read its output.
 *
 * Three jobs, and nothing else:
 *
 *   1. hold the stream and apply its frames (`Fleet`), so the board is TOLD
 *      about a change ~1 s after the observer sees it instead of finding out on
 *      its next five-second poll;
 *   2. fetch the two things no frame carries — see `refresh()`;
 *   3. fan the composed state out to every tab: unicast on the port when a tab
 *      joins, BroadcastChannel for updates.
 *
 * DEGRADE, NEVER ASSUME. A board that silently stopped updating because a
 * socket died would be far worse than one that polls, so polling is not a
 * legacy path here — it is what runs whenever the stream is not PROVEN live,
 * and the proof is a frame arriving, not a socket opening. The two refusals the
 * server can give (`--demo`, which runs no sweep, and the subscriber cap) both
 * arrive as a 503, which EventSource reports without retrying; that is the case
 * `streamDead` names, and it is retried on a slow clock in case the server
 * gains an observer or a slot.
 *
 * Loaded by node as a module (`tests/test_stream_js.py`) it exports `Fleet` and
 * stops before the worker half — the applier the tests check is the applier
 * that ships.
 */
(function (root) {
  "use strict";

  // ---------------------------------------------------------------- the view
  //
  // Everything a frame can say about the fleet, and the rules for patching it.
  // Deliberately free of sockets, timers and the DOM: it is the one part of
  // this file that can be wrong in a way no amount of clicking would reveal.

  function Fleet() { this.reset(); }

  Fleet.prototype.reset = function () {
    this.v = null;        // null = holding nothing a delta may be applied to
    this.cards = {};
    this.order = [];
    this.counts = {};
    this.other = [];
    this.at = null;
  };

  /* One frame in. Returns "applied" or "gap".
   *
   * THE GAP TEST IS `base`, AND ONLY `base`. It is tempting to check
   * `v === this.v + 1` and treat anything else as a lost frame — but the
   * server's loop waits on the version and THEN asks for a delta, so any
   * publishes landing in between are COALESCED into one frame whose `v` jumps
   * by more than one (tests/test_observer.py, "a delta can span more than one
   * version"). A client testing `v` would resync on every busy moment, which on
   * a fleet mid-turn is a reconnect loop. `base` is the cursor the server
   * actually resumed from, so `base === this.v` is exactly the question
   * "did I see everything before this?".
   *
   * A gap mutates NOTHING — the caller resyncs, and half-applying a frame we
   * are about to throw away would only make the thrown-away state harder to
   * reason about.
   */
  Fleet.prototype.apply = function (f) {
    if (f.type === "delta") {
      if (this.v === null || f.base !== this.v) return "gap";
      for (var k in f.cards) {
        if (!Object.prototype.hasOwnProperty.call(f.cards, k)) continue;
        if (f.cards[k] === null) delete this.cards[k];   // null = card removed
        else this.cards[k] = f.cards[k];
      }
    } else {
      this.cards = {};
      for (var s in f.cards) {
        if (Object.prototype.hasOwnProperty.call(f.cards, s)) this.cards[s] = f.cards[s];
      }
    }
    // `order`, not the key order of `cards`. Two reasons, both real: a delta
    // names only what moved, so patching a dict leaves every unchanged card at
    // its OLD position and a card that just flipped to NEEDS ANSWER would never
    // sort to the top; and `JSON.parse` hoists integer-like keys ahead of the
    // rest, so a worktree named `42` would sort itself to the front of the
    // board. The fallback is for a frame with no `order` at all — a server
    // older than this file — and is the best a dict can do.
    this.order = f.order || Object.keys(this.cards);
    this.counts = f.counts || {};
    // in full on every frame, because a loose claude process bumps the version
    // with no card changing (observer.delta_since says why)
    if (f.other_procs) this.other = f.other_procs;
    this.at = f.at;
    this.v = f.v;
    return "applied";
  };

  /* A full `/api/state` body in. Used when there is no stream to trust — and
   * it deliberately leaves `v` null, because that payload carries no version
   * and a delta applied on top of it would be applied to an unknown base. */
  Fleet.prototype.seed = function (d) {
    var wts = d.worktrees || [];
    this.cards = {};
    this.order = [];
    for (var i = 0; i < wts.length; i++) {
      this.cards[wts[i].name] = wts[i];
      this.order.push(wts[i].name);
    }
    this.counts = d.counts || {};
    this.other = d.other_procs || [];
    this.at = d.generated_at;
    this.v = null;
  };

  /* The `/api/state` shape the board already renders, rebuilt from the frames.
   *
   * `side` carries what no frame does. `free_worktrees` is DERIVED rather than
   * sent: it is `[card.name for card if availability == "free"]` and nothing
   * else (orchestra/observer.py, collect_state), a pure function of cards the
   * delta contract already guarantees are exact — putting it on the wire would
   * be a second copy of a fact that can then disagree with the first.
   */
  Fleet.prototype.state = function (side) {
    var wts = [];
    for (var i = 0; i < this.order.length; i++) {
      var c = this.cards[this.order[i]];
      if (c) wts.push(c);
    }
    var free = [];
    for (var j = 0; j < wts.length; j++) {
      if (wts[j].availability === "free") free.push(wts[j].name);
    }
    return {
      generated_at: this.at,
      hostname: side.hostname,
      user: side.user,
      counts: this.counts,
      free_worktrees: free,
      worktrees: wts,
      other_procs: this.other,
      resumes: side.resumes || {},
    };
  };

  root.Fleet = Fleet;
  if (typeof module === "object" && module && module.exports) {
    module.exports = { Fleet: Fleet };

  }

  // --------------------------------------------------------------- the worker

  var CHANNEL = "orchestra-state";
  var POLL_MS = 5000;        // no stream: the cadence the board always had
  var SIDE_MS = 20000;       // streaming: only the fields no frame carries
  var RETRY_MS = 60000;      // a server that refused the stream, asked again
  var RESYNC_BUDGET = 5;     // resyncs per minute before the stream is a liability

  var fleet = new Fleet();
  var side = { user: null, hostname: null, resumes: {} };
  var es = null;             // the one EventSource
  var live = false;          // a FRAME has arrived — not merely a socket opened
  var streamDead = false;    // refused (503) or gave up; retried on RETRY_MS
  var netErr = false;        // the last /api/state failed
  var fetching = false;
  var sideAt = 0;            // when /api/state last landed
  var triedAt = 0;           // when the stream was last opened
  var resyncs = [];
  var ports = [];
  var chan = (typeof BroadcastChannel === "function") ? new BroadcastChannel(CHANNEL) : null;
  var last = null;           // the last composed state, for a joining tab

  function status() {
    return {
      mode: live ? "stream" : (streamDead ? "poll" : "connecting"),
      v: fleet.v, tabs: ports.length, err: netErr,
    };
  }

  function send(msg) {
    if (chan) chan.postMessage(msg);
    // Ports as well as the channel: BroadcastChannel does NOT deliver to the
    // context that posted, and it is also the piece most likely to be missing
    // in a stripped-down browser. The tabs de-duplicate by identity, so a tab
    // that receives both renders once.
    for (var i = ports.length - 1; i >= 0; i--) {
      try { ports[i].postMessage(msg); } catch (e) { ports.splice(i, 1); }
    }
  }

  function publish() {
    last = fleet.state(side);
    send({ type: "state", st: last, status: status() });
  }

  function announce() { send({ type: "status", status: status() }); }

  // ---- the stream

  function openStream() {
    triedAt = Date.now();
    if (typeof EventSource !== "function") { streamDead = true; return; }
    try {
      es = new EventSource("/api/events");
    } catch (e) { es = null; streamDead = true; announce(); return; }
    es.addEventListener("state", onFrame);
    es.onerror = function () {
      live = false;
      // readyState 2 = CLOSED: EventSource does not retry a non-2xx, and the
      // server's two refusals (no sweep running under --demo; the subscriber
      // cap) are both 503s. Anything else is CONNECTING — the browser is
      // already retrying on its own and will resume from `Last-Event-ID`.
      if (es && es.readyState === 2) {
        try { es.close(); } catch (e2) {}
        es = null;
        streamDead = true;
      }
      announce();
    };
  }

  function onFrame(ev) {
    var f;
    try { f = JSON.parse(ev.data); } catch (e) { return; }
    if (fleet.apply(f) === "gap") { resync(); return; }
    live = true;
    streamDead = false;
    publish();
  }

  /* A gap is repaired by RECONNECTING, because a freshly constructed
   * EventSource sends no `Last-Event-ID` at all — and `delta_since` answers an
   * unknown cursor with a full snapshot (orchestra/server.py, `_stream`). There
   * is no resync request to invent; the reconnect IS the resync.
   *
   * Budgeted, because the one thing worse than a gap is a client that answers
   * every gap by opening another socket. Past the budget the stream is treated
   * as refused and the board goes back to polling, which always works. */
  function resync() {
    var now = Date.now();
    resyncs.push(now);
    resyncs = resyncs.filter(function (t) { return now - t < 60000; });
    fleet.reset();
    live = false;
    if (es) { try { es.close(); } catch (e) {} es = null; }
    if (resyncs.length > RESYNC_BUDGET) { streamDead = true; announce(); return; }
    openStream();
  }

  // ---- the poll that remains
  //
  /* TWO fields, and they are the honest reason this is not zero requests:
   *
   *   * `resumes` — armed auto-resumes. They live in `resume.py`, which the
   *     observer does not watch at all, so arming one moves no version and
   *     could not ride this stream today however the frame were shaped. Every
   *     arm/disarm the USER performs already forces a refresh (the board asks
   *     for one on the same click), so the cadence below only has to cover a
   *     schedule firing on its own.
   *   * `user` / `hostname` — constant for the life of the server process.
   *
   * So while the stream is live this runs every 20 s instead of every 5 s, and
   * it is a request the board makes about ITSELF rather than about the fleet.
   * With no stream it is the whole board and the cadence is the old 5 s.
   */
  function refresh() {
    if (fetching) return;
    fetching = true;
    fetch("/api/state", { cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        side = { user: d.user, hostname: d.hostname, resumes: d.resumes || {} };
        // never over a live stream: /api/state carries no version, and the
        // sweep that answered it may be older than the last frame applied
        if (!live) fleet.seed(d);
        sideAt = Date.now();
        netErr = false;
        publish();
      })
      .catch(function () { netErr = true; announce(); })
      .then(function () { fetching = false; });
  }

  // ---- the clock
  //
  // One 1 s timer, no network of its own: it decides whether anything is owed.
  // Reconnection and the fallback cadence are then the same two comparisons
  // rather than a nest of timers that can each be cancelled independently.
  function pump() {
    var now = Date.now();
    if (now - sideAt >= (live ? SIDE_MS : POLL_MS)) refresh();
    if (!es && (!streamDead || now - triedAt >= RETRY_MS)) openStream();
  }

  var started = false;
  function start() {
    if (started) return;
    started = true;
    openStream();
    refresh();
    setInterval(pump, 1000);
  }

  root.onconnect = function (ev) {
    var port = ev.ports[0];
    ports.push(port);
    port.onmessage = function (e) {
      var m = e.data || {};
      if (m.cmd === "refresh") { sideAt = 0; refresh(); }
      else if (m.cmd === "bye") {
        var i = ports.indexOf(port);
        if (i >= 0) ports.splice(i, 1);
        announce();
      }
    };
    port.start();
    // unicast, not broadcast: a tab joining is nobody else's business, and a
    // broadcast here would make every open board re-render for it
    port.postMessage(last ? { type: "state", st: last, status: status() }
                          : { type: "status", status: status() });
    start();
    announce();               // the tab count moved for everyone
  };
})(typeof self !== "undefined" ? self : globalThis);
