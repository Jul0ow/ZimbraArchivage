"""API web : connexion Zimbra, liste des dossiers, suggestions d'adresses et export ZIP d'un dossier."""

import logging
import os
import re
import unicodedata
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.archive import DateRange, ExportObserver, ExportRequest, stream_zip, summarize
from app.config import Settings, get_settings
from app.filters import MessageFilter
from app.limits import ExportLimiter, ExportSlot, LoginThrottle, TooManyExports
from app.progress import ExportProgress, ProgressRegistry, get_progress_registry
from app.sessions import SessionCipher, get_session_cipher
from app.suggestions import AddressField, suggest_addresses
from app.zimbra import (
    EXCLUDED_FROM_MAILBOX,
    ROOT_FOLDER_ID,
    AuthenticationFailed,
    Folder,
    SessionExpired,
    UnexpectedResponse,
    ZimbraClient,
    ZimbraError,
    ZimbraUnavailable,
    create_http_client,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
logger = logging.getLogger(__name__)

# Cookie de session contenant le jeton d'authentification Zimbra, chiffré
COOKIE_NAME = "zimbra_session"
COOKIE_PATH = "/api"

# Nombre maximal d'adresses suggérées dans les filtres
MAX_ADDRESS_SUGGESTIONS = 10

MAILBOX_NAME = "Toute la boîte aux lettres"

# Clients HTTP partagés, un par configuration (une seule en dehors des tests)
_http_clients: dict[int, httpx.Client] = {}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    for http in _http_clients.values():
        http.close()
    _http_clients.clear()


# Documentation OpenAPI désactivée par défaut : elle décrit toute la surface de l'API
_docs = os.environ.get("ENABLE_DOCS", "").lower() in ("1", "true", "yes")
app = FastAPI(
    title="Archivage Zimbra",
    docs_url="/api/docs" if _docs else None,
    redoc_url=None,
    openapi_url="/api/openapi.json" if _docs else None,
    lifespan=lifespan,
)

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


class SecurityHeadersMiddleware:
    """En-têtes de sécurité sur toutes les réponses de l'API (sauf la documentation)."""

    def __init__(self, inner: ASGIApp):
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith("/api/docs"):
            await self.inner(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []), *SECURITY_HEADERS]
            await send(message)

        await self.inner(scope, receive, send_with_headers)


app.add_middleware(SecurityHeadersMiddleware)

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_client_factory(settings: SettingsDep) -> Callable[[], ZimbraClient]:
    http = _http_clients.get(id(settings))
    if http is None:
        http = _http_clients.setdefault(id(settings), create_http_client(settings))
    return lambda: ZimbraClient(settings, http=http)


ClientFactoryDep = Annotated[Callable[[], ZimbraClient], Depends(get_client_factory)]


def get_cipher(settings: SettingsDep) -> SessionCipher:
    return get_session_cipher(settings.session_secret, settings.session_max_age)


CipherDep = Annotated[SessionCipher, Depends(get_cipher)]

_export_limiters: dict[int, ExportLimiter] = {}
_login_throttles: dict[int, LoginThrottle] = {}


def get_export_limiter(settings: SettingsDep) -> ExportLimiter:
    return _export_limiters.setdefault(
        id(settings),
        ExportLimiter(settings.max_concurrent_exports, settings.max_exports_per_session),
    )


def get_login_throttle(settings: SettingsDep) -> LoginThrottle:
    return _login_throttles.setdefault(
        id(settings),
        LoginThrottle(settings.max_login_failures_per_user, settings.max_login_failures_per_ip),
    )


ExportLimiterDep = Annotated[ExportLimiter, Depends(get_export_limiter)]
LoginThrottleDep = Annotated[LoginThrottle, Depends(get_login_throttle)]


def get_token(request: Request, cipher: CipherDep) -> str:
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non connecté")
    token = cipher.open(cookie)
    if token is None:
        raise SessionExpired("Session invalide ou trop ancienne")
    return token


