from __future__ import annotations
import asyncio
import gzip
import time
from email.utils import formatdate
from aiohttp import web

APP_JS = b"/* test script */ function hello(){return 42;}\n" * 50


def article(version: int, stamp: str = "") -> str:
    paras = "".join(
        f"<p>Paragraph {i} of the article explains how HTTP caching works and why freshness "
        f"signals matter for browsers, CDNs and crawlers in many different situations.</p>"
        for i in range(12))
    extra = f"<p>New section number {version}: the editors added this update with brand new facts, " \
            f"quotes from experts, and a long explanation of what changed since the first story.</p>" * version
    return (f"<html><head><title>Test article</title><script>var t='{time.time()}';</script></head>"
            f"<body><article><h1>Test article</h1><p class='time'>Updated {stamp}</p>{paras}{extra}"
            f"</article><footer>Footer text</footer></body></html>")


def make_app() -> web.Application:
    app = web.Application()
    app["log"] = []
    app["version"] = 0
    app["counts"] = {}

    @web.middleware
    async def logger(request, handler):
        app["log"].append((request.path, time.monotonic(), dict(request.headers)))
        app["counts"][request.path] = app["counts"].get(request.path, 0) + 1
        return await handler(request)

    app.middlewares.append(logger)
    r = app.router

    async def robots(req):
        return web.Response(text="User-agent: *\nDisallow: /private\n")

    async def static_js(req):
        h = {"ETag": '"v1"', "Cache-Control": "max-age=600", "Content-Type": "application/javascript", "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"}
        if req.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304, headers=h)
        return web.Response(body=APP_JS, headers=h)

    async def gzip_js(req):
        h = {"ETag": '"v1"', "Cache-Control": "max-age=600", "Content-Type": "application/javascript", "Content-Encoding": "gzip", "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"}
        return web.Response(body=gzip.compress(APP_JS), headers=h)

    async def br_js(req):
        try:
            import brotli
        except ImportError:
            raise web.HTTPNotImplemented(text="brotli not installed")
        h = {"ETag": '"v1"', "Cache-Control": "max-age=600", "Content-Type": "application/javascript", "Content-Encoding": "br", "Last-Modified": "Mon, 02 Nov 2026 10:00:00 GMT"}
        return web.Response(body=brotli.compress(APP_JS), headers=h)

    async def redirect(req):
        raise web.HTTPMovedPermanently("/static/app.js")

    async def changing(req):
        v = app["version"]
        etag = f'W/"c{v}"'
        h = {"ETag": etag, "Cache-Control": "max-age=60", "Content-Type": "text/html; charset=utf-8", "Last-Modified": formatdate(1793000000 + v * 3600, usegmt=True)}
        if req.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=h)
        return web.Response(text=article(v, stamp=f"v{v}"), headers=h)

    async def frozen_strong(req):
        v = app["version"]
        h = {"ETag": '"same-forever"', "Cache-Control": "max-age=3600", "Content-Type": "text/html"}
        if req.headers.get("If-None-Match") == '"same-forever"':
            return web.Response(status=304, headers=h)
        return web.Response(text=article(v), headers=h)

    async def lm_now(req):
        now = formatdate(time.time(), usegmt=True)
        return web.Response(text=article(0), headers={"Last-Modified": now, "Date": now, "Content-Type": "text/html"})

    async def noise(req):
        v = app["version"]
        stamp = f"Nov {3 + v}, 2026 10:{10 + v}"
        return web.Response(text=article(0, stamp=stamp), headers={"Content-Type": "text/html"})

    async def fail_503(req):
        return web.Response(status=503, text="unavailable")

    async def fail_429(req):
        return web.Response(status=429, text="slow down")

    async def hang(req):
        await asyncio.sleep(30)
        return web.Response(text="too late")

    async def drop(req):
        req.transport.close()
        await asyncio.sleep(0.05)
        return web.Response(text="never sent")

    async def private(req):
        return web.Response(text="this must never be fetched")

    async def page(req):
        return web.Response(text=article(0), headers={"Content-Type": "text/html", "ETag": '"p"'})

    r.add_get("/robots.txt", robots)
    r.add_get("/static/app.js", static_js)
    r.add_get("/gzip/app.js", gzip_js)
    r.add_get("/br/app.js", br_js)
    r.add_get("/redirect", redirect)
    r.add_get("/changing.html", changing)
    r.add_get("/frozen-strong.html", frozen_strong)
    r.add_get("/lm-now.html", lm_now)
    r.add_get("/noise.html", noise)
    r.add_get("/503", fail_503)
    r.add_get("/429", fail_429)
    r.add_get("/hang", hang)
    r.add_get("/drop", drop)
    r.add_get("/private", private)
    r.add_get("/page/{n}.html", page)
    return app


async def start(port: int = 0):
    app = make_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    real_port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{real_port}", app


def serve_forever(port: int = 8080):
    web.run_app(make_app(), host="127.0.0.1", port=port)
