#!/usr/bin/env python3
"""Prove orchestra/qr.py against two implementations that are not orchestra's.

    python3 tests/qr_ref.py [-v]

This is NOT part of `python3 -m unittest discover -s tests`, on purpose: it
compiles a Swift file and shells out per case, which is seconds rather than
milliseconds, and a slow step in the safety net is a step people stop running.
What the unit suite keeps instead is `tests/test_qr.py`, whose golden matrices
were recorded from a run of THIS script — so the fast tests fail the moment the
encoder drifts away from output that was externally proven correct.

Two checks per case, and they fail differently on purpose:

  ENCODER   Apple's `CIQRCodeGenerator` encodes the same payload at the same
            error correction level; we compare matrices module by module. Both
            implementations take the smallest version that fits and the mask
            with the lowest penalty score, so a conforming encoder produces a
            bit-identical symbol. A mismatch localises the bug — a wrong
            generator polynomial moves only the EC region, a wrong mask moves
            everything, a wrong format field moves fifteen known modules.

  DECODER   Vision's `VNDetectBarcodesRequest` reads a PNG rendered from OUR
            matrix and must return the string that went in. This is the check
            that answers the question the user actually has, which is not "is
            it correct" but "will a phone read it".

WHAT THIS HARNESS CANNOT SEE, measured rather than assumed: **the quiet zone.**
Setting `qr.QUIET = 0` leaves every case here green — Vision reads a
margin-less code perfectly out of a clean synthetic PNG. The margin exists for
a camera pointed at a screen that has other things on it, and nothing automated
here reproduces that. `tests/test_qr.py` pins it as a literal instead, and that
literal is the whole of the evidence for it.

METHOD.md §4: a test that cannot fail is not a test. Four mutations were
applied to `orchestra/qr.py` and run through this script: a shifted RS
generator and a reversed format field are caught here (Vision reports no
barcode at all, and the matrix comparison localises them); an unreserved
version-information block never reaches either check, because `encode`'s own
spare-module arithmetic raises first; and the quiet zone is the one above.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orchestra import qr  # noqa: E402

SOURCE = Path(__file__).resolve().parent / "qr_ref.swift"

# WHERE THE TWO ENCODERS LEGITIMATELY DIVERGE, because getting this wrong turns
# a passing oracle into a noisy one and a noisy oracle gets switched off.
#
# `CIQRCodeGenerator` performs SEGMENT OPTIMISATION: a run of digits inside an
# otherwise-textual payload is re-encoded as a numeric segment, which is denser,
# and a run of uppercase and punctuation becomes an alphanumeric segment. Both
# are conforming, and both change every data bit downstream. orchestra/qr.py
# encodes the whole payload in byte mode — deliberately, since the saving on a
# forty-byte URL is a fraction of one version — so on a payload containing
# digits the two produce different, equally valid symbols.
#
# That is a real difference and not a bug, so it is split rather than papered
# over. A payload of lowercase and punctuation alone leaves the reference no
# denser mode to reach for, and there the comparison is exact.

# Byte mode is forced for both encoders: compared MODULE FOR MODULE.
MATRIX_CASES = [
    ("a", "M"),
    ("a", "H"),
    ("~!@#$%^&*()_+-=[]{}|;:,.<>?", "M"),
    ("orc", "L"),
    ("orc://p?h=achills-macbook-pro.tail.ts.net", "M"),
    ("orc://p?h=achills-macbook-pro.tail.ts.net", "L"),
    ("orc://p?h=achills-macbook-pro.tail.ts.net", "Q"),
    ("orc://p?h=achills-macbook-pro.tail.ts.net", "H"),
    ("x" * 100, "M"),
    ("x" * 120, "Q"),           # version 9
    ("x" * 130, "M"),           # version 8
    ("x" * 150, "L"),           # version 7 — the first with version information
    ("x" * 200, "L"),
    ("x" * 213, "M"),           # the exact ceiling of version 10 at level M
]

# The payloads this feature actually renders. The reference may pick a denser
# mode here, so only the DECODER check applies — which is the check that
# answers the user's real question anyway: will a phone read it.
DECODE_CASES = [
    ("orc://p?h=100.113.110.31&p=4242&c=7K3M9QP2", "M"),
    ("orc://p?h=100.113.110.31&c=7K3M9QP2", "M"),
    ("orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4242&c=7K3M9QP2", "M"),
    ("orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4242&c=7K3M9QP2", "L"),
    ("orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4242&c=7K3M9QP2", "Q"),
    ("orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4242&c=7K3M9QP2", "H"),
    ("http://100.113.110.31:4242/pair", "M"),
    ("0123456789", "L"),
    ("orc://p?h=" + "n" * 60 + "&p=4242&c=ABCD2345", "M"),
]


def build(verbose=False):
    """Compile the Swift helper into a temp binary. Returns its path."""
    out = Path(tempfile.mkdtemp(prefix="qr-ref-")) / "qr_ref"
    cmd = ["swiftc", "-O", "-o", str(out), str(SOURCE)]
    if verbose:
        print("  $", " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"could not build {SOURCE}:\n{r.stderr}")
    return out


def reference(binary, payload, level):
    """Apple's matrix for this payload, as a list of rows of ints."""
    r = subprocess.run([str(binary), "generate", payload, level],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return [[int(c) for c in line] for line in r.stdout.split() if line]


def decode(binary, png_bytes):
    """What Vision reads out of these PNG bytes."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png_bytes)
        path = f.name
    try:
        r = subprocess.run([str(binary), "decode", path],
                           capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr.strip())
        return r.stdout.rstrip("\n")
    finally:
        Path(path).unlink(missing_ok=True)


def diff(ours, theirs):
    """A sentence naming the first disagreement, or None."""
    if len(ours) != len(theirs):
        return (f"size {len(ours)} (version {(len(ours) - 17) // 4}) against "
                f"reference {len(theirs)} (version {(len(theirs) - 17) // 4})")
    wrong = [(r, c) for r in range(len(ours)) for c in range(len(ours))
             if ours[r][c] != theirs[r][c]]
    if not wrong:
        return None
    return (f"{len(wrong)} of {len(ours) ** 2} modules differ, first at "
            f"row {wrong[0][0]} col {wrong[0][1]}")


def main():
    verbose = "-v" in sys.argv
    binary = build(verbose)
    bad = 0
    for payload, level, compare in ([(p, l, True) for p, l in MATRIX_CASES] +
                                    [(p, l, False) for p, l in DECODE_CASES]):
        label = f"{level} {len(payload):>3}B  {payload[:42]!r}"
        try:
            ours = qr.encode(payload, level)
        except ValueError as e:
            print(f"FAIL  {label}\n      encode raised: {e}")
            bad += 1
            continue
        version = (len(ours) - 17) // 4

        if compare:
            problem = diff(ours, reference(binary, payload, level))
            if problem:
                print(f"FAIL  {label}\n      ENCODER: {problem}")
                bad += 1
                continue

        got = decode(binary, qr.png(ours))
        if got != payload:
            print(f"FAIL  {label}\n      DECODER: Vision read {got!r}")
            bad += 1
            continue
        print(f"ok    v{version:<2} {'encoder+decoder' if compare else 'decoder      '}"
              f"  {label}")

    total = len(MATRIX_CASES) + len(DECODE_CASES)
    print()
    if bad:
        sys.exit(f"{bad} of {total} cases FAILED")
    print(f"qr OK — {len(MATRIX_CASES)} cases match Apple's CIQRCodeGenerator "
          f"module for module; Vision decodes all {total} back to the exact "
          f"payload that went in")


if __name__ == "__main__":
    main()
