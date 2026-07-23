#!/usr/bin/env python3
"""`stream.js` — the browser's half of the state stream, checked without a browser.

`tests/test_observer.py` proves the SERVER's delta is sufficient, against a
reference applier written in Python. That proves nothing about the applier the
board actually runs, and the applier the board actually runs is the piece that
can be wrong in a way no test run would reveal and no amount of clicking would
either: a delta that quietly stops re-sorting, or quietly loses a loose process,
looks exactly like a fleet that did not change.

So this drives the SHIPPED file. `stream.js` exports `Fleet` and returns before
its worker half when loaded as a module, so node gets the real applier — not a
copy of it — fed by frames the real `Observer.delta_since` produced. Each step
is checked three ways: against the Python reference applier, against the
snapshot taken whole, and against what the board would actually draw.

Needs node only to run; it is skipped without one, and the Python-side contract
in test_observer.py stands on its own.

    python3 -m unittest discover -s tests
"""

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import orchestra as fb            # noqa: E402
from test_observer import Client  # noqa: E402  — the Python reference applier

NODE = shutil.which("node")
STREAM_JS = ROOT / "stream.js"

# Reads [{op, ...}] on stdin, writes one result per step. Nothing here decides
# anything: it is a shell around `Fleet` so that every verdict below is the
# shipped code's verdict.
DRIVER = r"""
const { Fleet } = require(process.argv[2]);
let raw = "";
process.stdin.on("data", (d) => { raw += d; });
process.stdin.on("end", () => {
  const steps = JSON.parse(raw);
  const fleet = new Fleet();
  const out = [];
  for (const step of steps) {
    let verdict = null;
    if (step.op === "seed") { fleet.seed(step.body); verdict = "seeded"; }
    else if (step.op === "reset") { fleet.reset(); verdict = "reset"; }
    else verdict = fleet.apply(step.frame);
    out.push({ verdict: verdict, v: fleet.v,
               state: fleet.state({ user: "u", hostname: "h", resumes: {} }) });
  }
  process.stdout.write(JSON.stringify(out));
  // stream.js arms setInterval(pump, 1000) when it loads, and a pending timer
  // keeps node's event loop alive forever — the driver finishes its work and
  // then simply never exits, so every test burned its full 60s timeout and the
  // suite looked hung rather than failed. Exit explicitly once stdout is
  // flushed; the applier under test is pure and has nothing to unwind.
  process.stdout.once("drain", () => process.exit(0));
  if (process.stdout.writableLength === 0) process.exit(0);
});
"""


def fleet_state(at, cards, other=(9,), counts=None):
    """A `collect_state()` result with card order, status and availability all
    under the test's control. `cards` is [(name, dirty, status, availability)]."""
    return {
        "generated_at": at,
        "counts": counts or {"working": len(cards)},
        "worktrees": [
            {"name": name, "availability": avail,
             "git": {"branch": "main", "dirty": dirty},
             "sessions": [{"sid": f"s-{name}", "status": st,
                           "last_write_at": 1000.0}],
             "live_procs": []}
            for name, dirty, st, avail in cards],
        "other_procs": [{"pid": pid, "cpu": 0.5, "etime": "01:00",
                         "cwd": "/elsewhere"} for pid in other],
    }


def board(st):
    """What the board draws off one composed state — the order included, since
    a dict compares equal however its keys are arranged."""
    return {"names": [w["name"] for w in st["worktrees"]],
            "cards": st["worktrees"],
            "counts": st["counts"],
            "other_procs": st["other_procs"],
            "free_worktrees": st["free_worktrees"]}


def from_snapshot(snap):
    """The same, from the snapshot taken WHOLE — what a client that had just
    connected would render. `free_worktrees` mirrors observer.collect_state."""
    wts = list(snap.cards.values())
    return {"names": list(snap.cards), "cards": wts, "counts": snap.counts,
            "other_procs": snap.other_procs,
            "free_worktrees": [c["name"] for c in wts
                               if c["availability"] == "free"]}


def from_client(client):
    """And from the Python reference applier, so the two appliers are checked
    against each other and not only against the server."""
    wts = list(client.cards.values())
    return {"names": list(client.cards), "cards": wts, "counts": client.counts,
            "other_procs": client.other,
            "free_worktrees": [c["name"] for c in wts
                               if c["availability"] == "free"]}


