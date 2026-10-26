import httpx
import pytest

from app.config import Settings
from app.zimbra import ZimbraClient, ZimbraError
from tests.fake_zimbra import REST_URL, SOAP_URL, VALID_TOKEN, FakeMessage, FakeZimbra

SETTINGS = Settings(zimbra_soap_url=SOAP_URL, zimbra_rest_url=REST_URL)


def test_retries_while_zimbra_is_unavailable():
    zimbra = FakeZimbra()
    statuses = iter([503, 502])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses, None)
        return httpx.Response(status) if status else zimbra.handle(request)

    client = ZimbraClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=sleeps.append)

    assert client.login("bernard@test.fr", "secret").value == VALID_TOKEN
    assert sleeps == [3, 9]


def test_gives_up_after_max_tries():
    client = ZimbraClient(
        SETTINGS,
        transport=httpx.MockTransport(lambda request: httpx.Response(504)),
        sleep=lambda _: None,
    )

    with pytest.raises(ZimbraError, match="indisponible"):
        client.login("bernard@test.fr", "secret")


def test_root_folder_ignores_links_and_searches():
    client = ZimbraClient(SETTINGS, transport=FakeZimbra().transport())

    root = client.get_root_folder(VALID_TOKEN)

    assert {f.id for f in root.walk()} == {"1", "2", "257", "5", "3", "7"}


def test_iterates_over_all_search_pages():
    zimbra = FakeZimbra()
    zimbra.messages = [FakeMessage(str(i), "2", 0, "", b"") for i in range(5)]
    client = ZimbraClient(
        Settings(zimbra_soap_url=SOAP_URL, zimbra_rest_url=REST_URL, search_batch_size=2),
        transport=zimbra.transport(),
    )

    messages = list(client.iter_messages(VALID_TOKEN, "inid:2"))

    assert [m.id for m in messages] == ["0", "1", "2", "3", "4"]
    assert [s["offset"] for s in zimbra.searches] == [0, 2, 4]
