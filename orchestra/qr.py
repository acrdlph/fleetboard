"""orchestra.qr — a QR encoder, so that nobody has to type a pairing code.

WHY THIS EXISTS AT ALL. The pairing payload is a URL of about forty characters
and the user is standing in front of the Mac holding the phone. Typing it is
the failure mode that makes pairing not get used, which is how a token ends up
in a note-taking app instead. A QR is the difference between "point the camera"
and "read forty characters aloud to yourself".

WHY IT IS WRITTEN HERE. Zero dependencies is a real constraint on this project
(README, Conventions), and `qrcode` would be the first one. A QR encoder is a
few hundred lines of a fully specified, deterministic algorithm — ISO/IEC 18004
— which makes it exactly the kind of thing that can be written once and pinned
hard. The cost is bounded and it is paid here.

THE DANGER, STATED FIRST, because it is the whole reason this file has the
tests it has: **a QR encoder that is wrong produces a picture that looks
completely correct.** Three finder patterns, a plausible speckle, the right
dimensions — and no camera on earth can read it. There is no crash, no
exception, no red test. METHOD.md §3 is about exactly this shape of failure, so
this module is not trusted on the basis that its output looks like a QR code.
It is verified three independent ways, none of which is a second reading of
this file:

  1. **Against Apple's own encoder.** `CIQRCodeGenerator` is a reference
     implementation of the same spec shipped by a third party. `tests/qr_ref.py`
     generates the same payload at the same EC level with Core Image, extracts
     the module matrix, and compares it to ours MODULE BY MODULE. Both
     implementations choose the smallest version that fits and the mask with the
     lowest penalty, so a correct encoder produces a bit-identical matrix.
  2. **Through a decoder that has never seen this code.** The same
     `tests/qr_ref.py` renders our matrix to PNG (see `png` below) and runs it
     through Vision's `VNDetectBarcodesRequest`, then asserts the string that
     comes back is the string that went in.
  3. **Reed–Solomon syndromes, in the unit suite.** For a valid RS codeword,
     evaluating the codeword polynomial at alpha^1 … alpha^n is zero at every
     point. That is a property of the code rather than a re-run of the encoder,
     so it catches a generator polynomial that is wrong in a way that comparing
     against my own `_ec()` never could.

(1) and (2) need macOS and a Swift compile, so they live in a script that is
run deliberately rather than in the 68 s unit suite. What the unit suite keeps
is (3), the structural invariants, and GOLDEN MATRICES recorded from a run that
(1) and (2) both passed — so the suite fails the moment the encoder drifts from
the output that was externally proven correct.

WHAT IS IMPLEMENTED, and what is not:

  * **Byte mode only.** The payload is a URL: it contains `:`, `/`, `?`, `&`
    and lowercase, none of which alphanumeric mode can carry. Numeric and
    alphanumeric modes would compress a payload this module never sees.
  * **Versions 1–10.** Version 10 byte-mode at EC level M holds 213 bytes; the
    pairing URL is about 40 and cannot exceed ~120 even with the longest
    MagicDNS name Tailscale will issue. Stopping at 10 keeps the block table to
    forty rows instead of a hundred and sixty, for capacity nothing here can
    use. `encode` RAISES on a payload that does not fit — it does not silently
    truncate, and it does not silently pick a mode it cannot render.

    From **version 7** the symbol also carries an 18-bit version-information
    block, twice. That is not a detail: leaving it out leaves 36 modules that
    the placement walk happily fills with data, so the code is dense, pretty,
    the right size, and unreadable. It was caught here by an arithmetic
    self-check in `encode` rather than by looking at the picture — see the note
    on `spare`.
  * **All four EC levels**, because they cost four rows of table each and they
    give the external checks four times as much surface to disagree on.

THE QUIET ZONE IS PART OF THE CODE. Four light modules on every side are
required by the spec, and a decoder handed a matrix with no margin frequently
fails on a busy screen. `svg` puts them in the viewBox; `png` puts them in the
image. Leaving them out is the classic way to ship an "obviously fine" QR that
only reads half the time — which is worse than one that never reads, because
nobody believes the bug report.
"""

import struct
import zlib

# ---------------------------------------------------------------- GF(2^8)

