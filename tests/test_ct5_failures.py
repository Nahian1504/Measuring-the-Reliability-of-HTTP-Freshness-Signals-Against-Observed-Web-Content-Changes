import asyncio
from conftest import add_site
from freshness import testserver
from freshness.fetcher import RoundRunner


def test_ct5_failures(cfg, con):
    paths = ["/503", "/hang", "/drop", "/429", "/static/app.js"]

    async def go():
        runner, base, app = await testserver.start()
        try:
            ids = add_site(con, base, paths)
            await RoundRunner(cfg, con, "main", use_processes=False).run()
            return ids, app
        finally:
            await runner.cleanup()

    ids, app = asyncio.run(go())
    rows = {r["resource_id"]: dict(r) for r in con.execute("SELECT * FROM fetch")}
    assert len(rows) == len(paths), "one row per resource, no duplicates"
    expected_error = {"/503": "http_503", "/429": "http_429", "/hang": "timeout"}
    for p in ["/503", "/hang", "/drop", "/429"]:
        r = rows[ids[p]]
        assert r["attempts"] == 2, f"{p}: exactly one retry"
        assert r["error"] is not None, p
        if p in expected_error:
            assert r["error"] == expected_error[p], (p, r["error"])
        # aiohttp itself re-sends a request once when a reused keep-alive connection is closed by the server, so /drop can be seen up to 4 times for our 2 attempts.
        limit = 4 if p == "/drop" else 2
        assert 2 <= app["counts"][p] <= limit, f"{p}: server saw {app['counts'][p]} requests"
    ok = rows[ids["/static/app.js"]]
    assert ok["status"] == 200 and ok["error"] is None, "round continues after failures"
    rnd = con.execute("SELECT * FROM round").fetchone()
    assert rnd["finished_at"] is not None and rnd["n_ok"] == 1 and rnd["n_err"] == 4

    # running the same round again cannot create duplicate rows
    con.execute("INSERT OR IGNORE INTO fetch (resource_id, round_id, kind, fetched_at) VALUES (?,?,?,?)",
                (ids["/503"], rnd["round_id"], "main", 0))
    assert con.execute("SELECT COUNT(*) FROM fetch").fetchone()[0] == len(paths)
