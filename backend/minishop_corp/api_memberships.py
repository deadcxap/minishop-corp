"""Membership API; native authentication/CSRF and scoped server-side authorization."""

from typing import Literal
from uuid import UUID

from aiohttp import web

from .api_contracts import body, boundary, response
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor
from .membership_types import ConfirmMembership, DepartMembership
from .memberships import Memberships


@boundary
async def current(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        result = await Memberships(ContractHost.from_request(request)).current(
            actor.session, actor.user_id
        )
        return response(
            {"ok": True, "membership": result.model_dump(mode="json") if result else None}
        )


@boundary
async def confirm(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        result = await Memberships(ContractHost.from_request(request)).confirm(
            actor.session,
            actor.user_id,
            await body(request, ConfirmMembership),
        )
        await actor.session.commit()
        payload: dict[str, object] = {"ok": result.operation is not None}
        if result.operation is not None:
            payload["operation"] = result.operation.model_dump(mode="json")
        else:
            payload.update(error=result.error, retry_after=result.retry_after)
        reply = response(payload, status=result.status)
        if result.status == 429 and result.retry_after is not None:
            reply.headers["Retry-After"] = str(result.retry_after)
        return reply


def scope_of(request: web.Request) -> Literal["self", "manager", "admin"]:
    if request.path.startswith("/api/admin/"):
        require_administrator(request)
        return "admin"
    return "manager" if "/managed-contracts/" in request.path else "self"


@boundary
async def depart(request: web.Request) -> web.Response:
    scope = scope_of(request)
    async with request_actor(request) as actor:
        result = await Memberships(ContractHost.from_request(request)).depart(
            actor.session,
            actor.user_id,
            UUID(request.match_info["membership_id"]),
            await body(request, DepartMembership),
            scope=scope,
            contract_id=UUID(request.match_info["contract_id"]) if scope != "self" else None,
        )
        await actor.session.commit()
        return response({"ok": True, "operation": result.model_dump(mode="json")}, status=202)


@boundary
async def operation(request: web.Request) -> web.Response:
    scope = scope_of(request)
    async with request_actor(request) as actor:
        result = await Memberships(ContractHost.from_request(request)).operation(
            actor.session,
            actor.user_id,
            UUID(request.match_info["operation_id"]),
            scope=scope,
            contract_id=UUID(request.match_info["contract_id"]) if scope != "self" else None,
        )
        return response({"ok": True, "operation": result.model_dump(mode="json")})


def setup_membership_routes(app: web.Application) -> None:
    customer = "/api/plugins/minishop-corp"
    app.router.add_get(customer + "/membership", current)
    app.router.add_post(customer + "/membership/confirm", confirm)
    app.router.add_post(customer + "/memberships/{membership_id}/leave", depart)
    app.router.add_get(customer + "/operations/{operation_id}", operation)
    for prefix in (
        "/api/admin/minishop-corp/contracts/{contract_id}",
        customer + "/managed-contracts/{contract_id}",
    ):
        app.router.add_post(prefix + "/members/{membership_id}/exclude", depart)
        app.router.add_get(prefix + "/operations/{operation_id}", operation)
