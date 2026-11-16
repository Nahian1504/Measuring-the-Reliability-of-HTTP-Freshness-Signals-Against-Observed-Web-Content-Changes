from __future__ import annotations
import csv
import difflib
import gzip
import logging
import os
import random
import time
import zlib
from datetime import datetime

log = logging.getLogger("freshness")


class RunLock:
    def __init__(self, path: str, stale_h: float = 3.5):
        self.path, self.stale_h = path, stale_h
        self.acquired = False

    def __enter__(self):
        if os.path.exists(self.path):
            age_h = (time.time() - os.path.getmtime(self.path)) / 3600
            if age_h < self.stale_h:
                return self
            os.remove(self.path)
        with open(self.path, "w") as f:
            f.write(f"{os.getpid()} {datetime.now().isoformat()}\n")
        self.acquired = True
        return self

    def __exit__(self, *exc):
        if self.acquired and os.path.exists(self.path):
            os.remove(self.path)


# Status
def fmt(ts):
    return datetime.fromtimestamp(ts).strftime("%a %b %d %H:%M") if ts else "-"


def status(cfg, con):
    size_mb = os.path.getsize(cfg.db_path) / 1e6 if os.path.exists(cfg.db_path) else 0
    print(f"Database: {cfg.db_path} ({size_mb:.0f} MB)")
    s = dict(con.execute("SELECT status, COUNT(*) FROM site GROUP BY status").fetchall())
    r_all = con.execute("SELECT COUNT(*), SUM(active) FROM resource").fetchone()
    print(f"Sites: {s.get('active', 0)} active, {s.get('excluded', 0)} excluded   "
          f"Resources: {r_all[0]} ({r_all[1] or 0} active)")
    rounds = con.execute("SELECT round_id, started_at, finished_at, n_ok, n_err FROM round WHERE kind='main' ORDER BY started_at").fetchall()
    if not rounds:
        print("No main rounds yet. Run: python run.py collect")
        return
    first, last = rounds[0], rounds[-1]
    days = (last["started_at"] - first["started_at"]) / 86400
    print(f"Main rounds: {len(rounds)}  first {fmt(first['started_at'])}  last {fmt(last['started_at'])}  "
          f"({days:.1f} days collected)")
    if last["finished_at"]:
        mins = (last["finished_at"] - last["started_at"]) / 60
        tot = (last["n_ok"] or 0) + (last["n_err"] or 0)
        rate = (last["n_ok"] or 0) / tot if tot else 0
        print(f"Last round: took {mins:.1f} min, {last['n_ok']} ok, {last['n_err']} errors ({rate:.1%} ok)")
    else:
        print("Last round: still running (or it crashed - check logs/collect.log)")
    day_ago = time.time() - 86400
    n24 = sum(1 for r in rounds if r["started_at"] >= day_ago)
    h24 = con.execute("SELECT COUNT(*) FROM round WHERE kind='hourly' AND started_at>=?", (day_ago,)).fetchone()[0]
    print(f"Last 24 h: {n24} main rounds (expected 6), {h24} hourly rounds (expected 24)")
    interval = cfg.round_interval_h * 3600
    missed = []
    for a, b in zip(rounds, rounds[1:]):
        gap = b["started_at"] - a["started_at"]
        if gap > 1.5 * interval:
            missed.append((a["started_at"], b["started_at"], round(gap / interval) - 1))
    if missed:
        print(f"Gaps (about {sum(m[2] for m in missed)} missed rounds in total):")
        for a, b, n in missed[-10:]:
            print(f"   {fmt(a)} -> {fmt(b)}  (~{n} missed)")
    else:
        print("No missed rounds.")
    since = time.time() - last["started_at"]
    if since > 1.25 * interval:
        print(f"WARNING: the last round started {since / 3600:.1f} h ago. Is the laptop asleep or the task off?")
    errs = con.execute("""SELECT COALESCE(error,'ok') AS e, COUNT(*) AS n FROM fetch WHERE round_id=? GROUP BY e ORDER BY n DESC LIMIT 6""", (last["round_id"],)).fetchall()
    print("Last round results:", ", ".join(f"{e['e']}={e['n']}" for e in errs))


# Spot check
def spotcheck_export(cfg, con, path: str, n: int = 50, seed: int = 42):
    rows = con.execute("""SELECT p.pair_id, p.similarity, p.M, r.url, a.text_sha256 ta, b.text_sha256 tb
                          FROM fetch_pair p JOIN fetch a ON a.fetch_id=p.prev_fetch
                          JOIN fetch b ON b.fetch_id=p.next_fetch JOIN resource r ON r.resource_id=p.resource_id
                          WHERE a.text_sha256 IS NOT NULL AND b.text_sha256 IS NOT NULL AND a.text_sha256 != b.text_sha256""").fetchall()
    rng = random.Random(seed)
    pick = rng.sample(list(rows), min(n, len(rows)))

    def text(h):
        row = con.execute("SELECT main_text FROM page_text WHERE text_sha256=?", (h,)).fetchone()
        return zlib.decompress(row[0]).decode("utf-8", "replace") if row and row[0] else ""

    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair_id", "url", "similarity", "detector_says_meaningful", "what_changed", "my_label (1 = real change, 0 = noise)"])
        for p in pick:
            a, b = text(p["ta"]).splitlines(), text(p["tb"]).splitlines()
            diff = [l for l in difflib.unified_diff(a, b, lineterm="", n=0) if l[:1] in "+-" and l[:3] not in ("+++", "---")]
            w.writerow([p["pair_id"], p["url"], round(p["similarity"] or 0, 3), p["M"], "\n".join(diff)[:1500], ""])
    print(f"Wrote {len(pick)} pairs to {path}. Open it in Excel, fill the last column, save as CSV.")


def spotcheck_score(path: str):
    agree = total = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            label = (row.get("my_label (1 = real change, 0 = noise)") or "").strip()
            if label not in ("0", "1"):
                continue
            total += 1
            agree += int(label == str(row["detector_says_meaningful"]).strip())
    if total == 0:
        print("No labels found. Fill the last column with 1 or 0 first.")
        return
    print(f"Detector agrees with you on {agree}/{total} pairs ({agree / total:.0%}).")


def export(cfg, con, out_dir: str):
    os.makedirs(out_dir, exist_ok=True)
    queries = {
        "sites.csv.gz": "SELECT site_id, domain, tranco_rank, rank_group, status, exclude_reason FROM site",
        "resources.csv.gz": "SELECT resource_id, site_id, url, type, fingerprinted, cond_sample, b2b_sample, "
                            "hourly_sample, active, inactive_reason FROM resource",
        "rounds.csv.gz": "SELECT * FROM round",
        "fetches.csv.gz": "SELECT fetch_id, resource_id, round_id, kind, fetched_at, status, error, n_redirects, "
                          "etag, etag_weak, last_modified, date_hdr, cache_control, expires, lifetime_s, "
                          "lifetime_src, lm_uninformative, content_type, content_encoding, size, body_sha256, "
                          "text_sha256, cdn_headers, bot_challenge, cond_status FROM fetch",
        "pairs.csv.gz": "SELECT * FROM fetch_pair",
    }
    for name, q in queries.items():
        cur = con.execute(q)
        with gzip.open(os.path.join(out_dir, name), "wt", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([d[0] for d in cur.description])
            for row in cur:
                w.writerow(list(row))
        print("wrote", os.path.join(out_dir, name))
