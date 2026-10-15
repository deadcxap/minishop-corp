"""Admin-only selection of a native Minishop broadcast audience."""

from aiohttp import web

from .api_contracts import body, boundary, response
from .broadcasts import SelectBroadcastAudience, select_audience
from .integration.auth import require_administrator
from .integration.contracts import request_actor


@boundary
async def admin_broadcast_audience(request: web.Request) -> web.Response:
    require_administrator(request)
    draft = await body(request, SelectBroadcastAudience)
    async with request_actor(request) as actor:
        target = await select_audience(actor.session, draft)
        await actor.session.commit()
    return response({"ok": True, "target": target})


def setup_broadcast_routes(app: web.Application) -> None:
    app.router.add_post("/api/admin/minishop-corp/broadcast-audiences", admin_broadcast_audience)
