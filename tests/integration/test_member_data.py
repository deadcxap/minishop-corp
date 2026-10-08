import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from db.dal import subscription_dal, user_dal
from minishop_corp.integration.member_data import (
    CONCURRENCY,
    MemberData,
    MemberSource,
    ProfileRecord,
    StoredSubscription,
)

from .conftest import USER_ID, Host
from .test_memberships import joined

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def source_for(host: Host) -> MemberSource:
    await joined(host)
    host.service.panel_service._users_cache.ttl_seconds = 60
    host.service.panel_service._devices_cache.ttl_seconds = 60
    return (await MemberData(host.service).sources(host.session, [USER_ID]))[USER_ID]


async def test_statistics_refresh_missing_values_through_native_cache_and_do_not_change_access(
    host: Host,
) -> None:
    source = await source_for(host)
    reference = source.profile.panel_user_uuid
    assert reference is not None and source.subscription is not None
    host.panel.users[reference]["userTraffic"] = {"usedTrafficBytes": 234}
    host.panel.devices[reference] = [{"hwid": "synthetic-private-device"}]
    before = source.subscription.model_dump()
    service = MemberData(host.service)
    host.panel.requests.clear()
    stats = await service.statistics(source)
    assert stats.traffic_used_bytes.value == 234 and stats.traffic_used_bytes.state == "available"
    assert stats.device_count.value == 1 and stats.device_count.state == "available"
    assert stats.device_limit.value == 5
    assert ("GET", f"users/{reference}") in host.panel.requests
    assert ("GET", f"hwid/devices/{reference}") in host.panel.requests
    assert all(method == "GET" for method, _ in host.panel.requests)
    reloaded = (await service.sources(host.session, [USER_ID]))[USER_ID]
    assert reloaded.subscription is not None and reloaded.subscription.model_dump() == before
    host.panel.requests.clear()
    assert await service.statistics(reloaded) == stats
    assert not host.panel.requests
    assert "synthetic-private-device" not in stats.model_dump_json()


async def test_stored_traffic_and_cached_empty_devices_do_not_trigger_user_fetch(
    host: Host,
) -> None:
    source = await source_for(host)
    reference = source.profile.panel_user_uuid
    assert reference is not None
    row = await subscription_dal.get_latest_subscription_by_user_id(host.session, USER_ID)
    assert row is not None
    await subscription_dal.update_subscription(
        host.session,
        row.subscription_id,
        {
            "traffic_used_bytes": 0,
            "extra_hwid_devices": 2,
            "is_active": False,
        },
    )
    source = (await MemberData(host.service).sources(host.session, [USER_ID]))[USER_ID]
    assert await host.service.panel_service.get_user_devices(reference) == []
    host.panel.requests.clear()
    stats = await MemberData(host.service).statistics(source)
    assert stats.traffic_used_bytes.value == 0 and stats.traffic_used_bytes.state == "stored"
    assert stats.device_count.value == 0 and stats.device_count.state == "available"
    assert stats.device_limit.value == 7 and stats.device_limit.state == "stored"
    assert not host.panel.requests


async def test_missing_counters_on_host_failure_remain_unknown(host: Host) -> None:
    source = await source_for(host)
    host.panel.fail_reads = host.panel.fail_devices = True
    stats = await MemberData(host.service).statistics(source)
    assert stats.traffic_used_bytes.value is None
    assert stats.traffic_used_bytes.state == stats.device_count.state == "unavailable"
    assert stats.device_count.value is None
    assert stats.traffic_limit_bytes.value is not None  # Known stored entitlement is retained.
    assert stats.traffic_limit_bytes.state == "stored"


async def test_stale_native_cache_is_retained_after_failed_refresh(host: Host) -> None:
    source = await source_for(host)
    reference = source.profile.panel_user_uuid
    assert reference is not None
    panel = host.service.panel_service
    panel._users_cache.ttl_seconds = panel._devices_cache.ttl_seconds = 0.01
    host.panel.users[reference]["userTraffic"] = {"usedTrafficBytes": 456}
    host.panel.devices[reference] = [{"hwid": "a"}, {"hwid": "b"}]
    assert await panel.get_user_by_uuid(reference)
    assert len(await panel.get_user_devices(reference)) == 2
    await asyncio.sleep(0.02)
    host.panel.fail_reads = host.panel.fail_devices = True
    stats = await MemberData(host.service).statistics(source)
    assert stats.traffic_used_bytes.value == 456 and stats.traffic_used_bytes.state == "stale"
    assert stats.device_count.value == 2 and stats.device_count.state == "stale"


