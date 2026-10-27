"""Avancement des exports en cours, affiché par l'interface pendant le téléchargement.

L'avancement est conservé dans la mémoire du processus : avec plusieurs réplicas du backend,
seul celui qui construit l'archive le connaît (l'interface affiche alors une attente simple).
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.sessions import session_key

# Un export sans nouvelle depuis ce délai est oublié
EXPIRATION_SECONDS = 3600
# Exports suivis au plus, par session et au total : les plus anciens sont oubliés au-delà
MAX_TRACKED_PER_SESSION = 5
MAX_TRACKED = 1000


@dataclass
class ExportProgress:
    done: int = 0
    # Nombre de courriels à traiter, connu une fois les dossiers parcourus
    total: int | None = None
    finished: bool = False
    # Raison d'un export interrompu avant la fin (session expirée, Zimbra indisponible…)
    error: str | None = None
    updated_at: float = 0.0


class ProgressRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        # Ordre d'insertion conservé : les premières entrées sont les plus anciennes
        self._exports: dict[tuple[str, str], ExportProgress] = {}

    @staticmethod
    def _key(token: str, export_id: str) -> tuple[str, str]:
        # Un export n'est visible que de la session qui l'a lancé, sans conserver le jeton
        return session_key(token), export_id

    def _purge(self, now: float) -> None:
        for key, other in list(self._exports.items()):
            if now - other.updated_at > EXPIRATION_SECONDS:
                del self._exports[key]

    def start(self, token: str, export_id: str) -> ExportProgress:
        now = self._clock()
        progress = ExportProgress(updated_at=now)
        key = self._key(token, export_id)
        with self._lock:
            self._purge(now)
            self._exports.pop(key, None)
            same_session = [k for k in self._exports if k[0] == key[0]]
            for old in same_session[: max(len(same_session) - MAX_TRACKED_PER_SESSION + 1, 0)]:
                del self._exports[old]
            while len(self._exports) >= MAX_TRACKED:
                del self._exports[next(iter(self._exports))]
            self._exports[key] = progress
        return progress

    def set_total(self, progress: ExportProgress, total: int) -> None:
        progress.total = total
        progress.updated_at = self._clock()

    def advance(self, progress: ExportProgress) -> None:
        progress.done += 1
        progress.updated_at = self._clock()

    def finish(self, progress: ExportProgress, error: str | None = None) -> None:
        if error and not progress.error:
            progress.error = error
        progress.finished = True
        progress.updated_at = self._clock()

    def get(self, token: str, export_id: str) -> ExportProgress | None:
        with self._lock:
            self._purge(self._clock())
            return self._exports.get(self._key(token, export_id))


registry = ProgressRegistry()


def get_progress_registry() -> ProgressRegistry:
    return registry