TokenDep = Annotated[str, Depends(get_token)]

ProgressRegistryDep = Annotated[ProgressRegistry, Depends(get_progress_registry)]

# Identifiant d'un export choisi par l'interface pour en suivre l'avancement
ExportId = Annotated[str, Query(pattern=r"^[A-Za-z0-9-]{8,64}$")]


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)


@app.exception_handler(SessionExpired)
def session_expired_handler(request: Request, exc: SessionExpired) -> JSONResponse:
    response = JSONResponse({"detail": "Session expirée"}, status.HTTP_401_UNAUTHORIZED)
    clear_session_cookie(response)
    return response


@app.exception_handler(ZimbraError)
def zimbra_error_handler(request: Request, exc: ZimbraError) -> JSONResponse:
    logger.warning("Erreur Zimbra sur %s : %s (%s)", request.url.path, exc.message, exc.code)
    # Seuls les messages rédigés par l'application sont relayés : la raison d'une faute SOAP
    # peut décrire l'infrastructure interne de Zimbra
    if isinstance(exc, ZimbraUnavailable | UnexpectedResponse):
        detail = exc.message
    else:
        detail = f"code {exc.code}" if exc.code else "requête refusée"
    return JSONResponse({"detail": f"Erreur Zimbra : {detail}"}, status.HTTP_502_BAD_GATEWAY)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    username: str


class FolderResponse(BaseModel):
    id: str
    name: str
    path: str
    message_count: int
    color: str | None
    children: list["FolderResponse"]


class AddressResponse(BaseModel):
    address: str
    name: str


class ProgressResponse(BaseModel):
    # Nombre de courriels traités (ajoutés à l'archive ou en erreur)
    done: int
    # Nombre de courriels à traiter, null tant que les dossiers sont parcourus
    total: int | None
    finished: bool
    # Raison d'un export interrompu avant la fin
    error: str | None


class SummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    folder_count: int
    message_count: int
    total_size: int


def to_folder_response(folder: Folder) -> FolderResponse:
    return FolderResponse(
        id=folder.id,
        name=folder.human_name,
        path=folder.abs_folder_path,
        message_count=folder.message_count or 0,
        color=folder.color,
        children=[to_folder_response(c) for c in folder.children if c.is_mail_folder],
    )


def mailbox_response(root: Folder) -> FolderResponse:
    """Entrée « Toute la boîte aux lettres » : mêmes dossiers que l'export (hors Corbeille et Spam)."""
    count = sum(f.message_count or 0 for f in root.walk(EXCLUDED_FROM_MAILBOX) if f.is_mail_folder)
    return FolderResponse(
        id=ROOT_FOLDER_ID, name=MAILBOX_NAME, path="/", message_count=count, color=None, children=[]
    )


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@app.get("/api/health")
async def health() -> dict[str, str]:
    # async : répond depuis la boucle d'évènements, même quand le pool de threads est occupé
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(
    credentials: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
    cipher: CipherDep,
    throttle: LoginThrottleDep,
) -> UserResponse:
    ip = client_ip(request)
    if throttle.is_blocked(credentials.username, ip):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Trop de tentatives de connexion : réessayez dans quelques minutes",
        )
    with client_factory() as client:
        try:
            token = client.login(credentials.username, credentials.password, ip)
        except AuthenticationFailed:
            throttle.record_failure(credentials.username, ip)
            logger.info("Connexion refusée depuis %s", ip)
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Identifiant ou mot de passe incorrect"
            ) from None
    throttle.reset(credentials.username)
    lifetime = token.lifetime_ms // 1000
    response.set_cookie(
        COOKIE_NAME,
        cipher.seal(token.value),
        max_age=min(lifetime, settings.session_max_age) if lifetime else settings.session_max_age,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        # Aucun lien entrant n'a besoin de la session : elle n'est jamais envoyée cross-site
        samesite="strict",
    )
    return UserResponse(username=credentials.username)


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, response: Response, client_factory: ClientFactoryDep, cipher: CipherDep) -> None:
    cookie = request.cookies.get(COOKIE_NAME)
    token = cipher.open(cookie) if cookie else None
    if token:
        with client_factory() as client:
            try:
                client.end_session(token)
            except ZimbraError as e:
                # Jeton déjà expiré, ou Zimbra indisponible : le cookie est supprimé quand même
                logger.info("Fermeture de la session Zimbra impossible : %s", e.message)
    clear_session_cookie(response)


