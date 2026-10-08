"""Contract API; native authentication and CSRF middleware remain authoritative."""

from collections.abc import Awaitable, Callable
from functools import wraps
from uuid import UUID

from aiohttp import web
from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import IntegrityError

from .contracts import Contracts, summary_of
from .contracts_types import ContractError, ContractPage, CreateContract, UpdateContract
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor

type Handler = Callable[[web.Request], Awaitable[web.Response]]


def response(payload: dict[str, object], *, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers={"Cache-Control": "private, no-store"})


def boundary(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except (ValidationError, ValueError):
            return response({"ok": False, "error": "minishop_corp_invalid_request"}, status=400)
        except ContractError as exc:
            return response({"ok": False, "error": exc.code}, status=exc.status)
        except IntegrityError:
            # Concurrent create/update/deletion: do not expose schema or account data.
            return response({"ok": False, "error": "minishop_corp_request_conflict"}, status=409)

    return wrapped


async def body[T: BaseModel](request: web.Request, model: type[T]) -> T:
    if request.content_type != "application/json":
        raise ContractError("minishop_corp_invalid_request", 400)
    data = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        data.extend(chunk)
        if len(data) > 16_384:
            raise ContractError("minishop_corp_invalid_request", 413)
    return model.model_validate_json(data)


def service(request: web.Request) -> Contracts:
    return Contracts(ContractHost.from_request(request))


@boundary
async def admin_collection(request: web.Request) -> web.Response:
    require_administrator(request)
    async with request_actor(request) as actor:
        contracts = service(request)
        if request.method in {"GET", "HEAD"}:
            page = ContractPage.model_validate(dict(request.query))
            rows = await contracts.admin_list(actor.session, actor.user_id, page)
            return response(
                {"ok": True, "contracts": [row.model_dump(mode="json") for row in rows]}
            )
        draft = await body(request, CreateContract)
        result, created = await contracts.create(actor.session, actor.user_id, draft)
        await actor.session.commit()
        return response(
            {"ok": True, "contract": result.model_dump(mode="json")}, status=201 if created else 200
        )


@boundary
async def admin_contract(request: web.Request) -> web.Response:
    require_administrator(request)
    async with request_actor(request) as actor:
        contract_id = UUID(request.match_info["contract_id"])
        contracts = service(request)
        if request.method in {"GET", "HEAD"}:
            result = await contracts.admin_get(actor.session, actor.user_id, contract_id)
        else:
            draft = await body(request, UpdateContract)
            result = await contracts.update(actor.session, actor.user_id, contract_id, draft)
            await actor.session.commit()
        return response({"ok": True, "contract": result.model_dump(mode="json")})


@boundary
async def managed_collection(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        page = ContractPage.model_validate(dict(request.query))
        rows = await service(request).managed_list(actor.session, actor.user_id, page)
        return response({"ok": True, "contracts": [row.model_dump(mode="json") for row in rows]})


@boundary
async def scoped_contract(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        contract_id = UUID(request.match_info["contract_id"])
        contracts = service(request)
        if "/managed-contracts/" in request.path:
            row = await contracts.require_manager(actor.session, actor.user_id, contract_id)
        else:
            row = await contracts.require_member(actor.session, actor.user_id, contract_id)
        return response({"ok": True, "contract": summary_of(row).model_dump(mode="json")})


def setup_contract_routes(app: web.Application) -> None:
    admin = "/api/admin/minishop-corp/contracts"
    customer = "/api/plugins/minishop-corp"
    app.router.add_get(admin, admin_collection)
    app.router.add_post(admin, admin_collection)
    app.router.add_get(admin + "/{contract_id}", admin_contract)
    app.router.add_put(admin + "/{contract_id}", admin_contract)
    app.router.add_get(customer + "/managed-contracts", managed_collection)
    app.router.add_get(customer + "/managed-contracts/{contract_id}", scoped_contract)
    app.router.add_get(customer + "/contracts/{contract_id}", scoped_contract)
