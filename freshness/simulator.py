from __future__ import annotations
import logging
import math
import os
from dataclasses import dataclass
import numpy as np
import pandas as pd

log = logging.getLogger("freshness")
HEADER_BYTES = 400  
SLACK_S = 3600


@dataclass
class Trace:
    resource_id: int
    group: str
    t: np.ndarray       
    v: np.ndarray        
    etag: list
    lm: list
    life: list
    size: np.ndarray


# Data loading
def load_traces(con):
    q = """SELECT f.fetch_id, f.resource_id, f.fetched_at, f.etag, f.last_modified, f.lifetime_s, f.size,
                  f.content_type, r.type AS rtype, r.fingerprinted, p.M, p.category
           FROM fetch f JOIN resource r USING(resource_id)
           LEFT JOIN fetch_pair p ON p.next_fetch = f.fetch_id
           WHERE f.kind='main' AND f.status=200 AND f.error IS NULL AND f.bot_challenge=0
           ORDER BY f.resource_id, f.fetched_at"""
    df = pd.read_sql_query(q, con)
    start = con.execute("SELECT MIN(started_at) FROM round WHERE kind='main'").fetchone()[0]
    return df, start


def _clean(col) -> list:
    return [v if isinstance(v, str) and v else None for v in col]


def group_of(rtype: str, fingerprinted: int, content_type: str | None) -> str:
    if fingerprinted:
        return "fingerprinted"
    ctype = content_type if isinstance(content_type, str) else ""
    if rtype in ("homepage", "article") or "html" in ctype.lower():
        return "html"
    return "other"


def split_traces(df: pd.DataFrame, t_split: float):
    train, test = {}, []
    for rid, d in df.groupby("resource_id", sort=False):
        d = d.reset_index(drop=True)
        version = np.cumsum(d["M"].fillna(0).to_numpy().astype(int))
        tr = d[d.fetched_at < t_split]
        te_idx = np.where(d.fetched_at.to_numpy() >= t_split)[0]
        grp = group_of(d.rtype.iloc[0], int(d.fingerprinted.iloc[0]), d.content_type.iloc[0])
        if len(tr) >= 2:
            m = tr["M"].iloc[1:].fillna(0).to_numpy()
            gaps = np.diff(tr.fetched_at.to_numpy())
            cats = tr["category"].iloc[1:].fillna("").tolist()
            last = tr.iloc[-1]
            train[rid] = {"X": int(m.sum()), "T": float(gaps.sum()), "n_pairs": len(m),
                          "bad_validator": sum(c in ("missed_change", "strong_etag_inconsistency") for c in cats),
                          "has_validator": bool(last.etag) or bool(last.last_modified)}
        if len(te_idx) >= 2:
            s = te_idx[0] - 1 if te_idx[0] > 0 else te_idx[0]
            dd = d.iloc[s:]
            test.append(Trace(rid, grp, dd.fetched_at.to_numpy(float), version[s:],
                              _clean(dd.etag), _clean(dd.last_modified), dd.lifetime_s.tolist(),
                              dd["size"].fillna(0).to_numpy(float)))
    return train, test


def replay(tr: Trace, mode: str, interval: float | None, conditional: bool) -> dict:
    t, v = tr.t, tr.v
    n = len(t)
    j, tv = 0, t[0]               
    req = 0
    byts = 0.0
    stale = {"start": 0.0, "mid": 0.0, "end": 0.0}

    def next_due():
        if mode == "headers":
            life = tr.life[j_life]
            life = life if (life is not None and not (isinstance(life, float) and math.isnan(life))) else 0.0
            return tv + max(life, 0.0)
        return tv + interval

    j_life = 0
    for k in range(n):
        if k > 0 and t[k] >= next_due() - SLACK_S:   
            req += 1
            not_modified = False
            if conditional:
                if tr.etag[j]:
                    not_modified = tr.etag[k] == tr.etag[j]
                elif tr.lm[j]:
                    not_modified = tr.lm[k] == tr.lm[j]
            if not_modified:
                byts += HEADER_BYTES
                tv, j_life = t[k], k       # 304: freshness restarts, old content kept
            else:
                byts += HEADER_BYTES + tr.size[k]
                j, tv, j_life = k, t[k], k
        if k + 1 < n:
            dt = t[k + 1] - t[k]
            c = v[j]
            stale["start"] += dt * (c != v[k + 1])
            stale["end"] += dt * (c != v[k])
            stale["mid"] += dt / 2 * (c != v[k]) + dt / 2 * (c != v[k + 1])
    total = t[-1] - t[0]
    days = total / 86400
    return {"stale_start": stale["start"] / total, "stale_mid": stale["mid"] / total,
            "stale_end": stale["end"] / total, "req_per_day": req / days, "bytes_per_day": byts / days}


def poisson_interval(info: dict | None, f: float, cfg) -> float:
    lo, hi = cfg.min_interval_h * 3600, cfg.max_interval_h * 3600
    if not info or info["T"] <= 0:
        return lo
    lam = max(info["X"], 0.5) / info["T"]          
    return float(min(max(-math.log(f) / lam, lo), hi))


def reliable(info: dict | None) -> bool:
    return bool(info) and info["has_validator"] and info["bad_validator"] == 0 and info["n_pairs"] >= 3


