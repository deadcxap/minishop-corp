"""Loopback-only test host for the installed ESM views, using synthetic sessions."""

import asyncio
import json
import os
import re
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
BACKEND = os.environ.get("CORP_BACKEND_ORIGIN", "http://127.0.0.1:18081")
ALLOWED = re.compile(
    r"/api/(?:plugins/minishop-corp/status|admin/minishop-corp/status|"
    r"plugins/minishop-corp/(?:membership|operations/[0-9a-f-]{36}|"
    r"managed-contracts(?:/[0-9a-f-]{36}(?:/invitations|/members(?:/[0-9a-f-]{36}/avatar)?)?)?)|"
    r"admin/minishop-corp/options/(?:tariffs|squad|accounts|context)|"
    r"admin/minishop-corp/contracts(?:/[0-9a-f-]{36}(?:/invitations|"
    r"/members(?:/[0-9a-f-]{36}/avatar)?|/synchronization(?:/operations)?)?)?|"
    r"extensions/runtime|admin/plugins/runtime|"
    r"(?:extensions|admin/plugins)/assets/minishop-corp/[0-9a-f]{64}/"
    r"(?:admin|customer)/index\.(?:js|css))\Z"
)


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "dev" / "preview.html")


async def script(request: web.Request) -> web.FileResponse:
    return web.FileResponse(ROOT / "dev" / "preview.mjs")


async def proxy(request: web.Request) -> web.Response:
    if not ALLOWED.fullmatch(request.path):
        raise web.HTTPNotFound()
    raw = await asyncio.to_thread((ROOT / ".local/runtime/sessions.json").read_text)
    sessions = json.loads(raw)
    audience = "admin" if request.path.startswith("/api/admin/") else "customer"
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as client:
        async with client.get(
            BACKEND + request.path_qs,
            headers={"Authorization": f"Bearer {sessions[audience]}"},
            allow_redirects=False,
        ) as response:
            return web.Response(
                body=await response.read(),
                status=response.status,
                headers={
                    "Content-Type": response.headers.get(
                        "Content-Type", "application/octet-stream"
                    ),
                    "Cache-Control": "no-store",
                },
            )


app = web.Application()
app.router.add_get("/", index)
app.router.add_get("/preview.mjs", script)
app.router.add_get("/api/{path:.*}", proxy)
web.run_app(
    app,
    host=os.environ.get("CORP_PREVIEW_HOST", "127.0.0.1"),
    port=int(os.environ.get("CORP_PREVIEW_PORT", "18082")),
)
