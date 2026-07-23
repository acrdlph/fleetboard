"""Regression tests for the PROCS fix wave (procs.py, limits.py, config.py).

Each test FAILS on the pre-fix code and PASSES after. The suite mocks by
reassigning module attributes (`fb.shell.run`, `fb.CFG`, `fb._limits`), so it
never touches a real subprocess or the developer's config file.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestra as fb  # noqa: E402


class _Guard(unittest.TestCase):
    """Snapshot & restore every module global these tests poke."""

    def setUp(self):
        self._cfg = dict(fb.CFG)
        self._limits = dict(fb._limits)
        self._flight = dict(getattr(fb.limits, "_limits_flight", {}))
        self._demo = fb.config.DEMO
        self._cpath = fb.config.CONFIG_PATH
        self._run = fb.shell.run
        self._port = os.environ.get("ORCHESTRA_PORT")

    def tearDown(self):
        fb.CFG.clear(); fb.CFG.update(self._cfg)
        fb._limits.clear(); fb._limits.update(self._limits)
        if hasattr(fb.limits, "_limits_flight"):
            fb.limits._limits_flight.clear(); fb.limits._limits_flight.update(self._flight)
        fb.config.DEMO = self._demo
        fb.config.CONFIG_PATH = self._cpath
        fb.shell.run = self._run
        if self._port is None:
            os.environ.pop("ORCHESTRA_PORT", None)
        else:
            os.environ["ORCHESTRA_PORT"] = self._port


# ------------------------------------------------------------------- F1: ps locale

class TestPsLocaleIsForced(_Guard):
    """macOS `ps` formats `lstart=` per the process's locale; a non-English
    Mac emits e.g. 'mar. 14 juil. …', which `_PS_ROW` cannot match, so the
    date spills into the command column and `claude_processes` returns [].
    Forcing C on the ps children keeps lstart parseable everywhere."""

    def _capture(self):
        seen = []

        def fake(cmd, *a, **k):
            seen.append({"cmd": list(cmd), "env": k.get("env")})
            return (0, "")

        fb.shell.run = fake
        return seen

    def _assert_c_locale(self, call):
        # The locale is forced through the ENVIRONMENT, not by wrapping the argv
        # in `env` — so the ps command line stays exactly ["ps", ...] (which the
        # rest of the suite asserts) while the child still gets C for lstart.
        env = call["env"]
        self.assertIsNotNone(env, "ps must run with an explicit environment")
        self.assertEqual(env.get("LC_ALL"), "C")
        self.assertEqual(env.get("LC_TIME"), "C")
        self.assertIn("PATH", env, "the forced env must keep PATH so ps is found")

    def test_process_table_ps_runs_under_c_locale(self):
        seen = self._capture()
        fb.claude_processes()
        ps_calls = [c for c in seen if "ps" in c["cmd"]]
        self.assertTrue(ps_calls, "expected claude_processes to shell out to ps")
        for c in ps_calls:
            self._assert_c_locale(c)

    @unittest.skipIf(sys.platform.startswith("linux"), "the ps eww probe is macOS/BSD only")
    def test_env_probe_ps_runs_under_c_locale(self):
        seen = self._capture()
        fb.procs._pid_config_dirs([4242])
        ps_calls = [c for c in seen if "ps" in c["cmd"]]
        self.assertTrue(ps_calls)
        for c in ps_calls:
            self._assert_c_locale(c)


# --------------------------------------------------------------- F2: atomic reserve

class TestSetReserveIsAtomic(_Guard):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"pattern": "keep", "reserve_percent": {}}, self.tmp)
        self.tmp.close()
        fb.config.CONFIG_PATH = Path(self.tmp.name)
        fb.CFG["reserve_percent"] = {}
        fb._limits["data"] = None

    def tearDown(self):
        Path(self.tmp.name).unlink(missing_ok=True)
        Path(self.tmp.name).with_suffix(".tmp").unlink(missing_ok=True)
        super().tearDown()

    def test_persist_goes_through_os_replace(self):
        """A bare write_text is not crash-safe; the fix must temp-file +
        os.replace so a mid-write crash cannot truncate the config."""
        calls = []
        real_replace = os.replace

        def spy(src, dst, *a, **k):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *a, **k)

        os.replace = spy
        try:
            res = fb.set_reserve("main", 20)
        finally:
            os.replace = real_replace
        self.assertTrue(res["ok"])
        self.assertTrue(calls, "set_reserve must publish via os.replace")
        self.assertEqual(calls[0][1], self.tmp.name)
        disk = json.loads(Path(self.tmp.name).read_text())
        self.assertEqual(disk["reserve_percent"], {"main": 20})
        self.assertEqual(disk["pattern"], "keep")  # untouched

    def test_writer_holds_a_lock(self):
        self.assertIsInstance(fb.limits._reserve_lock, type(threading.Lock()))

    def test_no_stray_tmp_file_left_behind(self):
        fb.set_reserve("main", 30)
        self.assertFalse(Path(self.tmp.name).with_suffix(".tmp").exists())


# ---------------------------------------------------- F3: single-flight + backoff

class _LimitsFetchGuard(_Guard):
    def setUp(self):
        super().setUp()
        fb.config.DEMO = False
        fb.CFG["cclimits_cmd"] = "/usr/bin/true"   # a real path so _cclimits_bin resolves
        fb.CFG["reserve_percent"] = {}
        fb._limits["data"], fb._limits["t"] = None, 0.0
        if hasattr(fb.limits, "_limits_flight"):
            fb.limits._limits_flight.clear()
            fb.limits._limits_flight.update({"busy": False, "fail_t": 0.0})


class TestFailureBackoff(_LimitsFetchGuard):
    def test_a_broken_cclimits_is_not_respawned_every_call(self):
        n = [0]

        def failing(cmd, *a, **k):
            n[0] += 1
            return (1, "")            # cclimits crashed / timed out

        fb.shell.run = failing
        fb.cached_limits()            # first stale poll: one real spawn, fails
        self.assertEqual(n[0], 1)
        fb.cached_limits()            # within the backoff window: no re-spawn
        fb.cached_limits()
        self.assertEqual(n[0], 1, "backoff must suppress re-spawning a failed fetch")


class TestSingleFlight(_LimitsFetchGuard):
    def test_a_flight_in_progress_serves_stale_without_spawning(self):
        stale = {"available": True, "accounts": [], "fetched_at": 1.0}
        fb._limits["data"], fb._limits["t"] = stale, 0.0   # TTL long lapsed
        fb.limits._limits_flight["busy"] = True             # someone is fetching
        n = [0]

        def counted(cmd, *a, **k):
            n[0] += 1
            return (0, json.dumps({"accounts": []}))

        fb.shell.run = counted
        got = fb.cached_limits()      # non-refresh, stale, flight busy
        self.assertIs(got, stale)
        self.assertEqual(n[0], 0, "a busy flight must not spawn a second subprocess")

    def test_explicit_refresh_still_fetches_while_a_flight_is_busy(self):
        fb._limits["data"], fb._limits["t"] = {"available": True, "accounts": []}, 0.0
        fb.limits._limits_flight["busy"] = True
        n = [0]

        def counted(cmd, *a, **k):
            n[0] += 1
            return (0, json.dumps({"accounts": [
                {"slug": "s", "config_dir": "/h/.claude", "ok": True,
                 "headroom_percent": 50.0, "limits": []}]}))

        fb.shell.run = counted
        data = fb.cached_limits(refresh=True)
        self.assertEqual(n[0], 1)
        self.assertTrue(data["available"])


# ------------------------------------------------- F4: payload normalisation

class TestPayloadNormalisation(_LimitsFetchGuard):
    def _fetch(self, raw):
        fb.shell.run = lambda *a, **k: (0, json.dumps(raw))
        return fb.cached_limits(refresh=True)

    def test_account_without_a_usable_config_dir_is_dropped(self):
        raw = {"accounts": [
            {"slug": "good", "config_dir": "/h/.claude", "ok": True,
             "headroom_percent": 40.0, "limits": []},
            {"slug": "bad", "config_dir": None, "ok": True,
             "headroom_percent": 10.0, "limits": []}]}
        data = self._fetch(raw)
        dirs = [a["config_dir"] for a in data["accounts"]]
        self.assertEqual(dirs, ["/h/.claude"])
        # limits_by_account iterates the cached payload doing Path(config_dir);
        # a None config_dir used to TypeError every observer sweep.
        self.assertIn("main", fb.limits_by_account())

    def test_null_label_is_coerced_so_model_remaining_cannot_crash(self):
        raw = {"accounts": [
            {"slug": "good", "config_dir": "/h/.claude", "ok": True,
             "headroom_percent": 40.0, "limits": [
                 {"label": None, "model_scoped": True, "exhausted_now": False,
                  "remaining_percent": 50}]}]}
        data = self._fetch(raw)
        acc = data["accounts"][0]
        self.assertEqual(acc["limits"][0]["label"], "")
        # a model-scoped limit with a null label used to raise
        # AttributeError on None.lower(); now it just does not match.
        self.assertIsNone(fb._model_remaining(acc, "opus"))
        self.assertEqual(fb.model_candidates("opus"), [])

    def test_a_non_object_payload_is_rejected_not_crashed(self):
        fb.shell.run = lambda *a, **k: (0, "42")
        out = fb.cached_limits(refresh=True)
        self.assertFalse(out.get("available"))


# ------------------------------------------------------- F6: config validation

class TestConfigValidation(_Guard):
    def _write(self, blob):
        d = tempfile.mkdtemp(prefix="fb-cfg-")
        p = Path(d) / "orchestra.config.json"
        p.write_text(blob)
        return p

    def test_non_object_config_exits_cleanly(self):
        p = self._write("42")
        with self.assertRaises(SystemExit):
            fb.load_config(["--config", str(p)])

    def test_bad_pattern_exits_at_boot(self):
        p = self._write(json.dumps({"pattern": "([unclosed"}))
        with self.assertRaises(SystemExit):
            fb.load_config(["--config", str(p)])

    def test_valid_config_still_loads(self):
        p = self._write(json.dumps({"pattern": "confid", "idle_s": 1.5}))
        fb.load_config(["--config", str(p)])
        self.assertEqual(fb.CFG["pattern"], "confid")
        self.assertEqual(fb.CFG["idle_s"], 1.5)

    def test_bad_env_port_exits_cleanly(self):
        p = self._write(json.dumps({"pattern": ""}))
        os.environ["ORCHESTRA_PORT"] = "not-a-number"
        with self.assertRaises(SystemExit):
            fb.load_config(["--config", str(p)])


if __name__ == "__main__":
    unittest.main()
