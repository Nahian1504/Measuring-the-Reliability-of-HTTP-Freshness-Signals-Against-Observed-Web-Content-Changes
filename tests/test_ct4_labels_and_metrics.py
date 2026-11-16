import numpy as np
import pandas as pd
import pytest
from freshness.analysis import rq1_rates, site_bootstrap
from freshness.pairs import flags_for


def f(body, etag=None, weak=None, lm=None, text=None, t=0.0, life=None, ctype="text/html"):
    return {"body_sha256": body, "etag": etag, "etag_weak": weak, "last_modified": lm, "text_sha256": text,
            "fetched_at": t, "lifetime_s": life, "content_type": ctype}


H = 4 * 3600
# (description, prev, next, similarity, expected category)
CASES = [
    # strong ETag unchanged, bytes changed -> strong_etag_inconsistency
    ("strong same, body changed (js)", f("a", '"x"', 0, ctype="text/js"), f("b", '"x"', 0, ctype="text/js", t=H), None, "strong_etag_inconsistency"),
    ("strong same, html real change", f("a", '"x"', 0, text="t1"), f("b", '"x"', 0, text="t2", t=H), 0.5, "strong_etag_inconsistency"),
    ("strong same, html noise only", f("a", '"x"', 0, text="t1"), f("b", '"x"', 0, text="t1", t=H), None, "strong_etag_inconsistency"),
    ("strong same, lm same too", f("a", '"x"', 0, "L1", ctype="img"), f("b", '"x"', 0, "L1", ctype="img", t=H), None, "strong_etag_inconsistency"),
    ("strong same, lm changed", f("a", '"x"', 0, "L1", ctype="img"), f("b", '"x"', 0, "L2", ctype="img", t=H), None, "strong_etag_inconsistency"),
    # weak ETag unchanged + meaningful change -> missed_change
    ("weak same, real change", f("a", 'W/"x"', 1, text="t1"), f("b", 'W/"x"', 1, text="t2", t=H), 0.6, "missed_change"),
    ("weak same, real change, lm changed", f("a", 'W/"x"', 1, "L1", text="t1"), f("b", 'W/"x"', 1, "L2", text="t2", t=H), 0.3, "missed_change"),
    ("weak same, non-html change", f("a", 'W/"x"', 1, ctype="css"), f("b", 'W/"x"', 1, ctype="css", t=H), None, "missed_change"),
    # no ETag, Last-Modified unchanged + meaningful change -> missed_change
    ("no etag, lm same, real change", f("a", lm="L1", text="t1"), f("b", lm="L1", text="t2", t=H), 0.4, "missed_change"),
    ("no etag, lm same, css changed", f("a", lm="L1", ctype="css"), f("b", lm="L1", ctype="css", t=H), None, "missed_change"),
    ("no etag, lm same, sim just below theta", f("a", lm="L1", text="t1"), f("b", lm="L1", text="t2", t=H), 0.94, "missed_change"),
    # weak ETag unchanged, small change -> allowed_weak_etag
    ("weak same, noise only", f("a", 'W/"x"', 1, text="t1"), f("b", 'W/"x"', 1, text="t1", t=H), None, "allowed_weak_etag"),
    ("weak same, tiny text change", f("a", 'W/"x"', 1, text="t1"), f("b", 'W/"x"', 1, text="t2", t=H), 0.97, "allowed_weak_etag"),
    ("weak same, sim exactly theta", f("a", 'W/"x"', 1, text="t1"), f("b", 'W/"x"', 1, text="t2", t=H), 0.95, "allowed_weak_etag"),
    # ETag changed, bytes same -> unnecessary_redownload
    ("strong changed, same body", f("a", '"x"', 0, ctype="js"), f("a", '"y"', 0, ctype="js", t=H), None, "unnecessary_redownload"),
    ("weak changed, same body", f("a", 'W/"x"', 1, text="t1"), f("a", 'W/"y"', 1, text="t1", t=H), None, "unnecessary_redownload"),
    ("strong changed, same body, lm changed", f("a", '"x"', 0, "L1", ctype="js"), f("a", '"y"', 0, "L2", ctype="js", t=H), None, "unnecessary_redownload"),
    ("etag changed same body img", f("a", '"1"', 0, ctype="img"), f("a", '"2"', 0, ctype="img", t=H), None, "unnecessary_redownload"),
    # Last-Modified changed without meaningful change -> noisy_last_modified
    ("no etag, lm changed, noise only", f("a", lm="L1", text="t1"), f("b", lm="L2", text="t1", t=H), None, "noisy_last_modified"),
    ("no etag, lm changed, nothing changed", f("a", lm="L1", ctype="js"), f("a", lm="L2", ctype="js", t=H), None, "noisy_last_modified"),
    ("weak changed with body, lm changed, small", f("a", 'W/"x"', 1, "L1", text="t1"), f("b", 'W/"y"', 1, "L2", text="t2", t=H), 0.99, "noisy_last_modified"),
    ("no etag, lm changed, tiny change", f("a", lm="L1", text="t1"), f("b", lm="L2", text="t2", t=H), 0.98, "noisy_last_modified"),
    # consistent
    ("nothing changed at all", f("a", '"x"', 0, "L1", ctype="js"), f("a", '"x"', 0, "L1", ctype="js", t=H), None, "consistent"),
    ("strong changed with body", f("a", '"x"', 0, ctype="js"), f("b", '"y"', 0, ctype="js", t=H), None, "consistent"),
    ("weak changed with real change", f("a", 'W/"x"', 1, text="t1"), f("b", 'W/"y"', 1, text="t2", t=H), 0.2, "consistent"),
    ("lm changed with real change", f("a", lm="L1", text="t1"), f("b", lm="L2", text="t2", t=H), 0.3, "consistent"),
    ("no validators, body changed", f("a"), f("b", t=H), None, "consistent"),
    ("no validators, nothing changed", f("a"), f("a", t=H), None, "consistent"),
    ("weak same, nothing changed", f("a", 'W/"x"', 1, text="t1"), f("a", 'W/"x"', 1, text="t1", t=H), None, "consistent"),
    ("lm same, nothing changed", f("a", lm="L1", ctype="js"), f("a", lm="L1", ctype="js", t=H), None, "consistent"),
]


