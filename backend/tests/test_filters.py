import pytest
from pydantic import ValidationError

from app.filters import MessageFilter, quote_value


def test_quote_value():
    assert quote_value("budget") == "budget"
    assert quote_value("jean.dupont@test.fr") == "jean.dupont@test.fr"
    assert quote_value("budg*") == "budg*"
    assert quote_value("rapport mensuel") == '"rapport mensuel"'
    assert quote_value('dit "bonjour"') == '"dit \\"bonjour\\""'
    assert quote_value("or") == '"or"'


def test_filter_query_with_groups():
    message_filter = MessageFilter.model_validate({
        "operator": "or",
        "groups": [
            {"operator": "and", "conditions": [
                {"field": "subject", "value": " Budget 2024 "},
                {"field": "from", "operator": "not_contains", "value": "dupont"},
            ]},
            {"operator": "or", "conditions": [{"field": "to", "value": "compta@test.fr"}]},
        ],
    })

    assert message_filter.query() == (
        '((subject:"Budget 2024" and not from:dupont) or (tocc:compta@test.fr))'
    )


@pytest.mark.parametrize("data", [
    {"groups": []},
    {"groups": [{"conditions": []}]},
    {"groups": [{"conditions": [{"field": "subject", "value": "  "}]}]},
    {"groups": [{"conditions": [{"field": "body", "value": "x"}]}]},
    {"operator": "xor", "groups": [{"conditions": [{"field": "subject", "value": "x"}]}]},
])
def test_invalid_filters(data):
    with pytest.raises(ValidationError):
        MessageFilter.model_validate(data)
