"""Construction de l'archive ZIP d'un dossier Zimbra.

L'archive reprend l'organisation du projet Archivage : l'arborescence des dossiers
Zimbra, avec un fichier .eml par courriel nommé yyyyMMdd_<sujet>_<id>.eml.
Sur demande, les pièces jointes sont aussi extraites dans un dossier du même nom que le .eml.
"""

import hashlib
import logging
import mimetypes
import re
import tempfile
import zipfile
from collections import deque
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import IO, BinaryIO
from zoneinfo import ZoneInfo

import httpx

from app.filters import MessageFilter
from app.zimbra import (
    EXCLUDED_FROM_MAILBOX,
    ROOT_FOLDER_ID,
    Folder,
    MessageSummary,
    SessionExpired,
    ZimbraClient,
    ZimbraError,
    ZimbraUnavailable,
)

logger = logging.getLogger(__name__)

# Caractères interdits dans un nom de fichier sous Windows ou Linux
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Noms réservés sous Windows, qui empêcheraient d'extraire l'archive
WINDOWS_RESERVED_NAMES = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$", re.IGNORECASE)

# Taille au-delà de laquelle un .eml en cours de téléchargement est écrit sur disque
SPOOL_MAX_SIZE = 10 * 1024 * 1024

# Taille des blocs copiés dans l'archive, envoyés au client au fur et à mesure
COPY_CHUNK_SIZE = 1024 * 1024

# Au-delà, l'entrée ZIP doit utiliser les extensions ZIP64
ZIP64_LIMIT = (1 << 31) - 1

ERRORS_FILE_NAME = "erreurs.txt"

MAX_ATTACHMENT_NAME_LENGTH = 100
# Garde-fous de l'analyse MIME d'un courriel construit par un expéditeur quelconque
MAX_ATTACHMENT_PARTS = 500
MAX_MIME_DEPTH = 20

# Échecs consécutifs de Zimbra au-delà desquels l'export est abandonné
MAX_CONSECUTIVE_FAILURES = 20

# Formats déjà compressés : stockés tels quels dans l'archive, sans recompression
COMPRESSED_EXTENSIONS = {
    ".7z",
    ".avi",
    ".bz2",
    ".docx",
    ".gif",
    ".gz",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".odp",
    ".ods",
    ".odt",
    ".png",
    ".pptx",
    ".rar",
    ".webm",
    ".webp",
    ".xlsx",
    ".xz",
    ".zip",
}

SESSION_EXPIRED_MESSAGE = (
    "session Zimbra expirée, l'archive est incomplète : reconnectez-vous puis relancez l'export"
)
UNAVAILABLE_MESSAGE = "Zimbra ne répond plus, l'archive est incomplète : relancez l'export plus tard"


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
        return (start_ms is None or date_ms >= start_ms) and (end_ms is None or date_ms < end_ms)


@dataclass(frozen=True)
class ExportRequest:
    folder: Folder
    include_subfolders: bool
    date_range: DateRange
    message_filter: MessageFilter | None = None
    only_with_attachments: bool = False
    extract_attachments: bool = False

    @property
    def is_mailbox(self) -> bool:
        return self.folder.id == ROOT_FOLDER_ID

    def folders(self) -> list[Folder]:
        if self.is_mailbox:
            # Toute la boîte, sous-dossiers compris : la racine elle-même ne contient pas de messages
            folders = self.folder.walk(EXCLUDED_FROM_MAILBOX)
            return [f for f in folders if f.is_mail_folder and f.id != ROOT_FOLDER_ID]
        if not self.include_subfolders:
            return [self.folder]
        return [f for f in self.folder.walk() if f.is_mail_folder]

    def query(self, folder: Folder) -> str:
        terms = [f"inid:{folder.id}", *self.date_range.query_terms()]
        if self.only_with_attachments:
            terms.append("has:attachment")
        if self.message_filter is not None:
            terms.append(self.message_filter.query())
        return " and ".join(terms)


@dataclass(frozen=True)
class ExportSummary:
    folder_count: int
    message_count: int
    total_size: int


