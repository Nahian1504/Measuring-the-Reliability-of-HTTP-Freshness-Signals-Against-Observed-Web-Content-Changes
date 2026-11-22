import os
import time
from freshness.dashboard import build_dashboard, collect_data

def test_dashboard_builds(cfg, con, tmp_path):
    t = time.time()
    con.execute("INSERT INTO site (site_id, domain, tranco_rank, rank_group, status) VALUES (1,'a.com',5,'top10k','active')")
    con.execute("INSERT INTO resource (resource_id, site_id, url, type) VALUES (1,1,'https://a.com/','homepage')")
    con.execute("INSERT INTO round (round_id, kind, started_at, finished_at, n_ok, n_err) VALUES (1,'main',?,?,1,0)", (t, t + 60))
    con.execute("INSERT INTO round (round_id, kind, started_at, finished_at, n_ok, n_err) VALUES (2,'main',?,?,1,0)", (t + 14400, t + 14460))
    for fid, rid in ((1, 1), (2, 2)):
        con.execute("INSERT INTO fetch (fetch_id, resource_id, round_id, kind, fetched_at, status, etag, etag_weak) "
                    "VALUES (?,1,?,'main',?,200,'\"x\"',0)", (fid, rid, t + (rid - 1) * 14400))
    con.execute("INSERT INTO fetch_pair (prev_fetch, next_fetch, resource_id, gap_s, B, M, E, category) "
                "VALUES (1,2,1,14400,1,1,0,'strong_etag_inconsistency')")
    con.commit()
    out = tmp_path / "res"
    out.mkdir()
    (out / "rq1_rates.csv").write_text("rate,description,group_by,group,events,pairs,sites,value,ci_low,ci_high\n"
                                       "etag_false_change,x,all,all,1,10,1,0.1,0.05,0.2\n", encoding="utf-8")

    data = collect_data(cfg, con, str(out))
    assert data["meta"]["sites"] == 1 and data["meta"]["main_rounds"] == 2
    assert data["sites"][0]["problems"] == 1 and data["sites"][0]["label"] == "too little data"
    assert data["rates"][0]["rate"] == "etag_false_change"

    path = build_dashboard(cfg, con, str(out))
    assert os.path.exists(path) and os.path.getsize(path) > 5000
    with open(path, encoding="utf-8") as f:
        html = f.read()
    assert "/*__DATA__*/" not in html, "the data was not inserted into the page"
    assert "a.com" in html and "etag_false_change" in html
    assert 'data-t="dark"' in html and 'data-t="light"' in html, "light/dark switch missing"