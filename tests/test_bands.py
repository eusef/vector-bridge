from vector_bridge import bands


def test_band_of_knows_the_amateur_bands_and_nothing_else():
    assert bands.band_of(14_074_000) == "20 m" and bands.band_of(144_390_000) == "2 m"
    assert bands.band_of(5_357_000) == "60 m" and bands.band_of(28_000_000) == "10 m"
    for hz in (10_000_000, 1_130_000, 88_100_000, 121_500_000, 162_550_000):  # WWV, AM, FM, air, NOAA
        assert bands.band_of(hz) is None


def test_mhz_reads_like_a_dial():
    assert bands.mhz(14_074_000) == "14.074 MHz" and bands.mhz(10_000_000) == "10.000 MHz"
    assert bands.mhz(7_074_500) == "7.0745 MHz"
