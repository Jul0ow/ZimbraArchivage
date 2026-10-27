import httpx
import pytest

from app.config import Settings
from app.zimbra import (
    MAX_RETRIES,
    SessionExpired,
    UnexpectedResponse,
    ZimbraClient,
    ZimbraError,
    ZimbraUnavailable,
    _folder_color,
)
from tests.fake_zimbra import REST_URL, SOAP_URL, VALID_TOKEN, FakeMessage, FakeZimbra


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, zimbra_soap_url=SOAP_URL, zimbra_rest_url=REST_URL)


def client_for(settings: Settings, handler, sleeps: list[float] | None = None) -> ZimbraClient:
    sleep = sleeps.append if sleeps is not None else (lambda _: None)
    return ZimbraClient(settings, transport=httpx.MockTransport(handler), sleep=sleep)


def soap_response(name: str, body) -> httpx.Response:
    return httpx.Response(200, json={"Body": {name: body}})


def test_retries_while_zimbra_is_unavailable(settings: Settings):
    zimbra = FakeZimbra()
    statuses = iter([503, 502])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, None)
        return httpx.Response(status) if status else zimbra.handle(request)

    client = client_for(settings, handler, sleeps)

    assert client.login("bernard@test.fr", "secret").value == VALID_TOKEN
    assert sleeps == [1, 2]


def test_gives_up_after_max_retries(settings: Settings):
    calls: list[httpx.Request] = []
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(504)

    client = client_for(settings, handler, sleeps)

    with pytest.raises(ZimbraUnavailable, match=f"malgré {MAX_RETRIES + 1} essais"):
        client.login("bernard@test.fr", "secret")
    assert len(calls) == MAX_RETRIES + 1
    # Pauses croissantes, toutes plafonnées : 31 s d'attente au total
    assert sleeps == [1, 2, 4, 8, 16]


def test_retry_after_header_is_respected_and_capped(settings: Settings):
    zimbra = FakeZimbra()
    responses = iter(
        [
            httpx.Response(503, headers={"Retry-After": "5"}),
            httpx.Response(503, headers={"Retry-After": "3600"}),
        ]
    )
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses, None) or zimbra.handle(request)

    client_for(settings, handler, sleeps).login("bernard@test.fr", "secret")

    assert sleeps == [5, 30]


def test_connection_errors_are_retried(settings: Settings):
    zimbra = FakeZimbra()
    failures = iter([httpx.ConnectError("refusé"), httpx.ConnectTimeout("délai")])

    def handler(request: httpx.Request) -> httpx.Response:
        error = next(failures, None)
        if error:
            raise error
        return zimbra.handle(request)

    assert client_for(settings, handler).login("bernard@test.fr", "secret").value == VALID_TOKEN


def test_unreachable_zimbra(settings: Settings):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno -2] Name or service not known")

    with pytest.raises(ZimbraUnavailable) as error:
        client_for(settings, handler).login("bernard@test.fr", "secret")
    # Le détail technique (nom d'hôte, DNS) n'est pas dans le message destiné à l'utilisateur
    assert error.value.message == "Serveur Zimbra injoignable"


def test_download_is_retried(settings: Settings):
    zimbra = FakeZimbra(messages=[FakeMessage("1", "2", 0, "", b"contenu")])
    statuses = iter([503])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, None)
        return httpx.Response(status) if status else zimbra.handle(request)

    with client_for(settings, handler).download_eml(VALID_TOKEN, "1") as chunks:
        assert b"".join(chunks) == b"contenu"


@pytest.mark.parametrize("status", [401, 403])
def test_download_with_expired_session(settings: Settings, status: int):
    client = client_for(settings, lambda request: httpx.Response(status))

    with pytest.raises(SessionExpired), client.download_eml(VALID_TOKEN, "1"):
        pass