def test_thirty_cases():
    assert len(CASES) == 30


@pytest.mark.parametrize("desc,prev,nxt,sim,expected", CASES, ids=[c[0] for c in CASES])
def test_ct4_labels(desc, prev, nxt, sim, expected):
    assert flags_for(prev, nxt, sim, theta=0.95)["category"] == expected


def test_early_change_flag():
    # declared fresh for 1 day, changed after 4 h -> early change
    assert flags_for(f("a", life=86400), f("b", t=H), None, 0.95)["early_change"] == 1
    # declared fresh 4 h, unchanged -> 0
    assert flags_for(f("a", life=H), f("a", t=H), None, 0.95)["early_change"] == 0
    # declared fresh 60 s only -> not fresh for the whole gap -> unknown
    assert flags_for(f("a", life=60), f("b", t=H), None, 0.95)["early_change"] is None


def test_rate_matches_hand_calculation(cfg):
    # 3 sites. ETag false-change rate = pairs with E=1,B=0 / pairs with B=0 (ETag present)
    rows = []
    def add(site, B, E, n):
        for _ in range(n):
            rows.append({"site_id": site, "B": B, "M": B, "E": E, "L": np.nan, "etag_present": 1, "etag_weak": 0,
                         "etag_strength": "strong", "early_change": np.nan, "rtype": "script",
                         "fingerprinted": "not fingerprinted", "rank_group": "top10k", "fkind": "main"})
    add(1, 0, 1, 2); add(1, 0, 0, 8)   
    add(2, 0, 1, 5); add(2, 0, 0, 5)    
    add(3, 0, 0, 20)                    
    df = pd.DataFrame(rows)
    out = rq1_rates(df, cfg)
    row = out[(out.rate == "etag_false_change") & (out.group_by == "all")].iloc[0]
    assert row.events == 7 and row.pairs == 40
    assert row.value == pytest.approx(7 / 40)
    assert row.ci_low <= 7 / 40 <= row.ci_high


def test_bootstrap_single_site_has_no_spread():
    v, lo, hi, n = site_bootstrap([1, 1, 1, 1], [1, 0, 1, 0], [1, 1, 1, 1], 200, 1)
    assert (v, lo, hi, n) == (0.5, 0.5, 0.5, 1)
