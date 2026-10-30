"""Amateur bands: the union of the three IARU regions, so no region setting is needed.

Used to refuse Unlock Transmit on a frequency no amateur may use anywhere (WWV at 10 MHz,
AM broadcast, airband). A country's own edges are narrower, and the radio's transmit lockout
enforces those. The operator can defeat this check if an allocation changes.
"""

from __future__ import annotations

KHZ, MHZ = 1_000, 1_000_000

# (lowest Hz, highest Hz, name). Edges are the widest any region allocates.
BANDS: tuple[tuple[int, int, str], ...] = (
    (135_700, 137_800, "2200 m"),
    (472 * KHZ, 479 * KHZ, "630 m"),
    (1800 * KHZ, 2000 * KHZ, "160 m"),
    (3500 * KHZ, 4000 * KHZ, "80 m"),
    (5250 * KHZ, 5450 * KHZ, "60 m"),
    (7000 * KHZ, 7300 * KHZ, "40 m"),
    (10100 * KHZ, 10150 * KHZ, "30 m"),
    (14000 * KHZ, 14350 * KHZ, "20 m"),
    (18068 * KHZ, 18168 * KHZ, "17 m"),
    (21000 * KHZ, 21450 * KHZ, "15 m"),
    (24890 * KHZ, 24990 * KHZ, "12 m"),
    (28000 * KHZ, 29700 * KHZ, "10 m"),
    (50 * MHZ, 54 * MHZ, "6 m"),
    (69_900 * KHZ, 70_500 * KHZ, "4 m"),
    (144 * MHZ, 148 * MHZ, "2 m"),
    (219 * MHZ, 225 * MHZ, "1.25 m"),
    (420 * MHZ, 450 * MHZ, "70 cm"),
    (902 * MHZ, 928 * MHZ, "33 cm"),
    (1240 * MHZ, 1300 * MHZ, "23 cm"),
    (2300 * MHZ, 2450 * MHZ, "13 cm"),
    (3300 * MHZ, 3500 * MHZ, "9 cm"),
    (5650 * MHZ, 5925 * MHZ, "6 cm"),
    (10_000 * MHZ, 10_500 * MHZ, "3 cm"),
    (24_000 * MHZ, 24_250 * MHZ, "1.2 cm"),
    (47_000 * MHZ, 47_200 * MHZ, "6 mm"),
    (76_000 * MHZ, 81_000 * MHZ, "4 mm"),
    (122_250 * MHZ, 123_000 * MHZ, "2.5 mm"),
    (134_000 * MHZ, 141_000 * MHZ, "2 mm"),
    (241_000 * MHZ, 250_000 * MHZ, "1 mm"),
)


def band_of(hz: int) -> str | None:
    """The band's name, or None when no amateur may transmit there in any region."""
    return next((name for lo, hi, name in BANDS if lo <= hz <= hi), None)


def mhz(hz: int) -> str:
    """14074000 -> "14.074 MHz": trailing zeros dropped, never fewer than three decimals."""
    text = f"{hz / MHZ:.6f}".rstrip("0")
    whole, _, frac = text.partition(".")
    return f"{whole}.{frac.ljust(3, '0')} MHz"
