from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
import aiohttp
from .detector import analyze_html
from .parser import freshness_lifetime, lm_uninformative, parse_etag

log = logging.getLogger("freshness")

RETRY_STATUS = {429, 500, 502, 503, 504}
CDN_HEADERS = [
    "server", "via", "age", "vary", "x-cache", "x-cache-hits", "x-cache-status",
    "cf-cache-status", "cf-ray", "x-served-by", "x-amz-cf-pop", "x-amz-cf-id",
    "akamai-cache-status", "x-akamai-transformed", "x-varnish", "x-fastly-request-id",
    "x-vercel-cache", "x-nf-request-id", "cdn-cache-control", "surrogate-control",
]
CHALLENGE_MARKERS = [
    b"just a moment", b"attention required", b"captcha", b"verify you are human",
    b"are you a robot", b"enable javascript and cookies", b"access denied",
    b"checking your browser", b"ddos protection", b"bot detection",
]


def accept_encoding() -> str:
    try:
        import brotli  
        return "gzip, deflate, br"
    except ImportError:
        try:
            import brotlicffi 
            return "gzip, deflate, br"
        except ImportError:
            return "gzip, deflate"


def make_session(cfg) -> aiohttp.ClientSession:
    headers = {
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8",
        "Accept-Encoding": accept_encoding(),
    }
    connector = aiohttp.TCPConnector(limit=cfg.max_concurrency, limit_per_host=1, ttl_dns_cache=600, force_close=False)
    return aiohttp.ClientSession(headers=headers, connector=connector, cookie_jar=aiohttp.DummyCookieJar())


def is_bot_challenge(status: int | None, headers: dict, body: bytes | None) -> bool:
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    if body is None:
        return False
    sample = body[:20000].lower()
    if status in (403, 429, 503) and any(m in sample for m in CHALLENGE_MARKERS):
        return True
    if status == 200 and len(body) < 15000 and (b"captcha" in sample or b"just a moment" in sample):
        return True
    return False


def _headers_to_dict(resp_headers) -> dict:
    out: dict[str, str] = {}
    for key, value in resp_headers.items():
        key = key.lower()
        if key not in out:
            out[key] = value
        elif key in ("cache-control", "vary", "via"):
            out[key] = out[key] + ", " + value
    return out


async def http_get(session: aiohttp.ClientSession, url: str, cfg, extra_headers: dict | None = None, read_body: bool = True, gap_waiter=None) -> dict:
    attempts_allowed = 1 + int(cfg.retries)
    timeout = aiohttp.ClientTimeout(total=cfg.timeout_s)
    max_bytes = int(cfg.max_body_mb * 1024 * 1024)
    result: dict = {}
    for attempt in range(1, attempts_allowed + 1):
        if attempt > 1 and gap_waiter is not None:
            await gap_waiter()
        start = time.monotonic()
        result = {"status": None, "error": None, "final_url": url, "n_redirects": 0, "headers": {}, "body": None, "attempts": attempt, "fetched_at": time.time()}
        try:
            async with session.get(url, headers=extra_headers or {}, allow_redirects=True, max_redirects=cfg.max_redirects, timeout=timeout) as resp:
                result["status"] = resp.status
                result["final_url"] = str(resp.url)
                result["n_redirects"] = len(resp.history)
                result["headers"] = _headers_to_dict(resp.headers)
                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    result["error"] = "too_large"
                elif read_body:
                    chunks, total = [], 0
                    async for chunk in resp.content.iter_chunked(65536):
                        total += len(chunk)
                        if total > max_bytes:
                            result["error"] = "too_large"
                            break
                        chunks.append(chunk)
                    if result["error"] is None:
                        result["body"] = b"".join(chunks)
                else:
                    await resp.read()
        except asyncio.TimeoutError:
            result["error"] = "timeout"
        except aiohttp.TooManyRedirects:
            result["error"] = "too_many_redirects"
        except aiohttp.ClientError as e:
            result["error"] = f"client_error:{type(e).__name__}"
        except Exception as e:  
            result["error"] = f"error:{type(e).__name__}"
        result["elapsed_ms"] = int((time.monotonic() - start) * 1000)
        retryable = result["error"] in ("timeout",) or (result["error"] or "").startswith("client_error") \
            or (result["status"] in RETRY_STATUS)
        if not retryable or result["error"] == "too_large":
            break
    if result.get("error") is None and result.get("status") is not None and result["status"] >= 400:
        result["error"] = f"http_{result['status']}"
    return result


def parse_robots(text: str | None, status: int | None) -> RobotFileParser:
    rp = RobotFileParser()
    if status is not None and 200 <= status < 300 and text is not None:
        rp.parse(text.splitlines())
    elif status is not None and 400 <= status < 500:
        rp.parse([])
    else:
        rp.parse(["User-agent: *", "Disallow: /"])
    return rp


