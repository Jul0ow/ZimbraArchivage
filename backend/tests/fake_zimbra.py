"""Faux serveur Zimbra (SOAP JSON + ReST) branché sur httpx.MockTransport."""

import json
from dataclasses import dataclass, field

import httpx

SOAP_URL = "https://zimbra.test/service/soap"
REST_URL = "https://zimbra.test/service"

VALID_TOKEN = "jeton-valide"


@dataclass
class FakeMessage:
    id: str
    folder_id: str
    date_ms: int
    subject: str
    content: bytes


@dataclass
class FakeZimbra:
    username: str = "bernard@test.fr"
    password: str = "secret"
    messages: list[FakeMessage] = field(default_factory=list)
    failing_message_ids: set[str] = field(default_factory=set)
    searches: list[dict] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == SOAP_URL:
            return self._soap(request)
        if request.url.path == "/service/home/~/":
            return self._rest(request)
        return httpx.Response(404)

    def _authenticated(self, request: httpx.Request) -> bool:
        return request.headers.get("Cookie") == f"ZM_AUTH_TOKEN={VALID_TOKEN}"

    @staticmethod
    def _fault(code: str, reason: str) -> httpx.Response:
        body = {"Fault": {"Reason": {"Text": reason}, "Detail": {"Error": {"Code": code}}}}
        return httpx.Response(500, json={"Body": body})

    def _soap(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["Body"]
        name, params = next(iter(body.items()))
        if name == "AuthRequest":
            if (
                params["account"]["_content"] == self.username
                and params["password"]["_content"] == self.password
            ):
                return httpx.Response(200, json={"Body": {"AuthResponse": {
                    "authToken": [{"_content": VALID_TOKEN}], "lifetime": 172800000,
                }}})
            return self._fault("account.AUTH_FAILED", "authentication failed")
        if not self._authenticated(request):
            return self._fault("service.AUTH_REQUIRED", "no valid authtoken present")
        if name == "GetInfoRequest":
            return httpx.Response(200, json={"Body": {"GetInfoResponse": {"name": self.username}}})
        if name == "GetFolderRequest":
            return httpx.Response(200, json={"Body": {"GetFolderResponse": {"folder": [ROOT]}}})
        if name == "SearchRequest":
            self.searches.append(params)
            folder_id = params["query"].split()[0].removeprefix("inid:")
            matching = [m for m in self.messages if m.folder_id == folder_id]
            page = matching[params["offset"]: params["offset"] + params["limit"]]
            more = params["offset"] + params["limit"] < len(matching)
            return httpx.Response(200, json={"Body": {"SearchResponse": {
                "m": [
                    {"id": m.id, "l": m.folder_id, "d": m.date_ms, "s": len(m.content), "su": m.subject}
                    for m in page
                ],
                "more": more,
            }}})
        return self._fault("service.UNKNOWN_DOCUMENT", f"unknown request {name}")

    def _rest(self, request: httpx.Request) -> httpx.Response:
        if not self._authenticated(request):
            return httpx.Response(401)
        message_id = request.url.params["id"]
        if message_id in self.failing_message_ids:
            return httpx.Response(500)
        message = next((m for m in self.messages if m.id == message_id), None)
        if message is None:
            return httpx.Response(404)
        return httpx.Response(200, content=message.content)


ROOT = {
    "id": "1",
    "name": "USER_ROOT",
    "absFolderPath": "/",
    "folder": [
        {
            "id": "2",
            "name": "Inbox",
            "absFolderPath": "/Inbox",
            "view": "message",
            "n": 3,
            "color": 5,
            "folder": [
                {"id": "257", "name": "Projets: 2024", "absFolderPath": "/Inbox/Projets: 2024", "view": "message", "n": 1, "color": 1, "rgb": "#12ab9f"},
            ],
        },
        {"id": "5", "name": "Sent", "absFolderPath": "/Sent", "view": "message", "n": 0},
        {"id": "7", "name": "Contacts", "absFolderPath": "/Contacts", "view": "contact", "n": 0},
    ],
    "link": [{"id": "300", "name": "Partage", "absFolderPath": "/Partage"}],
    "search": [{"id": "301", "name": "Recherche", "absFolderPath": "/Recherche"}],
}
