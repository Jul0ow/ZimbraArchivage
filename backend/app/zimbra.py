"""Client pour l'API SOAP (en JSON) et l'API ReST de Zimbra.

Reprend les échanges du projet Archivage (ZimbraService) :
AuthRequest, GetFolderRequest, SearchRequest et téléchargement des .eml en ReST.
AutoCompleteRequest sert en plus aux suggestions d'adresses des filtres,
et EndSessionRequest à fermer la session Zimbra à la déconnexion.
"""

import logging
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Codes HTTP pour lesquels on considère le serveur Zimbra momentanément indisponible
RETRY_STATUS_CODES = {502, 503, 504}
# Nouvelles tentatives après un premier échec, soit MAX_RETRIES + 1 envois au plus
MAX_RETRIES = 5
# Pause avant la n-ième nouvelle tentative : 1, 2, 4, 8 puis 16 s, plafonnée (Retry-After compris)
RETRY_BASE_DELAY = 1.0
MAX_RETRY_DELAY = 30.0

# Locale des recherches : interprétation des dates des critères after: et before:
SEARCH_LOCALE = "fr_FR"

# Identifiants des dossiers système de Zimbra
ROOT_FOLDER_ID = "1"
INBOX_FOLDER_ID = "2"
TRASH_FOLDER_ID = "3"
JUNK_FOLDER_ID = "4"
# Exclus d'un export de toute la boîte, comme dans la recherche Zimbra
EXCLUDED_FROM_MAILBOX = frozenset({TRASH_FOLDER_ID, JUNK_FOLDER_ID})

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
    ROOT_FOLDER_ID: "Boîte aux lettres",
    INBOX_FOLDER_ID: "Réception",
    TRASH_FOLDER_ID: "Corbeille",
    JUNK_FOLDER_ID: "Spam",
    "5": "Envoyé",
    "6": "Brouillons",
    "7": "Contacts",
    "10": "Calendrier",
    "14": "Chats",
    "15": "Tâches",
}

SESSION_CODES = {"service.AUTH_REQUIRED", "service.AUTH_EXPIRED"}


class ZimbraError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


class AuthenticationFailed(ZimbraError):
    pass


class SessionExpired(ZimbraError):
    pass


class ZimbraUnavailable(ZimbraError):
    """Zimbra injoignable ou indisponible, malgré les nouvelles tentatives."""


class UnexpectedResponse(ZimbraError):
    def __init__(self, status_code: int | None = None):
        detail = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"Réponse inattendue de Zimbra{detail}")


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
    # Nombre de messages du dossier, None s'il n'est pas fourni par Zimbra
    message_count: int | None = None
    # Couleur du dossier au format #RRGGBB, None pour la couleur par défaut
    color: str | None = None
    children: list["Folder"] = field(default_factory=list)

    @property
    def human_name(self) -> str:
        return HUMAN_NAMES.get(self.id, self.name)

    @property
    def is_mail_folder(self) -> bool:
        return self.view in MAIL_VIEWS

    def walk(self, excluded: frozenset[str] = frozenset()) -> Iterator["Folder"]:
        """Le dossier et tous ses sous-dossiers, sans les branches dont l'id est exclu."""
        yield self
        for child in self.children:
            if child.id not in excluded:
                yield from child.walk(excluded)

    def find(self, folder_id: str) -> "Folder | None":
        return next((f for f in self.walk() if f.id == folder_id), None)


@dataclass(frozen=True)
class MessageSummary:
    id: str
    folder_id: str
    date_ms: int
    size: int
    subject: str
    # Drapeaux Zimbra : "a" signale un message avec pièces jointes
    flags: str = ""

    @property
    def has_attachment(self) -> bool:
        return "a" in self.flags


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
        message_count=int(data["n"]) if "n" in data else None,
        color=_folder_color(data),
        children=[_parse_folder(child) for child in data.get("folder", [])],
    )


