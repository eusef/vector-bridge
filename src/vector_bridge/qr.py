"""A small QR code encoder: byte mode, error correction level L, versions 1 to 10 (up to 271
bytes). Enough for a WIFI: string (at most about 115 bytes) and nothing else, so the project
keeps its no-new-deps rule. ISO/IEC 18004; the tables below are the level-L rows only.

encode(text) -> square list of rows of booleans (True = dark), with the 4-module quiet zone.
svg(matrix)  -> an <svg> string that scales to its box.
"""
from __future__ import annotations

# version: (data codewords, ec codewords per block, [block data sizes])
_TABLE = {
    1: (19, 7, [19]), 2: (34, 10, [34]), 3: (55, 15, [55]), 4: (80, 20, [80]), 5: (108, 26, [108]),
    6: (136, 18, [68, 68]), 7: (156, 20, [78, 78]), 8: (194, 24, [97, 97]), 9: (232, 30, [116, 116]),
    10: (274, 18, [68, 68, 69, 69]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34],
          7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}
QUIET = 4

# ---- GF(256) Reed-Solomon ---------------------------------------------------------

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


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(n: int) -> list[int]:
    g = [1]                                     # leading coefficient first
    for i in range(n):                          # multiply by (x + alpha^i)
        ng = [0] * (len(g) + 1)
        for j, coef in enumerate(g):
            ng[j] ^= coef
            ng[j + 1] ^= _mul(coef, _EXP[i])
        g = ng
    return g


def _ec(data: list[int], n: int) -> list[int]:
    g = _generator(n)
    rem = [0] * n
    for byte in data:
        factor = byte ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(g[j + 1], factor)
    return rem


# ---- bit stream --------------------------------------------------------------------