def simulate(cfg, con, out_dir: str | None = None) -> pd.DataFrame:
    out_dir = out_dir or cfg.results_dir
    os.makedirs(os.path.join(out_dir, "figures"), exist_ok=True)
    df, start = load_traces(con)
    if df.empty or start is None:
        raise SystemExit("No data to simulate yet.")
    end = df.fetched_at.max()
    t_split = start + cfg.train_days * 86400
    if t_split >= end - 86400:
        t_split = start + (end - start) * 2 / 3
        log.warning("Less than %d+1 days of data: using the first 2/3 for training instead.", cfg.train_days)
    train, test = split_traces(df, t_split)
    log.info("simulator: %d resources in the test period", len(test))

    rows = []
    for tr in test:
        info = train.get(tr.resource_id)
        res = [("a_obey_headers", "lifetime", replay(tr, "headers", None, True))]
        for h in cfg.fixed_intervals_h:
            res.append(("b_fixed", f"{h}h", replay(tr, "interval", h * 3600, False)))
        for f in cfg.poisson_freshness_targets:
            I = poisson_interval(info, f, cfg)
            res.append(("c_poisson", f"f={f}", replay(tr, "interval", I, False)))
            res.append(("d_hybrid", f"f={f}", replay(tr, "interval", I, reliable(info))))
        for policy, param, r in res:
            rows.append({"resource_id": tr.resource_id, "group": tr.group, "policy": policy, "param": param, **r})
    per = pd.DataFrame(rows)
    summary = (per.groupby(["group", "policy", "param"])
               .agg(resources=("resource_id", "nunique"), staleness_mid=("stale_mid", "mean"),
                    staleness_start=("stale_start", "mean"), staleness_end=("stale_end", "mean"),
                    requests_per_day=("req_per_day", "mean"), bytes_per_day=("bytes_per_day", "mean"))
               .reset_index())
    summary.to_csv(os.path.join(out_dir, "rq3_policies.csv"), index=False)
    eq = equal_cost(summary)
    eq.to_csv(os.path.join(out_dir, "rq3_equal_cost.csv"), index=False)
    fig_staleness_cost(summary, os.path.join(out_dir, "figures", "staleness_cost.png"))
    syn = synthetic_rate_check()
    syn.to_csv(os.path.join(out_dir, "t4_synthetic_check.csv"), index=False)
    print(eq.to_string(index=False))
    print(syn.to_string(index=False))
    log.info("simulation written to %s", out_dir)
    return summary


def equal_cost(summary: pd.DataFrame) -> pd.DataFrame:
    out = []
    for g, d in summary.groupby("group"):
        a = d[d.policy == "a_obey_headers"]
        if a.empty:
            continue
        cost = float(a.requests_per_day.iloc[0])
        row = {"group": g, "requests_per_day": cost, "a_obey_headers": float(a.staleness_mid.iloc[0])}
        for p in ("b_fixed", "c_poisson", "d_hybrid"):
            c = d[d.policy == p].sort_values("requests_per_day")
            if len(c) >= 2 and c.requests_per_day.min() <= cost <= c.requests_per_day.max() \
                    and c.requests_per_day.nunique() > 1:
                c = c.groupby("requests_per_day", as_index=False).staleness_mid.min()
                row[p] = float(np.interp(cost, c.requests_per_day, c.staleness_mid))
            else:
                cheaper = c[c.requests_per_day <= cost]
                row[p] = float(cheaper.staleness_mid.min()) if len(cheaper) else np.nan
        out.append(row)
    return pd.DataFrame(out)


def fig_staleness_cost(summary: pd.DataFrame, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    groups = [g for g in ("html", "fingerprinted", "other") if g in set(summary.group)]
    fig, axes = plt.subplots(1, len(groups), figsize=(4.2 * len(groups), 3.4), squeeze=False)
    styles = {"b_fixed": "o-", "c_poisson": "s-", "d_hybrid": "^--"}
    for ax, g in zip(axes[0], groups):
        d = summary[summary.group == g]
        for p, st in styles.items():
            c = d[d.policy == p].sort_values("requests_per_day")
            ax.plot(c.requests_per_day, c.staleness_mid * 100, st, ms=3, label=p)
        a = d[d.policy == "a_obey_headers"]
        ax.plot(a.requests_per_day, a.staleness_mid * 100, "k*", ms=10, label="a_obey_headers")
        ax.set_title(g)
        ax.set_xlabel("Requests per resource per day")
        ax.set_ylabel("Staleness (% of test time)")
        ax.legend(fontsize=7, frameon=False)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


# T4 completion criterion
def synthetic_rate_check(n_res: int = 500, days: int = 14, step_h: float = 4, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    out = []
    for per_day in (1 / 7, 1 / 3, 1 / 2, 1.0):
        lam = per_day / 86400
        T = days * 86400
        X_total = 0
        for _ in range(n_res):
            n_changes = rng.poisson(lam * T)
            times = rng.uniform(0, T, n_changes)
            slots = np.unique((times // (step_h * 3600)).astype(int))
            X_total += len(slots)         
        est = X_total / (n_res * T)
        err = abs(est - lam) / lam
        out.append({"true_changes_per_day": per_day, "estimated_per_day": est * 86400, "relative_error": err, "within_10pct": err <= 0.10})
    return pd.DataFrame(out)
