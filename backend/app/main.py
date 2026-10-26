"""API web : connexion Zimbra, liste des dossiers et export ZIP d'un dossier."""

import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app.archive import DateRange, ExportRequest, stream_zip, summarize
from app.config import Settings, get_settings
from app.filters import MessageFilter
from app.zimbra import AuthenticationFailed, Folder, SessionExpired, ZimbraClient, ZimbraError

app = FastAPI(title="Archivage Zimbra", docs_url="/api/docs", openapi_url="/api/openapi.json")

# Cookie de session contenant le jeton d'authentification Zimbra
COOKIE_NAME = "zimbra_session"

SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_client_factory(settings: SettingsDep) -> Callable[[], ZimbraClient]:
    return lambda: ZimbraClient(settings)


ClientFactoryDep = Annotated[Callable[[], ZimbraClient], Depends(get_client_factory)]


def get_token(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Non connecté")
    return token


TokenDep = Annotated[str, Depends(get_token)]


@app.exception_handler(SessionExpired)
def session_expired_handler(request: Request, exc: SessionExpired) -> JSONResponse:
    response = JSONResponse({"detail": "Session expirée"}, status.HTTP_401_UNAUTHORIZED)
    response.delete_cookie(COOKIE_NAME, path="/api")
    return response


@app.exception_handler(ZimbraError)
def zimbra_error_handler(request: Request, exc: ZimbraError) -> JSONResponse:
    return JSONResponse(
        {"detail": f"Erreur Zimbra : {exc.message}"}, status.HTTP_502_BAD_GATEWAY
    )


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class UserResponse(BaseModel):
    username: str


class FolderResponse(BaseModel):
    id: str
    name: str
    path: str
    message_count: int
    color: str | None
    children: list["FolderResponse"]


class SummaryResponse(BaseModel):
    folder_count: int
    message_count: int
    total_size: int


def to_folder_response(folder: Folder) -> FolderResponse:
    return FolderResponse(
        id=folder.id,
        name=folder.human_name,
        path=folder.abs_folder_path,
        message_count=folder.message_count,
        color=folder.color,
        children=[to_folder_response(c) for c in folder.children if c.is_mail_folder],
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(
    credentials: LoginRequest,
    response: Response,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
) -> UserResponse:
    with client_factory() as client:
        try:
            token = client.login(credentials.username, credentials.password)
        except AuthenticationFailed:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Identifiant ou mot de passe incorrect"
            )
    response.set_cookie(
        COOKIE_NAME,
        token.value,
        max_age=token.lifetime_ms // 1000 or None,
        path="/api",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )
    return UserResponse(username=credentials.username)


@app.post("/api/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/api")


@app.get("/api/auth/me")
def me(token: TokenDep, client_factory: ClientFactoryDep) -> UserResponse:
    with client_factory() as client:
        return UserResponse(username=client.get_account_name(token))


@app.get("/api/folders")
def folders(token: TokenDep, client_factory: ClientFactoryDep) -> list[FolderResponse]:
    with client_factory() as client:
        root = client.get_root_folder(token)
    return to_folder_response(root).children


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
    folder_id: Annotated[str, Query(min_length=1, description="Dossier, 1 pour toute la boîte")],
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
        except ValidationError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Filtre invalide")
    return ExportParams(
        folder_id, include_subfolders, start, end, message_filter, only_with_attachments
    )


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


@app.get("/api/export/summary")
def export_summary(
    token: TokenDep,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
    params: ExportParamsDep,
) -> SummaryResponse:
    with client_factory() as client:
        root = client.get_root_folder(token)
        export = build_export_request(root, params, settings)
        summary = summarize(client, token, export)
    return SummaryResponse(
        folder_count=summary.folder_count,
        message_count=summary.message_count,
        total_size=summary.total_size,
    )


def archive_file_name(folder: Folder, settings: Settings) -> str:
    """Nom du fichier ZIP, en ASCII pour être interprété de la même façon par tous les navigateurs."""
    today = datetime.now(settings.zone)
    ascii_name = unicodedata.normalize("NFKD", folder.human_name).encode("ascii", "ignore").decode()
    base_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("_") or "dossier"
    return f"{base_name}_{today:%Y%m%d}.zip"


@app.get("/api/export")
def export(
    token: TokenDep,
    settings: SettingsDep,
    client_factory: ClientFactoryDep,
    params: ExportParamsDep,
    extract_attachments: bool = False,
) -> StreamingResponse:
    client = client_factory()
    try:
        # Vérifications faites avant de commencer à envoyer l'archive, pour pouvoir
        # encore répondre avec un code d'erreur HTTP
        root = client.get_root_folder(token)
        export_request = build_export_request(root, params, settings, extract_attachments)
    except BaseException:
        client.close()
        raise

    def content() -> Iterator[bytes]:
        with client:
            yield from stream_zip(
                client, token, export_request, settings.zone, settings.max_file_name_length
            )

    file_name = archive_file_name(export_request.folder, settings)
    return StreamingResponse(
        content(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{file_name}"',
            "Cache-Control": "no-store",
            # Désactive la mise en tampon par un éventuel reverse proxy nginx
            "X-Accel-Buffering": "no",
        },
    )
