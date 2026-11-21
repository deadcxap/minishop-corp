"""Corporate membership in the host's admin user table."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Protocol, cast, runtime_checkable

from bot.plugins import extensions as sdk
from bot.plugins.extensions import ExtensionContributions
from bot.plugins.spec import PluginContext
from db.models import User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..storage.schema import Contract, Membership

type _ColumnResolver = Callable[[AsyncSession, tuple[int, ...]], Awaitable[dict[int, str | None]]]


@runtime_checkable
class _AdminUsersSdk(Protocol):
    def AdminUserColumn(
        self, *, id: str, label: str, resolve: _ColumnResolver, order: int
    ) -> object: ...

    def AdminUserFilterOption(self, *, id: str, label: str) -> object: ...

    def AdminUserFilter(
        self,
        *,
        id: str,
        label: str,
        options: tuple[object, ...],
        predicate: Callable[[str], ColumnElement[bool]],
    ) -> object: ...


_ACTIVE = Membership.state == "active"


class _ReplaceAdminUsers(Protocol):
    def __call__(
        self,
        contributions: ExtensionContributions,
        /,
        *,
        admin_user_columns: tuple[object, ...],
        admin_user_filters: tuple[object, ...],
    ) -> ExtensionContributions: ...


def _membership_filter(option: str) -> ColumnElement[bool]:
    member = (
        select(Membership.id)
        .where(Membership.current_user_id == User.user_id, _ACTIVE)
        .correlate(User)
        .exists()
    )
    return member if option == "member" else ~member


async def _subscription_names(
    session: AsyncSession, user_ids: tuple[int, ...]
) -> dict[int, str | None]:
    rows = await session.execute(
        select(Membership.current_user_id, Contract.name)
        .join(Contract, Contract.id == Membership.contract_id)
        .where(Membership.current_user_id.in_(user_ids), _ACTIVE)
    )
    return {identifier: name for identifier, name in rows if identifier is not None}


def with_admin_users(
    contributions: ExtensionContributions, ctx: PluginContext
) -> ExtensionContributions:
    i18n = ctx.i18n
    if not isinstance(sdk, _AdminUsersSdk) or i18n is None:
        return contributions

    def label(key: str) -> str:
        return i18n.gettext(ctx.settings.DEFAULT_LANGUAGE, "admin_minishop_corp_users_" + key)

    yes, no = label("yes"), label("no")

    async def membership(session: AsyncSession, user_ids: tuple[int, ...]) -> dict[int, str | None]:
        members = set(
            await session.scalars(
                select(Membership.current_user_id).where(
                    Membership.current_user_id.in_(user_ids), _ACTIVE
                )
            )
        )
        return {identifier: yes if identifier in members else no for identifier in user_ids}

    columns = (
        sdk.AdminUserColumn(
            id="corp-member", label=label("membership"), resolve=membership, order=100
        ),
        sdk.AdminUserColumn(
            id="corp-subscription",
            label=label("subscription"),
            resolve=_subscription_names,
            order=101,
        ),
    )
    filters = (
        sdk.AdminUserFilter(
            id="corp-membership",
            label=label("filter"),
            options=(
                sdk.AdminUserFilterOption(id="member", label=label("in")),
                sdk.AdminUserFilterOption(id="nonmember", label=label("out")),
            ),
            predicate=_membership_filter,
        ),
    )
    # These dataclass fields are only present on hosts that export the table SDK.
    return cast(_ReplaceAdminUsers, replace)(
        contributions, admin_user_columns=columns, admin_user_filters=filters
    )
