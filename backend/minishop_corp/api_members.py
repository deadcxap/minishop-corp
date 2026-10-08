"""Member and avatar reads stay within the plugin's authenticated JSON host API."""

from uuid import UUID

from aiohttp import web

from .api_contracts import boundary, response
from .contracts_types import ContractError, ContractPage
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor
from .members import Members


@boundary
async def collection(request: web.Request) -> web.Response:
    manager = not request.path.startswith("/api/admin/")
    if not manager:
        require_administrator(request)
    async with request_actor(request) as actor:
        result = await Members(ContractHost.from_request(request)).page(
            actor.session,
            actor.user_id,
            UUID(request.match_info["contract_id"]),
            ContractPage.model_validate(dict(request.query)),
            manager=manager,
            refresh=request.method != "HEAD",
        )
        return response({"ok": True, **result.model_dump(mode="json")})


@boundary
async def avatar(request: web.Request) -> web.Response:
    manager = not request.path.startswith("/api/admin/")
    if not manager:
        require_administrator(request)
    async with request_actor(request) as actor:
        if request.query:
            raise ContractError("minishop_corp_invalid_request", 400)
        result = await Members(ContractHost.from_request(request)).avatar(
            request,
            actor.session,
            actor.user_id,
            UUID(request.match_info["contract_id"]),
            UUID(request.match_info["membership_id"]),
            manager=manager,
            refresh=request.method != "HEAD",
        )
        # Only the native avatar cache can have changed; no membership/access writes.
        await actor.session.commit()
        return response({"ok": True, "avatar": result.model_dump(mode="json")})


def setup_member_routes(app: web.Application) -> None:
    for prefix in (
        "/api/admin/minishop-corp/contracts/{contract_id}",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}",
    ):
        app.router.add_get(prefix + "/members", collection)
        app.router.add_get(prefix + "/members/{membership_id}/avatar", avatar)
