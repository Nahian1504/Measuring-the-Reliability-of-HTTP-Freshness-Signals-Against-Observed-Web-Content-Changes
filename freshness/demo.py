from __future__ import annotations
import hashlib
import random
import zlib
from email.utils import formatdate

from .detector import minhash_bytes, normalize, text_hash

WORDS = ("cache server browser network content update page news story report data market city team "
         "player court policy study science health school weather energy music film travel food").split()


def _para(rng):
    return " ".join(rng.choice(WORDS) for _ in range(40)) + "."


def make_demo(cfg, con, n_sites: int = 40, days: int = 21, seed: int = 7):
    rng = random.Random(seed)
    import time
    t0 = time.time() - days * 86400 - 3600
    step = 4 * 3600
    n_rounds = days * 6
    text_cache = {}

    def store_text(main_text: str):
        clean = normalize(main_text)
        h = text_hash(clean)
        if h not in text_cache:
            con.execute("INSERT OR IGNORE INTO page_text VALUES (?,?,?,?)",
                        (h, zlib.compress(main_text.encode()), minhash_bytes(clean, cfg.shingle_words, cfg.minhash_perm), len(clean.split())))
            text_cache[h] = True
        return h

    resources = []
    for s in range(n_sites):
        grp = "top10k" if s % 2 == 0 else "10k-100k"
        cur = con.execute("INSERT INTO site (domain, tranco_rank, rank_group, robots_ok, homepage_url, status) "
                          "VALUES (?,?,?,?,?, 'active')", (f"demo{s}.example", s * 100 + 1, grp, 1, f"https://demo{s}.example/"))
        sid = cur.lastrowid
        for kind in ("homepage", "article", "article", "script", "script", "stylesheet", "image"):
            fp = int(kind in ("script", "stylesheet") and rng.random() < 0.5)
            url = f"https://demo{s}.example/{kind}{len(resources)}" + (".3f9a2c1b.js" if fp else "")
            hourly = int(kind == "homepage" and s < 8)
            cur = con.execute("INSERT INTO resource (site_id, url, type, fingerprinted, cond_sample, b2b_sample, "
                              "hourly_sample) VALUES (?,?,?,?,?,?,?)",
                              (sid, url, kind, fp, int(rng.random() < 0.1), int(rng.random() < 0.1), hourly))
            behavior = {
                "rid": cur.lastrowid, "kind": kind, "fp": fp, "hourly": hourly,
                "rate": 0 if fp else (rng.choice([1 / 4, 1 / 12, 1 / 48, 1 / 168]) if kind in ("homepage", "article") else rng.choice([0, 1 / 168, 1 / 336])),
                "etag": rng.choice(["strong", "weak", "weak_frozen", "none", "random"]),
                "lm": rng.choice(["correct", "now", "none"]),
                "max_age": rng.choice([0, 60, 300, 3600, 86400, 31536000 if fp else 600]),
                "paras": [_para(rng) for _ in range(10)],
            }
            resources.append(behavior)

    hourly_round = {}
    for k in range(n_rounds):
        t_round = t0 + k * step
        cur = con.execute("INSERT INTO round (kind, started_at, finished_at, n_ok, n_err) VALUES ('main',?,?,0,0)", (t_round, t_round + 600))
        rid_round = cur.lastrowid
        for h in range(1, 4):
            c2 = con.execute("INSERT INTO round (kind, started_at, finished_at) VALUES ('hourly',?,?)", (t_round + h * 3600, t_round + h * 3600 + 30))
            hourly_round[(k, h)] = c2.lastrowid
        for b in resources:
            plan = [("main", t_round, rid_round)]
            if b["hourly"]:
                plan += [("hourly", t_round + h * 3600, hourly_round[(k, h)]) for h in range(1, 4)]
            for kind_f, t, rnd in plan:
                _demo_fetch(con, b, t, rnd, kind_f, rng, store_text, cfg)
    con.execute("""UPDATE round SET
                     n_ok = (SELECT COUNT(*) FROM fetch f WHERE f.round_id=round.round_id AND f.status=200),
                     n_err = (SELECT COUNT(*) FROM fetch f WHERE f.round_id=round.round_id AND f.error IS NOT NULL)""")
    con.commit()
    print(f"Demo database ready: {n_sites} fake sites, {len(resources)} resources, {n_rounds} rounds.")


