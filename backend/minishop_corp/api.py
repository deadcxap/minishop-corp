"""Read-only scaffold routes. Corporate operations are added in later plan stages."""

from aiohttp import web

from .integration.auth import require_administrator, require_customer


def status_response() -> web.Response:
    return web.json_response(
        {"ok": True, "stage": "scaffold"}, headers={"Cache-Control": "private, no-store"}
    )


async def customer_status(request: web.Request) -> web.Response:
    await require_customer(request)
    return status_response()


async def admin_status(request: web.Request) -> web.Response:
    require_administrator(request)
    return status_response()


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/api/plugins/minishop-corp/status", customer_status)
    app.router.add_get("/api/admin/minishop-corp/status", admin_status)