class ExportObserver:
    """Suivi d'un export : par défaut, rien n'est fait."""

    def on_total(self, total: int) -> None:
        pass

    def on_message(self) -> None:
        """Appelé après chaque courriel traité, ajouté à l'archive ou en erreur."""

    def on_abort(self, reason: str) -> None:
        pass


class ExportAborted(Exception):
    pass


def sanitize_path_component(name: str) -> str:
    cleaned = INVALID_PATH_CHARS.sub("_", name).strip().rstrip(".")
    if WINDOWS_RESERVED_NAMES.match(cleaned):
        cleaned = "_" + cleaned
    return cleaned or "_"


def folder_archive_path(folder: Folder) -> str:
    """Chemin du dossier dans l'archive, relatif à la racine de la boîte."""
    parts = [p for p in folder.abs_folder_path.split("/") if p]
    return "/".join(sanitize_path_component(p) for p in parts)


def eml_file_name(message: MessageSummary, zone: ZoneInfo, max_length: int) -> str:
    """Même règle de nommage que EmlFilePath.generateFileName dans Archivage."""
    sent = datetime.fromtimestamp(message.date_ms / 1000, zone)
    prefix = f"{sent:%Y%m%d}_"
    message_id = sanitize_path_component(message.id)
    suffix = f"_{message_id}.eml"
    if len(prefix) + len(suffix) > max_length:
        # Identifiant trop long (élément partagé par exemple) : remplacé par une empreinte stable
        suffix = f"_{hashlib.sha256(message.id.encode()).hexdigest()[:16]}.eml"
    escaped_subject = re.sub(r"[^0-9a-zA-Z ]", "_", message.subject or "")
    available = max(max_length - len(prefix) - len(suffix), 0)
    return prefix + escaped_subject[:available] + suffix


def folder_messages(
    client: ZimbraClient, token: str, folder: Folder, export: ExportRequest
) -> list[MessageSummary]:
    """Messages d'un dossier correspondant à l'export, du plus ancien au plus récent."""
    if folder.message_count == 0:
        return []
    return [
        message
        for message in client.iter_messages(token, export.query(folder))
        # La recherche peut remonter des messages hors bornes : on refiltre sur la date exacte
        if message.folder_id == folder.id and export.date_range.contains(message.date_ms)
    ]


def summarize(client: ZimbraClient, token: str, export: ExportRequest) -> ExportSummary:
    folders = export.folders()
    message_count = total_size = 0
    for folder in folders:
        for message in folder_messages(client, token, folder, export):
            message_count += 1
            total_size += message.size
    return ExportSummary(len(folders), message_count, total_size)


class _ChunkSink:
    """Flux d'écriture non positionnable : zipfile écrit alors l'archive de façon séquentielle."""

    def __init__(self) -> None:
        self._chunks: deque[bytes] = deque()

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> Iterator[bytes]:
        while self._chunks:
            yield self._chunks.popleft()


@dataclass(frozen=True)
class _Job:
    message: MessageSummary
    arcname: str


def _download(client: ZimbraClient, token: str, message_id: str) -> IO[bytes]:
    # Le .eml est téléchargé entièrement avant d'être ajouté, pour ne jamais
    # laisser d'entrée tronquée dans l'archive en cas d'erreur réseau
    # Fichier rendu ouvert à l'appelant, qui le referme après l'avoir copié dans l'archive
    buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_SIZE)  # noqa: SIM115
    try:
        with client.download_eml(token, message_id) as chunks:
            for chunk in chunks:
                buffer.write(chunk)
    except BaseException:
        buffer.close()
        raise
    return buffer


def _close_result(future: Future[IO[bytes]]) -> None:
    if not future.cancelled() and future.exception() is None:
        future.result().close()


def _prefetch(
    pool: ThreadPoolExecutor, client: ZimbraClient, token: str, jobs: list[_Job], window: int
) -> Iterator[tuple[_Job, Future[IO[bytes]]]]:
    """Téléchargements lancés à l'avance, rendus dans l'ordre des messages."""
    pending: deque[tuple[_Job, Future[IO[bytes]]]] = deque()
    try:
        for job in jobs:
            pending.append((job, pool.submit(_download, client, token, job.message.id)))
            if len(pending) >= window:
                yield pending.popleft()
        while pending:
            yield pending.popleft()
    finally:
        # Export interrompu : les fichiers déjà téléchargés sont libérés
        for _, future in pending:
            if not future.cancel():
                future.add_done_callback(_close_result)


