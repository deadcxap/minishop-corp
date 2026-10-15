from uuid import uuid4

import pytest
from bot.plugins.spec import PluginContext
from bot.services.audience_segmentation import AudienceProvider, AudienceSegmentationService
from config.settings import Settings
from minishop_corp.broadcasts import SelectBroadcastAudience, contract_target, parse_target
from minishop_corp.integration.broadcasts import CorporateAudienceService, setup_broadcasts
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker


@pytest.fixture
def context() -> PluginContext:
    return PluginContext(
        Settings(
            _env_file=None,
            POSTGRES_USER="corp_test",
            POSTGRES_PASSWORD="local-tests-only",
            TELEGRAM_ENABLED=False,
        )
    )


@pytest.mark.parametrize(
    "target",
    [
        "all",
        "minishop-corp.contract:",
        "minishop-corp.unknown:12",
        "minishop-corp.contract:" + "z" * 32,
    ],
)
def test_invalid_corporate_target(target: str) -> None:
    assert parse_target(target) is None


def test_audience_selection_is_bounded_and_canonical() -> None:
    first, second = uuid4(), uuid4()
    assert SelectBroadcastAudience(contract_ids=[second, first, first]).contract_ids == sorted(
        [first, second]
    )
    assert parse_target(contract_target(first)) == ("minishop-corp.contract:", first)
    for values in ([], [uuid4() for _ in range(101)]):
        with pytest.raises(ValidationError):
            SelectBroadcastAudience(contract_ids=values)


async def test_service_decoration_preserves_native_and_other_plugin_providers(
    context: PluginContext,
) -> None:
    sessions = async_sessionmaker()
    native = AudienceSegmentationService(sessions)

    async def members() -> list[int]:
        return [12, 12, 34]

    native.register_provider(AudienceProvider("other.audience", "other", "Other", members))
    context.session_factory = sessions
    context.services["audience_segmentation_service"] = native
    setup_broadcasts(context)
    service = context.require_audience_segmentation_service()
    assert isinstance(service, CorporateAudienceService)
    assert service is not native
    assert type(native) is AudienceSegmentationService
    assert await service.resolve_user_ids("other.audience") == [12, 34]
    service.register_provider(AudienceProvider("another.audience", "another", "Another", members))
    assert native.has_target("another.audience")
    assert service.unregister_provider("another.audience")
    assert not native.has_target("another.audience")
    assert service.has_target("all") and service.is_target_available("all")
    assert not service.has_target("minishop-corp.invalid")
    setup_broadcasts(context)
    assert context.require_audience_segmentation_service() is service


def test_auxiliary_context_without_broadcast_service(context: PluginContext) -> None:
    setup_broadcasts(context)
    assert context.services == {}
