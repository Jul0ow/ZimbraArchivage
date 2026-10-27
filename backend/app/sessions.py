"""Cookie de session : le jeton Zimbra y est chiffré avec une clé propre au serveur.

Un cookie copié hors de l'application n'est donc pas utilisable tel quel sur Zimbra
(webmail, SOAP ou ReST), et il expire au bout de Settings.session_max_age.
"""

import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def session_key(token: str) -> str:
    """Identifiant d'une session Zimbra, sans conserver le jeton lui-même."""
    return hashlib.sha256(token.encode()).hexdigest()


class SessionCipher:
    def __init__(self, secret: str, max_age: int):
        if secret:
            self._fernet = Fernet(secret)
        else:
            logger.warning(
                "SESSION_SECRET non défini : clé de session aléatoire, les sessions seront "
                "perdues au redémarrage et ne sont pas partagées entre réplicas"
            )
            self._fernet = Fernet(Fernet.generate_key())
        self._max_age = max_age

    def seal(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def open(self, cookie: str) -> str | None:
        """Jeton Zimbra contenu dans le cookie, None s'il est invalide ou trop ancien."""
        try:
            return self._fernet.decrypt(cookie.encode(), ttl=self._max_age).decode()
        except (InvalidToken, ValueError):
            return None


@lru_cache
def get_session_cipher(secret: str, max_age: int) -> SessionCipher:
    return SessionCipher(secret, max_age)
