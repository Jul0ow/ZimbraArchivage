"""Suggestions d'adresses pour les filtres sur l'expéditeur et le destinataire.

Deux sources sont combinées : l'autocomplétion de Zimbra (contacts, liste globale et
correspondants habituels), puis les adresses des messages récents de la boîte, ce qui
couvre aussi les expéditeurs qui ne sont pas dans les contacts.
"""

from collections.abc import Callable
from typing import Literal

from app.filters import FIELD_PREFIXES, RESERVED_WORDS, SIMPLE_VALUE
from app.zimbra import EmailAddress, SessionExpired, ZimbraClient, ZimbraError

AddressField = Literal["from", "to"]

# Types d'adresses des messages correspondant à chaque champ du filtre
MESSAGE_ADDRESS_TYPES = {"from": {"f"}, "to": {"t", "c"}}

# Nombre de messages récents parcourus pour y trouver des adresses
SEARCH_LIMIT = 100


def _matches(address: EmailAddress, text: str) -> bool:
    text = text.lower()
    return text in address.address.lower() or text in address.name.lower()


def _safe(source: Callable[[], list[EmailAddress]]) -> list[EmailAddress]:
    """Une source en erreur (ex. trop de mots pour un début de mot très court) est ignorée."""
    try:
        return source()
    except SessionExpired:
        raise
    except ZimbraError:
        return []


def _from_messages(
    client: ZimbraClient, token: str, text: str, field: AddressField
) -> list[EmailAddress]:
    # Recherche sur un début de mot, possible seulement pour une valeur sans guillemets
    if not SIMPLE_VALUE.match(text) or "*" in text or text.lower() in RESERVED_WORDS:
        return []
    query = f"{FIELD_PREFIXES[field]}:{text}*"
    return [
        found.address
        for found in client.search_addresses(token, query, SEARCH_LIMIT)
        if found.type in MESSAGE_ADDRESS_TYPES[field] and _matches(found.address, text)
    ]


def suggest_addresses(
    client: ZimbraClient, token: str, text: str, field: AddressField, limit: int
) -> list[EmailAddress]:
    text = text.strip()
    suggestions: dict[str, EmailAddress] = {}
    sources = [
        lambda: client.autocomplete(token, text),
        lambda: _from_messages(client, token, text, field),
    ]
    for source in sources:
        for address in _safe(source):
            suggestions.setdefault(address.address.lower(), address)
            if len(suggestions) == limit:
                return list(suggestions.values())
    return list(suggestions.values())
