from __future__ import annotations
import json
import logging
import os
import re
import time
from datetime import datetime
import numpy as np
import pandas as pd

log = logging.getLogger("freshness")

CATS = ["strong_etag_inconsistency", "missed_change", "allowed_weak_etag", "unnecessary_redownload", "noisy_last_modified", "consistent"]

def _csv(path):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        try:
            return pd.read_csv(path)
        except pd.errors.EmptyDataError:
            return pd.DataFrame()
    return pd.DataFrame()

def _records(df: pd.DataFrame):
    if df is None or df.empty:
        return []
    df = df.replace({np.nan: None})
    return json.loads(df.to_json(orient="records"))

def collect_data(cfg, con, results_dir: str) -> dict:
    q = lambda sql, *p: con.execute(sql, p).fetchone()
    n_sites = q("SELECT COUNT(*) FROM site WHERE status='active'")[0]
    n_excl = q("SELECT COUNT(*) FROM site WHERE status='excluded'")[0]
    n_res = q("SELECT COUNT(*) FROM resource")[0]
    n_fetch = q("SELECT COUNT(*) FROM fetch")[0]
    r = q("SELECT COUNT(*), MIN(started_at), MAX(started_at), SUM(n_ok), SUM(n_err) FROM round WHERE kind='main'")
    days = (r[2] - r[1]) / 86400 if r[0] else 0
    ok_share = (r[3] or 0) / ((r[3] or 0) + (r[4] or 0)) if r[0] and ((r[3] or 0) + (r[4] or 0)) else None
    meta = {
        "generated": datetime.now().strftime("%b %d, %Y %H:%M"),
        "sites": n_sites, "excluded": n_excl, "resources": n_res, "fetches": n_fetch,
        "main_rounds": r[0], "days": round(days, 1), "ok_share": ok_share,
        "first": datetime.fromtimestamp(r[1]).strftime("%b %d %H:%M") if r[1] else None,
        "last": datetime.fromtimestamp(r[2]).strftime("%b %d %H:%M") if r[2] else None,
        "theta": cfg.theta, "tranco": getattr(cfg, "tranco_list_id", ""),
        "pairs": q("SELECT COUNT(*) FROM fetch_pair")[0],
    }

    rounds = pd.read_sql_query("SELECT kind, started_at, n_ok, n_err FROM round", con)
    health = []
    if not rounds.empty:
        rounds["day"] = [datetime.fromtimestamp(t).strftime("%m-%d") for t in rounds.started_at]
        for day, g in rounds.groupby("day"):
            m = g[g.kind == "main"]
            ok, err = m.n_ok.fillna(0).sum(), m.n_err.fillna(0).sum()
            health.append({"day": day, "main": int(len(m)), "hourly": int((g.kind == "hourly").sum()), "ok_share": float(ok / (ok + err)) if ok + err else None})

    cats = pd.read_sql_query(
        """SELECT r.type AS rtype, p.category, COUNT(*) AS n FROM fetch_pair p
           JOIN fetch f ON f.fetch_id=p.next_fetch JOIN resource r ON r.resource_id=p.resource_id
           WHERE f.kind='main' GROUP BY r.type, p.category""", con)

    # per-resource and per-site summaries 
    pr = pd.read_sql_query(
        """SELECT p.resource_id, COUNT(*) AS pairs, SUM(p.B) AS byte_changes, SUM(p.M) AS real_changes,
                  SUM(p.category='strong_etag_inconsistency') AS strong_etag_inconsistency,
                  SUM(p.category='missed_change') AS missed_change,
                  SUM(p.category='allowed_weak_etag') AS allowed_weak_etag,
                  SUM(p.category='unnecessary_redownload') AS unnecessary_redownload,
                  SUM(p.category='noisy_last_modified') AS noisy_last_modified
           FROM fetch_pair p JOIN fetch f ON f.fetch_id=p.next_fetch
           WHERE f.kind='main' GROUP BY p.resource_id""", con)
    fr = pd.read_sql_query(
        """SELECT resource_id, AVG(etag IS NOT NULL) AS etag, AVG(etag_weak=1) AS weak,
                  AVG(last_modified IS NOT NULL) AS lm, AVG(cache_control IS NOT NULL) AS cc,
                  AVG(lifetime_s) AS lifetime, COUNT(*) AS ok_fetches
           FROM fetch WHERE kind='main' AND status=200 AND error IS NULL GROUP BY resource_id""", con)
    res = pd.read_sql_query(
        """SELECT r.resource_id, r.site_id, r.url, r.type, r.fingerprinted, r.active
           FROM resource r JOIN site s USING(site_id) WHERE s.status='active'""", con)
    res = res.merge(pr, on="resource_id", how="left").merge(fr, on="resource_id", how="left")
    num = ["pairs", "byte_changes", "real_changes", "strong_etag_inconsistency", "missed_change",
           "allowed_weak_etag", "unnecessary_redownload", "noisy_last_modified"]
    res[num] = res[num].fillna(0).astype(int)
    res["problems"] = res.strong_etag_inconsistency + res.missed_change

    sites = pd.read_sql_query("SELECT site_id, domain, tranco_rank, rank_group FROM site WHERE status='active'", con)
    agg = res.groupby("site_id").agg(
        resources=("resource_id", "count"), pairs=("pairs", "sum"), real_changes=("real_changes", "sum"),
        problems=("problems", "sum"), redownloads=("unnecessary_redownload", "sum"),
        noisy_lm=("noisy_last_modified", "sum"), etag=("etag", "mean"), lm=("lm", "mean"),
        cc=("cc", "mean")).reset_index()
    sites = sites.merge(agg, on="site_id", how="left")
    for c in ("resources", "pairs", "real_changes", "problems", "redownloads", "noisy_lm"):
        sites[c] = sites[c].fillna(0).astype(int)
    sites["problem_rate"] = np.where(sites.pairs > 0, sites.problems / sites.pairs.clip(lower=1), np.nan)
    sites["label"] = np.select(
        [sites.pairs < 10, sites.problems == 0, sites.problem_rate < 0.01],
        ["too little data", "reliable", "mostly reliable"], "unreliable")
    res = res.round(4)
    sites = sites.round(4)

    return {
        "meta": meta,
        "health": health,
        "presence": _records(_csv(os.path.join(results_dir, "rq1_presence.csv"))),
        "rates": _records(_csv(os.path.join(results_dir, "rq1_rates.csv"))),
        "rq2": _records(_csv(os.path.join(results_dir, "rq2_meaningful.csv"))),
        "categories": _records(cats),
        "rq3": _records(_csv(os.path.join(results_dir, "rq3_policies.csv"))),
        "rq3_equal": _records(_csv(os.path.join(results_dir, "rq3_equal_cost.csv"))),
        "t4": _records(_csv(os.path.join(results_dir, "t4_synthetic_check.csv"))),
        "quality": {
            "Exclusions (why candidate sites were dropped)": _records(_csv(os.path.join(results_dir, "exclusions.csv"))),
            "Conditional requests vs hash comparison (5% sample)": _records(
                _csv(os.path.join(results_dir, "conditional_agreement.csv"))),
            "Back-to-back fetches (CDN check, 5% sample)": _records(
                _csv(os.path.join(results_dir, "cdn_back_to_back.csv"))),
            "Hourly vs 4-hourly changes (100 resources)": _records(_csv(os.path.join(results_dir, "hourly_missed.csv"))),
        },
        "sites": _records(sites.drop(columns=["site_id"]).assign(id=sites.site_id)),
        "resources": _records(res[["site_id", "url", "type", "fingerprinted", "pairs", "byte_changes",
                                   "real_changes", "strong_etag_inconsistency", "missed_change",
                                   "allowed_weak_etag", "unnecessary_redownload", "noisy_last_modified",
                                   "etag", "weak", "lm", "cc", "lifetime"]]),
    }


def build_dashboard(cfg, con, results_dir: str | None = None, out_path: str | None = None) -> str:
    results_dir = results_dir or cfg.results_dir
    out_path = out_path or os.path.join(results_dir, "dashboard.html")
    if not os.path.exists(os.path.join(results_dir, "rq1_rates.csv")):
        raise SystemExit(f"No analysis results in {results_dir}. Run 'python run.py pairs' and "
                         f"'python run.py analyze' (with the same --db and --out) first.")
    t0 = time.time()
    data = collect_data(cfg, con, results_dir)
    with open(os.path.join(os.path.dirname(__file__), "dashboard_template.html"), encoding="utf-8") as f:
        html = f.read()
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html, n = re.subn(r"const D\s*=\s*[^;\r\n]*;", lambda m: "const D = " + payload + ";", html, count=1)
    if n != 1:
        raise SystemExit("dashboard_template.html is damaged (no 'const D = ...;' line). "
                         "Copy it again from dashboard_update.zip.")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log.info("dashboard written to %s (%.0f KB, %.1f s)", out_path, os.path.getsize(out_path) / 1024,
             time.time() - t0)
    print(f"\nOpen this file in your browser:\n  {os.path.abspath(out_path)}")
    return out_path