def stream_zip(
    client: ZimbraClient,
    token: str,
    export: ExportRequest,
    zone: ZoneInfo,
    max_file_name_length: int,
    download_concurrency: int = 1,
    max_attachment_extraction_size: int = 25 * 1024 * 1024,
    observer: ExportObserver | None = None,
) -> Iterator[bytes]:
    """Génère l'archive ZIP au fil de l'eau, sans la stocker entièrement.

    Les messages de tous les dossiers sont d'abord recensés, ce qui donne le total à
    l'observateur et fige la liste des messages à télécharger.
    Une session expirée ou un Zimbra qui ne répond plus interrompt l'export : la raison
    est inscrite en tête de erreurs.txt et transmise à l'observateur.
    """
    observer = observer or ExportObserver()
    sink = _ChunkSink()
    errors: list[str] = []
    abort: str | None = None
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        try:
            jobs = _collect_jobs(client, token, export, zone, max_file_name_length, errors)
            observer.on_total(len(jobs))
            yield from _write_jobs(
                client,
                token,
                archive,
                sink,
                jobs,
                export,
                zone,
                download_concurrency,
                max_attachment_extraction_size,
                errors,
                observer,
            )
        except SessionExpired:
            abort = SESSION_EXPIRED_MESSAGE
        except ExportAborted as e:
            abort = str(e)
        if abort:
            logger.warning("Export interrompu : %s", abort)
            errors.insert(0, f"EXPORT INTERROMPU : {abort}")
            observer.on_abort(abort)
        if errors:
            archive.writestr(ERRORS_FILE_NAME, "\n".join(errors) + "\n")
    yield from sink.drain()


def _collect_jobs(
    client: ZimbraClient,
    token: str,
    export: ExportRequest,
    zone: ZoneInfo,
    max_file_name_length: int,
    errors: list[str],
) -> list[_Job]:
    jobs: list[_Job] = []
    used_names: set[str] = set()
    for folder in export.folders():
        folder_path = folder_archive_path(folder)
        try:
            messages = folder_messages(client, token, folder, export)
        except SessionExpired:
            raise
        except ZimbraError as e:
            logger.warning("Export : recherche en échec dans un dossier : %s", e.message)
            errors.append(f"Dossier {folder.abs_folder_path} : {e.message}")
            continue
        for message in messages:
            file_name = eml_file_name(message, zone, max_file_name_length)
            arcname = f"{folder_path}/{file_name}" if folder_path else file_name
            # Deux dossiers peuvent avoir le même nom une fois assainis (ex. « a:b » et « a?b »)
            arcname = _unique_name(arcname, used_names)
            jobs.append(_Job(message, arcname))
    return jobs


def _write_jobs(
    client: ZimbraClient,
    token: str,
    archive: zipfile.ZipFile,
    sink: _ChunkSink,
    jobs: list[_Job],
    export: ExportRequest,
    zone: ZoneInfo,
    download_concurrency: int,
    max_attachment_extraction_size: int,
    errors: list[str],
    observer: ExportObserver,
) -> Iterator[bytes]:
    pool = ThreadPoolExecutor(max_workers=download_concurrency, thread_name_prefix="export")
    downloads = _prefetch(pool, client, token, jobs, download_concurrency)
    consecutive_failures = 0
    try:
        for job, future in downloads:
            try:
                buffer = future.result()
            except SessionExpired:
                raise
            except (ZimbraError, httpx.HTTPError) as e:
                unavailable = isinstance(e, (ZimbraUnavailable, httpx.HTTPError))
                consecutive_failures = consecutive_failures + 1 if unavailable else 0
                message = (
                    e.message if isinstance(e, ZimbraError) else ("Erreur réseau pendant le téléchargement")
                )
                logger.warning("Export : téléchargement en échec : %s", e)
                errors.append(f"{job.arcname} : {message}")
                observer.on_message()
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise ExportAborted(UNAVAILABLE_MESSAGE) from e
                continue
            consecutive_failures = 0
            with buffer:
                error = yield from _write_message(
                    archive,
                    sink,
                    buffer,
                    job,
                    zone,
                    export.extract_attachments,
                    max_attachment_extraction_size,
                )
            if error:
                errors.append(f"{job.arcname} : {error}")
            observer.on_message()
            yield from sink.drain()
    finally:
        downloads.close()
        pool.shutdown(wait=False, cancel_futures=True)


