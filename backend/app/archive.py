"""Construction de l'archive ZIP d'un dossier Zimbra.

L'archive reprend l'organisation du projet Archivage : l'arborescence des dossiers
Zimbra, avec un fichier .eml par courriel nommé yyyyMMdd_<sujet>_<id>.eml.
"""

import re
import tempfile
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.zimbra import Folder, MessageSummary, ZimbraClient, ZimbraError

# Caractères interdits dans un nom de fichier sous Windows ou Linux
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Taille au-delà de laquelle un .eml en cours de téléchargement est écrit sur disque
SPOOL_MAX_SIZE = 10 * 1024 * 1024

ERRORS_FILE_NAME = "erreurs.txt"


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

    def folders(self) -> list[Folder]:
        if not self.include_subfolders:
            return [self.folder]
        return [f for f in self.folder.walk() if f.is_mail_folder]


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


def folder_query(folder: Folder, date_range: DateRange) -> str:
    return " and ".join([f"inid:{folder.id}", *date_range.query_terms()])


def iter_folder_messages(
    client: ZimbraClient, token: str, folder: Folder, date_range: DateRange
) -> Iterator[MessageSummary]:
    for message in client.iter_messages(token, folder_query(folder, date_range)):
        # La recherche peut remonter des messages hors bornes : on refiltre sur la date exacte
        if message.folder_id == folder.id and date_range.contains(message.date_ms):
            yield message


def summarize(client: ZimbraClient, token: str, export: ExportRequest) -> ExportSummary:
    folders = export.folders()
    message_count = total_size = 0
    for folder in folders:
        for message in iter_folder_messages(client, token, folder, export.date_range):
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
                for message in iter_folder_messages(client, token, folder, export.date_range):
                    file_name = eml_file_name(message, zone, max_file_name_length)
                    arcname = f"{folder_path}/{file_name}" if folder_path else file_name
                    error = _add_message(client, token, archive, message, arcname, zone)
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
        sent = datetime.fromtimestamp(message.date_ms / 1000, zone)
        info = zipfile.ZipInfo(arcname, date_time=_zip_date_time(sent))
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w") as entry:
            while chunk := buffer.read(1024 * 1024):
                entry.write(chunk)
    return None


def _zip_date_time(value: datetime) -> tuple[int, int, int, int, int, int]:
    # Le format ZIP ne gère pas les dates antérieures à 1980
    if value.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)