@unittest.skipUnless(NODE, "node not available")
class StreamJS(unittest.TestCase):
    """Every test here builds frames with the real Observer and applies them
    with the real stream.js."""

    def run_js(self, steps):
        driver = ROOT / "tests" / ".stream_driver.js"
        driver.write_text(DRIVER)
        self.addCleanup(lambda: driver.unlink(missing_ok=True))
        proc = subprocess.run([NODE, str(driver), str(STREAM_JS)],
                              input=json.dumps(steps), capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_the_shipped_applier_lands_where_a_snapshot_would(self):
        """The whole claim, end to end: a sequence of publishes, streamed as
        one snapshot and then deltas, leaves stream.js holding exactly the view
        a client that took each snapshot whole would hold — and exactly what
        the Python reference applier holds.

        The script walks EVERY term that can bump the version — cards, `counts`,
        `other_procs` — because a term that moves `v` and does not reach this
        applier is a board that is told something changed and cannot find out
        what (observer.delta_since's audit)."""
        obs, ref = fb.Observer(), Client()
        script = [
            ([("alpha", 0, "working", "busy"), ("beta", 0, "working", "busy")], (9,), None),
            ([("alpha", 1, "working", "busy"), ("beta", 0, "working", "busy")], (9,), None),
            # beta needs input: the server re-sorts it to the top, and the delta
            # names beta ONLY — alpha's position has to come off `order`
            ([("beta", 0, "needs_input", "attention"),
              ("alpha", 1, "working", "busy")], (9,), None),
            # a card arrives, and one of them is FREE (a derived field)
            ([("beta", 0, "needs_input", "attention"),
              ("alpha", 1, "working", "busy"),
              ("gamma", 0, "ended", "free")], (9,), None),
            # a card leaves — it must arrive as an explicit removal
            ([("beta", 0, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (9,), None),
            # nothing but a loose claude process appears: the version moves with
            # NO card changing at all
            ([("beta", 0, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (9, 11), None),
            # …and one of them exits, and then BOTH do. The empty list is the
            # boundary the applier has to get right: `[]` is truthy in JS and
            # must overwrite, where a tempting `f.other_procs.length` guard
            # would silently leave the ⌁ tile showing processes that are gone.
            ([("beta", 0, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (11,), None),
            ([("beta", 0, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (), None),
            # counts alone — the third bump term, and the only one whose change
            # is invisible on the cards
            ([("beta", 0, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (), {"needs_input": 1, "ended": 1}),
            # a card AND the loose list in the same frame: the diffed field and
            # a whole one have to land together
            ([("beta", 1, "needs_input", "attention"),
              ("gamma", 0, "ended", "free")], (13,), {"needs_input": 1, "ended": 1}),
        ]
        steps, expected = [], []
        for i, (cards, other, counts) in enumerate(script):
            obs.publish(fleet_state(1000.0 + i, cards, other=other, counts=counts))
            frame = obs.delta_since(ref.v or 0)
            ref.apply(frame)
            steps.append({"op": "apply", "frame": frame})
            expected.append((from_snapshot(obs.snapshot()), from_client(ref)))
        self.assertEqual([s["frame"]["type"] for s in steps][1:],
                         ["delta"] * (len(script) - 1), "only the first is a snapshot")

        got = self.run_js(steps)
        for i, (res, (want_snap, want_ref)) in enumerate(zip(got, expected)):
            self.assertEqual(res["verdict"], "applied", f"step {i}")
            self.assertEqual(board(res["state"]), want_snap, f"step {i} vs snapshot")
            self.assertEqual(board(res["state"]), want_ref, f"step {i} vs reference")

    def test_a_coalesced_delta_is_applied_not_mistaken_for_a_gap(self):
        """The server waits on the version and THEN asks for a delta, so a busy
        fleet produces frames whose `v` jumps by more than one. An applier that
        tested `v === last + 1` would call every one of those a lost frame and
        reconnect — a reconnect loop exactly when the board is busiest."""
        obs = fb.Observer()
        obs.publish(fleet_state(1000.0, [("alpha", 0, "working", "busy")]))
        first = obs.delta_since(0)
        for i in range(3):
            obs.publish(fleet_state(1001.0 + i,
                                    [("alpha", i + 1, "working", "busy")]))
        catchup = obs.delta_since(first["v"])
        self.assertGreater(catchup["v"], catchup["base"] + 1, "not coalesced")

        got = self.run_js([{"op": "apply", "frame": first},
                           {"op": "apply", "frame": catchup}])
        self.assertEqual([r["verdict"] for r in got], ["applied", "applied"])
        self.assertEqual(board(got[-1]["state"]), from_snapshot(obs.snapshot()))

    def test_a_frame_whose_base_is_not_ours_is_a_gap_and_changes_nothing(self):
        """A genuinely missed frame. The applier must refuse it — and refuse it
        WITHOUT half-applying it, since the worker throws the view away and
        reconnects for a fresh snapshot."""
        obs = fb.Observer()
        obs.publish(fleet_state(1000.0, [("alpha", 0, "working", "busy")]))
        first = obs.delta_since(0)
        obs.publish(fleet_state(1001.0, [("alpha", 1, "working", "busy"),
                                         ("beta", 0, "working", "busy")]))
        missed = obs.delta_since(first["v"])
        obs.publish(fleet_state(1002.0, [("alpha", 2, "working", "busy"),
                                         ("beta", 0, "working", "busy")]))
        after = obs.delta_since(missed["v"])       # base = the frame we "lost"

        got = self.run_js([{"op": "apply", "frame": first},
                           {"op": "apply", "frame": after}])
        self.assertEqual([r["verdict"] for r in got], ["applied", "gap"])
        # unchanged: still the single card from the first frame
        self.assertEqual(board(got[1]["state"])["names"], ["alpha"])
        self.assertEqual(got[1]["v"], first["v"])

    def test_a_delta_on_a_polled_seed_is_a_gap_rather_than_a_guess(self):
        """`/api/state` carries no version, so a client seeded from it has no
        base a delta could be applied to. Applying one anyway would patch a
        view whose age is unknown."""
        obs = fb.Observer()
        obs.publish(fleet_state(1000.0, [("alpha", 0, "working", "busy")]))
        body = dict(fb.observer.demo_state())          # any /api/state shape
        obs.publish(fleet_state(1001.0, [("alpha", 1, "working", "busy")]))
        got = self.run_js([{"op": "seed", "body": body},
                           {"op": "apply", "frame": obs.delta_since(1)}])
        self.assertEqual(got[0]["verdict"], "seeded")
        self.assertIsNone(got[0]["v"], "a polled seed has no version")
        self.assertEqual(got[1]["verdict"], "gap")

    def test_a_snapshot_always_applies_and_replaces_everything(self):
        """The resync path: whatever the client was holding, a snapshot lands.
        `cards` is replaced, never merged — a card that disappeared while the
        client was away must not survive its own removal."""
        obs = fb.Observer()
        obs.publish(fleet_state(1000.0, [("alpha", 0, "working", "busy"),
                                         ("beta", 0, "working", "busy")]))
        first = obs.delta_since(0)
        obs.publish(fleet_state(1001.0, [("gamma", 0, "working", "busy")]))
        fresh = obs.delta_since(0)
        self.assertEqual(fresh["type"], "snapshot")
        got = self.run_js([{"op": "apply", "frame": first},
                           {"op": "apply", "frame": fresh}])
        self.assertEqual([r["verdict"] for r in got], ["applied", "applied"])
        self.assertEqual(board(got[1]["state"])["names"], ["gamma"])

    def test_the_composed_state_has_the_shape_the_board_renders(self):
        """render() reads eight keys off /api/state. The worker rebuilds that
        object from frames plus the side fetch, and a missing key is a blank
        board rather than an error."""
        obs = fb.Observer()
        obs.publish(fleet_state(1000.0, [("alpha", 0, "working", "busy"),
                                         ("gamma", 0, "ended", "free")]))
        got = self.run_js([{"op": "apply", "frame": obs.delta_since(0)}])
        st = got[0]["state"]
        self.assertEqual(set(st), {"generated_at", "hostname", "user", "counts",
                                   "free_worktrees", "worktrees", "other_procs",
                                   "resumes"})
        self.assertEqual(st["free_worktrees"], ["gamma"])
        self.assertEqual(st["generated_at"], 1000.0)

    def test_the_worker_half_never_runs_under_node(self):
        """The export guard is what lets the tests above drive the SHIPPED
        file. If the worker half ever ran here it would touch EventSource and
        `onconnect` and blow up — and the safe fix would be to test a copy,
        which is how a test stops being about the code that ships."""
        driver = ROOT / "tests" / ".stream_probe.js"
        # Same exit trap as the main driver: requiring stream.js arms
        # setInterval(pump, 1000), and a pending timer keeps node alive forever,
        # so this probe hung for its whole timeout instead of answering.
        driver.write_text('const m = require(process.argv[2]);'
                          'process.stdout.write(JSON.stringify(Object.keys(m)));'
                          'process.exit(0);')
        self.addCleanup(lambda: driver.unlink(missing_ok=True))
        proc = subprocess.run([NODE, str(driver), str(STREAM_JS)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), ["Fleet"])


class StreamJSIsServed(unittest.TestCase):
    """The worker must be reachable at a STABLE same-origin URL: a SharedWorker
    is identified by (origin, script URL, name), so a Blob URL — the obvious
    way to avoid a route — would give every tab its own worker, its own stream,
    and the six-connection ceiling the worker exists to stay under."""

    def test_the_file_exists_beside_the_page_it_serves(self):
        self.assertTrue(STREAM_JS.exists())

    def test_the_server_serves_it_as_javascript(self):
        import http.client
        import io
        import threading

        h = fb.Handler.__new__(fb.Handler)
        h.path = "/stream.js"
        h.command, h.request_version = "GET", "HTTP/1.1"
        h.requestline = "GET /stream.js HTTP/1.1"
        h.client_address = ("127.0.0.1", 1)
        h.close_connection = True
        h.headers = http.client.HTTPMessage()
        h.wfile = io.BytesIO()
        h.rfile = io.BytesIO()
        t = threading.Thread(target=h.do_GET, daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        out = h.wfile.getvalue()
        self.assertIn(b"200", out.split(b"\r\n", 1)[0])
        self.assertIn(b"Content-Type: application/javascript", out)
        self.assertIn(b"SharedWorker", out)


if __name__ == "__main__":
    unittest.main()