def _write_message(
    archive: zipfile.ZipFile,
    sink: _ChunkSink,
    buffer: IO[bytes],
    job: _Job,
    zone: ZoneInfo,
    extract_attachments: bool,
    max_attachment_extraction_size: int,
) -> Iterator[bytes]:
    """Ajoute un courriel à l'archive, et retourne le message d'erreur en cas d'échec partiel."""
    size = buffer.seek(0, 2)
    buffer.seek(0)
    date_time = _zip_date_time(datetime.fromtimestamp(job.message.date_ms / 1000, zone))
    info = _zip_info(job.arcname, date_time, compressed=True)
    with archive.open(info, "w", force_zip64=size > ZIP64_LIMIT) as entry:
        while chunk := buffer.read(COPY_CHUNK_SIZE):
            entry.write(chunk)
            yield from sink.drain()

    # Le drapeau "a" de Zimbra évite d'analyser les courriels sans pièce jointe
    if not extract_attachments or not job.message.has_attachment:
        return None
    if size > max_attachment_extraction_size:
        return "pièces jointes non extraites (courriel trop volumineux), elles restent dans le .eml"
    # Le .eml est déjà dans l'archive : une erreur ici ne concerne que les pièces jointes
    buffer.seek(0)
    directory = job.arcname.removesuffix(".eml")
    try:
        for name, content in extract_attachment_files(buffer):
            extension = name.rpartition(".")[2].lower()
            compressed = f".{extension}" not in COMPRESSED_EXTENSIONS
            with archive.open(_zip_info(f"{directory}/{name}", date_time, compressed), "w") as out:
                out.write(content)
            del content
            yield from sink.drain()
    except Exception:
        logger.warning("Export : analyse MIME en échec pour le message %s", job.message.id)
        return "pièces jointes non extraites (format du courriel invalide)"
    return None


def extract_attachment_files(eml: BinaryIO) -> Iterator[tuple[str, bytes]]:
    """Pièces jointes d'un courriel : (nom de fichier unique, contenu décodé)."""
    message = BytesParser(policy=policy.default).parse(eml)
    used_names: set[str] = set()
    for index, part in enumerate(_attachment_parts(message, depth=0), start=1):
        if index > MAX_ATTACHMENT_PARTS:
            raise ValueError("trop de pièces jointes")
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


def _attachment_parts(part: EmailMessage, depth: int) -> Iterator[EmailMessage]:
    if depth > MAX_MIME_DEPTH:
        raise ValueError("structure MIME trop profonde")
    is_root = depth == 0
    if not is_root and part.get_content_type() == "message/rfc822":
        yield part
        return
    if part.is_multipart():
        for child in part.iter_parts():
            yield from _attachment_parts(child, depth + 1)
        return
    # Pièce jointe explicite, ou partie nommée (images insérées dans le corps, par exemple).
    # À la racine, seule une pièce jointe explicite compte : c'est alors tout le courriel
    # (cas de certains scanners), et non le texte d'un message simple.
    if part.get_content_disposition() == "attachment" or (not is_root and part.get_filename()):
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
    if not dot or "/" in extension:
        stem, dot, extension = name, "", ""
    candidate, counter = name, 2
    while candidate.lower() in used_names:
        candidate = f"{stem} ({counter}){dot}{extension}"
        counter += 1
    used_names.add(candidate.lower())
    return candidate


def _zip_info(
    arcname: str, date_time: tuple[int, int, int, int, int, int], compressed: bool
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED if compressed else zipfile.ZIP_STORED
    return info


def _zip_date_time(value: datetime) -> tuple[int, int, int, int, int, int]:
    # Le format ZIP ne gère pas les dates antérieures à 1980
    if value.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    return (value.year, value.month, value.day, value.hour, value.minute, value.second)
