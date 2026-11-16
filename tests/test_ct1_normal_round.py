import asyncio
import hashlib
from conftest import add_site
from freshness import testserver
from freshness.fetcher import RoundRunner, accept_encoding

EXPECTED_SHA = hashlib.sha256(testserver.APP_JS).hexdigest()


def test_ct1_normal_round(cfg, con):
    paths = ["/static/app.js", "/gzip/app.js", "/redirect"]
    if "br" in accept_encoding():
        paths.append("/br/app.js")

    async def go():
        runner, base, app = await testserver.start()
        try:
            ids = add_site(con, base, paths)
            await RoundRunner(cfg, con, "main", use_processes=False).run()
            return ids
        finally:
            await runner.cleanup()

    ids = asyncio.run(go())
    rows = {r["resource_id"]: dict(r) for r in con.execute("SELECT * FROM fetch")}
    assert len(rows) == len(paths), "exactly one row per resource"
    for p, rid in ids.items():
        r = rows[rid]
        assert r["status"] == 200 and r["error"] is None, p
        assert r["etag"] == '"v1"' and r["etag_weak"] == 0, p
        assert r["cache_control"] == "max-age=600", p
        assert r["lifetime_s"] == 600 and r["lifetime_src"] == "max-age", p
        assert r["last_modified"] == "Mon, 02 Nov 2026 10:00:00 GMT", p
        assert r["body_sha256"] == EXPECTED_SHA, f"{p}: body must be decompressed before hashing"
        assert r["size"] == len(testserver.APP_JS), p
    assert rows[ids["/redirect"]]["n_redirects"] == 1
    assert rows[ids["/gzip/app.js"]]["content_encoding"] == "gzip"
    rnd = con.execute("SELECT * FROM round").fetchone()
    assert rnd["n_ok"] == len(paths) and rnd["n_err"] == 0 and rnd["finished_at"]


def test_conditional_request_and_b2b(cfg, con):
    async def go():
        runner, base, app = await testserver.start()
        try:
            ids = add_site(con, base, ["/static/app.js"], cond=1, b2b=1)
            await RoundRunner(cfg, con, "main", use_processes=False).run()
            await RoundRunner(cfg, con, "main", use_processes=False).run()
            return ids, app
        finally:
            await runner.cleanup()

    ids, app = asyncio.run(go())
    main = con.execute("SELECT * FROM fetch WHERE kind='main' ORDER BY fetch_id").fetchall()
    b2b = con.execute("SELECT * FROM fetch WHERE kind='b2b'").fetchall()
    assert len(main) == 2 and len(b2b) == 2
    assert main[0]["cond_status"] is None          
    assert main[1]["cond_status"] == 304         
    sent = [h for (path, t, h) in app["log"] if path == "/static/app.js" and "If-None-Match" in h]
    assert sent and sent[0]["If-None-Match"] == '"v1"'
