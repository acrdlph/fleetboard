#!/usr/bin/env python3
"""The QR encoder, pinned — without needing a camera or a Swift compiler.

The claim this file makes is narrower than "the encoder is correct", and saying
so plainly is the point. Correctness was established ONCE, externally, by
`tests/qr_ref.py`: thirteen payloads that came out module-for-module identical
to Apple's `CIQRCodeGenerator`, and twenty-two that Vision decoded back to the
exact string that went in. What THIS file does is stop the encoder drifting
away from that proven output, in 30 ms, on every run of the suite.

Four kinds of evidence, deliberately different from each other:

* **Golden matrices** — the literal modules of codes that passed the external
  check, and sha256 digests for the large ones. A change anywhere in the
  pipeline moves these.
* **Reed–Solomon syndromes** — a property of the code (`P(a^i) = 0` for every
  check position), not a re-run of `ec_codewords`. This is the only check here
  that would catch a generator polynomial that is wrong CONSISTENTLY, which a
  golden recorded from the same wrong code could not.
* **The tables, recomputed** — every `(version, level)` row's blocks must sum
  to that version's own total codeword count. One mistyped digit breaks it.
* **Structure** — finder patterns, timing, the dark module, the version field
  against the value printed in the spec, and the format field decoded back out
  of the finished matrix.

METHOD.md §4 — the mutation log is in the commit message; every mutation was
watched red here before being reverted. Two results from that round are worth
keeping, because both corrected a guess:

* **The external harness cannot see a missing quiet zone.** Removing it left
  `tests/qr_ref.py` completely green — Vision reads a margin-less code fine out
  of a pristine synthetic PNG. The quiet zone is for a camera pointed at a
  screen with other things on it, which no automated check here reproduces. So
  it is pinned as a literal, in `TestRendering`, and that is the only thing
  standing behind it.
* **A corrupted Reed–Solomon generator does NOT still decode.** The guess was
  that error correction would absorb it; measured, Vision found no barcode at
  all. Both harnesses catch it.

    python3 -m unittest discover -s tests
"""

import hashlib
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from orchestra import qr  # noqa: E402


def digest(matrix):
    blob = "".join("".join(str(v) for v in row) for row in matrix).encode()
    return hashlib.sha256(blob).hexdigest()[:32]


def rows(matrix):
    return ["".join(str(v) for v in row) for row in matrix]


# --------------------------------------------------------------------- tables

class TestTheTables(unittest.TestCase):
    """A transcription error in `_BLOCKS` is 160 numbers deep and silent."""

    def test_every_row_sums_to_the_versions_codeword_count(self):
        for version in range(1, qr.MAX_VERSION + 1):
            for level in qr.LEVELS:
                ec_per, groups = qr._BLOCKS[version][level]
                total = sum(blocks * (words + ec_per)
                            for blocks, words in groups)
                self.assertEqual(total, qr._TOTAL[version],
                                 f"version {version} level {level}")

    def test_more_correction_never_carries_more_data(self):
        for version in range(1, qr.MAX_VERSION + 1):
            caps = [qr.capacity(version, lv) for lv in ("L", "M", "Q", "H")]
            self.assertEqual(caps, sorted(caps, reverse=True), f"v{version}")

    def test_every_version_is_present_at_every_level(self):
        for version in range(1, qr.MAX_VERSION + 1):
            self.assertEqual(set(qr._BLOCKS[version]), set(qr.LEVELS))

    def test_the_count_indicator_widens_at_version_ten(self):
        for version in range(1, 10):
            self.assertEqual(qr.count_bits(version), 8)
        self.assertEqual(qr.count_bits(10), 16)

    def test_alignment_centres_exist_from_version_two(self):
        self.assertEqual(qr._ALIGN[1], ())
        for version in range(2, qr.MAX_VERSION + 1):
            centres = qr._ALIGN[version]
            self.assertEqual(centres[0], 6)
            self.assertEqual(centres[-1], qr.size_of(version) - 7)


# ------------------------------------------------------- Reed–Solomon, checked

