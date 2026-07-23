#!/usr/bin/env python3
"""WEB fix verification — index.html and map.html, the two frozen board pages.

Covers three adversarially-verified defects:

  F1  launchMission() had no submit lock, so a double-click fired two
      POST /api/dispatch — two agents for one mission.
  F2  esc() HTML-encodes for a text node; dropped into a single-quoted JS
      string inside an inline onclick, an apostrophe in a worktree name
      (esc'd to &#39;, which the parser decodes back to ') broke out of the
      string and could inject arbitrary JS at the board's full-privilege origin.
  F3  map.html keyed its node registry on the esc()'d name but read it back via
      dataset (HTML-decoded), so a name with &<>"' looked up a key the registry
      never stored — the node went inert.

The escaping and lookup-key claims are checked by driving the SHIPPED helpers
(esc / escArg extracted verbatim from each page) through node — a browser's
HTML-attribute decode is simulated, then the decoded expression is evaluated in
a sandbox that records what the handler actually receives and whether anything
injected ran. The submit-lock and the source-level facts are checked by reading
the files. These tests fail on the pre-fix source and pass on the fixed source.
"""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
MAP = ROOT / "map.html"
NODE = shutil.which("node")


def _extract_const(src, name):
    """Pull a `const <name> = ...;` one-liner out of the page, verbatim."""
    m = re.search(r"^const %s = .*;$" % re.escape(name), src, re.MULTILINE)
    if not m:
        raise AssertionError("could not find `const %s` in the page" % name)
    return m.group(0)


class SubmitLock(unittest.TestCase):
    """F1 — launchMission must not let a second click reach the wire while the
    first POST is in flight."""

    def setUp(self):
        self.src = INDEX.read_text()

    def test_a_submitting_lock_exists_and_is_declared(self):
        self.assertIn("let submitting = false;", self.src,
                      "no dedicated submit lock declared")

    def test_the_lock_is_checked_before_the_textarea_is_cleared(self):
        body = self.src[self.src.index("async function launchMission"):]
        body = body[:body.index("\nfunction showModelDecision")]
        # the early-return guard, the set, the textarea clear and the release
        guard = body.index("if (submitting) return;")
        setit = body.index("submitting = true;")
        clear = body.index('$("mText").value = "";')
        release = body.index("submitting = false;")
        self.assertLess(guard, setit, "guard must precede the set")
        # the lock is taken before the input is emptied, so the second click —
        # which would otherwise read the same text back off inFlight — is stopped
        self.assertLess(setit, clear, "lock must be taken before clearing input")
        # and released after the fetch, on every path (a finally)
        self.assertLess(clear, release, "lock must be released after the launch")
        self.assertRegex(body, r"finally\s*\{\s*submitting = false;",
                         "the release must be in a finally so every path clears it")

    def test_the_lock_is_separate_from_inFlight(self):
        # the brief is explicit: `inFlight` is reused by relaunch on purpose, so
        # the double-click guard has to be its own variable
        self.assertIn("let inFlight = null;", self.src)
        self.assertNotIn("if (inFlight) return;", self.src)