@app.get("/api/auth/me")
def me(token: TokenDep, client_factory: ClientFactoryDep) -> UserResponse:
    with client_factory() as client:
        return UserResponse(username=client.get_account_name(token))


@app.get("/api/folders")
def folders(token: TokenDep, client_factory: ClientFactoryDep) -> list[FolderResponse]:
    """Arborescence des dossiers de mails, précédée de l'entrée « Toute la boîte aux lettres »."""
    with client_factory() as client:
        root = client.get_root_folder(token)
    return [mailbox_response(root), *to_folder_response(root).children]


@app.get("/api/addresses")
def addresses(
    token: TokenDep,
    client_factory: ClientFactoryDep,
    q: Annotated[str, Query(min_length=2, max_length=100, description="Début du nom ou de l'adresse")],
    field: AddressField = "from",
) -> list[AddressResponse]:
    """Adresses suggérées pour un filtre sur l'expéditeur ou le destinataire."""
    with client_factory() as client:
        found = suggest_addresses(client, token, q, field, MAX_ADDRESS_SUGGESTIONS)
    return [AddressResponse(address=a.address, name=a.name) for a in found]


@dataclass(frozen=True)
class ExportParams:
    """Paramètres communs à l'estimation et à l'export."""

    folder_id: str
    include_subfolders: bool
    start: date | None
    end: date | None
    message_filter: MessageFilter | None
    only_with_attachments: bool


def export_params(
    folder_id: Annotated[
        str, Query(min_length=1, max_length=100, description="Dossier, 1 pour toute la boîte")
    ],
    include_subfolders: bool = True,
    start: date | None = None,
    end: date | None = None,
    filter_json: Annotated[
        str | None,
        Query(
            alias="filter",
            max_length=20000,
            description="Filtre au format JSON (voir MessageFilter dans app/filters.py)",
        ),
    ] = None,
    only_with_attachments: bool = False,
) -> ExportParams:
    if start and end and start > end:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "La date de début doit précéder la date de fin",
        )
    message_filter = None
    if filter_json:
        try:
            message_filter = MessageFilter.model_validate_json(filter_json)
        except ValidationError as e:
            first = e.errors()[0]
            location = ".".join(str(part) for part in first["loc"])
            detail = (
                f"Filtre invalide ({location} : {first['msg']})"
                if location
                else (f"Filtre invalide ({first['msg']})")
            )
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from None
    return ExportParams(folder_id, include_subfolders, start, end, message_filter, only_with_attachments)


ExportParamsDep = Annotated[ExportParams, Depends(export_params)]


def build_export_request(
    root: Folder,
    params: ExportParams,
    settings: Settings,
    extract_attachments: bool = False,
) -> ExportRequest:
    folder = root.find(params.folder_id)
    if folder is None or not folder.is_mail_folder:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dossier introuvable")
    return ExportRequest(
        folder,
        params.include_subfolders,
        DateRange(params.start, params.end, settings.zone),
        message_filter=params.message_filter,
        only_with_attachments=params.only_with_attachments,
        extract_attachments=extract_attachments,
    )


def acquire_export_slot(limiter: ExportLimiter, token: str) -> ExportSlot:
    try:
        return limiter.acquire(token)
    except TooManyExports:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Trop d'exports en cours : attendez la fin d'un téléchargement puis réessayez",
        ) from None


