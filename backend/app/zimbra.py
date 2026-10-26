"""Client pour l'API SOAP (en JSON) et l'API ReST de Zimbra.

Reprend les échanges du projet Archivage (ZimbraService) :
AuthRequest, GetFolderRequest, SearchRequest et téléchargement des .eml en ReST.
AutoCompleteRequest sert en plus aux suggestions d'adresses des filtres.
"""

import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

import httpx

from app.config import Settings

# Codes HTTP pour lesquels on considère le serveur Zimbra momentanément indisponible
RETRY_STATUS_CODES = {502, 503, 504}
MAX_TRY = 5

# Vues Zimbra des dossiers contenant des courriels (les dossiers sans vue sont des dossiers de mails)
MAIL_VIEWS = {None, "", "message", "conversation"}

# Palette des couleurs prédéfinies de Zimbra, indexée par l'attribut "color" d'un dossier
# (0 = couleur par défaut) : bleu, cyan, vert, violet, rouge, jaune, rose, gris, orange
ZIMBRA_COLORS = [
    None,
    "#5B9BF2",
    "#43EDED",
    "#6ACB9E",
    "#BA86E5",
    "#F66666",
    "#F8FA33",
    "#FE98D3",
    "#BABABA",
    "#FF9B27",
]

RGB_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")

HUMAN_NAMES = {
    "1": "Boîte aux lettres",
    "2": "Réception",
    "3": "Corbeille",
    "4": "Spam",
    "5": "Envoyé",
    "6": "Brouillons",
    "7": "Contacts",
    "10": "Calendrier",
    "14": "Chats",
    "15": "Tâches",
}


class ZimbraError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


class AuthenticationFailed(ZimbraError):
    pass


class SessionExpired(ZimbraError):
    pass


@dataclass(frozen=True)
class AuthToken:
    value: str
    lifetime_ms: int