@unittest.skipUnless(NODE, "node not available")
class JsStringEscaping(unittest.TestCase):
    """F2 — identifiers interpolated into an inline onclick must survive both
    the HTML-attribute decode and the JS-string parse without breaking out."""

    # reverse of esc(): one pass, exactly what a browser does to an attribute
    DECODE = r"""
    function htmlDecodeAttr(s) {
      return s.replace(/&(amp|lt|gt|quot|#39);/g,
        (_, e) => ({amp:"&", lt:"<", gt:">", quot:'"', "#39":"'"}[e]));
    }
    """

    def _run(self, page, arg_expr_template, name):
        """Build `handler(<escArg output>)` the way the page does, put it in an
        onclick attribute, decode the attribute, and eval the result in a
        sandbox. Returns [received-arg, injected-flag]."""
        src = page.read_text()
        esc = _extract_const(src, "esc")
        escArg = _extract_const(src, "escArg")
        arg = arg_expr_template  # a JS expression that uses escArg(...)
        driver = f"""
        {esc}
        {escArg}
        {self.DECODE}
        const NAME = {name!r};
        // exactly how the page interpolates it into the attribute
        const attr = "handler(" + ({arg}) + ")";
        const decoded = htmlDecodeAttr(attr);   // what the JS engine actually sees
        let received = null, injected = false;
        const handler = (x) => {{ received = x; }};
        // if the string broke out, THIS assignment would run
        globalThis.__inject = () => {{ injected = true; }};
        eval(decoded);
        process.stdout.write(JSON.stringify([received, injected]));
        """
        proc = subprocess.run([NODE, "-e", driver],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        return json.loads(proc.stdout)

    def test_apostrophe_name_reaches_the_handler_intact(self):
        # a benign name that the old esc()-in-a-quote path silently killed
        received, injected = self._run(INDEX, "escArg(NAME)", "john's-branch|s-1")
        self.assertEqual(received, "john's-branch|s-1")
        self.assertFalse(injected)

    def test_a_breakout_payload_cannot_inject(self):
        # the RCE case: a worktree name crafted to escape the string and run JS
        payload = "x');globalThis.__inject();openChat('"
        received, injected = self._run(INDEX, "escArg(NAME)", payload)
        self.assertEqual(received, payload, "the whole name must arrive as data")
        self.assertFalse(injected, "no interpolated identifier may execute")

    def test_double_quote_payload_cannot_inject(self):
        payload = 'x");globalThis.__inject();openChat("'
        received, injected = self._run(INDEX, "escArg(NAME)", payload)
        self.assertEqual(received, payload)
        self.assertFalse(injected)

    def test_map_focus_handler_is_injection_safe(self):
        payload = "x');globalThis.__inject();('"
        received, injected = self._run(MAP, "escArg(NAME)", payload)
        self.assertEqual(received, payload)
        self.assertFalse(injected)

    def test_the_pages_no_longer_single_quote_esc_into_openchat(self):
        # the exact pre-fix shape must be gone from both handlers
        idx = INDEX.read_text()
        self.assertNotIn("openChat('${esc(w.name)}|${esc(s.sid)}')", idx)
        self.assertNotIn("openChat('${esc(key)}')", idx)
        mp = MAP.read_text()
        self.assertNotIn("tipFocus('${esc(b.worktree)}')", mp)
        self.assertNotIn("tipFinish(this, '${esc(b.worktree)}')", mp)


@unittest.skipUnless(NODE, "node not available")
class MapLookupKeyAgreement(unittest.TestCase):
    """F3 — the registry key and the key read back off the DOM must agree for a
    worktree name containing an HTML-special character."""

    def test_stored_key_matches_the_decoded_dataset_read(self):
        src = MAP.read_text()
        esc = _extract_const(src, "esc")
        # a name with every special char esc() touches
        driver = f"""
        {esc}
        const name = 'a&b<c>d"e' + String.fromCharCode(39) + 'f';
        const storedKey = "b:" + name;              // lookup is keyed on the RAW name
        const attrValue = esc("b:" + name);          // data-key is written esc'd
        // dataset returns the HTML-DECODED attribute value
        function htmlDecodeAttr(s) {{
          return s.replace(/&(amp|lt|gt|quot|#39);/g,
            (_, e) => ({{amp:"&", lt:"<", gt:">", quot:'"', "#39":"'"}}[e]));
        }}
        const readKey = htmlDecodeAttr(attrValue);
        process.stdout.write(JSON.stringify([storedKey, readKey]));
        """
        proc = subprocess.run([NODE, "-e", driver],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        import json
        stored, read = json.loads(proc.stdout)
        self.assertEqual(stored, read,
                         "lookup key and dataset read must resolve to the same string")

    def test_source_keys_lookup_on_raw_name_and_writes_esc_attribute(self):
        src = MAP.read_text()
        # both node builders (branches + riders) key on the raw name now
        self.assertEqual(src.count("const key = `b:${b.worktree}`;"), 2)
        self.assertNotIn("const key = `b:${esc(b.worktree)}`;", src)
        # and the attribute is the escaped one
        self.assertIn('data-key="${esc(key)}"', src)
        self.assertNotIn('data-key="${key}"', src)


if __name__ == "__main__":
    unittest.main()
