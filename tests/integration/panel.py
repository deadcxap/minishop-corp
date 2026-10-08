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
        self.lose_create_response = False
        self.fail_after_trial_activation = False
        self.trial_applied = False
        self.external_squads: dict[str, dict[str, object]] = {}
        self.fail_squads = False
        self.app = web.Application()
        self.app.router.add_route("*", "/api/{path:.*}", self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        path = request.match_info["path"]
        method = request.method
        self.requests.append((method, path))
        if method == "GET" and path == "system/metadata":
            return web.json_response({"response": {"version": "2.8.1"}})
        if method == "GET" and path.startswith("external-squads/"):
            if self.fail_squads:
                return web.json_response({"message": "Unavailable"}, status=503)
            squad = self.external_squads.get(path.removeprefix("external-squads/"))
            if squad is None:
                return web.json_response({"message": "Not found"}, status=404)
            return web.json_response({"response": squad})
        if method == "GET" and path == "users":
            return web.json_response(
                {"response": {"users": list(self.users.values()), "total": len(self.users)}}
            )
        if method == "DELETE" and path.startswith("users/"):
            self.users.pop(path.removeprefix("users/"), None)
            return web.json_response({"response": {"isDeleted": True}})
        if method == "POST" and path == "users/bulk/update-squads":
            body = await request.json()
            assert set(body) == {"uuids", "activeInternalSquads"}
            for identifier in body["uuids"]:
                self.users[identifier]["activeInternalSquads"] = list(body["activeInternalSquads"])
            return web.json_response({"response": {"affectedRows": len(body["uuids"])}})
        if method == "GET" and path.startswith("users/"):
            identifier = path.removeprefix("users/")
            if "/" in identifier:
                kind, value = identifier.split("/", 1)
                field = {
                    "by-username": "username",
                    "by-telegram-id": "telegramId",
                    "by-email": "email",
                }.get(kind)
                if field:
                    matches = [u for u in self.users.values() if str(u.get(field)) == value]
                    if matches or kind != "by-username":
                        return web.json_response(
                            {"response": matches[0] if kind == "by-username" else matches}
                        )
            elif identifier in self.users:
                return web.json_response({"response": self.users[identifier]})
            return web.json_response({"message": "Not found"}, status=404)
        if path == "users" and method in {"POST", "PATCH"}:
            body = await request.json()
            assert isinstance(body, dict)
            if method == "POST":
                if any(user["username"] == body["username"] for user in self.users.values()):
                    return web.json_response({"errorCode": "A019"}, status=409)
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
                if self.lose_create_response:
                    return web.json_response(
                        {"message": "Lost creation acknowledgement"}, status=503
                    )
                return web.json_response({"response": user}, status=201)
            if self.fail_updates or (self.fail_after_trial_activation and self.trial_applied):
                return web.json_response({"message": "Unavailable"}, status=503)
            identifier = str(body["uuid"])
            user = {**self.users[identifier], **body}
            if not self.echo_without_saving:
                self.users[identifier] = deepcopy(user)
                if body.get("tag") == "TRIAL":
                    self.trial_applied = True
            if self.lose_update_response:
                return web.json_response({"message": "Lost acknowledgement"}, status=503)
            return web.json_response({"response": user})
        # A newly required endpoint must be added deliberately, not silently mocked.
        raise AssertionError(f"Unsupported panel call: {method} {path}")