class TestReedSolomon(unittest.TestCase):
    """Syndromes, not a second copy of the encoder.

    `ec_codewords` divides; `syndromes` evaluates. They share only the GF
    tables, so a wrong generator polynomial produces check bytes that
    `ec_codewords` is perfectly happy with and that fail here at every one of
    the n evaluation points.
    """

    def test_the_field_is_the_one_the_spec_names(self):
        # a^8 = a^4 + a^3 + a^2 + 1 under 0x11D.
        self.assertEqual(qr._EXP[8], 0b0001_1101)
        self.assertEqual(qr._EXP[255], qr._EXP[0])
        self.assertEqual(len(set(qr._EXP[:255])), 255)  # a generator, not a cycle

    def test_every_codeword_this_encoder_makes_has_zero_syndromes(self):
        for version in range(1, qr.MAX_VERSION + 1):
            for level in qr.LEVELS:
                ec_per, groups = qr._BLOCKS[version][level]
                payload = bytes((i * 37 + version) % 256
                                for i in range(qr.capacity(version, level)))
                _, blocks, checks = qr._bitstream(payload, version, level)
                for block, check in zip(blocks, checks):
                    self.assertEqual(
                        qr.syndromes(block + check, ec_per), [0] * ec_per,
                        f"v{version} {level}")

    def test_syndromes_can_actually_fail(self):
        """Guard the guard: a check that is zero for everything proves nothing."""
        block = list(b"orchestra")
        check = qr.ec_codewords(block, 10)
        self.assertEqual(qr.syndromes(block + check, 10), [0] * 10)
        broken = list(block + check)
        broken[3] ^= 1
        self.assertNotEqual(qr.syndromes(broken, 10), [0] * 10)

    def test_the_generator_polynomial_is_monic_and_the_right_degree(self):
        for n in (7, 10, 13, 17, 26, 30):
            g = qr._generator(n)
            self.assertEqual(len(g), n + 1)
            self.assertEqual(g[0], 1)


# ------------------------------------------------------------------- structure

