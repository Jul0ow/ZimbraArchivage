"""Construction de l'archive ZIP d'un dossier Zimbra.

L'archive reprend l'organisation du projet Archivage : l'arborescence des dossiers
Zimbra, avec un fichier .eml par courriel nommé yyyyMMdd_<sujet>_<id>.eml.
Sur demande, les pièces jointes sont aussi extraites dans un dossier du même nom que le .eml.
"""

import mimetypes
import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import BinaryIO
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.filters import MessageFilter
from app.zimbra import Folder, MessageSummary, ZimbraClient, ZimbraError

# Caractères interdits dans un nom de fichier sous Windows ou Linux
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Taille au-delà de laquelle un .eml en cours de téléchargement est écrit sur disque
SPOOL_MAX_SIZE = 10 * 1024 * 1024

ERRORS_FILE_NAME = "erreurs.txt"

ROOT_FOLDER_ID = "1"

# Corbeille et Spam : exclues d'un export de toute la boîte, comme dans la recherche Zimbra
EXCLUDED_FROM_MAILBOX = {"3", "4"}

MAX_ATTACHMENT_NAME_LENGTH = 100


@dataclass(frozen=True)
class DateRange:
    """Période (bornes incluses) sur laquelle restreindre l'export."""

    start: date | None
    end: date | None
    zone: ZoneInfo

    def _start_ms(self) -> int | None:
        if self.start is None:
            return None
        return int(datetime.combine(self.start, time.min, self.zone).timestamp() * 1000)

    def _end_ms(self) -> int | None:
        if self.end is None:
            return None
        next_day = datetime.combine(self.end + timedelta(days=1), time.min, self.zone)
        return int(next_day.timestamp() * 1000)

    def query_terms(self) -> list[str]:
        """Critères de la recherche Zimbra : after et before sont exclusifs, on élargit d'un jour."""
        terms = []
        if self.start is not None:
            terms.append(f"after:{(self.start - timedelta(days=1)):%d/%m/%y}")
        if self.end is not None:
            terms.append(f"before:{(self.end + timedelta(days=1)):%d/%m/%y}")
        return terms

    def contains(self, date_ms: int) -> bool:
        start_ms, end_ms = self._start_ms(), self._end_ms()
        if start_ms is not None and date_ms < start_ms:
            return False
        if end_ms is not None and date_ms >= end_ms:
            return False
        return True


@dataclass(frozen=True)
class ExportRequest:
    folder: Folder
    include_subfolders: bool
    date_range: DateRange
    message_filter: MessageFilter | None = None
    only_with_attachments: bool = False
    extract_attachments: bool = False

    def folders(self) -> list[Folder]:
        if not self.include_subfolders:
            return [self.folder]
        if self.folder.id != ROOT_FOLDER_ID:
            return [f for f in _walk(self.folder, set()) if f.is_mail_folder]
        # Toute la boîte : la racine elle-même ne contient pas de messages
        folders = _walk(self.folder, EXCLUDED_FROM_MAILBOX)
        return [f for f in folders if f.is_mail_folder and f.id != ROOT_FOLDER_ID]

    def query(self, folder: Folder) -> str:
        terms = [f"inid:{folder.id}", *self.date_range.query_terms()]
        if self.only_with_attachments:
            terms.append("has:attachment")
        if self.message_filter is not None:
            terms.append(self.message_filter.query())
        return " and ".join(terms)


def _walk(folder: Folder, excluded: set[str]) -> Iterator[Folder]:
    yield folder
    for child in folder.children:
        if child.id not in excluded:
            yield from _walk(child, excluded)


@dataclass(frozen=True)
class ExportSummary:
    folder_count: int
    message_count: int
    total_size: int


