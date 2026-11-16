import asyncio
from conftest import add_site
from freshness import testserver
from freshness.fetcher import RoundRunner
from freshness.sampler import discover_resources, is_fingerprinted, is_session_like


def test_ct6_politeness(cfg, con):
    cfg.per_domain_gap_s = 2.0  
    paths = ["/page/1.html", "/page/2.html", "/private", "/page/3.html"]

    async def go():
        runner, base, app = await testserver.start()
        try:
            ids = add_site(con, base, paths)
            await RoundRunner(cfg, con, "main", use_processes=False, force_robots=True).run()
            return ids, app
        finally:
            await runner.cleanup()

    ids, app = asyncio.run(go())
    requested = [path for (path, t, h) in app["log"]]
    assert "/private" not in requested, "robots.txt must be obeyed"
    assert tuple(con.execute("SELECT active, inactive_reason FROM resource WHERE resource_id=?", (ids["/private"],)).fetchone()) == (0, "robots")
    times = [t for (path, t, h) in app["log"]]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert len(times) == 4, requested            
    assert all(g >= 1.95 for g in gaps), gaps    
    ua = app["log"][0][2]["User-Agent"]
    assert ua.startswith("FreshnessStudy/1.0")


def test_discover_resources_same_site_only():
    html = b"""<html><head>
      <link rel="stylesheet" href="/css/site.css"><link rel="stylesheet" href="https://other.com/x.css">
      <link rel="alternate" type="application/rss+xml" href="/feed.xml">
      <script src="/js/app.3f9a2c1b7d.js"></script><script src="https://cdn.other.com/lib.js"></script>
      <script src="https://static.example.com/js/b.js"></script></head><body>
      <a href="/news/2026/11/03/city-council-approves-new-budget">story</a>
      <a href="/sport/football-team-wins-the-cup-final">story 2</a>
      <a href="/login">login</a><a href="/about">about</a><a href="/news/x?sessionid=123">s</a>
      <img src="/img/logo.png"><img src="data:image/png;base64,AAA"></body></html>"""
    res = discover_resources(html, "https://www.example.com/", "example.com", 8)
    kinds = [k for _, k in res]
    urls = [u for u, _ in res]
    assert kinds.count("homepage") == 1 and kinds.count("article") == 2
    assert kinds.count("script") == 2 and kinds.count("stylesheet") == 1
    assert kinds.count("image") == 1 and kinds.count("feed") == 1
    assert not any("other.com" in u for u in urls)
    assert not any("login" in u or "about" in u or "sessionid" in u for u in urls)
    assert len(res) <= 8


def test_fingerprint_and_session_rules():
    assert is_fingerprinted("https://a.com/js/app.3f9a2c1b7d.js")
    assert is_fingerprinted("https://a.com/assets/index-BkL3x9aZ.js")
    assert is_fingerprinted("https://a.com/style.css?ver=6.4.2")
    assert not is_fingerprinted("https://a.com/js/app.js")
    assert not is_fingerprinted("https://a.com/news/city-council-budget")
    assert is_session_like("https://a.com/x?PHPSESSID=abc")
    assert is_session_like("https://a.com/x?utm_source=tw")
    assert not is_session_like("https://a.com/x?page=2")