@pytest.mark.parametrize("code", ["service.AUTH_REQUIRED", "service.AUTH_EXPIRED"])
def test_session_faults(settings: Settings, code: str):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"Fault": {"Reason": {"Text": "expiré"}, "Detail": {"Error": {"Code": code}}}}
        return httpx.Response(500, json={"Body": body})

    with pytest.raises(SessionExpired):
        client_for(settings, handler).get_root_folder(VALID_TOKEN)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json=["pas", "un", "objet"]),
        httpx.Response(200, json={"Body": "texte"}),
        httpx.Response(200, json={"Body": {"AutreResponse": {}}}),
        httpx.Response(500, json={"Body": {}}),
        soap_response("GetFolderResponse", {"folder": []}),
        soap_response("GetFolderResponse", {"folder": [{"name": "sans id"}]}),
    ],
)
def test_malformed_responses(settings: Settings, response: httpx.Response):
    with pytest.raises(UnexpectedResponse):
        client_for(settings, lambda request: response).get_root_folder(VALID_TOKEN)


def test_malformed_search_results(settings: Settings):
    client = client_for(settings, lambda request: soap_response("SearchResponse", {"m": [{"d": 1}]}))

    with pytest.raises(UnexpectedResponse):
        list(client.iter_messages(VALID_TOKEN, "inid:2"))


def test_login_refuses_an_empty_token(settings: Settings):
    client = client_for(settings, lambda request: soap_response("AuthResponse", {"authToken": ""}))

    with pytest.raises(ZimbraError):
        client.login("bernard@test.fr", "secret")


def test_end_session_invalidates_the_token(settings: Settings):
    zimbra = FakeZimbra()
    client = ZimbraClient(settings, transport=zimbra.transport())

    client.end_session(VALID_TOKEN)

    with pytest.raises(SessionExpired):
        client.get_account_name(VALID_TOKEN)


def test_shared_http_client_is_not_closed(settings: Settings):
    http = httpx.Client(transport=FakeZimbra().transport())

    with ZimbraClient(settings, http=http) as client:
        client.get_root_folder(VALID_TOKEN)

    assert not http.is_closed


def test_root_folder_ignores_links_and_searches(settings: Settings):
    client = ZimbraClient(settings, transport=FakeZimbra().transport())

    root = client.get_root_folder(VALID_TOKEN)

    assert {f.id for f in root.walk()} == {"1", "2", "257", "5", "3", "7"}
    assert {f.id for f in root.walk(frozenset({"2"}))} == {"1", "5", "3", "7"}


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"color": 5}, "#F66666"),
        ({"rgb": "#12ab9f", "color": 1}, "#12AB9F"),
        ({"rgb": "#12ab9", "color": 1}, "#5B9BF2"),
        ({"color": "x"}, None),
        ({"color": 42}, None),
        ({}, None),
    ],
)
def test_folder_color(data: dict, expected: str | None):
    assert _folder_color(data) == expected


def test_iterates_over_all_search_pages(settings: Settings):
    zimbra = FakeZimbra()
    zimbra.messages = [FakeMessage(str(i), "2", i, "", b"") for i in range(5)]
    client = ZimbraClient(settings.model_copy(update={"search_batch_size": 2}), transport=zimbra.transport())

    messages = list(client.iter_messages(VALID_TOKEN, "inid:2"))

    assert [m.id for m in messages] == ["0", "1", "2", "3", "4"]
    assert all(s["sortBy"] == "dateAsc" for s in zimbra.searches)
    assert [s.get("cursor") for s in zimbra.searches] == [
        None,
        {"id": "1", "sortVal": "1"},
        {"id": "3", "sortVal": "3"},
    ]


def test_pagination_is_stable_when_messages_arrive_or_move(settings: Settings):
    zimbra = FakeZimbra()
    zimbra.messages = [FakeMessage(str(i), "2", i, "", b"") for i in range(6)]
    client = ZimbraClient(settings.model_copy(update={"search_batch_size": 2}), transport=zimbra.transport())

    found = []
    for message in client.iter_messages(VALID_TOKEN, "inid:2"):
        found.append(message.id)
        if message.id == "1":
            # Pendant l'export : un message déjà vu est déplacé, un nouveau arrive
            zimbra.messages = [m for m in zimbra.messages if m.id != "0"]
            zimbra.messages.append(FakeMessage("9", "2", 100, "", b""))

    # Ni doublon ni message sauté
    assert found == ["0", "1", "2", "3", "4", "5", "9"]