def _demo_fetch(con, b, t, rnd, kind, rng, store_text, cfg):
    if "last_t" not in b:
        b["last_t"], b["version"], b["changed_at"] = t, 0, t
    dt_h = (t - b["last_t"]) / 3600
    if b["rate"] and dt_h > 0 and rng.random() < 1 - pow(2.718281828, -b["rate"] * dt_h):
        b["version"] += 1
        b["changed_at"] = t - rng.uniform(0, dt_h * 3600)
        b["paras"].append(_para(rng))
        b["paras"] = b["paras"][-12:]
    b["last_t"] = t
    v = b["version"]
    fail = rng.random() < 0.02
    if fail:
        con.execute("INSERT OR IGNORE INTO fetch (resource_id, round_id, kind, fetched_at, status, error) "
                    "VALUES (?,?,?,?,?,?)", (b["rid"], rnd, kind, t, None, "timeout"))
        return
    is_html = b["kind"] in ("homepage", "article")
    if is_html:
        stamp = formatdate(t, usegmt=True)
        main_text = f"Updated {stamp}\n" + "\n".join(b["paras"])
        body = (main_text + str(t)).encode()  
        th = store_text(main_text)
        ctype = "text/html; charset=utf-8"
    else:
        body = f"{b['rid']}-{v}".encode() * 100
        th = None
        ctype = "application/javascript" if b["kind"] == "script" else "text/css" if b["kind"] == "stylesheet" else "image/png"
    sha = hashlib.sha256(body).hexdigest()
    et = b["etag"]
    etag = {"strong": f'"{sha[:12]}"', "weak": f'W/"v{v}"', "weak_frozen": 'W/"frozen"', "none": None, "random": f'"{rng.randrange(10 ** 6)}"'}[et]
    lm = {"correct": formatdate(b["changed_at"], usegmt=True), "now": formatdate(t, usegmt=True), "none": None}[b["lm"]]
    life = float(b["max_age"])
    cond = None
    if kind == "main" and con.execute("SELECT cond_sample FROM resource WHERE resource_id=?", (b["rid"],)).fetchone()[0]:
        cond = 304 if (etag and rng.random() < 0.7) else 200
    con.execute(
        "INSERT OR IGNORE INTO fetch (resource_id, round_id, kind, fetched_at, status, etag, etag_weak, "
        "last_modified, date_hdr, cache_control, lifetime_s, lifetime_src, lm_uninformative, content_type, size, "
        "body_sha256, text_sha256, bot_challenge, cond_status, attempts) "
        "VALUES (?,?,?,?,200,?,?,?,?,?,?,?,?,?,?,?,?,0,?,1)",
        (b["rid"], rnd, kind, t, etag, None if etag is None else int(etag.startswith("W/")), lm,
         formatdate(t, usegmt=True), f"max-age={b['max_age']}", life, "max-age",
         None if lm is None else int(b["lm"] == "now"), ctype, len(body), sha, th, cond))
    if kind == "main" and con.execute("SELECT b2b_sample FROM resource WHERE resource_id=?", (b["rid"],)).fetchone()[0]:
        etag2 = f'"{rng.randrange(10 ** 6)}"' if et == "random" else etag
        con.execute("INSERT OR IGNORE INTO fetch (resource_id, round_id, kind, fetched_at, status, etag, "
                    "last_modified, size, body_sha256, text_sha256, bot_challenge, content_type, attempts) "
                    "VALUES (?,?,?,?,200,?,?,?,?,?,0,?,1)",
                    (b["rid"], rnd, "b2b", t + 2, etag2, lm, len(body), sha, th, ctype))
