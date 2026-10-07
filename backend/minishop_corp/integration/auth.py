"""Use the host's session, account-ban and administrator checks."""

from aiohttp import web
from bot.app.web.admin_api_impl.auth import _require_admin_user_id
from bot.app.web.webapp.extension_runtime import user_context


async def require_customer(request: web.Request) -> int:
    async with user_context(request) as context:
        return context.user_id


def require_administrator(request: web.Request) -> int:
    # The core admin middleware resolves the current role on every request.
    # A missing middleware therefore fails closed, including a valid user session.
    return _require_admin_user_id(request)
