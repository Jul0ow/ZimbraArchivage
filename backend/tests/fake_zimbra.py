"""Faux serveur Zimbra (SOAP JSON + ReST) branché sur httpx.MockTransport.

La recherche reproduit la sémantique de Zimbra utilisée par l'application : recherche par
mots (et non par sous-chaîne), joker de début de mot, after: / before: exclusifs au jour près
dans le fuseau du serveur, tri par date et pagination par curseur.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

SOAP_URL = "https://zimbra.test/service/soap"
REST_URL = "https://zimbra.test/service"

VALID_TOKEN = "jeton-valide"

# Fuseau du serveur Zimbra, dans lequel sont interprétées les dates de recherche
SERVER_ZONE = ZoneInfo("Europe/Paris")


@dataclass
class FakeMessage:
    id: str
    folder_id: str
    date_ms: int
    subject: str
    content: bytes
    sender: str = ""
    recipients: str = ""
    sender_name: str = ""

    @property
    def has_attachment(self) -> bool:
        return b"content-disposition: attachment" in self.content.lower()


@dataclass
class FakeZimbra:
    username: str = "bernard@test.fr"
    password: str = "secret"
    messages: list[FakeMessage] = field(default_factory=list)
    # Contacts proposés par AutoCompleteRequest : (nom, adresse)
    contacts: list[tuple[str, str]] = field(default_factory=list)
    autocomplete_fault: str | None = None
    search_fault: str | None = None
    failing_message_ids: set[str] = field(default_factory=set)
    # Codes HTTP renvoyés pour le téléchargement de certains messages
    rest_statuses: dict[str, int] = field(default_factory=dict)
    # Messages dont le téléchargement est coupé après quelques octets
    broken_stream_ids: set[str] = field(default_factory=set)
    # Code HTTP renvoyé à toute requête (Zimbra indisponible)
    unavailable_status: int | None = None
    valid_tokens: set[str] = field(default_factory=lambda: {VALID_TOKEN})
    # Jeton invalidé après ce nombre de SearchRequest (session expirée pendant un export)
    expire_after_searches: int | None = None
    searches: list[dict] = field(default_factory=list)
    auth_requests: list[httpx.Request] = field(default_factory=list)
    ended_sessions: list[str] = field(default_factory=list)
    root: dict = field(default_factory=lambda: json.loads(json.dumps(ROOT)))

    def __post_init__(self) -> None:
        # Les téléchargements sont faits en parallèle par l'export
        self._lock = threading.Lock()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            if self.unavailable_status:
                return httpx.Response(self.unavailable_status)
            if str(request.url) == SOAP_URL:
                return self._soap(request)
            if request.url.path == "/service/home/~/":
                return self._rest(request)
            return httpx.Response(404)

    def _token(self, request: httpx.Request) -> str | None:
        cookie = request.headers.get("Cookie", "")
        token = cookie.removeprefix("ZM_AUTH_TOKEN=")
        return token if cookie.startswith("ZM_AUTH_TOKEN=") and token in self.valid_tokens else None

    @staticmethod
    def _fault(code: str, reason: str) -> httpx.Response:
        body = {"Fault": {"Reason": {"Text": reason}, "Detail": {"Error": {"Code": code}}}}
        return httpx.Response(500, json={"Body": body})

    def _soap(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)["Body"]
        name, params = next(iter(body.items()))
        if name == "AuthRequest":
            self.auth_requests.append(request)
            if (
                params["account"]["_content"] == self.username
                and params["password"]["_content"] == self.password
            ):
                return httpx.Response(
                    200,
                    json={
                        "Body": {
                            "AuthResponse": {
                                "authToken": [{"_content": VALID_TOKEN}],
                                "lifetime": 172800000,
                            }
                        }
                    },
                )
            return self._fault("account.AUTH_FAILED", "authentication failed")
        token = self._token(request)
        if token is None:
            return self._fault("service.AUTH_REQUIRED", "no valid authtoken present")
        if name == "EndSessionRequest":
            assert params.get("logoff") == "1"
            self.valid_tokens.discard(token)
            self.ended_sessions.append(token)
            return httpx.Response(200, json={"Body": {"EndSessionResponse": {}}})
        if name == "GetInfoRequest":
            return httpx.Response(200, json={"Body": {"GetInfoResponse": {"name": self.username}}})
        if name == "GetFolderRequest":
            return httpx.Response(200, json={"Body": {"GetFolderResponse": {"folder": [self.root]}}})
        if name == "AutoCompleteRequest":
            if self.autocomplete_fault:
                return self._fault(self.autocomplete_fault, "autocomplete failed")
            text = params["name"].lower()
            return httpx.Response(
                200,
                json={
                    "Body": {
                        "AutoCompleteResponse": {
                            "match": [
                                {"email": f'"{full}" <{address}>', "type": "contact", "full": full}
                                for full, address in self.contacts
                                if any(word.startswith(text) for word in [*full.lower().split(), address])
                            ]
                        }
                    }
                },
            )
        if name == "SearchRequest":
            return self._search(params, token)
        return self._fault("service.UNKNOWN_DOCUMENT", f"unknown request {name}")

    def _search(self, params: dict, token: str) -> httpx.Response:
        self.searches.append(params)
        if self.expire_after_searches is not None and len(self.searches) > self.expire_after_searches:
            self.valid_tokens.discard(token)
            return self._fault("service.AUTH_EXPIRED", "auth credentials have expired")
        if self.search_fault:
            return self._fault(self.search_fault, "search failed")
        query = QueryParser(params["query"])
        ascending = params.get("sortBy") == "dateAsc"
        matching = sorted(
            (m for m in self.messages if query.matches(m)),
            key=lambda m: (m.date_ms, m.id),
            reverse=not ascending,
        )
        if cursor := params.get("cursor"):
            assert ascending, "curseur utilisé sans tri explicite"
            position = (int(cursor["sortVal"]), cursor["id"])
            matching = [m for m in matching if (m.date_ms, m.id) > position]
        offset = params.get("offset", 0)
        page = matching[offset : offset + params["limit"]]
        more = offset + params["limit"] < len(matching)
        return httpx.Response(
            200,
            json={
                "Body": {
                    "SearchResponse": {
                        "m": [
                            {
                                "id": m.id,
                                "l": m.folder_id,
                                "d": m.date_ms,
                                "sf": str(m.date_ms),
                                "s": len(m.content),
                                "su": m.subject,
                                "f": "a" if m.has_attachment else "",
                                "e": self._addresses(m, params.get("recip")),
                            }
                            for m in page
                        ],
                        "more": more,
                    }
                }
            },
        )

    @staticmethod
    def _addresses(message: FakeMessage, recip: str | None) -> list[dict]:
        sender = [{"t": "f", "a": message.sender, "p": message.sender_name}] if message.sender else []
        if recip != "2":
            return sender
        recipients = [r.strip() for r in message.recipients.split(",") if r.strip()]
        return sender + [{"t": "t", "a": r} for r in recipients]

    def _rest(self, request: httpx.Request) -> httpx.Response:
        if self._token(request) is None:
            return httpx.Response(401)
        message_id = request.url.params["id"]
        if message_id in self.failing_message_ids:
            return httpx.Response(500)
        if message_id in self.broken_stream_ids:
            return httpx.Response(200, stream=BrokenStream())
        if message_id in self.rest_statuses:
            return httpx.Response(self.rest_statuses[message_id])
        message = next((m for m in self.messages if m.id == message_id), None)
        if message is None:
            return httpx.Response(404)
        return httpx.Response(200, content=message.content)


class BrokenStream(httpx.SyncByteStream):
    """Connexion coupée au milieu du téléchargement."""

    def __iter__(self):
        yield b"Subject: debut"
        raise httpx.ReadError("coupure")


def words(text: str) -> set[str]:
    """Mots indexés d'un champ : l'adresse entière et chacun de ses mots, comme Zimbra."""
    text = text.lower()
    tokens = set(re.split(r"[^\w]+", text))
    tokens.update(part.strip() for part in text.split(","))
    tokens.discard("")
    return tokens


def _server_day_ms(value: str) -> int:
    day = datetime.strptime(value, "%d/%m/%y").date()
    return int(datetime.combine(day, time.min, SERVER_ZONE).timestamp() * 1000)


class QueryParser:
    """Évalue la syntaxe de recherche Zimbra utilisée par l'application."""

    TOKEN = re.compile(r'\(|\)|[a-z]+:(?:"(?:[^"\\]|\\.)*"|[^\s()]+)|[^\s()]+')

    def __init__(self, query: str):
        self.tokens = self.TOKEN.findall(query)

    def matches(self, message: FakeMessage) -> bool:
        self.position = 0
        result = self._or(message)
        assert self.position == len(self.tokens), f"requête mal formée : {self.tokens}"
        return result

    def _next(self, expected: str | None = None) -> str:
        token = self.tokens[self.position]
        assert expected is None or token == expected, f"{expected} attendu : {self.tokens}"
        self.position += 1
        return token

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _or(self, message: FakeMessage) -> bool:
        result = self._and(message)
        while self._peek() == "or":
            self._next()
            result = self._and(message) or result
        return result

    def _and(self, message: FakeMessage) -> bool:
        result = self._unary(message)
        while self._peek() == "and":
            self._next()
            result = self._unary(message) and result
        return result

    def _unary(self, message: FakeMessage) -> bool:
        token = self._next()
        if token == "not":
            return not self._unary(message)
        if token == "(":
            result = self._or(message)
            self._next(")")
            return result
        return self._term(token, message)

    @staticmethod
    def _term(token: str, message: FakeMessage) -> bool:
        name, _, value = token.partition(":")
        quoted = value.startswith('"')
        if quoted:
            value = re.sub(r"\\(.)", r"\1", value[1:-1])
        value = value.lower()
        if name == "inid":
            return message.folder_id == value
        # after: et before: excluent le jour donné
        if name == "after":
            return message.date_ms >= _server_day_ms(value) + int(timedelta(days=1).total_seconds() * 1000)
        if name == "before":
            return message.date_ms < _server_day_ms(value)
        if name == "has" and value == "attachment":
            return message.has_attachment
        fields = {"subject": message.subject, "from": message.sender, "tocc": message.recipients}
        indexed = words(fields[name])
        if quoted:
            # Expression : suite de mots consécutifs
            return " ".join(re.split(r"[^\w]+", value)) in " ".join(re.split(r"[^\w]+", fields[name].lower()))
        if value.endswith("*"):
            return any(word.startswith(value[:-1]) for word in indexed)
        return value in indexed


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
                {
                    "id": "257",
                    "name": "Projets: 2024",
                    "absFolderPath": "/Inbox/Projets: 2024",
                    "view": "message",
                    "n": 1,
                    "color": 1,
                    "rgb": "#12ab9f",
                },
            ],
        },
        {"id": "5", "name": "Sent", "absFolderPath": "/Sent", "view": "message", "n": 0},
        {"id": "3", "name": "Trash", "absFolderPath": "/Trash", "view": "message", "n": 1},
        {"id": "7", "name": "Contacts", "absFolderPath": "/Contacts", "view": "contact", "n": 0},
    ],
    "link": [{"id": "300", "name": "Partage", "absFolderPath": "/Partage"}],
    "search": [{"id": "301", "name": "Recherche", "absFolderPath": "/Recherche"}],
}