async def fetch_robots(session, base_url: str, cfg) -> tuple[RobotFileParser, str | None, int | None]:
    parts = urlsplit(base_url)
    url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    r = await http_get(session, url, cfg)
    status = r["status"] if r["error"] in (None,) or (r["error"] or "").startswith("http_") else None
    text = r["body"].decode("utf-8", errors="replace") if r["body"] else None
    return parse_robots(text, status), text, status


class DomainGap:
    def __init__(self, gap: float):
        self.gap = gap
        self.last = 0.0

    async def wait(self):
        delay = self.last + self.gap - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        self.last = time.monotonic()


def row_from_result(r: dict, resource_id: int, round_id: int, kind: str) -> dict:
    h = r["headers"]
    etag = parse_etag(h.get("etag"))
    lifetime, src = freshness_lifetime(h, shared=True)
    body = r["body"]
    return {
        "resource_id": resource_id, "round_id": round_id, "kind": kind,
        "fetched_at": r["fetched_at"], "status": r["status"], "error": r["error"],
        "final_url": r["final_url"], "n_redirects": r["n_redirects"], "attempts": r["attempts"],
        "etag": etag["etag"], "etag_weak": None if etag["weak"] is None else int(etag["weak"]),
        "last_modified": h.get("last-modified"), "date_hdr": h.get("date"),
        "cache_control": h.get("cache-control"), "expires": h.get("expires"),
        "lifetime_s": lifetime, "lifetime_src": src,
        "lm_uninformative": _int(lm_uninformative(h.get("last-modified"), h.get("date"))),
        "content_type": h.get("content-type"), "content_encoding": h.get("content-encoding"),
        "size": len(body) if body is not None else None,
        "body_sha256": hashlib.sha256(body).hexdigest() if body is not None else None,
        "text_sha256": None,
        "cdn_headers": json.dumps({k: h[k] for k in CDN_HEADERS if k in h}) or None,
        "bot_challenge": int(is_bot_challenge(r["status"], h, body)),
        "cond_status": None, "cond_error": None, "elapsed_ms": r.get("elapsed_ms"),
    }


def _int(x):
    return None if x is None else int(x)


FETCH_COLS = ["resource_id", "round_id", "kind", "fetched_at", "status", "error", "final_url",
              "n_redirects", "attempts", "etag", "etag_weak", "last_modified", "date_hdr",
              "cache_control", "expires", "lifetime_s", "lifetime_src", "lm_uninformative",
              "content_type", "content_encoding", "size", "body_sha256", "text_sha256",
              "cdn_headers", "bot_challenge", "cond_status", "cond_error", "elapsed_ms"]