def _codewords(data: bytes, version: int) -> list[int]:
    total, ecn, blocks = _TABLE[version]
    bits = []

    def put(value: int, n: int) -> None:
        bits.extend((value >> (n - 1 - i)) & 1 for i in range(n))

    put(0b0100, 4)
    put(len(data), 8 if version < 10 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, total * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += ([0xEC, 0x11] * total)[:total - len(words)]
    # split into blocks, interleave data then ec
    dblocks, at = [], 0
    for size in blocks:
        dblocks.append(words[at:at + size])
        at += size
    eblocks = [_ec(b, ecn) for b in dblocks]
    out = [b[i] for i in range(max(blocks)) for b in dblocks if i < len(b)]
    out += [b[i] for i in range(ecn) for b in eblocks]
    return out


# ---- matrix ------------------------------------------------------------------------


def _bch(value: int, poly: int, bits: int) -> int:
    """value << bits, then the remainder modulo poly appended."""
    v = value << bits
    top = poly.bit_length() - 1
    for i in range(v.bit_length() - 1, top - 1, -1):
        if v >> i & 1:
            v ^= poly << (i - top)
    return (value << bits) | v


def _fresh(version: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    """Matrix with function patterns set (None = free for data) and a mask of reserved cells."""
    size = version * 4 + 17
    m: list[list[int | None]] = [[None] * size for _ in range(size)]

    def finder(r0: int, c0: int) -> None:
        for r in range(-1, 8):
            for c in range(-1, 8):
                if 0 <= r0 + r < size and 0 <= c0 + c < size:
                    inside = 0 <= r <= 6 and 0 <= c <= 6
                    ring = inside and (r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4))
                    m[r0 + r][c0 + c] = 1 if ring else 0
    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)
    for i in range(8, size - 8):
        m[6][i] = m[i][6] = int(i % 2 == 0)
    centres = _ALIGN[version]
    for r in centres:
        for c in centres:
            if (r < 9 and c < 9) or (r < 9 and c > size - 9) or (r > size - 9 and c < 9):
                continue                        # the three finder corners; timing-line ones are real
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = int(max(abs(dr), abs(dc)) != 1)
    m[size - 8][8] = 1                          # the dark module
    cells = [(8, i) for i in range(9)] + [(i, 8) for i in range(9)]          # format info areas
    cells += [(8, size - 1 - i) for i in range(8)] + [(size - 1 - i, 8) for i in range(8)]
    for r, c in cells:
        if m[r][c] is None:
            m[r][c] = 0
    if version >= 7:
        for i in range(18):
            m[i // 3][size - 11 + i % 3] = m[size - 11 + i % 3][i // 3] = 0
    reserved = [[cell is not None for cell in row] for row in m]
    return m, reserved


def _place(m: list[list[int | None]], words: list[int]) -> None:
    size = len(m)
    bits = iter(bit for w in words for bit in ((w >> (7 - i)) & 1 for i in range(8)))
    col, up = size - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if up else range(size)
        for r in rows:
            for c in (col, col - 1):
                if m[r][c] is None:
                    m[r][c] = next(bits, 0)
        col -= 2
        up = not up


_MASKS = [
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply(m: list[list[int]], reserved: list[list[bool]], mask: int) -> list[list[int]]:
    size = len(m)
    f = _MASKS[mask]
    out = [[m[r][c] ^ (1 if not reserved[r][c] and f(r, c) else 0) for c in range(size)] for r in range(size)]
    fmt = _bch(0b01 << 3 | mask, 0b10100110111, 10) ^ 0b101010000010010    # level L
    for i in range(15):
        bit = (fmt >> (14 - i)) & 1
        if i < 6:
            out[8][i] = bit
        elif i < 8:
            out[8][i + 1] = bit
        elif i == 8:
            out[7][8] = bit
        else:
            out[14 - i][8] = bit
        if i < 7:                               # 7 up column 8, then the dark module, 8 along row 8
            out[size - 1 - i][8] = bit
        else:
            out[8][size - 15 + i] = bit
    version = (size - 17) // 4
    if version >= 7:
        info = _bch(version, 0b1111100100101, 12)
        for i in range(18):
            bit = (info >> i) & 1
            out[i // 3][size - 11 + i % 3] = out[size - 11 + i % 3][i // 3] = bit
    return out


def _penalty(m: list[list[int]]) -> int:
    size = len(m)
    score = 0
    for lines in (m, [list(col) for col in zip(*m)]):
        for line in lines:
            run, prev = 0, None
            for cell in line + [None]:
                if cell == prev:
                    run += 1
                else:
                    if run >= 5:
                        score += run - 2
                    run, prev = 1, cell
            s = "".join(map(str, line))
            score += 40 * (s.count("10111010000") + s.count("00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m)) * 100 // (size * size)
    score += 10 * min(abs(dark - 50) // 5, abs(dark - 50 + 4) // 5 if dark < 50 else abs(dark - 50 - 4) // 5)
    return score


def encode(text: str, version: int | None = None, mask: int | None = None) -> list[list[bool]]:
    data = text.encode()
    if version is None:
        # mode (4 bits) and count (8 or 16) come off the top, then whole bytes
        version = next((v for v in _TABLE if (_TABLE[v][0] * 8 - (12 if v < 10 else 20)) // 8 >= len(data)), None)
        if version is None:
            raise ValueError(f"too long for QR level L version 10: {len(data)} bytes")
    m, reserved = _fresh(version)
    _place(m, _codewords(data, version))
    m = [[cell or 0 for cell in row] for row in m]
    if mask is None:
        candidates = [_apply(m, reserved, k) for k in range(8)]
        out = min(candidates, key=_penalty)
    else:
        out = _apply(m, reserved, mask)
    size = len(out) + 2 * QUIET
    blank = [False] * size
    body = [[False] * QUIET + [bool(x) for x in row] + [False] * QUIET for row in out]
    return [blank[:] for _ in range(QUIET)] + body + [blank[:] for _ in range(QUIET)]


def svg(matrix: list[list[bool]], dark: str = "#000", light: str = "#fff") -> str:
    size = len(matrix)
    path = "".join(f"M{c} {r}h1v1h-1z" for r, row in enumerate(matrix) for c, on in enumerate(row) if on)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            f'<rect width="{size}" height="{size}" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')


def wifi_string(ssid: str, passphrase: str | None) -> str:
    """The de-facto phone format. Backslash escapes the five special characters."""
    def esc(s: str) -> str:
        return "".join("\\" + ch if ch in '\\;,":' else ch for ch in s)
    if passphrase:
        return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(passphrase)};;"
    return f"WIFI:T:nopass;S:{esc(ssid)};;"
