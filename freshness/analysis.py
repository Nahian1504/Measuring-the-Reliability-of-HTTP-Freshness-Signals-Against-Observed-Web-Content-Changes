from __future__ import annotations
import logging
import os
import numpy as np
import pandas as pd

log = logging.getLogger("freshness")


# Loading
def load_pairs(con) -> pd.DataFrame:
    q = """SELECT p.*, f.kind AS fkind, f.content_type, f.fetched_at AS t_next, f.lifetime_s AS next_lifetime,
                  r.type AS rtype, r.fingerprinted, r.site_id, s.rank_group
           FROM fetch_pair p
           JOIN fetch f ON f.fetch_id = p.next_fetch
           JOIN resource r ON r.resource_id = p.resource_id
           JOIN site s ON s.site_id = r.site_id"""
    df = pd.read_sql_query(q, con)
    if df.empty:
        return df
    df["is_html"] = df["content_type"].fillna("").str.contains("html", case=False)
    df["etag_strength"] = np.where(df["etag_present"] == 1, np.where(df["etag_weak"] == 1, "weak", "strong"), "none")
    df["fingerprinted"] = df["fingerprinted"].map({1: "fingerprinted", 0: "not fingerprinted"})
    return df


def load_fetches(con, kind: str = "main") -> pd.DataFrame:
    q = """SELECT f.fetch_id, f.resource_id, f.kind, f.fetched_at, f.status, f.error, f.etag, f.etag_weak,
                  f.last_modified, f.cache_control, f.lifetime_s, f.lifetime_src, f.lm_uninformative,
                  f.size, f.bot_challenge, f.content_type,
                  r.type AS rtype, r.fingerprinted, r.site_id, s.rank_group
           FROM fetch f JOIN resource r USING(resource_id) JOIN site s USING(site_id)
           WHERE f.kind = ?"""
    df = pd.read_sql_query(q, con, params=(kind,))
    if not df.empty:
        df["fingerprinted"] = df["fingerprinted"].map({1: "fingerprinted", 0: "not fingerprinted"})
        df["etag_strength"] = np.where(df["etag"].notna(), np.where(df["etag_weak"] == 1, "weak", "strong"), "none")
    return df


# Bootstrap
def site_bootstrap(site_ids, num, den, n_boot: int = 1000, seed: int = 42):
    d = pd.DataFrame({"site": site_ids, "num": num, "den": den}).groupby("site")[["num", "den"]].sum()
    total_den = d["den"].sum()
    if total_den == 0:
        return np.nan, np.nan, np.nan, 0
    value = d["num"].sum() / total_den
    n = len(d)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    nums, dens = d["num"].to_numpy()[idx].sum(1), d["den"].to_numpy()[idx].sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        ratios = nums / dens
    ratios = ratios[np.isfinite(ratios)]
    lo, hi = (np.percentile(ratios, [2.5, 97.5]) if len(ratios) else (np.nan, np.nan))
    return value, lo, hi, n


# RQ1 rates
def rate_definitions():
    return [
        ("etag_false_change", "ETag changed but bytes did not (share of unchanged pairs)",
         lambda d: (d.etag_present == 1) & (d.B == 0), lambda d: d.E == 1),
        ("strong_etag_missed_change", "Strong ETag unchanged but bytes changed (share of changed pairs)",
         lambda d: (d.etag_strength == "strong") & (d.B == 1), lambda d: d.E == 0),
        ("weak_etag_missed_meaningful", "Weak ETag unchanged but a meaningful change happened",
         lambda d: (d.etag_strength == "weak") & (d.M == 1), lambda d: d.E == 0),
        ("lm_false_change", "Last-Modified changed without a meaningful change",
         lambda d: d.L.notna() & (d.M == 0), lambda d: d.L == 1),
        ("lm_missed_change", "Last-Modified unchanged but a meaningful change happened",
         lambda d: d.L.notna() & (d.M == 1), lambda d: d.L == 0),
        ("cc_early_change", "Changed while Cache-Control said it was still fresh",
         lambda d: d.early_change.notna(), lambda d: d.early_change == 1),
    ]


GROUPS = [("all", None), ("type", "rtype"), ("fingerprinted", "fingerprinted"),
          ("etag_strength", "etag_strength"), ("rank_group", "rank_group")]


