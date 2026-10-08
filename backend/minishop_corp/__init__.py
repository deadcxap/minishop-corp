"""Minishop Plugin API v1 entry point."""

from pathlib import Path

from aiohttp import web
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext

from .api import setup_routes
from .integration.migrations import Migration, migrations

__version__ = "0.1.0"


class CorporatePlugin(Plugin):
    name = "minishop-corp"
    version = __version__
    plugin_api_min_version = 1
    plugin_api_max_version = 1

    def migrations(self) -> list[Migration]:
        return migrations()

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope == WEB_SCOPE_WEBAPP:
            setup_routes(app)

    def locales_dir(self) -> Path:
        packaged = Path(__file__).parent / "locales"
        # Wheels include resources beside the module; editable and signed packages
        # keep the same source dictionaries at the repository/archive root.
        return packaged if packaged.is_dir() else Path(__file__).parents[2] / "locales"

    # The inherited worker hook remains empty until S06.


plugin = CorporatePlugin()
