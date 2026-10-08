"""Only the external transport is substituted; host services and DAL are real."""

from copy import deepcopy
from uuid import uuid4

from aiohttp import web


class Panel:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, object]] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_updates = False
        self.echo_without_saving = False
        self.lose_update_response = False
        self.app = web.Application()
        self.app.router.add_route("*", "/api/{path:.*}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        method = request.method
        self.requests.append((method, path))
        if method == "GET" and path == "system/metadata":
            return web.json_response({"response": {"version": "2.8.0"}})
        if method == "GET" and path == "users":
            return web.json_response(
                {"response": {"users": list(self.users.values()), "total": len(self.users)}}
            )
        if method == "GET" and path.startswith("users/"):
            identifier = path.removeprefix("users/")
            if "/" in identifier:
                kind, value = identifier.split("/", 1)
                field = {"by-username": "username", "by-telegram-id": "telegramId"}.get(kind)
                if field:
                    matches = [u for u in self.users.values() if str(u.get(field)) == value]
                    if matches:
                        return web.json_response(
                            {"response": matches if kind == "by-telegram-id" else matches[0]}
                        )
            elif identifier in self.users:
                return web.json_response({"response": self.users[identifier]})
            return web.json_response({"message": "Not found"}, status=404)
        if path == "users" and method in {"POST", "PATCH"}:
            body = await request.json()
            assert isinstance(body, dict)
            if method == "POST":
                identifier = str(uuid4())
                short = uuid4().hex[:12]
                user: dict[str, object] = {
                    "uuid": identifier,
                    "shortUuid": short,
                    "subscriptionUuid": str(uuid4()),
                    "subscriptionUrl": f"https://panel.invalid/sub/{short}",
                    "userTraffic": {"usedTrafficBytes": 0, "lifetimeUsedTrafficBytes": 0},
                    "activeInternalSquads": [],
                    "externalSquadUuid": None,
                    "tag": None,
                    **body,
                }
                self.users[identifier] = user
                return web.json_response({"response": user}, status=201)
            if self.fail_updates:
                return web.json_response({"message": "Unavailable"}, status=503)
            identifier = str(body["uuid"])
            user = {**self.users[identifier], **body}
            if not self.echo_without_saving:
                self.users[identifier] = deepcopy(user)
            if self.lose_update_response:
                return web.json_response({"message": "Lost acknowledgement"}, status=503)
            return web.json_response({"response": user})
        # A newly required endpoint must be added deliberately, not silently mocked.
        raise AssertionError(f"Unsupported panel call: {method} {path}")