# The field the spec names: x^8 + x^4 + x^3 + x^2 + 1 (0x11D), generator 2.
# Logs go to 511 so a product of two exponents can be looked up without a
# modulo on the hot path.
_EXP = [0] * 512
_LOG = [0] * 256

_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    """Multiply in GF(2^8). Zero is special-cased because it has no log."""
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(n):
    """The RS generator polynomial for `n` check symbols, highest degree first.

    (x - a^0)(x - a^1)…(x - a^(n-1)), and in GF(2) subtraction is XOR, so the
    signs the textbook carries do not appear.
    """
    g = [1]
    for i in range(n):
        nxt = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            nxt[j] ^= c                       # c * x
            nxt[j + 1] ^= _mul(c, _EXP[i])    # c * a^i
        g = nxt
    return g


def ec_codewords(data, n):
    """The `n` Reed–Solomon check bytes for `data`. Polynomial long division."""
    g = _generator(n)
    rem = list(data) + [0] * n
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j in range(1, n + 1):
                rem[i + j] ^= _mul(g[j], coef)
    return rem[len(data):]


def syndromes(codeword, n):
    """Evaluate the codeword polynomial at each of the generator's roots.

    All zero iff `codeword` is a valid codeword of this RS code. This is the
    INDEPENDENT check on `ec_codewords`: it uses the defining property of the
    code rather than repeating the division, so a wrong generator polynomial
    fails here while agreeing with itself everywhere else.

    THE ROOTS START AT a^0. QR's generator is (x-a^0)(x-a^1)…(x-a^(n-1)), not
    the a^1…a^n convention that many Reed–Solomon texts and most non-QR uses
    (CDs, DVDs, RAID) assume. Evaluating at the wrong set leaves exactly one
    non-zero syndrome on a perfectly valid codeword — which is what this
    function did first, and it read like an encoder bug.
    """
    out = []
    for i in range(n):
        acc = 0
        for byte in codeword:
            acc = _mul(acc, _EXP[i]) ^ byte
        out.append(acc)
    return out


# ------------------------------------------------------------------ the tables

# version -> level -> (ec codewords per block, ((blocks, data codewords), …))
#
# Straight out of ISO/IEC 18004 table 9. Every row is checked by
# `tests/test_qr.py::TestTheTables`, which recomputes the total codeword count
# for each (version, level) and compares it against the version's own total —
# a transcription error in any single number breaks that identity.
_BLOCKS = {
    1:  {"L": (7,  ((1, 19),)),         "M": (10, ((1, 16),)),
         "Q": (13, ((1, 13),)),         "H": (17, ((1, 9),))},
    2:  {"L": (10, ((1, 34),)),         "M": (16, ((1, 28),)),
         "Q": (22, ((1, 22),)),         "H": (28, ((1, 16),))},
    3:  {"L": (15, ((1, 55),)),         "M": (26, ((1, 44),)),
         "Q": (18, ((2, 17),)),         "H": (22, ((2, 13),))},
    4:  {"L": (20, ((1, 80),)),         "M": (18, ((2, 32),)),
         "Q": (26, ((2, 24),)),         "H": (16, ((4, 9),))},
    5:  {"L": (26, ((1, 108),)),        "M": (24, ((2, 43),)),
         "Q": (18, ((2, 15), (2, 16))), "H": (22, ((2, 11), (2, 12)))},
    6:  {"L": (18, ((2, 68),)),         "M": (16, ((4, 27),)),
         "Q": (24, ((4, 19),)),         "H": (28, ((4, 15),))},
    7:  {"L": (20, ((2, 78),)),         "M": (18, ((4, 31),)),
         "Q": (18, ((2, 14), (4, 15))), "H": (26, ((4, 13), (1, 14)))},
    8:  {"L": (24, ((2, 97),)),         "M": (22, ((2, 38), (2, 39))),
         "Q": (22, ((4, 18), (2, 19))), "H": (26, ((4, 14), (2, 15)))},
    9:  {"L": (30, ((2, 116),)),        "M": (22, ((3, 36), (2, 37))),
         "Q": (20, ((4, 16), (4, 17))), "H": (24, ((4, 12), (4, 13)))},
    10: {"L": (18, ((2, 68), (2, 69))), "M": (26, ((4, 43), (1, 44))),
         "Q": (24, ((6, 19), (2, 20))), "H": (28, ((6, 15), (2, 16)))},
}

