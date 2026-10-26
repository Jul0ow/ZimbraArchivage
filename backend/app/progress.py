"""Avancement des exports en cours, affiché par l'interface pendant le téléchargement.

L'avancement est conservé dans la mémoire du processus : avec plusieurs réplicas du backend,
seul celui qui construit l'archive le connaît (l'interface affiche alors une attente simple).
"""

import hashlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

# Un export sans nouvelle depuis ce délai est oublié
EXPIRATION_SECONDS = 3600


@dataclass
class ExportProgress:
    done: int = 0
    finished: bool = False
    updated_at: float = 0.0


class ProgressRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._exports: dict[tuple[str, str], ExportProgress] = {}

    @staticmethod
    def _key(token: str, export_id: str) -> tuple[str, str]:
        # Un export n'est visible que de la session qui l'a lancé, sans conserver le jeton
        return hashlib.sha256(token.encode()).hexdigest(), export_id

    def start(self, token: str, export_id: str) -> ExportProgress:
        now = self._clock()
        progress = ExportProgress(updated_at=now)
        with self._lock:
            for key, other in list(self._exports.items()):
                if now - other.updated_at > EXPIRATION_SECONDS:
                    del self._exports[key]
            self._exports[self._key(token, export_id)] = progress
        return progress

    def advance(self, progress: ExportProgress) -> None:
        progress.done += 1
        progress.updated_at = self._clock()

    def finish(self, progress: ExportProgress) -> None:
        progress.finished = True
        progress.updated_at = self._clock()

    def get(self, token: str, export_id: str) -> ExportProgress | None:
        with self._lock:
            return self._exports.get(self._key(token, export_id))


registry = ProgressRegistry()


def get_progress_registry() -> ProgressRegistry:
    return registry
