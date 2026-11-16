import os
import sys
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from freshness.config import load_config  
from freshness.db import connect 


@pytest.fixture
def cfg(tmp_path):
    c = load_config(db_path=str(tmp_path / "test.db"), results_dir=str(tmp_path / "results"))
    c.timeout_s = 3         
    c.per_domain_gap_s = 0.2  
    return c


@pytest.fixture
def con(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


def add_site(con, base_url, paths, domain="127.0.0.1", **flags):
    cur = con.execute("INSERT INTO site (domain, tranco_rank, rank_group, robots_ok, homepage_url, status) VALUES (?,?,?,?,?, 'active')", (domain, 1, "top10k", 1, base_url + "/"))
    sid = cur.lastrowid
    ids = {}
    for p in paths:
        c = con.execute("INSERT INTO resource (site_id, url, type, cond_sample, b2b_sample) VALUES (?,?,?,?,?)", (sid, base_url + p, "page", int(flags.get("cond", 0)), int(flags.get("b2b", 0))))
        ids[p] = c.lastrowid
    con.commit()
    return ids