# Total codewords (data + error correction) in each version, table 1. Used only
# to check `_BLOCKS` — see the note above.
_TOTAL = {1: 26, 2: 44, 3: 70, 4: 100, 5: 134,
          6: 172, 7: 196, 8: 242, 9: 292, 10: 346}

# Alignment pattern centre coordinates, table E.1. Every pair (r, c) drawn from
# this list gets a pattern EXCEPT the three that would land on a finder.
_ALIGN = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
          7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50)}

# The two-bit level indicator that goes into the format information. NOT the
# obvious order — L is 01 and M is 00, because the field is ordered by error
# correction capability rather than by name, and getting this wrong produces a
# code that is structurally perfect and reads as the wrong EC level, i.e. does
# not read at all.
_LEVEL_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

LEVELS = ("L", "M", "Q", "H")
MAX_VERSION = 10
MODE_BYTE = 0b0100

# The eight data mask patterns, table 10, as predicates on (row, column).
# A mask is applied to the DATA modules only; function patterns are never
# masked, which is why placement records which modules are function modules.
_MASKS = (
    lambda i, j: (i + j) % 2 == 0,
    lambda i, j: i % 2 == 0,
    lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0,
    lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
    lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
)


def size_of(version):
    """Modules per side, excluding the quiet zone."""
    return version * 4 + 17


def count_bits(version):
    """Width of the byte-mode character count indicator.

    8 bits up to version 9, 16 from version 10. This module stops at 10, so it
    sees exactly one step — and that step is a real one: encoding a version-10
    payload with an 8-bit count produces a code that scanners reject.
    """
    return 8 if version <= 9 else 16


def capacity(version, level):
    """How many payload bytes fit, after the mode and count indicators."""
    ec_per, groups = _BLOCKS[version][level]
    data_codewords = sum(blocks * words for blocks, words in groups)
    return (data_codewords * 8 - 4 - count_bits(version)) // 8


def smallest_version(payload, level):
    """The smallest version that holds `payload`, or None if none does.

    None rather than a default, and the caller raises. A silent fallback to the
    largest version would be the wrong shape of failure — the honest answer to
    "this does not fit" is to say so, not to make a code nobody asked for.
    """
    for version in range(1, MAX_VERSION + 1):
        if len(payload) <= capacity(version, level):
            return version
    return None


# ------------------------------------------------------------------ the bits

def _bitstream(payload, version, level):
    """Payload -> the interleaved codeword sequence for this (version, level).

    Mode indicator, character count, the bytes, a terminator, padding to a byte
    boundary, then the alternating pad bytes; split into blocks; RS per block;
    then INTERLEAVED — data codeword i of every block in turn, then EC codeword
    i of every block in turn. The interleave is what makes a burst of damage
    land one symbol deep in many blocks rather than wiping one block out.
    """
    ec_per, groups = _BLOCKS[version][level]
    data_codewords = sum(blocks * words for blocks, words in groups)

    bits = []

    def put(value, width):
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    put(MODE_BYTE, 4)
    put(len(payload), count_bits(version))
    for byte in payload:
        put(byte, 8)
    # Terminator: up to four zero bits, fewer if the capacity is nearly full.
    put(0, min(4, data_codewords * 8 - len(bits)))
    if len(bits) % 8:
        put(0, 8 - len(bits) % 8)

    words = [int("".join(str(b) for b in bits[i:i + 8]), 2)
             for i in range(0, len(bits), 8)]
    # 0xEC, 0x11 alternating — the spec's pad codewords, and they alternate so
    # that a run of padding does not look like a run of data to the masker.
    pads, at_pad = (0xEC, 0x11), 0
    while len(words) < data_codewords:
        words.append(pads[at_pad % 2])
        at_pad += 1

    blocks, checks = [], []
    at = 0
    for count, per_block in groups:
        for _ in range(count):
            block = words[at:at + per_block]
            at += per_block
            blocks.append(block)
            checks.append(ec_codewords(block, ec_per))

    out = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_per):
        for check in checks:
            out.append(check[i])
    return out, blocks, checks


# ------------------------------------------------------------- the modules

