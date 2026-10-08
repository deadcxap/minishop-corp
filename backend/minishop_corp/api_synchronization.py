"""Read-only synchronization status for global admins and the contract's manager."""

from uuid import UUID

from aiohttp import web

from .api_contracts import boundary, response
from .contracts_types import ContractPage
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor
from .synchronization import Synchronization


@boundary
async def status(request: web.Request) -> web.Response:
    manager = not request.path.startswith("/api/admin/")
    if not manager:
        require_administrator(request)
    async with request_actor(request) as actor:
        service = Synchronization(ContractHost.from_request(request))
        contract_id = UUID(request.match_info["contract_id"])
        if request.path.endswith("/operations"):
            rows = await service.operations(
                actor.session,
                actor.user_id,
                contract_id,
                ContractPage.model_validate(dict(request.query)),
                manager=manager,
            )
            return response(
                {"ok": True, "operations": [row.model_dump(mode="json") for row in rows]}
            )
        result = await service.progress(actor.session, actor.user_id, contract_id, manager=manager)
        return response({"ok": True, "synchronization": result.model_dump(mode="json")})


def setup_synchronization_routes(app: web.Application) -> None:
    for prefix in (
        "/api/admin/minishop-corp/contracts/{contract_id}",
        "/api/plugins/minishop-corp/managed-contracts/{contract_id}",
    ):
        app.router.add_get(prefix + "/synchronization", status)
        app.router.add_get(prefix + "/synchronization/operations", status)