class TestStructure(unittest.TestCase):

    def matrix(self, text="orchestra", level="M", **kw):
        return qr.encode(text, level, **kw)

    def test_the_three_finder_patterns_are_where_they_must_be(self):
        for version in range(1, qr.MAX_VERSION + 1):
            m = qr.encode("x" * 10, "M", version=version)
            size = qr.size_of(version)
            for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
                block = [r[left:left + 7] for r in m[top:top + 7]]
                self.assertEqual(rows(block), [
                    "1111111", "1000001", "1011101", "1011101",
                    "1011101", "1000001", "1111111"], f"v{version} {top},{left}")

    def test_the_separators_are_light(self):
        m = self.matrix()
        size = len(m)
        for i in range(8):
            self.assertEqual(m[7][i], 0)
            self.assertEqual(m[i][7], 0)
            self.assertEqual(m[7][size - 1 - i], 0)
            self.assertEqual(m[size - 8][i], 0)

    def test_the_timing_patterns_alternate(self):
        for version in (1, 3, 7, 10):
            m = qr.encode("x" * 10, "M", version=version)
            for i in range(8, qr.size_of(version) - 8):
                self.assertEqual(m[6][i], int(i % 2 == 0), f"v{version} row")
                self.assertEqual(m[i][6], int(i % 2 == 0), f"v{version} col")

    def test_the_dark_module_is_always_dark(self):
        for version in range(1, qr.MAX_VERSION + 1):
            m = qr.encode("x", "M", version=version)
            self.assertEqual(m[qr.size_of(version) - 8][8], 1, f"v{version}")

    def test_the_format_field_decodes_back_to_its_level_and_mask(self):
        """Both copies, read back out of the finished matrix.

        This is what caught the reversed bit order: the encoder agreed with
        itself, every data module was right, and the field said the wrong mask.
        """
        for level in qr.LEVELS:
            for mask in range(8):
                m = qr.encode("orchestra", level, version=4, mask=mask)
                size = len(m)
                for copy in (0, 1):
                    bits = 0
                    if copy == 0:
                        seq = ([(8, i) for i in range(6)] + [(8, 7), (8, 8),
                               (7, 8)] + [(14 - i, 8) for i in range(9, 15)])
                    else:
                        seq = ([(size - 1 - i, 8) for i in range(7)] +
                               [(8, size - 15 + i) for i in range(7, 15)])
                    for i, (r, c) in enumerate(seq):
                        bits |= m[r][c] << (14 - i)
                    raw = bits ^ 0b101_0100_0001_0010
                    self.assertEqual((raw >> 10) & 0b111, mask,
                                     f"{level} mask {mask} copy {copy}")
                    self.assertEqual((raw >> 13) & 0b11, qr._LEVEL_BITS[level],
                                     f"{level} mask {mask} copy {copy}")

    def test_the_level_indicator_is_not_in_name_order(self):
        """L is 01 and M is 00. Nothing about the picture says so."""
        self.assertEqual(qr._LEVEL_BITS,
                         {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10})

    def test_the_version_field_matches_the_value_printed_in_the_spec(self):
        # ISO/IEC 18004 table D.1, transcribed. Independent of anything in
        # this module, and every one of the four is ALSO confirmed by Apple's
        # encoder: `tests/qr_ref.py` matches versions 7, 8, 9 and 10
        # module for module, and the version field is 36 of those modules.
        self.assertEqual(f"{qr._version_bits(7):018b}", "000111110010010100")
        self.assertEqual(f"{qr._version_bits(8):018b}", "001000010110111100")
        self.assertEqual(f"{qr._version_bits(9):018b}", "001001101010011001")
        self.assertEqual(f"{qr._version_bits(10):018b}", "001010010011010011")

    def test_the_version_field_is_present_from_seven_and_absent_below(self):
        for version in range(1, qr.MAX_VERSION + 1):
            _, fixed = qr._skeleton(version)
            size = qr.size_of(version)
            reserved = fixed[0][size - 11] and fixed[size - 11][0]
            self.assertEqual(reserved, version >= 7, f"v{version}")

    def test_the_data_modules_exactly_fit_the_codewords(self):
        """The arithmetic that found the missing version-information block.

        Free modules must equal the codeword bits plus the version's remainder
        bits (0 or 7 in this range). Too many means something is not being
        reserved; too few means placement is stealing from a function pattern.
        """
        for version in range(1, qr.MAX_VERSION + 1):
            _, fixed = qr._skeleton(version)
            free = len(qr._free_positions(version, fixed))
            spare = free - qr._TOTAL[version] * 8
            expected = 7 if 2 <= version <= 6 else 0
            self.assertEqual(spare, expected, f"v{version}: {free} free")

    def test_a_mask_actually_changes_the_data_and_never_the_finders(self):
        a = qr.encode("orchestra", "M", version=4, mask=0)
        b = qr.encode("orchestra", "M", version=4, mask=5)
        self.assertNotEqual(a, b)
        for top, left in ((0, 0), (0, len(a) - 7), (len(a) - 7, 0)):
            self.assertEqual([r[left:left + 7] for r in a[top:top + 7]],
                             [r[left:left + 7] for r in b[top:top + 7]])

    def test_the_chosen_mask_is_the_lowest_penalty_one(self):
        text = "orc://p?h=100.113.110.31&p=4242&c=7K3M9QP2"
        auto = qr.encode(text, "M")
        size = len(auto)
        scores = {m: qr._penalty(qr.encode(text, "M", mask=m), size)
                  for m in range(8)}
        best = min(scores, key=lambda m: (scores[m], m))
        self.assertEqual(auto, qr.encode(text, "M", mask=best))


# -------------------------------------------------------------------- capacity

