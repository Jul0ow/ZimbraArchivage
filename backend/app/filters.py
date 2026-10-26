"""Filtres sur l'objet, l'expéditeur et les destinataires, traduits en requête de recherche Zimbra.

Un filtre est composé de groupes de conditions : les conditions d'un groupe sont reliées
par un même opérateur (ET / OU), et les groupes entre eux par un autre. Par exemple :
(objet contient « budget » ET expéditeur contient « dupont ») OU (objet contient « facture »).
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

# Critères de la recherche Zimbra : tocc couvre les champs À et Cc
FIELD_PREFIXES = {"subject": "subject", "from": "from", "to": "tocc"}

# Valeur utilisable sans guillemets, ce qui permet aussi le joker de préfixe (ex. budg*)
SIMPLE_VALUE = re.compile(r"^[\w@.+*-]+$")

# Mots réservés de la syntaxe de recherche, à mettre entre guillemets
RESERVED_WORDS = {"and", "or", "not"}

Operator = Literal["and", "or"]
FilterValue = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def quote_value(value: str) -> str:
    if SIMPLE_VALUE.match(value) and value.lower() not in RESERVED_WORDS:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class Condition(BaseModel):
    field: Literal["subject", "from", "to"]
    operator: Literal["contains", "not_contains"] = "contains"
    value: FilterValue

    def query(self) -> str:
        term = f"{FIELD_PREFIXES[self.field]}:{quote_value(self.value)}"
        return f"not {term}" if self.operator == "not_contains" else term


class ConditionGroup(BaseModel):
    operator: Operator = "and"
    conditions: list[Condition] = Field(min_length=1, max_length=20)

    def query(self) -> str:
        return "(" + f" {self.operator} ".join(c.query() for c in self.conditions) + ")"


class MessageFilter(BaseModel):
    operator: Operator = "or"
    groups: list[ConditionGroup] = Field(min_length=1, max_length=10)

    def query(self) -> str:
        return "(" + f" {self.operator} ".join(g.query() for g in self.groups) + ")"