def _parse_message(data: dict[str, Any]) -> MessageSummary:
    return MessageSummary(
        id=str(data["id"]),
        folder_id=str(data.get("l", "")),
        date_ms=int(data.get("d", 0)),
        size=int(data.get("s", 0)),
        subject=str(data.get("su", "")),
        flags=str(data.get("f", "")),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Cookie": f"ZM_AUTH_TOKEN={token}"}


def _retry_delay(attempt: int, response: httpx.Response | None) -> float:
    delay = RETRY_BASE_DELAY * 2 ** (attempt - 1)
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after and retry_after.isdigit():
        delay = float(retry_after)
    return min(delay, MAX_RETRY_DELAY)


def create_http_client(settings: Settings, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Client HTTP partagé : les connexions TLS vers Zimbra sont réutilisées d'un appel à l'autre."""
    return httpx.Client(
        # Un serveur injoignable est détecté vite, sans attendre le délai de lecture
        timeout=httpx.Timeout(settings.http_timeout, connect=10.0, pool=30.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        verify=settings.verify_tls,
        transport=transport,
    )


class ZimbraClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        http: httpx.Client | None = None,
    ):
        self._settings = settings
        self._sleep = sleep
        # Un client HTTP fourni est partagé : il n'est pas fermé avec ce client Zimbra
        self._owns_http = http is None
        self._http = http or create_http_client(settings, transport)

    def __enter__(self) -> "ZimbraClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _send(self, request: httpx.Request, stream: bool = False) -> httpx.Response:
        """Envoie la requête, en la retentant si Zimbra est injoignable ou répond 502, 503 ou 504."""
        response: httpx.Response | None = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                delay = _retry_delay(attempt, response)
                logger.warning(
                    "Zimbra indisponible (%s), nouvel essai dans %.0f s",
                    response.status_code if response is not None else "connexion",
                    delay,
                )
                self._sleep(delay)
            try:
                response = self._http.send(request, stream=stream)
            except httpx.TransportError as e:
                logger.warning("Erreur de connexion avec Zimbra : %s", e)
                response = None
                continue
            if response.status_code not in RETRY_STATUS_CODES:
                return response
            response.close()
        if response is None:
            raise ZimbraUnavailable("Serveur Zimbra injoignable")
        raise ZimbraUnavailable(f"Zimbra indisponible malgré {MAX_RETRIES + 1} essais")

    def _soap(
        self,
        request_name: str,
        namespace: str,
        body: dict[str, Any] | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "Header": {"context": {"_jsns": "urn:zimbra"}},
            "Body": {request_name: {"_jsns": namespace, **(body or {})}},
        }
        all_headers = {**(_auth_headers(token) if token else {}), **(headers or {})}
        request = self._http.build_request(
            "POST", self._settings.zimbra_soap_url, json=payload, headers=all_headers
        )
        response = self._send(request)
        try:
            data = response.json()
        except ValueError as e:
            raise UnexpectedResponse(response.status_code) from e
        response_body = data.get("Body") if isinstance(data, dict) else None
        if not isinstance(response_body, dict):
            raise UnexpectedResponse(response.status_code)

        fault = response_body.get("Fault")
        if isinstance(fault, dict):
            reason = _content((fault.get("Reason") or {}).get("Text")) or "Erreur Zimbra"
            code = _content(((fault.get("Detail") or {}).get("Error") or {}).get("Code"))
            if code in SESSION_CODES:
                raise SessionExpired(reason, code)
            raise ZimbraError(reason, code or None)
        response_name = request_name.removesuffix("Request") + "Response"
        result = response_body.get(response_name)
        if response.status_code != 200 or not isinstance(result, dict):
            raise UnexpectedResponse(response.status_code)
        return result

    def login(self, username: str, password: str, client_ip: str | None = None) -> AuthToken:
        body = {
            "account": {"by": "name", "_content": username},
            "password": {"_content": password},
        }
        # Adresse réelle de l'utilisateur, prise en compte si le backend est une IP de confiance
        # de Zimbra (zimbraMailTrustedIP) : journaux et blocages par IP de Zimbra restent justes
        headers = {"X-Originating-IP": client_ip} if client_ip else None
        try:
            response = self._soap("AuthRequest", "urn:zimbraAccount", body, headers=headers)
        except ZimbraError as e:
            if e.code == "account.AUTH_FAILED":
                raise AuthenticationFailed(e.message, e.code) from e
            raise
        token = _content(response.get("authToken"))
        if not token:
            raise UnexpectedResponse()
        try:
            lifetime_ms = int(_content(response.get("lifetime")) or 0)
        except ValueError:
            lifetime_ms = 0
        return AuthToken(value=token, lifetime_ms=lifetime_ms)

    def end_session(self, token: str) -> None:
        """Ferme la session Zimbra : le jeton n'est plus utilisable, ni ici ni sur Zimbra."""
        self._soap("EndSessionRequest", "urn:zimbraAccount", {"logoff": "1"}, token)

    def get_account_name(self, token: str) -> str:
        response = self._soap("GetInfoRequest", "urn:zimbraAccount", {"sections": "mbox"}, token)
        return _content(response.get("name"))

    def get_root_folder(self, token: str) -> Folder:
        """Récupère le dossier racine de la boîte, qui contient tous les autres dossiers."""
        response = self._soap("GetFolderRequest", "urn:zimbraMail", token=token)
        try:
            return _parse_folder(response["folder"][0])
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise UnexpectedResponse() from e

    def _search(self, token: str, query: str, limit: int, **extra: Any) -> dict[str, Any]:
        body = {
            "types": "message",
            "limit": limit,
            "query": query,
            "locale": {"_content": SEARCH_LOCALE},
            **extra,
        }
        return self._soap("SearchRequest", "urn:zimbraMail", body, token)

    def search_messages(
        self, token: str, query: str, limit: int, after: dict[str, str] | None = None
    ) -> tuple[list[MessageSummary], bool]:
        """Une page de résultats, du plus ancien au plus récent.

        after est le curseur du dernier message de la page précédente : contrairement à un
        décalage, il reste juste si des messages arrivent ou sont déplacés entre deux pages.
        """
        extra: dict[str, Any] = {"sortBy": "dateAsc"}
        if after:
            extra["cursor"] = after
        response = self._search(token, query, limit, **extra)
        raw = response.get("m", [])
        try:
            messages = [_parse_message(m) for m in raw]
        except (KeyError, TypeError, ValueError) as e:
            raise UnexpectedResponse() from e
        return messages, bool(response.get("more", False))

    def search_addresses(self, token: str, query: str, limit: int) -> list[MessageAddress]:
        """Expéditeurs et destinataires des messages les plus récents trouvés par la recherche."""
        # recip 2 : adresses de l'expéditeur et des destinataires dans chaque résultat
        response = self._search(token, query, limit, recip="2")
        return [
            MessageAddress(e.get("t", ""), EmailAddress(e["a"], e.get("p", "")))
            for m in response.get("m", [])
            for e in m.get("e", [])
            if e.get("a")
        ]

    def autocomplete(self, token: str, text: str) -> list[EmailAddress]:
        """Adresses des contacts, de la liste globale (GAL) et des correspondants habituels."""
        response = self._soap("AutoCompleteRequest", "urn:zimbraMail", {"name": text, "includeGal": 1}, token)
        addresses = []
        for match in response.get("match", []):
            name, address = parseaddr(match.get("email", ""))
            # Les groupes de contacts n'ont pas d'adresse propre
            if "@" in address:
                addresses.append(EmailAddress(address, name or match.get("full", "")))
        return addresses

    def iter_messages(self, token: str, query: str) -> Iterator[MessageSummary]:
        """Parcourt tous les résultats d'une recherche, page par page, sans doublon."""
        batch_size = self._settings.search_batch_size
        seen: set[str] = set()
        cursor: dict[str, str] | None = None
        while True:
            messages, more = self.search_messages(token, query, batch_size, cursor)
            for message in messages:
                if message.id not in seen:
                    seen.add(message.id)
                    yield message
            if not more or not messages:
                return
            last = messages[-1]
            cursor = {"id": last.id, "sortVal": str(last.date_ms)}

    @contextmanager
    def download_eml(self, token: str, message_id: str) -> Iterator[Iterator[bytes]]:
        """Télécharge un courriel au format .eml via l'API ReST."""
        request = self._http.build_request(
            "GET",
            f"{self._settings.zimbra_rest_url}/home/~/",
            params={"id": message_id},
            headers={**_auth_headers(token), "Accept-Encoding": "gzip"},
        )
        response = self._send(request, stream=True)
        try:
            if response.status_code in (401, 403):
                raise SessionExpired("Session Zimbra expirée", "service.AUTH_EXPIRED")
            if response.status_code != 200:
                raise ZimbraError(
                    f"Erreur lors du téléchargement du message {message_id}, "
                    f"code HTTP retourné = {response.status_code}"
                )
            yield response.iter_bytes()
        finally:
            response.close()
