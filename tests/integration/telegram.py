"""Local Telegram transport; avatar fetching, selection and DAL remain Minishop's."""

import asyncio
import base64

from aiohttp import web

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aTgAAAABJRU5ErkJggg=="
)


class Telegram:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False
        self.no_photo = False
        self.started: asyncio.Event | None = None
        self.release: asyncio.Event | None = None
        self.app = web.Application()
        self.app.router.add_post("/bot{token}/{method}", self.method)
        self.app.router.add_get("/file/bot{token}/avatars/fixture.png", self.download)

    async def method(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        self.calls.append(method)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.fail:
            return web.json_response(
                {"ok": False, "error_code": 500, "description": "Synthetic Telegram outage"},
                status=500,
            )
        photo = {
            "file_id": "synthetic-avatar",
            "file_unique_id": "synthetic-avatar-unique",
            "file_size": len(PNG),
            "width": 1,
            "height": 1,
        }
        if method == "getUserProfilePhotos":
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        "total_count": 0 if self.no_photo else 1,
                        "photos": [] if self.no_photo else [[photo]],
                    },
                }
            )
        if method == "getFile":
            return web.json_response(
                {
                    "ok": True,
                    "result": {
                        **photo,
                        "file_path": "avatars/fixture.png",
                    },
                }
            )
        raise AssertionError(f"Unexpected Telegram method: {method}")

    async def download(self, request: web.Request) -> web.Response:
        self.calls.append("download")
        return web.Response(body=PNG, content_type="image/png")