def _skeleton(version):
    """`(modules, fixed)` — the function patterns drawn, everything else empty.

    `fixed[r][c]` is True where a module is part of a function pattern or is
    reserved for format information. It decides two different things and both
    are load-bearing: where data may NOT be placed, and which modules the mask
    must leave alone.
    """
    size = size_of(version)
    mod = [[0] * size for _ in range(size)]
    fixed = [[False] * size for _ in range(size)]

    def finder(row, col):
        # 7x7 finder plus its one-module separator, clipped at the edges.
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                inner = (0 <= r <= 6 and 0 <= c <= 6)
                dark = inner and (r in (0, 6) or c in (0, 6) or
                                  (2 <= r <= 4 and 2 <= c <= 4))
                mod[rr][cc] = 1 if dark else 0
                fixed[rr][cc] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    # Timing patterns: row 6 and column 6, alternating, dark on even indices.
    for i in range(size):
        if not fixed[6][i]:
            mod[6][i], fixed[6][i] = int(i % 2 == 0), True
        if not fixed[i][6]:
            mod[i][6], fixed[i][6] = int(i % 2 == 0), True

    # Alignment patterns everywhere two centres meet, except the three
    # positions that would collide with a finder.
    centres = _ALIGN[version]
    last = len(centres) - 1
    for a, row in enumerate(centres):
        for b, col in enumerate(centres):
            if (a, b) in ((0, 0), (0, last), (last, 0)):
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    dark = max(abs(r), abs(c)) != 1
                    mod[row + r][col + c] = int(dark)
                    fixed[row + r][col + c] = True

    # Format information areas, reserved now and written after the mask is
    # chosen — the format encodes the mask, so it cannot be written before.
    for i in range(9):
        fixed[8][i] = True
        fixed[i][8] = True
    for i in range(8):
        fixed[8][size - 1 - i] = True
        fixed[size - 1 - i][8] = True

    # The dark module. Always set, always at (4v + 9, 8), and it is not part of
    # the format information despite sitting in its column.
    mod[size - 8][8], fixed[size - 8][8] = 1, True

    # Version information, from version 7 up: 18 bits, twice, in a 3x6 block
    # beside the top-right finder and a 6x3 block above the bottom-left one.
    #
    # This is the block whose absence is invisible. Without it those 36 modules
    # are not reserved, the data walk fills them, every codeword after the
    # first one lands one position early, and the result is a QR code that is
    # structurally perfect and decodes to nothing. Nothing about the picture
    # says so. What said so was `encode`'s spare-module check.
    if version >= 7:
        bits = _version_bits(version)
        for i in range(18):
            v = (bits >> i) & 1
            r, c = i // 3, size - 11 + i % 3
            mod[r][c], fixed[r][c] = v, True
            mod[c][r], fixed[c][r] = v, True
    return mod, fixed


def _free_positions(version, fixed):
    """Every data module position, in placement order.

    Two-module columns walked right to left, alternating upwards and downwards,
    skipping column 6 entirely — it is the vertical timing pattern, and the
    column indices either side of it are what the "skip" actually means.
    """
    size = size_of(version)
    out = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1        # the timing column is not half of a pair
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not fixed[row][c]:
                    out.append((row, c))
        upward = not upward
        col -= 2
    return out


