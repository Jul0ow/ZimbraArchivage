import io
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.main import app, get_client_factory
from app.zimbra import ZimbraClient
from tests.fake_zimbra import REST_URL, SOAP_URL, VALID_TOKEN, FakeMessage, FakeZimbra

PARIS = ZoneInfo("Europe/Paris")


def ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=PARIS).timestamp() * 1000)


@pytest.fixture
def zimbra() -> FakeZimbra:
    return FakeZimbra(messages=[
        FakeMessage("101", "2", ms(2024, 1, 10, 9, 30), "Réunion d'équipe", b"Subject: a\r\n\r\nA"),
        FakeMessage("102", "2", ms(2024, 3, 15, 23, 30), "Budget", b"Subject: b\r\n\r\nB"),
        FakeMessage("103", "2", ms(2024, 6, 1, 8, 0), "Été", b"Subject: c\r\n\r\nC"),
        FakeMessage("201", "257", ms(2024, 3, 1, 12, 0), "Projet X", b"Subject: d\r\n\r\nD"),
    ])


@pytest.fixture
def settings() -> Settings:
    return Settings(
        zimbra_soap_url=SOAP_URL,
        zimbra_rest_url=REST_URL + "/",
        search_batch_size=2,
        cookie_secure=False,
    )


@pytest.fixture
def client(zimbra: FakeZimbra, settings: Settings):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_client_factory] = lambda: (
        lambda: ZimbraClient(settings, transport=zimbra.transport(), sleep=lambda _: None)
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def logged_client(client: TestClient) -> TestClient:
    response = client.post("/api/auth/login", json={"username": "bernard@test.fr", "password": "secret"})
    assert response.status_code == 200
    return client


def test_login_sets_http_only_cookie(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "bernard@test.fr", "password": "secret"})

    assert response.status_code == 200
    assert response.json() == {"username": "bernard@test.fr"}
    set_cookie = response.headers["set-cookie"]
    assert f"zimbra_session={VALID_TOKEN}" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/api" in set_cookie


def test_login_with_wrong_password(client: TestClient):
    response = client.post("/api/auth/login", json={"username": "bernard@test.fr", "password": "faux"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Identifiant ou mot de passe incorrect"


def test_me_requires_session(client: TestClient):
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_returns_401(client: TestClient):
    client.cookies.set("zimbra_session", "jeton-expire", path="/api")

    response = client.get("/api/folders")

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expirée"}


def test_me(logged_client: TestClient):
    assert logged_client.get("/api/auth/me").json() == {"username": "bernard@test.fr"}


def test_logout_clears_cookie(logged_client: TestClient):
    response = logged_client.post("/api/auth/logout")

    assert response.status_code == 204
    assert logged_client.get("/api/auth/me").status_code == 401


def test_folders_only_lists_mail_folders(logged_client: TestClient):
    folders = logged_client.get("/api/folders").json()

    assert [(f["id"], f["name"]) for f in folders] == [("2", "Réception"), ("5", "Envoyé")]
    assert folders[0]["children"][0]["name"] == "Projets: 2024"
    assert folders[0]["message_count"] == 3


def test_folders_colors(logged_client: TestClient):
    folders = logged_client.get("/api/folders").json()

    inbox, sent = folders
    # Couleur prédéfinie 5 (rouge), couleur personnalisée prioritaire, pas de couleur
    assert inbox["color"] == "#F66666"
    assert inbox["children"][0]["color"] == "#12AB9F"
    assert sent["color"] is None


def test_summary_with_subfolders(logged_client: TestClient):
    response = logged_client.get("/api/export/summary", params={"folder_id": "2"})

    assert response.json()["folder_count"] == 2
    assert response.json()["message_count"] == 4


def test_summary_with_date_range(logged_client: TestClient, zimbra: FakeZimbra):
    response = logged_client.get("/api/export/summary", params={
        "folder_id": "2", "include_subfolders": "false", "start": "2024-01-11", "end": "2024-03-15",
    })

    assert response.json()["message_count"] == 1
    assert zimbra.searches[0]["query"] == "inid:2 and after:10/01/24 and before:16/03/24"


def test_summary_rejects_inverted_dates(logged_client: TestClient):
    response = logged_client.get("/api/export/summary", params={
        "folder_id": "2", "start": "2024-02-01", "end": "2024-01-01",
    })

    assert response.status_code == 422


def test_export_unknown_folder(logged_client: TestClient):
    assert logged_client.get("/api/export", params={"folder_id": "999"}).status_code == 404
    assert logged_client.get("/api/export", params={"folder_id": "7"}).status_code == 404


def test_export_zip_with_subfolders(logged_client: TestClient):
    response = logged_client.get("/api/export", params={"folder_id": "2"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"].startswith('attachment; filename="Reception_')
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == [
        "Inbox/20240110_R_union d__quipe_101.eml",
        "Inbox/20240315_Budget_102.eml",
        "Inbox/20240601__t__103.eml",
        "Inbox/Projets_ 2024/20240301_Projet X_201.eml",
    ]
    assert archive.read("Inbox/20240315_Budget_102.eml") == b"Subject: b\r\n\r\nB"


def test_export_zip_without_subfolders_and_date_range(logged_client: TestClient):
    response = logged_client.get("/api/export", params={
        "folder_id": "2", "include_subfolders": "false", "end": "2024-03-15",
    })

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == [
        "Inbox/20240110_R_union d__quipe_101.eml",
        "Inbox/20240315_Budget_102.eml",
    ]


def test_export_lists_failed_downloads(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.failing_message_ids.add("102")

    response = logged_client.get("/api/export", params={"folder_id": "2", "include_subfolders": "false"})

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "Inbox/20240315_Budget_102.eml" not in archive.namelist()
    errors = archive.read("erreurs.txt").decode()
    assert "Inbox/20240315_Budget_102.eml" in errors
    assert "code HTTP retourné = 500" in errors