@pytest.mark.parametrize("payload", ["partial", "malformed", "wrong_identity"])
async def test_incomplete_or_invalid_cache_refreshes_without_exposing_other_user(
    host: Host,
    payload: str,
) -> None:
    source = await source_for(host)
    reference = source.profile.panel_user_uuid
    assert reference is not None
    host.panel.users[reference]["userTraffic"] = {}
    assert await host.service.panel_service.get_user_by_uuid(reference)
    if payload == "malformed":
        host.service.panel_service._users_cache.get_fresh(f"uuid:{reference}")["userTraffic"] = {
            "usedTrafficBytes": "secret-invalid-value"
        }
    elif payload == "wrong_identity":
        host.service.panel_service._users_cache.get_fresh(f"uuid:{reference}")["uuid"] = str(
            uuid4()
        )
    host.panel.users[reference]["userTraffic"] = {"usedTrafficBytes": 42}
    stats = await MemberData(host.service).statistics(source)
    assert stats.traffic_used_bytes.value == 42
    assert stats.traffic_used_bytes.state == "available"
    assert "secret-invalid-value" not in stats.model_dump_json()


async def test_ambiguous_subscription_history_is_not_mistaken_for_current_statistics(
    host: Host,
) -> None:
    source = await source_for(host)
    reference = source.profile.panel_user_uuid
    assert reference is not None
    await subscription_dal.upsert_subscription(
        host.session,
        {
            "user_id": USER_ID,
            "panel_user_uuid": reference,
            "panel_subscription_uuid": str(uuid4()),
            "end_date": datetime.now(UTC) + timedelta(days=400),
            "is_active": False,
            "traffic_used_bytes": 999999,
            "traffic_limit_bytes": 9999999,
        },
    )
    source = (await MemberData(host.service).sources(host.session, [USER_ID]))[USER_ID]
    assert source.subscription is None
    host.panel.users[reference]["userTraffic"] = {"usedTrafficBytes": 87}
    stats = await MemberData(host.service).statistics(source)
    assert stats.traffic_used_bytes.value == 87 and stats.device_limit.value == 5


async def test_no_panel_link_is_unknown_and_no_telegram_link_uses_internal_id(host: Host) -> None:
    await user_dal.update_user(host.session, USER_ID, {"username": "local_username"})
    source = (await MemberData(host.service).sources(host.session, [USER_ID]))[USER_ID]
    stats = await MemberData(host.service).statistics(source)
    assert all(value["value"] is None for value in stats.model_dump().values())
    assert source.profile.public().telegram_url is None
    assert not host.panel.requests
    linked = source.profile.model_copy(update={"telegram_id": 12345, "username": "Example_user"})
    assert linked.public().telegram_url == "https://t.me/Example_user"
    missing_name = linked.model_copy(update={"username": None})
    assert missing_name.public().telegram_url == "tg://user?id=12345"
    unsafe_name = linked.model_copy(update={"username": "../another?token=secret"})
    assert unsafe_name.public().telegram_url == "tg://user?id=12345"


async def test_parallel_pages_share_a_bounded_native_refresh_limit(host: Host) -> None:
    adapters = [MemberData(host.service), MemberData(host.service)]
    host.panel.expected_reads = CONCURRENCY
    host.panel.reads_started = asyncio.Event()
    host.panel.continue_reads = asyncio.Event()
    template = MemberSource(
        ProfileRecord(
            user_id=USER_ID,
            first_name=None,
            last_name=None,
            username=None,
            telegram_id=None,
            panel_user_uuid=str(uuid4()),
        ),
        StoredSubscription(
            traffic_used_bytes=0, traffic_limit_bytes=0, hwid_device_limit=0, extra_hwid_devices=0
        ),
        False,
    )
    tasks = [
        asyncio.create_task(
            adapters[index % 2].statistics(
                replace(
                    template,
                    profile=template.profile.model_copy(update={"panel_user_uuid": str(uuid4())}),
                )
            )
        )
        for index in range(100)
    ]
    try:
        await asyncio.wait_for(host.panel.reads_started.wait(), 5)
        assert host.panel.max_active_reads == CONCURRENCY
    finally:
        host.panel.continue_reads.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
    assert host.panel.max_active_reads == CONCURRENCY
    assert len(results) == 100 and all(result.device_count.value == 0 for result in results)
    assert all(
        result.device_limit.value == 0 for result in results
    )  # Known unlimited, not missing.