def _penalty(mod, size):
    """The four penalty rules of table 11. Lower is better.

    Rules 1 and 2 punish runs and blocks that a scanner reads as structure;
    rule 3 punishes anything resembling a finder, which is the one that
    actually breaks decoding; rule 4 punishes a code that is mostly one colour.
    """
    score = 0

    # Rule 1 — five or more of one colour in a row, in both directions.
    for axis in range(2):
        for i in range(size):
            run, prev = 0, -1
            for j in range(size):
                v = mod[i][j] if axis == 0 else mod[j][i]
                if v == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run, prev = 1, v
            if run >= 5:
                score += 3 + (run - 5)

    # Rule 2 — every 2x2 block of one colour.
    for i in range(size - 1):
        row, nxt = mod[i], mod[i + 1]
        for j in range(size - 1):
            if row[j] == row[j + 1] == nxt[j] == nxt[j + 1]:
                score += 3

    # Rule 3 — the finder-like 1:1:3:1:1 sequence with four light either side.
    a = (1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0)
    b = (0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1)
    for i in range(size):
        row = tuple(mod[i])
        col = tuple(mod[j][i] for j in range(size))
        for line in (row, col):
            for j in range(size - 10):
                window = line[j:j + 11]
                if window == a or window == b:
                    score += 40

    # Rule 4 — deviation from an even split, in 5 % steps.
    dark = sum(sum(r) for r in mod)
    percent = dark * 100 // (size * size)
    score += 10 * (abs(percent - 50) // 5)
    return score


def _format_bits(level, mask):
    """The 15-bit format information: 5 data bits, BCH(15,5), then the mask.

    The final XOR with 0x5412 is what stops an all-zero format (level M, mask
    0) from being an unreadable field of light modules — without it a valid
    code exists whose format area carries no transitions at all.
    """
    value = (_LEVEL_BITS[level] << 3) | mask
    rem = value << 10
    # BCH(15, 5) with generator 0b10100110111 = x^10+x^8+x^5+x^4+x^2+x+1.
    # Five steps, one per data bit, highest first.
    for i in range(4, -1, -1):
        if rem & (1 << (i + 10)):
            rem ^= 0b101_0011_0111 << i
    return ((value << 10) | rem) ^ 0b101_0100_0001_0010


def _version_bits(version):
    """The 18-bit version information: 6 data bits, BCH(18, 6), no final XOR.

    Generator 0x1F25 = x^12+x^11+x^10+x^9+x^8+x^5+x^2+1, which is THIRTEEN
    bits — twelve check bits plus the leading term. Writing it with a spare
    nibble (0b1111100100100101, sixteen bits) produces a remainder of the wrong
    degree and a version field that is wrong in twelve of its eighteen bits,
    which is a code no scanner will read and which looks like every other code.
    Version 7 must come out as 000111110010010100; that value is in the spec's
    own table and is asserted in `tests/test_qr.py`.

    Unlike the format information there is no mask applied at the end — the
    field can never be all zero, because version 0 does not exist and versions
    below 7 do not carry it at all.
    """
    rem = version << 12
    for i in range(5, -1, -1):
        if rem & (1 << (i + 12)):
            rem ^= 0x1F25 << i
    return (version << 12) | rem


def _place_format(mod, size, level, mask):
    """Both copies of the format information, MOST significant bit first.

    Two copies because the top-left finder's neighbourhood can be damaged
    without the code being lost; a QR that carries its own mask in one place
    only is a QR that a fingerprint destroys.

    THE BIT ORDER IS THE TRAP. The spec places bit 14 — the most significant —
    at (8, 0) and walks down to bit 0. Writing it LSB-first instead produces a
    code in which every one of the 1,681 data modules is correct and the
    thirty format modules are the field reversed; it is indistinguishable from
    a working code by eye, and no scanner will read it, because the fifteen
    bits that say which mask was applied say the wrong thing. It was found by
    comparing against Apple's encoder, which reported exactly twelve differing
    modules, all of them here.
    """
    bits = _format_bits(level, mask)

    def bit(i):
        """Bit `i` counting from the most significant, which is how the
        placement tables in the spec are indexed."""
        return (bits >> (14 - i)) & 1

    for i in range(6):
        mod[8][i] = bit(i)
    mod[8][7] = bit(6)
    mod[8][8] = bit(7)
    mod[7][8] = bit(8)
    for i in range(9, 15):
        mod[14 - i][8] = bit(i)

    for i in range(7):
        mod[size - 1 - i][8] = bit(i)
    for i in range(7, 15):
        mod[8][size - 15 + i] = bit(i)


def encode(text, level="M", version=None, mask=None):
    """`text` -> a list of rows of 0/1, no quiet zone. THE entry point.

    `version` and `mask` are for the tests and for the reference comparison;
    production passes neither and gets the smallest version that fits and the
    mask with the lowest penalty, which is what every other conforming encoder
    also picks — that determinism is what makes comparing against Apple's
    encoder a real test rather than a coincidence.

    Raises `ValueError` on a payload that does not fit. It does not truncate,
    and it does not quietly step outside the versions it can render: a QR that
    encodes half a URL is indistinguishable from one that encodes all of it
    until somebody scans it.
    """
    if level not in _BLOCKS[1]:
        raise ValueError(f"error correction level must be one of {LEVELS}: "
                         f"{level!r}")
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    if version is None:
        version = smallest_version(payload, level)
        if version is None:
            raise ValueError(
                f"{len(payload)} bytes does not fit a version-{MAX_VERSION} "
                f"byte-mode QR at level {level} (max "
                f"{capacity(MAX_VERSION, level)}); this encoder stops at "
                f"version {MAX_VERSION} — see orchestra/qr.py")
    if not 1 <= version <= MAX_VERSION:
        raise ValueError(f"version must be 1..{MAX_VERSION}: {version}")
    if len(payload) > capacity(version, level):
        raise ValueError(f"{len(payload)} bytes does not fit version "
                         f"{version} at level {level} "
                         f"(max {capacity(version, level)})")

    size = size_of(version)
    words, _, _ = _bitstream(payload, version, level)
    base, fixed = _skeleton(version)
    slots = _free_positions(version, fixed)

    # A real self-check, not an assertion of style: the number of free modules
    # is fixed by the version, and if placement and the block table disagree
    # the code is silently wrong. `slots` may exceed the bits by the version's
    # remainder bits (0 or 7 in this range), never the other way round.
    spare = len(slots) - len(words) * 8
    if not 0 <= spare <= 7:
        raise AssertionError(f"version {version} level {level}: {len(slots)} "
                             f"data modules for {len(words) * 8} bits")

    bits = [(w >> s) & 1 for w in words for s in range(7, -1, -1)]
    candidates = range(8) if mask is None else (mask,)
    best, best_score = None, None
    for candidate in candidates:
        mod = [row[:] for row in base]
        rule = _MASKS[candidate]
        for i, (row, col) in enumerate(slots):
            value = bits[i] if i < len(bits) else 0
            mod[row][col] = value ^ (1 if rule(row, col) else 0)
        _place_format(mod, size, level, candidate)
        score = _penalty(mod, size)
        if best_score is None or score < best_score:
            best, best_score = mod, score
    return best


# ------------------------------------------------------------------ rendering

QUIET = 4       # modules of margin, required by the spec and by real cameras


def svg(matrix, quiet=QUIET, dark="#111", light="#fff", scale=None):
    """The matrix as an SVG string, quiet zone included.

    One `<path>` of one-by-one squares rather than N `<rect>`s: a version-6
    code has ~700 dark modules, and the rect form is four times the bytes for
    an identical picture. `shape-rendering="crispEdges"` is not cosmetic — at
    the sizes a phone camera actually sees, antialiased module edges are what
    turns a marginal scan into a failed one.

    No width/height unless `scale` is given, so the caller's CSS decides how
    big it is. A QR with a baked-in pixel size is one that looks wrong on every
    screen but the one it was written on.
    """
    n = len(matrix)
    span = n + quiet * 2
    parts = []
    for r, row in enumerate(matrix):
        for c, v in enumerate(row):
            if v:
                parts.append(f"M{c + quiet} {r + quiet}h1v1h-1z")
    dims = "" if not scale else f' width="{span * scale}" height="{span * scale}"'
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {span} '
            f'{span}"{dims} shape-rendering="crispEdges" role="img" '
            f'aria-label="pairing code">'
            f'<rect width="{span}" height="{span}" fill="{light}"/>'
            f'<path fill="{dark}" d="{"".join(parts)}"/></svg>')


def png(matrix, quiet=QUIET, scale=8):
    """The matrix as PNG bytes. For the external decode check, not the board.

    It is here rather than in the test harness because the harness is what
    proves this module correct, and a proof that lives only in a script is one
    that rots the first time somebody moves the script. Greyscale, one byte a
    pixel, one zlib stream — `struct` and `zlib` and nothing else.
    """
    n = len(matrix)
    span = (n + quiet * 2) * scale
    blank = bytes([255]) * span
    rows = [b"\x00" + blank for _ in range(quiet * scale)]
    for row in matrix:
        line = bytearray()
        line += bytes([255]) * (quiet * scale)
        for v in row:
            line += bytes([0 if v else 255]) * scale
        line += bytes([255]) * (quiet * scale)
        rows.extend([b"\x00" + bytes(line)] * scale)
    rows.extend([b"\x00" + blank] * (quiet * scale))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", span, span, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) +
            chunk(b"IEND", b""))
