"""Plugin routes and the original UI scaffold health checks."""

from aiohttp import web

from .api_contracts import setup_contract_routes
from .api_invitations import setup_invitation_routes
from .api_memberships import setup_membership_routes
from .api_synchronization import setup_synchronization_routes
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
    setup_contract_routes(app)
    setup_invitation_routes(app)
    setup_membership_routes(app)
    setup_synchronization_routes(app)