class TestCapacity(unittest.TestCase):

    # ISO/IEC 18004 table 7, byte mode, versions 1–10 as (L, M, Q, H).
    #
    # THESE LITERALS ARE THE POINT. Every other test in this class derives its
    # input FROM `qr.capacity`, so a capacity that is wrong by a whole codeword
    # makes them all build the wrong payload and all still pass — a mutation
    # (`- 4` -> `+ 4`) proved exactly that, green across the board. A number
    # taken from the spec is the only thing here that `capacity` cannot also
    # decide. METHOD.md §3: a test that could not have failed is worth nothing.
    SPEC = {
        1: (17, 14, 11, 7),      2: (32, 26, 20, 14),
        3: (53, 42, 32, 24),     4: (78, 62, 46, 34),
        5: (106, 84, 60, 44),    6: (134, 106, 74, 58),
        7: (154, 122, 86, 64),   8: (192, 152, 108, 84),
        9: (230, 180, 130, 98),  10: (271, 213, 151, 119),
    }

    def test_capacity_matches_the_table_in_the_spec(self):
        for version, want in self.SPEC.items():
            got = tuple(qr.capacity(version, lv) for lv in ("L", "M", "Q", "H"))
            self.assertEqual(got, want, f"version {version}")

    def test_the_version_chosen_for_a_payload_matches_the_spec_table(self):
        """Version selection against the literals, not against `capacity`."""
        for version, caps in self.SPEC.items():
            for level, at in zip(("L", "M", "Q", "H"), caps):
                self.assertEqual(len(qr.encode("x" * at, level)),
                                 qr.size_of(version), f"{level} {at}B")
                if version < qr.MAX_VERSION:
                    self.assertEqual(len(qr.encode("x" * (at + 1), level)),
                                     qr.size_of(version + 1), f"{level} {at + 1}B")

    def test_the_smallest_version_that_fits_is_the_one_chosen(self):
        for level in qr.LEVELS:
            for version in range(1, qr.MAX_VERSION + 1):
                at = qr.capacity(version, level)
                self.assertEqual(len(qr.encode("x" * at, level)),
                                 qr.size_of(version), f"{level} v{version}")

    def test_one_byte_past_a_version_moves_to_the_next(self):
        for level in qr.LEVELS:
            for version in range(1, qr.MAX_VERSION):
                at = qr.capacity(version, level)
                self.assertEqual(len(qr.encode("x" * (at + 1), level)),
                                 qr.size_of(version + 1), f"{level} v{version}")

    def test_a_payload_that_does_not_fit_raises_rather_than_truncating(self):
        """The failure direction that matters. A QR carrying two thirds of a
        pairing URL is indistinguishable from a working one until it is
        scanned, which is after the user has walked away."""
        too_big = "x" * (qr.capacity(qr.MAX_VERSION, "M") + 1)
        with self.assertRaises(ValueError) as e:
            qr.encode(too_big, "M")
        self.assertIn("does not fit", str(e.exception))
        self.assertIsNone(qr.smallest_version(too_big.encode(), "M"))

    def test_an_unknown_level_raises(self):
        with self.assertRaises(ValueError):
            qr.encode("x", "Z")

    def test_a_version_outside_the_supported_range_raises(self):
        for version in (0, 11, 40):
            with self.assertRaises((ValueError, KeyError)):
                qr.encode("x", "M", version=version)

    def test_utf8_is_counted_in_bytes_not_characters(self):
        m = qr.encode("café ☕", "M")
        self.assertEqual(len(m), qr.size_of(1))
        # c,a,f + 2-byte e-acute + space + 3-byte cup = 9 bytes, 6 characters.
        self.assertEqual(len("café ☕".encode()), 9)
        self.assertEqual(len("café ☕"), 6)


# --------------------------------------------------------------------- goldens

