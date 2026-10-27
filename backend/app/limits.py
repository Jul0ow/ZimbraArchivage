"""Garde-fous contre la surcharge : exports simultanés et tentatives de connexion.

Les compteurs sont gardés en mémoire, pour le processus courant.
"""

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from app.sessions import session_key

# Fenêtre sur laquelle les connexions échouées sont comptées
LOGIN_WINDOW_SECONDS = 300


class TooManyExports(Exception):
    pass


class ExportSlot:
    """Place réservée pour un export ou une estimation, libérée une seule fois."""

    def __init__(self, release: Callable[[], None]):
        self._release = release
        self._released = False
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()


class ExportLimiter:
    def __init__(self, max_total: int, max_per_session: int):
        self.max_total = max_total
        self.max_per_session = max_per_session
        self._lock = threading.Lock()
        self._total = 0
        self._per_session: dict[str, int] = defaultdict(int)

    def acquire(self, token: str) -> ExportSlot:
        key = session_key(token)
        with self._lock:
            if self._total >= self.max_total or self._per_session[key] >= self.max_per_session:
                raise TooManyExports
            self._total += 1
            self._per_session[key] += 1

        def release() -> None:
            with self._lock:
                self._total -= 1
                self._per_session[key] -= 1
                if self._per_session[key] <= 0:
                    del self._per_session[key]

        return ExportSlot(release)


class LoginThrottle:
    """Bloque un identifiant ou une adresse IP après trop de connexions échouées."""

    def __init__(self, max_per_user: int, max_per_ip: int, clock: Callable[[], float] = time.monotonic):
        self._max = {"user": max_per_user, "ip": max_per_ip}
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[tuple[str, str], deque[float]] = {}

    def _keys(self, username: str, ip: str | None) -> list[tuple[str, str]]:
        keys = [("user", username.strip().lower())]
        if ip and self._max["ip"] > 0:
            keys.append(("ip", ip))
        return keys

    def _recent(self, key: tuple[str, str], now: float) -> deque[float]:
        failures = self._failures.get(key, deque())
        while failures and now - failures[0] > LOGIN_WINDOW_SECONDS:
            failures.popleft()
        if not failures:
            self._failures.pop(key, None)
        return failures

    def is_blocked(self, username: str, ip: str | None) -> bool:
        now = self._clock()
        with self._lock:
            return any(len(self._recent(key, now)) >= self._max[key[0]] for key in self._keys(username, ip))

    def record_failure(self, username: str, ip: str | None) -> None:
        now = self._clock()
        with self._lock:
            for key in self._keys(username, ip):
                self._recent(key, now)
                self._failures.setdefault(key, deque()).append(now)
            # Les entrées anciennes sont nettoyées au fil des échecs
            if len(self._failures) > 10000:
                for key in list(self._failures):
                    self._recent(key, now)

    def reset(self, username: str) -> None:
        with self._lock:
            self._failures.pop(("user", username.strip().lower()), None)