def sanitize_path_component(name: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "_"


def folder_archive_path(folder: Folder) -> str:
    """Chemin du dossier dans l'archive, relatif à la racine de la boîte."""
    parts = [p for p in folder.abs_folder_path.split("/") if p]
    return "/".join(sanitize_path_component(p) for p in parts)


def eml_file_name(message: MessageSummary, zone: ZoneInfo, max_length: int) -> str:
    """Même règle de nommage que EmlFilePath.generateFileName dans Archivage."""
    sent = datetime.fromtimestamp(message.date_ms / 1000, zone)
    prefix = f"{sent:%Y%m%d}_"
    suffix = f"_{sanitize_path_component(message.id)}.eml"
    escaped_subject = re.sub(r"[^0-9a-zA-Z ]", "_", message.subject or "")
    available = max(max_length - len(prefix) - len(suffix), 0)
    return prefix + escaped_subject[:available] + suffix


def iter_folder_messages(
    client: ZimbraClient, token: str, folder: Folder, export: ExportRequest
) -> Iterator[MessageSummary]:
    for message in client.iter_messages(token, export.query(folder)):
        # La recherche peut remonter des messages hors bornes : on refiltre sur la date exacte
        if message.folder_id == folder.id and export.date_range.contains(message.date_ms):
            yield message


def summarize(client: ZimbraClient, token: str, export: ExportRequest) -> ExportSummary:
    folders = export.folders()
    message_count = total_size = 0
    for folder in folders:
        for message in iter_folder_messages(client, token, folder, export):
            message_count += 1
            total_size += message.size
    return ExportSummary(len(folders), message_count, total_size)


class _ChunkSink:
    """Flux d'écriture non positionnable : zipfile écrit alors l'archive de façon séquentielle."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def stream_zip(
    client: ZimbraClient,
    token: str,
    export: ExportRequest,
    zone: ZoneInfo,
    max_file_name_length: int,
) -> Iterator[bytes]:
    """Génère l'archive ZIP au fil de l'eau, sans la stocker entièrement."""
    sink = _ChunkSink()
    errors: list[str] = []
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for folder in export.folders():
            folder_path = folder_archive_path(folder)
            try:
                for message in iter_folder_messages(client, token, folder, export):
                    file_name = eml_file_name(message, zone, max_file_name_length)
                    arcname = f"{folder_path}/{file_name}" if folder_path else file_name
                    error = _add_message(
                        client, token, archive, message, arcname, zone, export.extract_attachments
                    )
                    if error:
                        errors.append(f"{arcname} : {error}")
                    yield sink.drain()
            except ZimbraError as e:
                errors.append(f"Dossier {folder.abs_folder_path} : {e.message}")
        if errors:
            archive.writestr(ERRORS_FILE_NAME, "\n".join(errors) + "\n")
    yield sink.drain()


def _add_message(
    client: ZimbraClient,
    token: str,
    archive: zipfile.ZipFile,
    message: MessageSummary,
    arcname: str,
    zone: ZoneInfo,
    extract_attachments: bool,
) -> str | None:
    """Ajoute un courriel à l'archive, et retourne le message d'erreur en cas d'échec."""
    # Le .eml est téléchargé entièrement avant d'être ajouté, pour ne jamais
    # laisser d'entrée tronquée dans l'archive en cas d'erreur réseau
    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_SIZE) as buffer:
        try:
            with client.download_eml(token, message.id) as chunks:
                for chunk in chunks:
                    buffer.write(chunk)
        except ZimbraError as e:
            return e.message
        except httpx.HTTPError as e:
            return f"Erreur réseau pendant le téléchargement : {e}"
        buffer.seek(0)
        date_time = _zip_date_time(datetime.fromtimestamp(message.date_ms / 1000, zone))
        with archive.open(_zip_info(arcname, date_time), "w") as entry:
            while chunk := buffer.read(1024 * 1024):
                entry.write(chunk)
        if extract_attachments:
            buffer.seek(0)
            # Le .eml est déjà dans l'archive : une erreur ici ne concerne que les pièces jointes
            try:
                attachments = list(extract_attachment_files(buffer))
            except Exception as e:
                return f"pièces jointes non extraites ({e})"
            directory = arcname.removesuffix(".eml")
            for name, content in attachments:
                archive.writestr(_zip_info(f"{directory}/{name}", date_time), content)
    return None


def extract_attachment_files(eml: BinaryIO) -> Iterator[tuple[str, bytes]]:
    """Pièces jointes d'un courriel : (nom de fichier unique, contenu décodé)."""
    message = BytesParser(policy=policy.default).parse(eml)
    used_names: set[str] = set()
    for index, part in enumerate(_attachment_parts(message, is_root=True), start=1):
        if part.get_content_type() == "message/rfc822":
            # Courriel joint : enregistré tel quel au format .eml
            inner = part.get_payload(0)
            content = inner.as_bytes() if isinstance(inner, EmailMessage) else b""
            extension = ".eml"
        else:
            content = part.get_payload(decode=True) or b""
            extension = mimetypes.guess_extension(part.get_content_type()) or ".bin"
        name = attachment_file_name(part.get_filename(), f"piece_jointe_{index}{extension}")
        yield _unique_name(name, used_names), content


def _attachment_parts(part: EmailMessage, is_root: bool = False) -> Iterator[EmailMessage]:
    if not is_root and part.get_content_type() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for child in part.iter_parts():
            yield from _attachment_parts(child)
        return
    # Pièce jointe explicite, ou partie nommée (images insérées dans le corps, par exemple)
    if not is_root and (part.get_content_disposition() == "attachment" or part.get_filename()):
        yield part


def attachment_file_name(file_name: str | None, default: str) -> str:
    # On ne garde que le nom, sans un éventuel chemin fourni par l'expéditeur
    base_name = re.split(r"[/\\]", file_name or "")[-1]
    name = sanitize_path_component(base_name) if base_name.strip() else default
    if len(name) > MAX_ATTACHMENT_NAME_LENGTH:
        stem, dot, extension = name.rpartition(".")
        if dot and len(extension) <= 10:
            name = stem[: MAX_ATTACHMENT_NAME_LENGTH - len(extension) - 1] + "." + extension
        else:
            name = name[:MAX_ATTACHMENT_NAME_LENGTH]
    return name


def _unique_name(name: str, used_names: set[str]) -> str:
    stem, dot, extension = name.rpartition(".")
    if not dot:
        stem, extension = name, ""
    candidate, counter = name, 2
    while candidate.lower() in used_names:
        candidate = f"{stem} ({counter}){dot}{extension}"
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def _zip_info(arcname: str, date_time: tuple[int, int, int, int, int, int]) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _zip_date_time(value: datetime) -> tuple[int, int, int, int, int, int]:
    # Le format ZIP ne gère pas les dates antérieures à 1980
    if value.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)