class RoundRunner:
    def __init__(self, cfg, con, kind: str = "main", use_processes: bool = True, force_robots: bool = False):
        self.cfg, self.con, self.kind = cfg, con, kind
        self.force_robots = force_robots
        self.use_processes = use_processes
        self.pool = None
        self.n_ok = self.n_err = 0

    # Helpers
    def _load_resources(self):
        blocked = set(d.lower() for d in (self.cfg.blocked_domains or []))
        q = """SELECT r.*, s.domain FROM resource r JOIN site s USING(site_id) WHERE r.active=1 AND s.status='active'"""
        if self.kind == "hourly":
            q += " AND r.hourly_sample=1"
        rows = [dict(x) for x in self.con.execute(q)]
        by_site = defaultdict(list)
        for row in rows:
            if row["domain"].lower() in blocked:
                continue
            by_site[(row["site_id"], row["domain"])].append(row)
        return by_site

    def _previous_validators(self) -> dict:
        q = """SELECT resource_id, etag, last_modified FROM fetch WHERE fetch_id IN (
                 SELECT MAX(fetch_id) FROM fetch WHERE kind='main' AND status=200 GROUP BY resource_id)"""
        return {r["resource_id"]: (r["etag"], r["last_modified"]) for r in self.con.execute(q)}

    def _robots_due(self) -> bool:
        if self.kind != "main":
            return False
        n = self.con.execute("SELECT COUNT(*) FROM round WHERE kind='main'").fetchone()[0]
        every = int(self.cfg.robots_recheck_every_rounds)
        return every > 0 and n > 1 and (n - 1) % every == 0

    async def _analyze(self, body: bytes) -> dict:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.pool, analyze_html, body, int(self.cfg.shingle_words), int(self.cfg.minhash_perm))

    async def _store(self, row: dict, body: bytes | None):
        ctype = (row["content_type"] or "").lower()
        if body is not None and row["status"] == 200 and "html" in ctype and not row["bot_challenge"]:
            try:
                info = await self._analyze(body)
                row["text_sha256"] = info["text_sha256"]
                self.con.execute("INSERT OR IGNORE INTO page_text VALUES (?,?,?,?)", (info["text_sha256"], info["main_text_z"], info["minhash"], info["n_words"]))
            except Exception as e:
                log.warning("text extraction failed for resource %s: %s", row["resource_id"], e)
        cols = ",".join(FETCH_COLS)
        marks = ",".join("?" for _ in FETCH_COLS)
        self.con.execute(f"INSERT OR IGNORE INTO fetch ({cols}) VALUES ({marks})", [row[c] for c in FETCH_COLS])
        if row["error"] is None and row["status"] == 200:
            self.n_ok += 1
        else:
            self.n_err += 1
        if row["error"] == "too_large":
            self.con.execute("UPDATE resource SET active=0, inactive_reason='too_large' WHERE resource_id=?", (row["resource_id"],))

    # One site
    async def _do_site(self, session, sem, site_key, resources, round_id, prev, check_robots):
        site_id, domain = site_key
        async with sem:
            gap = DomainGap(float(self.cfg.per_domain_gap_s))
            if check_robots:
                await gap.wait()
                rp, text, status = await fetch_robots(session, resources[0]["url"], self.cfg)
                self.con.execute("UPDATE site SET robots_txt=? WHERE site_id=?", (text, site_id))
                keep = []
                for res in resources:
                    if rp.can_fetch(self.cfg.robots_token, res["url"]):
                        keep.append(res)
                    else:
                        self.con.execute("UPDATE resource SET active=0, inactive_reason='robots' WHERE resource_id=?", (res["resource_id"],))
                resources = keep
            for res in resources:
                await gap.wait()
                kind = "hourly" if self.kind == "hourly" else "main"
                r = await http_get(session, res["url"], self.cfg, gap_waiter=gap.wait)
                row = row_from_result(r, res["resource_id"], round_id, kind)

                # 5% sample: conditional request with the validators from the last round
                if kind == "main" and res["cond_sample"] and res["resource_id"] in prev:
                    etag, lm = prev[res["resource_id"]]
                    cond_headers = {}
                    if etag:
                        cond_headers["If-None-Match"] = etag
                    if lm:
                        cond_headers["If-Modified-Since"] = lm
                    if cond_headers:
                        await gap.wait()
                        c = await http_get(session, res["url"], self.cfg, extra_headers=cond_headers, read_body=False, gap_waiter=gap.wait)
                        row["cond_status"] = c["status"]
                        row["cond_error"] = c["error"] if c["status"] not in (200, 304) else None
                await self._store(row, r["body"])

                # 5% sample: second fetch straight away (CDN check)
                if kind == "main" and res["b2b_sample"]:
                    await gap.wait()
                    r2 = await http_get(session, res["url"], self.cfg, gap_waiter=gap.wait)
                    await self._store(row_from_result(r2, res["resource_id"], round_id, "b2b"), r2["body"])
            self.con.commit()


    async def run(self) -> int:
        by_site = self._load_resources()
        prev = self._previous_validators() if self.kind == "main" else {}
        cur = self.con.execute("INSERT INTO round (kind, started_at) VALUES (?,?)", (self.kind, time.time()))
        round_id = cur.lastrowid
        self.con.commit()
        check_robots = self.force_robots or self._robots_due()
        n_res = sum(len(v) for v in by_site.values())
        log.info("round %s (%s): %d sites, %d resources%s", round_id, self.kind, len(by_site), n_res, ", re-checking robots.txt" if check_robots else "")
        if self.use_processes:
            workers = max(1, (os.cpu_count() or 2) - 1)
            self.pool = ProcessPoolExecutor(max_workers=workers)
        sem = asyncio.Semaphore(int(self.cfg.max_concurrency))
        try:
            async with make_session(self.cfg) as session:
                tasks = [self._do_site(session, sem, key, res, round_id, prev, check_robots) for key, res in by_site.items()]
                await asyncio.gather(*tasks)
        finally:
            if self.pool is not None:
                self.pool.shutdown(wait=True)
            self.con.execute("UPDATE round SET finished_at=?, n_ok=?, n_err=? WHERE round_id=?", (time.time(), self.n_ok, self.n_err, round_id))
            self.con.commit()
        log.info("round %s done: %d ok, %d errors", round_id, self.n_ok, self.n_err)
        return round_id


def run_round(cfg, con, kind: str = "main", use_processes: bool = True) -> int:
    return asyncio.run(RoundRunner(cfg, con, kind, use_processes).run())
