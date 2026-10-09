"""Authenticated choices for corporate contract forms; no generic host API proxy."""

from aiohttp import web

from .api_contracts import boundary, response
from .contracts_types import ContractError
from .integration.admin_options import AccountQuery, AdminOptions, SquadQuery
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor


@boundary
async def options(request: web.Request) -> web.Response:
    require_administrator(request)
    async with request_actor(request) as actor:
        host = ContractHost.from_request(request)
        await host.require_admin(actor.session, actor.user_id)
        service = AdminOptions(host)
        kind = request.match_info["kind"]
        if kind in {"tariffs", "context"}:
            if request.query:
                raise ContractError("minishop_corp_invalid_request", 400)
            payload: dict[str, object] = (
                {"actor_user_id": actor.user_id}
                if kind == "context"
                else {"tariffs": [row.model_dump(mode="json") for row in service.tariffs()]}
            )
        elif kind == "squad":
            query = SquadQuery.model_validate(dict(request.query))
            payload = {"squad": (await service.squad(query.uuid)).model_dump(mode="json")}
        else:
            page = AccountQuery.model_validate(dict(request.query))
            payload = (await service.accounts(actor.session, page)).model_dump(mode="json")
        actor.session.expire_all()
        await host.require_admin(actor.session, actor.user_id)
        return response({"ok": True, **payload})


def setup_admin_option_routes(app: web.Application) -> None:
    app.router.add_get(
        "/api/admin/minishop-corp/options/{kind:tariffs|squad|accounts|context}", options
    )
