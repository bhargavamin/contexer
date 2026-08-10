from benchmarks.report import wilson_interval


def test_wilson_known_value():
    lo, hi = wilson_interval(8, 8)
    assert 0.62 < lo < 0.68 and hi == 1.0


def test_wilson_zero_n():
    assert wilson_interval(0, 0) == (0.0, 0.0)
