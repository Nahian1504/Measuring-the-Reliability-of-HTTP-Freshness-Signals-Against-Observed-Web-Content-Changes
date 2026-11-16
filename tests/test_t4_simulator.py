import numpy as np
import pytest
from freshness.simulator import Trace, poisson_interval, replay, synthetic_rate_check

H = 3600.0


def test_t4_rate_estimate_within_10_percent():
    out = synthetic_rate_check()
    print(out.to_string(index=False))
    assert out.within_10pct.all()


def trace(versions, etags=None, lm=None, life=None, size=1000):
    n = len(versions)
    return Trace(1, "html", np.arange(n) * 4 * H, np.array(versions), etags or [None] * n, lm or [None] * n, life or [None] * n, np.full(n, float(size)))


def test_replay_fixed_every_4h_is_never_stale_at_end_bound():
    tr = trace([0, 0, 1, 1, 1])                
    r = replay(tr, "interval", 4 * H, conditional=False)
    assert r["stale_end"] == 0
    assert r["stale_start"] == pytest.approx(4 / 16)
    assert r["stale_mid"] == pytest.approx(2 / 16)
    assert r["req_per_day"] == pytest.approx(4 / (16 / 24))


def test_replay_obey_headers_with_frozen_weak_etag_stays_stale():
    # a weak ETag that never changes: the cache gets 304 and keeps the old copy forever
    tr = trace([0, 1, 1, 1, 1], etags=['W/"same"'] * 5, life=[0.0] * 5)
    r = replay(tr, "headers", None, conditional=True)
    assert r["stale_end"] == pytest.approx(12 / 16)
    assert r["bytes_per_day"] == pytest.approx(4 * 400 / (16 / 24))   # only 304s, no body downloaded


def test_replay_obey_headers_with_correct_etag():
    tr = trace([0, 1, 1, 2, 2], etags=['"a"', '"b"', '"b"', '"c"', '"c"'], life=[0.0] * 5)
    r = replay(tr, "headers", None, conditional=True)
    assert r["stale_end"] == 0


def test_poisson_interval():
    class C: min_interval_h = 4; max_interval_h = 168
    info = {"X": 7, "T": 7 * 86400.0}          # one change per day
    assert poisson_interval(info, 0.9, C) == 4 * H           
    assert poisson_interval(info, 0.5, C) == pytest.approx(-np.log(0.5) * 86400)  
    assert poisson_interval({"X": 0, "T": 14 * 86400.0}, 0.5, C) <= 168 * H
