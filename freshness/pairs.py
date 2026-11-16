from __future__ import annotations
import logging
from .detector import similarity_from_bytes

log = logging.getLogger("freshness")

CATEGORIES = ["strong_etag_inconsistency", "missed_change", "allowed_weak_etag", "unnecessary_redownload", "noisy_last_modified", "consistent"]


def categorize(B: int, M: int, E: int | None, L: int | None, etag_present: bool, weak: bool | None) -> str:
    if etag_present and not weak and E == 0 and B == 1:
        return "strong_etag_inconsistency"
    validator_same = (etag_present and weak and E == 0) or (not etag_present and L == 0)
    if validator_same and M == 1:
        return "missed_change"
    if etag_present and weak and E == 0 and B == 1 and M == 0:
        return "allowed_weak_etag"
    if E == 1 and B == 0:
        return "unnecessary_redownload"
    if L == 1 and M == 0:
        return "noisy_last_modified"
    return "consistent"


def flags_for(prev: dict, nxt: dict, sim: float | None, theta: float) -> dict:
    B = int(prev["body_sha256"] != nxt["body_sha256"])
    is_html = "html" in (nxt.get("content_type") or "").lower()
    if is_html and prev.get("text_sha256") and nxt.get("text_sha256"):
        if prev["text_sha256"] == nxt["text_sha256"]:
            sim, M = 1.0, 0
        else:
            sim = 0.0 if sim is None else sim
            M = int(sim < theta)
    else:
        sim, M = None, B
    etag_present = bool(prev.get("etag")) and bool(nxt.get("etag"))
    E = int(prev["etag"] != nxt["etag"]) if etag_present else None
    weak = bool(prev.get("etag_weak")) if etag_present else None
    lm_present = bool(prev.get("last_modified")) and bool(nxt.get("last_modified"))
    L = int(prev["last_modified"] != nxt["last_modified"]) if lm_present else None
    gap = nxt["fetched_at"] - prev["fetched_at"]
    life = prev.get("lifetime_s")
    early = B if (life is not None and life > 0 and life >= gap) else None
    return {"gap_s": gap, "B": B, "M": M, "E": E, "L": L, "etag_present": int(etag_present),
            "etag_weak": None if weak is None else int(weak), "similarity": sim,
            "early_change": early, "category": categorize(B, M, E, L, etag_present, weak)}


def build_pairs(cfg, con, rebuild: bool = False) -> int:
    theta = float(cfg.theta)
    if rebuild:
        con.execute("DELETE FROM fetch_pair")
    done = {(r[0], r[1]) for r in con.execute("SELECT prev_fetch, next_fetch FROM fetch_pair")}
    minhash_cache: dict[str, bytes] = {}

    def mh(text_sha):
        if text_sha not in minhash_cache:
            row = con.execute("SELECT minhash FROM page_text WHERE text_sha256=?", (text_sha,)).fetchone()
            minhash_cache[text_sha] = row[0] if row else None
            if len(minhash_cache) > 50000:
                minhash_cache.clear()
        return minhash_cache[text_sha]

    sql = """SELECT fetch_id, resource_id, kind, fetched_at, body_sha256, text_sha256, etag, etag_weak,
                    last_modified, lifetime_s, content_type
             FROM fetch
             WHERE resource_id=? AND kind IN ('main','hourly') AND status=200 AND error IS NULL
               AND bot_challenge=0
             ORDER BY kind, fetched_at"""
    n_new = 0
    batch = []
    resource_ids = [r[0] for r in con.execute("SELECT resource_id FROM resource ORDER BY resource_id")]
    for rid in resource_ids:
        prev = None
        for row in con.execute(sql, (rid,)).fetchall():
            cur = dict(row)
            if prev and prev["kind"] == cur["kind"] and (prev["fetch_id"], cur["fetch_id"]) not in done:
                sim = None
                if prev["text_sha256"] and cur["text_sha256"] and prev["text_sha256"] != cur["text_sha256"]:
                    a, b = mh(prev["text_sha256"]), mh(cur["text_sha256"])
                    sim = similarity_from_bytes(a, b) if a and b else 0.0
                f = flags_for(prev, cur, sim, theta)
                batch.append((prev["fetch_id"], cur["fetch_id"], cur["resource_id"], f["gap_s"], f["B"], f["M"],
                              f["E"], f["L"], f["etag_present"], f["etag_weak"], f["similarity"],
                              f["early_change"], f["category"]))
                n_new += 1
            prev = cur
        if len(batch) >= 5000:
            _flush(con, batch)
    _flush(con, batch)
    log.info("pairs: %d new pairs", n_new)
    return n_new


def _flush(con, batch):
    if batch:
        con.executemany(
            "INSERT OR IGNORE INTO fetch_pair (prev_fetch, next_fetch, resource_id, gap_s, B, M, E, L, "
            "etag_present, etag_weak, similarity, early_change, category) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            batch)
        con.commit()
        batch.clear()
