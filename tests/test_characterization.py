"""The characterization snapshot, as a test, so CI enforces it.

See tests/characterize.py for why this exists: the unit suite monkeypatches module globals at
67 sites, and a module split silently disarms those patch points. This check patches nothing,
so it stays honest across the refactor.

To accept an intentional behaviour change, re-record and commit the golden in the SAME commit
that changes behaviour, so the diff shows exactly which cases moved:

    python3 tests/characterize.py --record
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import characterize  # noqa: E402


class TestCharacterization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not characterize.GOLDEN.exists():
            raise unittest.SkipTest("no golden recorded; run tests/characterize.py --record")
        cls.mod, cls.layout = characterize.load_orchestra()
        cls.snap = characterize.build(cls.mod)
        cls.dig = characterize.digest(cls.snap)
        import json
        cls.want = json.loads(characterize.GOLDEN.read_text())

    def test_every_pinned_function_is_still_reachable(self):
        """The package facade must export everything the single file did."""
        missing = [k for k in self.want["digests"] if k not in self.dig]
        self.assertEqual([], missing,
                         f"function(s) no longer reachable from the top-level module: {missing}")

    def test_behaviour_is_unchanged(self):
        drift = [k for k in sorted(self.want["digests"])
                 if self.want["digests"][k]["sha256"] != self.dig.get(k, {}).get("sha256")]
        if not drift:
            return
        detail = []
        for k in drift:
            import json
            old = {json.dumps(c, sort_keys=True, default=str)
                   for c in self.want["snapshot"].get(k, [])}
            new = {json.dumps(c, sort_keys=True, default=str) for c in self.snap.get(k, [])}
            for line in sorted(new - old)[:2]:
                detail.append(f"  {k} now: {line[:160]}")
            for line in sorted(old - new)[:2]:
                detail.append(f"  {k} was: {line[:160]}")
        self.fail(f"behaviour changed in {drift} ({self.layout} layout):\n" + "\n".join(detail))

    def test_case_counts_did_not_shrink(self):
        """Guards against a silently narrowed input space hiding a regression."""
        for k, v in self.want["digests"].items():
            self.assertGreaterEqual(
                self.dig.get(k, {}).get("cases", 0), v["cases"],
                f"{k}: fewer cases exercised than when the golden was recorded")


if __name__ == "__main__":
    unittest.main()
