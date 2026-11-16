from __future__ import annotations
import asyncio
import csv
import logging
import random
import re
import time
from urllib.parse import parse_qs, urldefrag, urlsplit
from .fetcher import fetch_robots, http_get, is_bot_challenge, make_session

log = logging.getLogger("freshness")

SESSION_KEYS = re.compile(r"(^|_)(sid|session|sessionid|phpsessid|jsessionid|token|auth|sig|signature|"
                          r"utm_[a-z]+|fbclid|gclid|nonce|timestamp|ts|cb|_)$", re.I)
SKIP_PATH = re.compile(r"/(login|log-in|signin|sign-in|signup|sign-up|register|account|my-account|cart|basket|"
                       r"checkout|search|privacy|terms|cookie|cookies|contact|about|subscribe|logout|password|"
                       r"help|support|careers|jobs|tag|tags|category|categories|author|authors|feed|rss|"
                       r"cdn-cgi|wp-admin|wp-login)(/|$|\.)", re.I)
STATIC_EXT = re.compile(r"\.(pdf|zip|gz|rar|7z|exe|dmg|mp4|mp3|mov|avi|jpg|jpeg|png|gif|webp|svg|ico|css|js|"
                        r"woff2?|ttf|xml|json|txt)$", re.I)
HEX_HASH = re.compile(r"[.\-_~@]([0-9a-f]{8,})(?=[.\-_~]|$)", re.I)
VITE_HASH = re.compile(r"[.\-_]([A-Za-z0-9_]{8,12})\.(?:m?js|css)$")
VERSION_PARAMS = {"v", "ver", "version", "hash", "h", "rev", "build"}


def read_tranco(path: str) -> list[tuple[int, str]]:
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for rec in csv.reader(f):
            if len(rec) < 2 or not rec[0].strip().isdigit():
                continue
            rows.append((int(rec[0]), rec[1].strip().lower()))
    return rows


def base_domain(domain: str) -> str:
    return domain[4:] if domain.startswith("www.") else domain


