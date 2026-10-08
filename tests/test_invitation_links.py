from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from bot.app.web.context import BOT_USERNAME, SETTINGS
from config.settings import Settings
from minishop_corp.contracts_types import ContractError
from minishop_corp.integration.invitation_links import invitation_link
from minishop_corp.invitations_types import code_digest, new_code
from pydantic import SecretStr


def test_code_normalization_and_secret_representation() -> None:
    secret = new_code()
    value = secret.get_secret_value()
    assert value not in str(secret) and value not in repr(secret)
    assert code_digest(secret) == code_digest(SecretStr(" " + value.lower() + " "))
    assert code_digest(secret) == code_digest(SecretStr(value.replace("-", " ")))
    with pytest.raises(ContractError, match="invitation_unavailable") as error:
        code_digest(SecretStr("not-a-valid-code"))
    assert "not-a-valid-code" not in str(error.value)


@pytest.mark.parametrize(
    "bot,base",
    [
        ("example_bot", "https://shop.invalid/app"),
        ("", "https://shop.invalid/app?lang=ru"),
        ("", None),
    ],
)
def test_links_use_native_identity_and_keep_web_code_in_fragment(
    bot: str, base: str | None
) -> None:
    settings = Settings(
        _env_file=None,
        POSTGRES_USER="corp_test",
        POSTGRES_PASSWORD="local-tests-only",
        TELEGRAM_ENABLED=False,
        TELEMETRY_ENABLED=False,
        SUBSCRIPTION_MINI_APP_URL=base,
    )
    app = web.Application()
    app[SETTINGS], app[BOT_USERNAME] = settings, bot
    request = make_mocked_request("POST", "/api/admin/minishop-corp", app=app)
    code = new_code()
    link = invitation_link(request, code)
    if bot:
        assert link is not None
        parsed = urlsplit(link)
        assert parsed.netloc == "t.me" and parsed.path == "/example_bot"
        assert parse_qs(parsed.query)["startapp"] == ["corp_" + code.get_secret_value()[5:]]
    elif base:
        assert link is not None
        parsed = urlsplit(link)
        assert parsed.query == "lang=ru"
        assert parsed.fragment == "corp_code=" + code.get_secret_value()
        assert code.get_secret_value() not in parsed.path + parsed.query
    else:
        assert link is None