def same_origin_navigation(request: Request) -> None:
    """L'export est un GET : on refuse qu'un autre site le déclenche chez un utilisateur connecté."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site not in ("same-origin", "none"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Export lancé depuis un autre site")


@app.get("/api/export/summary")
def export_summary(
    token: TokenDep,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
    params: ExportParamsDep,
    limiter: ExportLimiterDep,
) -> SummaryResponse:
    slot = acquire_export_slot(limiter, token)
    try:
        with client_factory() as client:
            root = client.get_root_folder(token)
            export = build_export_request(root, params, settings)
            summary = summarize(client, token, export)
    finally:
        slot.release()
    return SummaryResponse.model_validate(summary)


@app.get("/api/export/progress")
def export_progress(
    token: TokenDep, progress_registry: ProgressRegistryDep, export_id: ExportId
) -> ProgressResponse:
    progress = progress_registry.get(token, export_id)
    if progress is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export introuvable")
    return ProgressResponse(
        done=progress.done, total=progress.total, finished=progress.finished, error=progress.error
    )


def archive_file_name(folder: Folder, settings: Settings) -> str:
    """Nom du fichier ZIP, en ASCII pour être interprété de la même façon par tous les navigateurs."""
    today = datetime.now(settings.zone)
    ascii_name = unicodedata.normalize("NFKD", folder.human_name).encode("ascii", "ignore").decode()
    base_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._") or "dossier"
    return f"{base_name}_{today:%Y%m%d}.zip"


class _ProgressObserver(ExportObserver):
    def __init__(self, registry: ProgressRegistry, progress: ExportProgress | None):
        self._registry = registry
        self._progress = progress

    def on_total(self, total: int) -> None:
        if self._progress:
            self._registry.set_total(self._progress, total)

    def on_message(self) -> None:
        if self._progress:
            self._registry.advance(self._progress)

    def on_abort(self, reason: str) -> None:
        if self._progress:
            self._registry.finish(self._progress, reason)


@app.get("/api/export", dependencies=[Depends(same_origin_navigation)])
def export(
    token: TokenDep,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
    params: ExportParamsDep,
    progress_registry: ProgressRegistryDep,
    limiter: ExportLimiterDep,
    extract_attachments: bool = False,
    export_id: ExportId | None = None,
) -> StreamingResponse:
    slot = acquire_export_slot(limiter, token)
    client = client_factory()
    try:
        # Vérifications faites avant de commencer à envoyer l'archive, pour pouvoir
        # encore répondre avec un code d'erreur HTTP
        root = client.get_root_folder(token)
        export_request = build_export_request(root, params, settings, extract_attachments)
    except BaseException:
        client.close()
        slot.release()
        raise

    # Enregistré avant l'envoi, pour que l'interface trouve l'export dès son lancement
    progress = progress_registry.start(token, export_id) if export_id else None

    def cleanup() -> None:
        # Archive complète, téléchargement interrompu, ou réponse jamais envoyée
        client.close()
        slot.release()
        if progress:
            progress_registry.finish(progress)

    def content() -> Iterator[bytes]:
        try:
            yield from stream_zip(
                client,
                token,
                export_request,
                settings.zone,
                settings.max_file_name_length,
                download_concurrency=settings.download_concurrency,
                max_attachment_extraction_size=settings.max_attachment_extraction_size,
                observer=_ProgressObserver(progress_registry, progress),
            )
        finally:
            cleanup()

    body = content()
    # Un générateur jamais démarré n'exécute pas son finally : la place est alors libérée
    # quand il est détruit (client déconnecté avant le premier octet)
    weakref.finalize(body, cleanup)
    file_name = archive_file_name(export_request.folder, settings)
    return StreamingResponse(
        body,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Cache-Control": "no-store",
            # Désactive la mise en tampon par un éventuel reverse proxy nginx
            "X-Accel-Buffering": "no",
        },
    )
