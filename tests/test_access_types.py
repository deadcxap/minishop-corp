import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from minishop_corp import plugin
from minishop_corp.integration.access_types import AccessFailure, PeriodAccess, TrialAccess


def test_absolute_dates_use_host_precision_without_changing_timezone_meaning() -> None:
    local = datetime(2026, 10, 10, 12, 0, 0, 123456, tzinfo=timezone(timedelta(hours=3)))
    target = PeriodAccess("corp", local, "fixture")
    assert target.ends_at == datetime(2026, 10, 10, 9, 0, 0, 123000, tzinfo=UTC)
    trial = TrialAccess(local, local + timedelta(days=3))
    assert trial.ends_at - trial.starts_at == timedelta(days=3)


def test_ambiguous_dates_and_empty_windows_are_rejected() -> None:
    with pytest.raises(ValueError, match="aware"):
        PeriodAccess("corp", datetime(2026, 10, 10), "fixture")
    aware = datetime(2026, 10, 10, tzinfo=UTC)
    with pytest.raises(ValueError, match="follow"):
        TrialAccess(aware, aware)


def test_all_access_errors_have_ru_and_en_messages() -> None:
    for language in ("ru", "en"):
        messages = json.loads((plugin.locales_dir() / f"{language}.json").read_text())
        assert all(messages.get(code.value) for code in AccessFailure)