@dataclass
class Folder:
    id: str
    name: str
    abs_folder_path: str
    view: str | None = None
    message_count: int = 0
    # Couleur du dossier au format #RRGGBB, None pour la couleur par défaut
    color: str | None = None
    children: list["Folder"] = field(default_factory=list)

    @property
    def human_name(self) -> str:
        return HUMAN_NAMES.get(self.id, self.name)

    @property
    def is_mail_folder(self) -> bool:
        return self.view in MAIL_VIEWS

    def walk(self) -> Iterator["Folder"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find(self, folder_id: str) -> "Folder | None":
        return next((f for f in self.walk() if f.id == folder_id), None)


@dataclass(frozen=True)
class MessageSummary:
    id: str
    folder_id: str
    date_ms: int
    size: int
    subject: str


@dataclass(frozen=True)
class EmailAddress:
    address: str
    name: str = ""


@dataclass(frozen=True)
class MessageAddress:
    """Adresse d'un message : type "f" (expéditeur), "t" (À) ou "c" (Cc)."""

    type: str
    address: EmailAddress


def _content(value: Any) -> str:
    """En JSON, Zimbra renvoie un élément texte sous la forme [{"_content": ...}] ou {"_content": ...}."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("_content", "")
    return "" if value is None else str(value)


def _folder_color(data: dict[str, Any]) -> str | None:
    """Couleur personnalisée (attribut rgb) en priorité, sinon couleur prédéfinie (attribut color)."""
    rgb = data.get("rgb")
    if isinstance(rgb, str) and RGB_PATTERN.match(rgb):
        return rgb.upper()
    try:
        index = int(data.get("color", 0))
    except (TypeError, ValueError):
        return None
    return ZIMBRA_COLORS[index] if 0 <= index < len(ZIMBRA_COLORS) else None


def _parse_folder(data: dict[str, Any]) -> Folder:
    # Les balises link (partages montés) et search (dossiers de recherche) sont ignorées
    return Folder(
        id=str(data["id"]),
        name=data.get("name", ""),
        abs_folder_path=data.get("absFolderPath", "/"),
        view=data.get("view"),
        message_count=int(data.get("n", 0)),
        color=_folder_color(data),
        children=[_parse_folder(child) for child in data.get("folder", [])],
    )


class ZimbraClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._settings = settings
        self._sleep = sleep
        self._http = httpx.Client(
            timeout=settings.http_timeout,
            verify=settings.verify_tls,
            transport=transport,
        )

    def __enter__(self) -> "ZimbraClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _send(self, request: httpx.Request, stream: bool = False) -> httpx.Response:
        """Envoie la requête en la retentant tant que Zimbra répond 502, 503 ou 504."""
        for attempt in range(MAX_TRY + 1):
            if attempt > 0:
                self._sleep(3**attempt)
            try:
                response = self._http.send(request, stream=stream)
            except httpx.HTTPError as e:
                raise ZimbraError(f"Erreur de connexion avec Zimbra : {e}") from e
            if response.status_code not in RETRY_STATUS_CODES:
                return response
            response.close()
        raise ZimbraError(f"Zimbra indisponible malgré {MAX_TRY} essais")

    def _soap(
        self,
        request_name: str,
        namespace: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "Header": {"context": {"_jsns": "urn:zimbra"}},
            "Body": {request_name: {"_jsns": namespace, **(body or {})}},
        }
        headers = {"Cookie": f"ZM_AUTH_TOKEN={token}"} if token else {}
        request = self._http.build_request(
            "POST", self._settings.zimbra_soap_url, json=payload, headers=headers
        )
        response = self._send(request)
        try:
            data = response.json()
        except ValueError as e:
            raise ZimbraError(
                f"Réponse inattendue de Zimbra (HTTP {response.status_code})"
            ) from e

        response_body = data.get("Body", {})
        fault = response_body.get("Fault")
        if fault:
            reason = _content(fault.get("Reason", {}).get("Text")) or "Erreur Zimbra"
            code = _content(fault.get("Detail", {}).get("Error", {}).get("Code"))
            if code in ("service.AUTH_REQUIRED", "service.AUTH_EXPIRED"):
                raise SessionExpired(reason, code)
            raise ZimbraError(reason, code)
        response_name = request_name.removesuffix("Request") + "Response"
        if response.status_code != 200 or response_name not in response_body:
            raise ZimbraError(f"Réponse inattendue de Zimbra (HTTP {response.status_code})")
        return response_body[response_name]

    def login(self, username: str, password: str) -> AuthToken:
        body = {
            "account": {"by": "name", "_content": username},
            "password": {"_content": password},
        }
        try:
            response = self._soap("AuthRequest", "urn:zimbraAccount", body)
        except ZimbraError as e:
            if e.code == "account.AUTH_FAILED":
                raise AuthenticationFailed(e.message, e.code) from e
            raise
        return AuthToken(
            value=_content(response.get("authToken")),
            lifetime_ms=int(_content(response.get("lifetime")) or 0),
        )

    def get_account_name(self, token: str) -> str:
        response = self._soap("GetInfoRequest", "urn:zimbraAccount", {"sections": "mbox"}, token)
        return _content(response.get("name"))

    def get_root_folder(self, token: str) -> Folder:
        """Récupère le dossier racine de la boîte, qui contient tous les autres dossiers."""
        response = self._soap("GetFolderRequest", "urn:zimbraMail", token=token)
        return _parse_folder(response["folder"][0])

    def search_messages(
        self, token: str, query: str, offset: int, limit: int
    ) -> tuple[list[MessageSummary], bool]:
        body = {
            "types": "message",
            "limit": limit,
            "offset": offset,
            "query": query,
            "locale": {"_content": "fr_FR"},
        }
        response = self._soap("SearchRequest", "urn:zimbraMail", body, token)
        messages = [
            MessageSummary(
                id=str(m["id"]),
                folder_id=str(m.get("l", "")),
                date_ms=int(m.get("d", 0)),
                size=int(m.get("s", 0)),
                subject=m.get("su", ""),
            )
            for m in response.get("m", [])
        ]
        return messages, bool(response.get("more", False))

    def search_addresses(self, token: str, query: str, limit: int) -> list[MessageAddress]:
        """Expéditeurs et destinataires des messages les plus récents trouvés par la recherche."""
        body = {
            "types": "message",
            "limit": limit,
            "query": query,
            # 2 : adresses de l'expéditeur et des destinataires dans chaque résultat
            "recip": "2",
            "locale": {"_content": "fr_FR"},
        }
        response = self._soap("SearchRequest", "urn:zimbraMail", body, token)
        return [
            MessageAddress(e.get("t", ""), EmailAddress(e["a"], e.get("p", "")))
            for m in response.get("m", [])
            for e in m.get("e", [])
            if e.get("a")
        ]

    def autocomplete(self, token: str, text: str) -> list[EmailAddress]:
        """Adresses des contacts, de la liste globale (GAL) et des correspondants habituels."""
        response = self._soap(
            "AutoCompleteRequest", "urn:zimbraMail", {"name": text, "includeGal": 1}, token
        )
        addresses = []
        for match in response.get("match", []):
            name, address = parseaddr(match.get("email", ""))
            # Les groupes de contacts n'ont pas d'adresse propre
            if "@" in address:
                addresses.append(EmailAddress(address, name or match.get("full", "")))
        return addresses

    def iter_messages(self, token: str, query: str) -> Iterator[MessageSummary]:
        """Parcourt tous les résultats d'une recherche, page par page."""
        batch_size = self._settings.search_batch_size
        offset = 0
        while True:
            messages, more = self.search_messages(token, query, offset, batch_size)
            yield from messages
            if not more:
                return
            offset += batch_size

    @contextmanager
    def download_eml(self, token: str, message_id: str) -> Iterator[Iterator[bytes]]:
        """Télécharge un courriel au format .eml via l'API ReST."""
        request = self._http.build_request(
            "GET",
            f"{self._settings.zimbra_rest_url}/home/~/",
            params={"id": message_id},
            headers={"Cookie": f"ZM_AUTH_TOKEN={token}", "Accept-Encoding": "gzip"},
        )
        response = self._send(request, stream=True)
        try:
            if response.status_code != 200:
                raise ZimbraError(
                    f"Erreur lors du téléchargement du message {message_id}, "
                    f"code HTTP retourné = {response.status_code}"
                )
            yield response.iter_bytes()
        finally:
            response.close()
