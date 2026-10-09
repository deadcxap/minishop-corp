from uuid import uuid4

import pytest
from bot.services.account_roles import grant_role
from minishop_corp.integration.contracts import ContractHost
from minishop_corp.integration.migrations import migrations
from minishop_corp.invitations import Invitations
from minishop_corp.invitations_types import CreateInvitation, InvitationInfo, code_digest, new_code
from minishop_corp.storage.invitations import lookup_invitation
from minishop_corp.storage.schema import Invitation
from sqlalchemy import text

from .conftest import USER_ID, Host
from .test_storage import seed_contract

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_populated_0007_upgrade_retains_legacy_invites_and_stores_new_codes(
    host: Host,
) -> None:
    schema = "ext_minishop_corp_upgrade_" + uuid4().hex
    await grant_role(host.session, USER_ID, "admin", source="corp_codes_test")
    async with host.session.begin_nested():
        await host.session.execute(text(f'CREATE SCHEMA "{schema}"'))
        await host.session.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
        connection = await host.session.connection()
        for migration in migrations()[:7]:
            await connection.run_sync(migration.upgrade)
        contract = await seed_contract(host.session)
        contract_id = contract.id
        identifier = uuid4()
        secret = new_code()
        await host.session.execute(
            text("""INSERT INTO ext_minishop_corp_invitations
                (id, contract_id, kind, code_digest, use_limit, created_by)
                VALUES (:id, :contract, 'single', :digest, 1, :actor)"""),
            {
                "id": identifier,
                "contract": contract.id,
                "digest": code_digest(secret),
                "actor": USER_ID,
            },
        )
        savepoint = await host.session.begin_nested()
        connection = await host.session.connection()
        for migration in migrations()[7:]:
            await connection.run_sync(migration.upgrade)
        await savepoint.rollback()
        assert (
            await host.session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
                    "AND table_name='ext_minishop_corp_invitations' AND column_name='code_value'"
                ),
                {"schema": schema},
            )
            == 0
        )
        connection = await host.session.connection()
        for migration in migrations()[7:]:
            await connection.run_sync(migration.upgrade)
        legacy = await host.session.get(Invitation, identifier)
        assert legacy is not None and legacy.code_value is None and legacy.revoked_at is None
        assert (await lookup_invitation(host.session, secret)).invitation_id == identifier
        service = Invitations(ContractHost(host.service))
        draft = CreateInvitation(id=uuid4(), kind="single", use_limit=1)
        issued = await service.create(host.session, USER_ID, contract.id, draft)
        assert issued.code is not None
        host.session.expire_all()
        repeated = await service.create(host.session, USER_ID, contract_id, draft)
        assert repeated.code == issued.code and repeated.created is False
        # SecretStr still masks accidental model serialization/repr; only API wrappers reveal it.
        row = await host.session.get(Invitation, draft.id)
        assert row is not None
        info = InvitationInfo.model_validate(row)
        assert issued.code.get_secret_value() not in info.model_dump_json()
        assert issued.code.get_secret_value() not in repr(info)
