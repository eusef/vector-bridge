"""Write imager/vector-bridge-icon.png (128 px, RGBA) with the standard library only.

Same drawing as imager/icon.svg: a rounded blue tile, a white arch over a deck with four posts,
and a small yellow light. Quick Look rasterises the SVG at the wrong scale, so the PNG is drawn here.
"""
import struct
import sys
import zlib
from itertools import pairwise
from pathlib import Path

N = 128
S = N / 64  # the SVG is drawn in 64 units
BLUE, WHITE, YELLOW = (0x1F, 0x4E, 0x79), (0xFF, 0xFF, 0xFF), (0xFF, 0xD1, 0x66)


def arch(t):  # quadratic Bezier (8,44) (32,18) (56,44)
    return ((1 - t) ** 2 * 8 + 2 * (1 - t) * t * 32 + t * t * 56,
            (1 - t) ** 2 * 44 + 2 * (1 - t) * t * 18 + t * t * 44)


ARCH = [arch(i / 200) for i in range(201)]
POSTS = [(14, 34, 44), (24, 28, 44), (40, 28, 44), (50, 34, 44)]  # x, y_top, y_bottom


def near_segment(px, py, x0, y0, x1, y1, half):
    dx, dy = x1 - x0, y1 - y0
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - x0) * dx + (py - y0) * dy) / (dx * dx + dy * dy)))
    cx, cy = x0 + t * dx, y0 + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= half * half


def pixel(ix, iy):
    x, y = (ix + 0.5) / S, (iy + 0.5) / S
    r = 12
    if (x < r or x > 64 - r) and (y < r or y > 64 - r):
        cx, cy = (r if x < r else 64 - r), (r if y < r else 64 - r)
        if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
            return (0, 0, 0, 0)
    if (x - 32) ** 2 + (y - 16) ** 2 <= 9:
        return YELLOW + (255,)
    if near_segment(x, y, 8, 46, 56, 46, 2.5):
        return WHITE + (255,)
    for px, top, bottom in POSTS:
        if near_segment(x, y, px, top, px, bottom, 2):
            return WHITE + (255,)
    for (x0, y0), (x1, y1) in pairwise(ARCH):
        if near_segment(x, y, x0, y0, x1, y1, 2.5):
            return WHITE + (255,)
    return BLUE + (255,)


def main(out: Path) -> None:
    rows = b"".join(b"\x00" + bytes(c for ix in range(N) for c in pixel(ix, iy)) for iy in range(N))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", N, N, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))
    out.write_bytes(png)
    print(out)


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "imager/vector-bridge-icon.png"))