def same_site(url: str, base: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == base or host.endswith("." + base)


def is_session_like(url: str) -> bool:
    q = urlsplit(url).query
    if ";jsessionid" in url.lower():
        return True
    return any(SESSION_KEYS.search(k) for k in parse_qs(q, keep_blank_values=True))


def is_fingerprinted(url: str) -> bool:
    parts = urlsplit(url)
    last = parts.path.rsplit("/", 1)[-1]
    if HEX_HASH.search(last):
        return True
    m = VITE_HASH.search(last)
    if m and re.search(r"\d", m.group(1)) and re.search(r"[A-Za-z]", m.group(1)):
        return True
    q = parse_qs(parts.query)
    return any(k.lower() in VERSION_PARAMS and v and v[0] for k, v in q.items())


def _article_score(url: str) -> int:
    path = urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    last = segs[-1] if segs else ""
    score = 0
    if len(segs) >= 2:
        score += 1
    if "-" in last and len(last) >= 12:
        score += 2
    if re.search(r"\d", path):
        score += 1
    return score


def discover_resources(html: bytes, page_url: str, base: str, max_n: int = 8) -> list[tuple[str, str]]:
    import lxml.html
    found: list[tuple[str, str]] = [(page_url, "homepage")]
    seen = {page_url}
    try:
        doc = lxml.html.fromstring(html)
        doc.make_links_absolute(page_url, resolve_base_href=True)
    except Exception:
        return found

    def ok(u: str) -> bool:
        u = urldefrag(u)[0]
        return (u.startswith("https://") and same_site(u, base) and u not in seen and not is_session_like(u) and len(u) < 500)

    def take(urls, kind, limit):
        n = 0
        for u in urls:
            u = urldefrag(u.strip())[0]
            if n >= limit or len(found) >= max_n:
                break
            if ok(u):
                found.append((u, kind))
                seen.add(u)
                n += 1

    links = []
    for u in doc.xpath("//a/@href"):
        u = urldefrag(u.strip())[0]
        path = urlsplit(u).path
        if path in ("", "/") or SKIP_PATH.search(path) or STATIC_EXT.search(path):
            continue
        links.append(u)
    links = sorted(dict.fromkeys(links), key=lambda u: -_article_score(u))
    take([u for u in links if _article_score(u) >= 2], "article", 2)
    take(doc.xpath("//script/@src"), "script", 2)
    take(doc.xpath("//link[contains(concat(' ', normalize-space(@rel), ' '), ' stylesheet ')]/@href"), "stylesheet", 1)
    imgs = [u for u in doc.xpath("//img/@src") if not u.startswith("data:")]
    take(imgs, "image", 1)
    take(doc.xpath("//link[@rel='alternate'][contains(@type,'rss') or contains(@type,'atom')]/@href"), "feed", 1)
    return found[:max_n]


async def check_candidate(session, cfg, rank: int, domain: str) -> dict:
    base = base_domain(domain)
    out = {"domain": domain, "rank": rank, "status": "excluded", "reason": None,
           "homepage": None, "robots_txt": None, "resources": []}
    if domain in set(d.lower() for d in (cfg.blocked_domains or [])):
        out["reason"] = "blocked"
        return out
    r = None
    for start in (f"https://{domain}/", f"https://www.{base}/"):
        r = await http_get(session, start, cfg)
        if r["status"] == 200 and r["error"] is None:
            break
        await asyncio.sleep(float(cfg.per_domain_gap_s))
    if r["error"] or r["status"] != 200:
        out["reason"] = r["error"] or f"http_{r['status']}"
        return out
    final = r["final_url"]
    if not final.startswith("https://"):
        out["reason"] = "not_https"
        return out
    if not same_site(final, base):
        out["reason"] = "offsite_redirect"
        return out
    if "html" not in (r["headers"].get("content-type") or "").lower():
        out["reason"] = "not_html"
        return out
    if is_bot_challenge(r["status"], r["headers"], r["body"]):
        out["reason"] = "bot_challenge"
        return out
    await asyncio.sleep(float(cfg.per_domain_gap_s))
    rp, text, _ = await fetch_robots(session, final, cfg)
    out["robots_txt"] = text
    if not rp.can_fetch(cfg.robots_token, final):
        out["reason"] = "robots_disallow"
        return out
    res = discover_resources(r["body"], final, base, int(cfg.max_resources_per_site))
    out["resources"] = [(u, t) for (u, t) in res if rp.can_fetch(cfg.robots_token, u)]
    out["homepage"] = final
    out["status"] = "active"
    return out


async def _sample(cfg, con, n_sites: int):
    rows = read_tranco(cfg.tranco_csv)
    if not rows:
        raise SystemExit(f"No rows found in {cfg.tranco_csv}. Did you download the Tranco list?")
    rng = random.Random(int(cfg.seed))
    groups = {}
    for name, (lo, hi) in cfg.rank_groups.items():
        cand = [(rank, d) for rank, d in rows if lo <= rank <= hi]
        rng.shuffle(cand)
        groups[name] = cand
    target = {name: n_sites // len(groups) for name in groups}
    first = list(groups)[0]
    target[first] += n_sites - sum(target.values())

    have = {name: 0 for name in groups}
    tried = set()
    for row in con.execute("SELECT domain, rank_group, status FROM site"):
        tried.add(row["domain"])
        if row["status"] == "active" and row["rank_group"] in have:
            have[row["rank_group"]] += 1

    lock = asyncio.Lock()
    async with make_session(cfg) as session:
        for name, cand in groups.items():
            queue = [c for c in cand if c[1] not in tried]
            log.info("group %s: need %d more sites (have %d), %d candidates",
                     name, max(0, target[name] - have[name]), have[name], len(queue))
            it = iter(queue)

            async def worker():
                while True:
                    async with lock:
                        if have[name] >= target[name]:
                            return
                        nxt = next(it, None)
                    if nxt is None:
                        return
                    rank, domain = nxt
                    res = await check_candidate(session, cfg, rank, domain)
                    async with lock:
                        status = res["status"]
                        if status == "active" and have[name] >= target[name]:
                            return  # group already full, do not store
                        cur = con.execute(
                            "INSERT OR IGNORE INTO site (domain, tranco_rank, rank_group, robots_ok, robots_txt, "
                            "homepage_url, status, exclude_reason, checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (domain, rank, name, int(status == "active"), res["robots_txt"], res["homepage"],
                             status, res["reason"], time.time()))
                        if status == "active":
                            site_id = cur.lastrowid
                            for url, kind in res["resources"]:
                                con.execute("INSERT OR IGNORE INTO resource (site_id, url, type, fingerprinted) "
                                            "VALUES (?,?,?,?)", (site_id, url, kind, int(is_fingerprinted(url))))
                            have[name] += 1
                            if have[name] % 50 == 0:
                                log.info("group %s: %d/%d sites", name, have[name], target[name])
                        con.commit()

            await asyncio.gather(*[worker() for _ in range(int(cfg.sampling_concurrency))])
            log.info("group %s finished with %d active sites", name, have[name])


def assign_subsamples(cfg, con):
    ids = [r[0] for r in con.execute(
        "SELECT resource_id FROM resource r JOIN site s USING(site_id) WHERE s.status='active' ORDER BY resource_id")]
    rng = random.Random(int(cfg.seed) + 1)
    con.execute("UPDATE resource SET cond_sample=0, b2b_sample=0, hourly_sample=0")
    n = len(ids)
    for col, k in (("cond_sample", round(n * cfg.conditional_share)),
                   ("b2b_sample", round(n * cfg.back_to_back_share)),
                   ("hourly_sample", min(int(cfg.hourly_count), n))):
        for rid in rng.sample(ids, k):
            con.execute(f"UPDATE resource SET {col}=1 WHERE resource_id=?", (rid,))
    con.commit()
    log.info("subsamples: %d resources, %d conditional, %d back-to-back, %d hourly", n,
             round(n * cfg.conditional_share), round(n * cfg.back_to_back_share), min(int(cfg.hourly_count), n))


def sample_sites(cfg, con, n_sites: int | None = None):
    if con.execute("SELECT COUNT(*) FROM fetch").fetchone()[0] > 0:
        log.warning("This database already has fetches. New sites will be added, and the "
                    "5%%/hourly subsamples will be re-drawn.")
    asyncio.run(_sample(cfg, con, int(n_sites or cfg.n_sites)))
    assign_subsamples(cfg, con)