class TestGoldens(unittest.TestCase):
    """Recorded from the run in which `tests/qr_ref.py` reported every case ok.

    Re-record ONLY in a commit that deliberately changes the output, and only
    after `python3 tests/qr_ref.py` passes again — that is the same rule the
    characterization net follows, for the same reason.
    """

    LETTER_A_M = [
        "111111100101101111111", "100000101011001000001",
        "101110101101001011101", "101110101011001011101",
        "101110100100101011101", "100000100011001000001",
        "111111101010101111111", "000000001100000000000",
        "100000101011011001110", "100110000001110111001",
        "001011100110101100000", "010101011001111101010",
        "110100111101111111111", "000000001100100000101",
        "111111100111010011110", "100000100010001000111",
        "101110100111010011100", "101110100101111101000",
        "101110100101110111011", "100000100011111101000",
        "111111101010100100110",
    ]

    PAIRING_URL_M = [
        "11111110110010100001001111111", "10000010101101101000101000001",
        "10111010001011111010101011101", "10111010111100100111001011101",
        "10111010001011101111101011101", "10000010000011100001001000001",
        "11111110101010101010101111111", "00000000100100011110100000000",
        "10110111000010000101101001011", "11111101110101100011001010101",
        "10010010010100101010000100000", "10011001011111100001001001010",
        "00110111110010101100110100110", "11000100101010010111101101001",
        "00011110100111000001110100111", "00010100011110100010011010011",
        "00001110111100000000000010000", "01101100100001110110010000110",
        "10010010110110010000010111100", "00011000101000111111010011111",
        "01101110111100001100111110110", "00000000110000100000100010011",
        "11111110111101100111101010110", "10000010110001011001100011000",
        "10111010010110011110111111111", "10111010100011110111000011001",
        "10111010100011101101010000101", "10000010001110011011110011010",
        "11111110110001001000110110010",
    ]

    # (payload, level, expected version, sha256 of the flattened matrix)
    DIGESTS = [
        ("x" * 100, "M", 6, "3b1cbac996b5a950f7fb084505c80f58"),
        ("x" * 150, "L", 7, "da7bffc962b319b84fb9338727e43c49"),
        ("x" * 213, "M", 10, "f673820bb5ccd3f0f6035d879c98fa30"),
        ("orc://p?h=achills-macbook-pro.tail1205d9.ts.net&p=4242&c=7K3M9QP2",
         "Q", 6, "463cbf492d907344047ba0dcc699ddd5"),
    ]

    def test_a_single_letter_at_level_m(self):
        self.assertEqual(rows(qr.encode("a", "M")), self.LETTER_A_M)

    def test_the_pairing_url_this_feature_actually_renders(self):
        url = "orc://p?h=100.113.110.31&p=4242&c=7K3M9QP2"
        self.assertEqual(rows(qr.encode(url, "M")), self.PAIRING_URL_M)

    def test_the_larger_versions_by_digest(self):
        for payload, level, version, want in self.DIGESTS:
            m = qr.encode(payload, level)
            self.assertEqual((len(m) - 17) // 4, version, payload[:20])
            self.assertEqual(digest(m), want, payload[:20])


# ------------------------------------------------------------------- rendering

class TestRendering(unittest.TestCase):

    def test_the_svg_carries_the_quiet_zone_in_its_viewbox(self):
        """Four light modules a side. A code rendered flush to its own edge
        reads intermittently on a busy screen, which is worse than one that
        never reads — nobody believes the bug report."""
        m = qr.encode("orchestra", "M")
        s = qr.svg(m)
        span = len(m) + qr.QUIET * 2
        self.assertIn(f'viewBox="0 0 {span} {span}"', s)
        self.assertEqual(qr.QUIET, 4)

    def test_the_svg_draws_every_dark_module_and_no_others(self):
        m = qr.encode("orchestra", "M")
        s = qr.svg(m)
        drawn = re.findall(r"M(\d+) (\d+)h1v1h-1z", s)
        want = {(str(c + qr.QUIET), str(r + qr.QUIET))
                for r, row in enumerate(m) for c, v in enumerate(row) if v}
        self.assertEqual(set(drawn), want)
        self.assertEqual(len(drawn), sum(sum(r) for r in m))

    def test_the_svg_has_no_baked_in_pixel_size_unless_asked(self):
        """The <svg> element itself, not the background rect — a QR with a
        pixel size baked in looks wrong on every screen but one."""
        m = qr.encode("orchestra", "M")
        span = len(m) + qr.QUIET * 2
        tag = qr.svg(m).split(">", 1)[0]
        self.assertNotIn("width=", tag)
        self.assertIn(f'width="{span * 4}"', qr.svg(m, scale=4).split(">", 1)[0])

    def test_the_svg_is_inert(self):
        """It is interpolated into a page. No script, no external reference."""
        s = qr.svg(qr.encode("orc://p?h=x&c=<script>", "M"))
        for forbidden in ("<script", "href", "onload", "xlink"):
            self.assertNotIn(forbidden, s.lower())

    def test_the_png_is_a_png_of_the_right_size(self):
        m = qr.encode("orchestra", "M")
        blob = qr.png(m, scale=3)
        self.assertTrue(blob.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn(b"IEND", blob)
        span = (len(m) + qr.QUIET * 2) * 3
        # IHDR width and height, big-endian, right after the 8-byte signature
        # and the 8-byte length+tag.
        self.assertEqual(int.from_bytes(blob[16:20], "big"), span)
        self.assertEqual(int.from_bytes(blob[20:24], "big"), span)


if __name__ == "__main__":
    unittest.main()
