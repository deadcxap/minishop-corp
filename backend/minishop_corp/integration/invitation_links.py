"""Native Mini App identity/configuration, without inventing a deployment URL."""

import re
from urllib.parse import quote, urlsplit, urlunsplit

from aiohttp import web
from bot.app.web.context import get_bot_username, get_settings
from pydantic import SecretStr


def invitation_link(request: web.Request, code: SecretStr) -> str | None:
    value = code.get_secret_value()
    username = get_bot_username(request).strip().lstrip("@")
    if re.fullmatch(r"[A-Za-z0-9_]{5,32}", username):
        return f"https://t.me/{username}?startapp=corp_{value.removeprefix('CORP-')}"
    configured = get_settings(request).SUBSCRIPTION_MINI_APP_URL
    if not configured:
        return None
    try:
        parts = urlsplit(str(configured))
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username:
        return None
    # Fragments are not sent in HTTP requests/access logs or Referer headers.
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "corp_code=" + quote(value))
    )