def rq1_rates(pairs: pd.DataFrame, cfg) -> pd.DataFrame:
    out = []
    main = pairs[pairs["fkind"] == "main"]
    for name, desc, pop, event in rate_definitions():
        base = main[pop(main)]
        for gname, col in GROUPS:
            parts = [("all", base)] if col is None else list(base.groupby(col))
            for gval, d in parts:
                if d.empty:
                    continue
                v, lo, hi, ns = site_bootstrap(d["site_id"], event(d).astype(int), np.ones(len(d)),
                                               int(cfg.bootstrap_resamples), int(cfg.bootstrap_seed))
                out.append({"rate": name, "description": desc, "group_by": gname, "group": gval,
                            "events": int(event(d).sum()), "pairs": len(d), "sites": ns,
                            "value": v, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(out)


def rq1_presence(fetches: pd.DataFrame, cfg) -> pd.DataFrame:
    ok = fetches[(fetches.status == 200) & fetches.error.isna() & (fetches.bot_challenge == 0)]
    measures = [
        ("has_etag", "Response has an ETag", lambda d: np.ones(len(d), bool), lambda d: d.etag.notna()),
        ("has_weak_etag", "ETag is weak (W/)", lambda d: d.etag.notna(), lambda d: d.etag_weak == 1),
        ("has_last_modified", "Response has Last-Modified", lambda d: np.ones(len(d), bool),
         lambda d: d.last_modified.notna()),
        ("lm_uninformative", "Last-Modified within 60 s of Date (useless)", lambda d: d.last_modified.notna(),
         lambda d: d.lm_uninformative == 1),
        ("has_cache_control", "Response has Cache-Control", lambda d: np.ones(len(d), bool),
         lambda d: d.cache_control.notna()),
        ("has_lifetime", "Declares a freshness lifetime (s-maxage/max-age/Expires)",
         lambda d: np.ones(len(d), bool), lambda d: d.lifetime_s.notna()),
        ("no_store_or_no_cache", "no-store or no-cache", lambda d: np.ones(len(d), bool),
         lambda d: d.lifetime_src.isin(["no-store", "no-cache"])),
    ]
    out = []
    for name, desc, pop, event in measures:
        base = ok[np.asarray(pop(ok), bool)]
        for gname, col in GROUPS:
            if col == "etag_strength":
                continue
            parts = [("all", base)] if col is None else list(base.groupby(col))
            for gval, d in parts:
                if d.empty:
                    continue
                v, lo, hi, ns = site_bootstrap(d["site_id"], np.asarray(event(d), int), np.ones(len(d)), int(cfg.bootstrap_resamples), int(cfg.bootstrap_seed))
                out.append({"measure": name, "description": desc, "group_by": gname, "group": gval,
                            "fetches": len(d), "sites": ns, "value": v, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(out)


# RQ2
def rq2_meaningful(pairs: pd.DataFrame, cfg) -> pd.DataFrame:
    d = pairs[(pairs.fkind == "main") & pairs.is_html & (pairs.B == 1) & pairs.similarity.notna()]
    out = []
    for theta in cfg.theta_sweep:
        meaningful = (d.similarity < theta).astype(int)
        for gname, col in [("all", None), ("type", "rtype"), ("rank_group", "rank_group")]:
            parts = [("all", d)] if col is None else list(d.groupby(col))
            for gval, g in parts:
                if g.empty:
                    continue
                v, lo, hi, ns = site_bootstrap(g["site_id"], meaningful.loc[g.index], np.ones(len(g)), int(cfg.bootstrap_resamples), int(cfg.bootstrap_seed))
                out.append({"theta": theta, "group_by": gname, "group": gval, "byte_changes": len(g),
                            "meaningful": int(meaningful.loc[g.index].sum()), "sites": ns,
                            "share_meaningful": v, "ci_low": lo, "ci_high": hi})
    return pd.DataFrame(out)


def conditional_agreement(con) -> pd.DataFrame:
    q = """SELECT f.resource_id, f.cond_status, f.body_sha256, r.type AS rtype, r.fingerprinted,
                  (SELECT p.body_sha256 FROM fetch p WHERE p.resource_id=f.resource_id AND p.kind='main'
                     AND p.status=200 AND p.fetched_at < f.fetched_at ORDER BY p.fetched_at DESC LIMIT 1) AS prev_sha
           FROM fetch f JOIN resource r USING(resource_id)
           WHERE f.kind='main' AND f.cond_status IS NOT NULL AND f.status=200"""
    d = pd.read_sql_query(q, con)
    d = d[d.prev_sha.notna()]
    if d.empty:
        return pd.DataFrame()
    changed = d.body_sha256 != d.prev_sha
    d["outcome"] = np.select(
        [(d.cond_status == 304) & ~changed, (d.cond_status == 200) & changed,
         (d.cond_status == 304) & changed, (d.cond_status == 200) & ~changed],
        ["agree_304_unchanged", "agree_200_changed", "WRONG_304_but_changed", "extra_200_unchanged"], "other")
    t = d.groupby(["rtype", "outcome"]).size().unstack(fill_value=0)
    t["agreement"] = (t.get("agree_304_unchanged", 0) + t.get("agree_200_changed", 0)) / t.sum(axis=1)
    return t.reset_index()


def cdn_back_to_back(con) -> pd.DataFrame:
    q = """SELECT a.resource_id, r.type AS rtype, a.body_sha256 AS h1, b.body_sha256 AS h2,
                  a.etag AS e1, b.etag AS e2, a.last_modified AS l1, b.last_modified AS l2
           FROM fetch a JOIN fetch b ON a.resource_id=b.resource_id AND a.round_id=b.round_id
           JOIN resource r ON r.resource_id=a.resource_id
           WHERE a.kind='main' AND b.kind='b2b' AND a.status=200 AND b.status=200"""
    d = pd.read_sql_query(q, con)
    if d.empty:
        return pd.DataFrame()
    same_body = d.h1 == d.h2
    d["etag_differs_same_body"] = same_body & d.e1.notna() & d.e2.notna() & (d.e1 != d.e2)
    d["lm_differs_same_body"] = same_body & d.l1.notna() & d.l2.notna() & (d.l1 != d.l2)
    d["body_differs"] = ~same_body
    t = d.groupby("rtype")[["etag_differs_same_body", "lm_differs_same_body", "body_differs"]].agg(["sum", "mean"])
    t.columns = [f"{a}_{'count' if b == 'sum' else 'share'}" for a, b in t.columns]
    t.insert(0, "pairs", d.groupby("rtype").size())
    return t.reset_index()


def hourly_missed(pairs: pd.DataFrame) -> pd.DataFrame:
    h = pairs[pairs.fkind == "hourly"]
    if h.empty:
        return pd.DataFrame()
    ids = h.resource_id.unique()
    m = pairs[(pairs.fkind == "main") & pairs.resource_id.isin(ids)]
    a = h.groupby("resource_id").agg(hourly_changes=("M", "sum"), hourly_hours=("gap_s", lambda s: s.sum() / 3600))
    b = m.groupby("resource_id").agg(fourhour_changes=("M", "sum"), fourhour_hours=("gap_s", lambda s: s.sum() / 3600))
    t = a.join(b, how="inner")
    t["hourly_rate_per_day"] = t.hourly_changes / t.hourly_hours * 24
    t["fourhour_rate_per_day"] = t.fourhour_changes / t.fourhour_hours * 24
    total = pd.DataFrame([{
        "resources": len(t),
        "hourly_rate_per_day": t.hourly_changes.sum() / t.hourly_hours.sum() * 24,
        "fourhour_rate_per_day": t.fourhour_changes.sum() / t.fourhour_hours.sum() * 24,
    }])
    total["share_of_changes_missed_by_4h"] = 1 - total.fourhour_rate_per_day / total.hourly_rate_per_day
    return total


def exclusions(con) -> pd.DataFrame:
    return pd.read_sql_query(
        """SELECT rank_group, status, COALESCE(exclude_reason,'') AS reason, COUNT(*) AS sites
           FROM site GROUP BY rank_group, status, reason ORDER BY rank_group, sites DESC""", con)


# Figures
def fig_categories(pairs: pd.DataFrame, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .pairs import CATEGORIES
    d = pairs[(pairs.fkind == "main") & (pairs.category != "consistent")]
    t = d.groupby(["rtype", "category"]).size().unstack(fill_value=0)
    t = t.reindex(columns=[c for c in CATEGORIES if c != "consistent" and c in t.columns])
    totals = pairs[pairs.fkind == "main"].groupby("rtype").size()
    t = t.div(totals, axis=0).fillna(0) * 100
    fig, ax = plt.subplots(figsize=(7, 3.6))
    t.plot(kind="bar", ax=ax, width=0.8)
    ax.set_ylabel("% of pairs")
    ax.set_xlabel("")
    ax.set_title("Pair categories by resource type (consistent pairs not shown)")
    ax.legend(fontsize=7, frameon=False)
    plt.xticks(rotation=0)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


def fig_lifetime_vs_change(pairs: pd.DataFrame, path: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    d = pairs[pairs.fkind == "main"]
    g = d.groupby(["resource_id", "rtype"]).agg(changes=("M", "sum"), hours=("gap_s", lambda s: s.sum() / 3600), lifetime=("next_lifetime", "median")).reset_index()
    g = g[(g.lifetime > 0) & (g.changes > 0)]
    if g.empty:
        return
    g["mean_hours_between_changes"] = g.hours / g.changes
    fig, ax = plt.subplots(figsize=(5.5, 4))
    for t, part in g.groupby("rtype"):
        ax.scatter(part.lifetime / 3600, part.mean_hours_between_changes, s=8, alpha=0.6, label=t)
    lim = [1e-3, 1e4]
    ax.plot(lim, lim, "k--", lw=0.8, label="lifetime = time between changes")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Declared lifetime (hours, log)")
    ax.set_ylabel("Mean hours between real changes (log)")
    ax.legend(fontsize=7, frameon=False)
    ax.set_title("Below the line: content changes before the declared lifetime ends", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=200)
    plt.close(fig)


# Main
def run_analysis(cfg, con, out_dir: str | None = None):
    out_dir = out_dir or cfg.results_dir
    fig_dir = os.path.join(out_dir, "figures")
    os.makedirs(fig_dir, exist_ok=True)
    pairs = load_pairs(con)
    fetches = load_fetches(con, "main")
    if pairs.empty:
        raise SystemExit("No pairs yet. Run 'python run.py pairs' after at least two rounds.")
    files = {}
    files["rq1_rates.csv"] = rq1_rates(pairs, cfg)
    files["rq1_presence.csv"] = rq1_presence(fetches, cfg)
    files["rq2_meaningful.csv"] = rq2_meaningful(pairs, cfg)
    cats = pairs[pairs.fkind == "main"].groupby(["rtype", "category"]).size().unstack(fill_value=0)
    files["categories.csv"] = cats.reset_index()
    files["conditional_agreement.csv"] = conditional_agreement(con)
    files["cdn_back_to_back.csv"] = cdn_back_to_back(con)
    files["hourly_missed.csv"] = hourly_missed(pairs)
    files["exclusions.csv"] = exclusions(con)
    for name, df in files.items():
        df.to_csv(os.path.join(out_dir, name), index=False)
    fig_categories(pairs, os.path.join(fig_dir, "categories.png"))
    fig_lifetime_vs_change(pairs, os.path.join(fig_dir, "lifetime_vs_change.png"))
    write_summary(cfg, con, pairs, files, os.path.join(out_dir, "summary.txt"))
    log.info("analysis written to %s", out_dir)


def write_summary(cfg, con, pairs, files, path):
    n_sites = con.execute("SELECT COUNT(*) FROM site WHERE status='active'").fetchone()[0]
    n_res = con.execute("SELECT COUNT(*) FROM resource").fetchone()[0]
    n_fetch = con.execute("SELECT COUNT(*) FROM fetch").fetchone()[0]
    rounds = con.execute("SELECT COUNT(*), MIN(started_at), MAX(started_at) FROM round WHERE kind='main'").fetchone()
    days = (rounds[2] - rounds[1]) / 86400 if rounds[0] else 0
    lines = [f"Sites: {n_sites}   Resources: {n_res}   Fetches: {n_fetch}",
             f"Main rounds: {rounds[0]} over {days:.1f} days", "",
             "RQ1 - error rates (all resources, 95% site-level bootstrap interval):"]
    r = files["rq1_rates.csv"]
    for _, row in r[r.group_by == "all"].iterrows():
        lines.append(f"  {row.rate:30s} {row.value:7.2%}  [{row.ci_low:.2%}, {row.ci_high:.2%}]  "
                     f"({row.events}/{row.pairs} pairs) - {row.description}")
    p = files["rq1_presence.csv"]
    lines.append("")
    lines.append("RQ1 - signal presence (all fetches):")
    for _, row in p[p.group_by == "all"].iterrows():
        lines.append(f"  {row.measure:30s} {row.value:7.2%}  [{row.ci_low:.2%}, {row.ci_high:.2%}]")
    q = files["rq2_meaningful.csv"]
    lines.append("")
    lines.append("RQ2 - share of HTML byte changes that are meaningful:")
    for _, row in q[q.group_by == "all"].iterrows():
        lines.append(f"  theta={row.theta:.2f}: {row.share_meaningful:7.2%}  [{row.ci_low:.2%}, {row.ci_high:.2%}]  "
                     f"({row.meaningful}/{row.byte_changes})")
    lines.append("")
    lines.append("Pair categories (main rounds):")
    for c, n in pairs[pairs.fkind == "main"].category.value_counts().items():
        lines.append(f"  {c:28s} {n}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
