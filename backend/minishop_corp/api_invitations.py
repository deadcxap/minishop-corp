"""Scoped invitation management and authenticated, throttled offer previews."""

from uuid import UUID

from aiohttp import web

from .api_contracts import body, boundary, response
from .contracts_types import ContractPage
from .integration.auth import require_administrator
from .integration.contracts import ContractHost, request_actor
from .integration.invitation_links import invitation_link
from .invitation_lookup import InvitationLookup
from .invitations import Invitations
from .invitations_types import (
    CreateInvitation,
    InvitationInfo,
    IssuedInvitation,
    PreviewInvitation,
    RotateInvitation,
)


@boundary
async def customer_preview(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        draft = await body(request, PreviewInvitation)
        result = await InvitationLookup(ContractHost.from_request(request)).inspect(
            actor.session, actor.user_id, draft.code
        )
        # Expected failure responses must retain the persistent attempt budget.
        await actor.session.commit()
        payload: dict[str, object] = {"ok": result.status == 200, "retry_after": result.retry_after}
        if result.offer is not None and result.tariff is not None:
            payload.update(
                offer=result.offer.model_dump(mode="json"),
                tariff=result.tariff.model_dump(mode="json"),
            )
        else:
            payload["error"] = result.error
        reply = response(payload, status=result.status)
        if result.status == 429 and result.retry_after is not None:
            reply.headers["Retry-After"] = str(result.retry_after)
        return reply


def invitation_payload(request: web.Request, row: InvitationInfo) -> dict[str, object]:
    """Only call after admin or contract-manager authorization, never for public offers."""
    return {
        **row.model_dump(mode="json", exclude={"code"}),
        "code": row.code.get_secret_value() if row.code else None,
        "link": invitation_link(request, row.code) if row.code else None,
    }


def issued_response(request: web.Request, issued: IssuedInvitation) -> web.Response:
    code = issued.code
    return response(
        {
            "ok": True,
            "invitation": invitation_payload(request, issued.invitation),
            "code": code.get_secret_value() if code else None,
            "link": invitation_link(request, code) if code else None,
            "created": issued.created,
            "notice": "minishop_corp_code_saved",
        },
        status=201 if issued.created else 200,
    )


@boundary
async def admin_collection(request: web.Request) -> web.Response:
    require_administrator(request)
    async with request_actor(request) as actor:
        contract_id = UUID(request.match_info["contract_id"])
        invitations = Invitations(ContractHost.from_request(request))
        if request.method in {"GET", "HEAD"}:
            page = ContractPage.model_validate(dict(request.query))
            rows = await invitations.list(actor.session, actor.user_id, contract_id, page)
            return response(
                {"ok": True, "invitations": [invitation_payload(request, row) for row in rows]}
            )
        issued = await invitations.create(
            actor.session, actor.user_id, contract_id, await body(request, CreateInvitation)
        )
        await actor.session.commit()
        return issued_response(request, issued)


@boundary
async def admin_rotate(request: web.Request) -> web.Response:
    require_administrator(request)
    return await rotate(request, manager=False)


@boundary
async def manager_rotate(request: web.Request) -> web.Response:
    return await rotate(request, manager=True)


async def rotate(request: web.Request, *, manager: bool) -> web.Response:
    async with request_actor(request) as actor:
        issued = await Invitations(ContractHost.from_request(request)).rotate(
            actor.session,
            actor.user_id,
            UUID(request.match_info["contract_id"]),
            UUID(request.match_info["invitation_id"]),
            await body(request, RotateInvitation),
            manager=manager,
        )
        await actor.session.commit()
        return issued_response(request, issued)


@boundary
async def admin_revoke(request: web.Request) -> web.Response:
    require_administrator(request)
    async with request_actor(request) as actor:
        result = await Invitations(ContractHost.from_request(request)).revoke(
            actor.session,
            actor.user_id,
            UUID(request.match_info["contract_id"]),
            UUID(request.match_info["invitation_id"]),
        )
        await actor.session.commit()
        return response({"ok": True, "invitation": invitation_payload(request, result)})


@boundary
async def manager_collection(request: web.Request) -> web.Response:
    async with request_actor(request) as actor:
        page = ContractPage.model_validate(dict(request.query))
        rows = await Invitations(ContractHost.from_request(request)).list(
            actor.session,
            actor.user_id,
            UUID(request.match_info["contract_id"]),
            page,
            manager=True,
        )
        return response(
            {"ok": True, "invitations": [invitation_payload(request, row) for row in rows]}
        )


def setup_invitation_routes(app: web.Application) -> None:
    app.router.add_post("/api/plugins/minishop-corp/invitations/preview", customer_preview)
    admin = "/api/admin/minishop-corp/contracts/{contract_id}/invitations"
    manager = "/api/plugins/minishop-corp/managed-contracts/{contract_id}/invitations"
    app.router.add_get(admin, admin_collection)
    app.router.add_post(admin, admin_collection)
    app.router.add_post(admin + "/{invitation_id}/rotate", admin_rotate)
    app.router.add_post(admin + "/{invitation_id}/revoke", admin_revoke)
    app.router.add_get(manager, manager_collection)
    app.router.add_post(manager + "/{invitation_id}/rotate", manager_rotate)
