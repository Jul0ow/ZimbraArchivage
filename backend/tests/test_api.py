import gc
import io
import json
import zipfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import archive as archive_module
from app import main
from app.config import Settings, get_settings
from app.limits import ExportLimiter, LoginThrottle
from app.main import app, get_client_factory, get_export_limiter, get_login_throttle
from app.progress import ProgressRegistry, get_progress_registry
from app.sessions import get_session_cipher
from app.zimbra import ZimbraClient
from tests.fake_zimbra import REST_URL, SOAP_URL, VALID_TOKEN, FakeMessage, FakeZimbra

PARIS = ZoneInfo("Europe/Paris")
SECRET = Fernet.generate_key().decode()


def ms(*args: int) -> int:
    return int(datetime(*args, tzinfo=PARIS).timestamp() * 1000)


@pytest.fixture
def zimbra() -> FakeZimbra:
    return FakeZimbra(
        messages=[
            FakeMessage(
                "101",
                "2",
                ms(2024, 1, 10, 9, 30),
                "Réunion d'équipe",
                b"Subject: a\r\n\r\nA",
                sender="alice@test.fr",
                recipients="bernard@test.fr",
                sender_name="Alice Martin",
            ),
            FakeMessage(
                "102",
                "2",
                ms(2024, 3, 15, 23, 30),
                "Budget",
                b"Subject: b\r\n\r\nB",
                sender="compta@test.fr",
                recipients="bernard@test.fr, direction@test.fr",
            ),
            FakeMessage(
                "103",
                "2",
                ms(2024, 6, 1, 8, 0),
                "Été",
                b"Subject: c\r\n\r\nC",
                sender="alice@test.fr",
                recipients="tous@test.fr",
                sender_name="Alice Martin",
            ),
            FakeMessage(
                "201",
                "257",
                ms(2024, 3, 1, 12, 0),
                "Projet X",
                b"Subject: d\r\n\r\nD",
                sender="chef@test.fr",
                recipients="bernard@test.fr",
            ),
            FakeMessage("301", "3", ms(2024, 2, 1, 12, 0), "Spam supprimé", b"Subject: e\r\n\r\nE"),
        ]
    )


@pytest.fixture
def settings() -> Settings:
    # Aucune variable d'environnement ni fichier .env du poste ne doit changer les résultats
    return Settings(
        _env_file=None,
        zimbra_soap_url=SOAP_URL,
        zimbra_rest_url=REST_URL + "/",
        zone_id="Europe/Paris",
        search_batch_size=2,
        max_file_name_length=50,
        download_concurrency=3,
        cookie_secure=False,
        session_secret=SECRET,
    )


@pytest.fixture
def registry() -> ProgressRegistry:
    return ProgressRegistry()


@pytest.fixture
def limiter(settings: Settings) -> ExportLimiter:
    return ExportLimiter(settings.max_concurrent_exports, settings.max_exports_per_session)


@pytest.fixture
def client(zimbra: FakeZimbra, settings: Settings, registry: ProgressRegistry, limiter: ExportLimiter):
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_client_factory] = lambda: (
        lambda: ZimbraClient(settings, transport=zimbra.transport(), sleep=lambda _: None)
    )
    app.dependency_overrides[get_progress_registry] = lambda: registry
    app.dependency_overrides[get_export_limiter] = lambda: limiter
    throttle = LoginThrottle(settings.max_login_failures_per_user, 20)
    app.dependency_overrides[get_login_throttle] = lambda: throttle
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def seal(settings: Settings, token: str) -> str:
    return get_session_cipher(settings.session_secret, settings.session_max_age).seal(token)


def login(client: TestClient, password: str = "secret"):
    return client.post("/api/auth/login", json={"username": "bernard@test.fr", "password": password})


@pytest.fixture
def logged_client(client: TestClient) -> TestClient:
    assert login(client).status_code == 200
    return client


def test_login_sets_encrypted_http_only_cookie(client: TestClient, settings: Settings):
    response = login(client)

    assert response.status_code == 200
    assert response.json() == {"username": "bernard@test.fr"}
    set_cookie = response.headers["set-cookie"]
    # Le jeton Zimbra n'apparaît jamais en clair dans le cookie
    assert VALID_TOKEN not in set_cookie
    cookie = client.cookies.get("zimbra_session")
    assert get_session_cipher(SECRET, settings.session_max_age).open(cookie) == VALID_TOKEN
    assert "HttpOnly" in set_cookie
    assert "Path=/api" in set_cookie
    assert "SameSite=strict" in set_cookie
    # Durée de vie du jeton (2 jours) plafonnée à la durée maximale d'une session (8 h)
    assert "Max-Age=28800" in set_cookie


def test_login_cookie_is_secure_by_default(client: TestClient, settings: Settings):
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"cookie_secure": True})

    response = login(client)

    assert "Secure" in response.headers["set-cookie"]


def test_login_with_wrong_password(client: TestClient):
    response = login(client, "faux")

    assert response.status_code == 401
    assert response.json()["detail"] == "Identifiant ou mot de passe incorrect"


def test_login_is_throttled_after_repeated_failures(client: TestClient, zimbra: FakeZimbra):
    for _ in range(5):
        assert login(client, "faux").status_code == 401

    response = login(client)

    assert response.status_code == 429
    # La tentative bloquée n'est pas relayée à Zimbra
    assert len(zimbra.auth_requests) == 5


def test_login_sends_client_ip_to_zimbra(client: TestClient, zimbra: FakeZimbra):
    login(client)

    assert zimbra.auth_requests[0].headers["X-Originating-IP"] == "testclient"


def test_login_rejects_invalid_body(client: TestClient):
    assert client.post("/api/auth/login", json={"username": ""}).status_code == 422


def test_me_requires_session(client: TestClient):
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("cookie", ["jeton-brut", "gAAAAAfalsifie"])
def test_invalid_session_cookie_returns_401_and_clears_it(client: TestClient, cookie: str):
    client.cookies.set("zimbra_session", cookie, path="/api")

    response = client.get("/api/folders")

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expirée"}
    assert 'zimbra_session=""' in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_expired_zimbra_session_returns_401(client: TestClient, settings: Settings):
    client.cookies.set("zimbra_session", seal(settings, "jeton-expire"), path="/api")

    response = client.get("/api/folders")

    assert response.status_code == 401
    assert response.json() == {"detail": "Session expirée"}


def test_me(logged_client: TestClient):
    assert logged_client.get("/api/auth/me").json() == {"username": "bernard@test.fr"}


def test_logout_ends_zimbra_session(logged_client: TestClient, zimbra: FakeZimbra):
    cookie = logged_client.cookies.get("zimbra_session")

    response = logged_client.post("/api/auth/logout")

    assert response.status_code == 204
    assert zimbra.ended_sessions == [VALID_TOKEN]
    assert logged_client.get("/api/auth/me").status_code == 401
    # Un cookie copié avant la déconnexion n'est plus utilisable
    logged_client.cookies.set("zimbra_session", cookie, path="/api")
    assert logged_client.get("/api/auth/me").status_code == 401


def test_logout_without_session(client: TestClient):
    assert client.post("/api/auth/logout").status_code == 204


def test_api_responses_have_security_headers(client: TestClient):
    response = client.get("/api/health")

    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_docs_are_disabled_by_default(client: TestClient):
    assert client.get("/api/docs").status_code == 404
    assert client.get("/api/openapi.json").status_code == 404


def test_folders_only_lists_mail_folders(logged_client: TestClient):
    folders = logged_client.get("/api/folders").json()

    assert [(f["id"], f["name"]) for f in folders] == [
        ("1", "Toute la boîte aux lettres"),
        ("2", "Réception"),
        ("5", "Envoyé"),
        ("3", "Corbeille"),
    ]
    assert folders[1]["children"][0]["name"] == "Projets: 2024"
    assert folders[1]["message_count"] == 3
    # Toute la boîte : Réception et son sous-dossier, sans la Corbeille
    assert folders[0]["message_count"] == 4
    assert folders[0]["children"] == []


def test_folders_colors(logged_client: TestClient):
    _, inbox, sent, _ = logged_client.get("/api/folders").json()

    # Couleur prédéfinie 5 (rouge), couleur personnalisée prioritaire, pas de couleur
    assert inbox["color"] == "#F66666"
    assert inbox["children"][0]["color"] == "#12AB9F"
    assert sent["color"] is None


def test_zimbra_fault_returns_502_without_raw_reason(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.search_fault = "mail.QUERY_PARSE_ERROR"

    response = logged_client.get("/api/export/summary", params={"folder_id": "2"})

    assert response.status_code == 502
    assert response.json() == {"detail": "Erreur Zimbra : code mail.QUERY_PARSE_ERROR"}


@pytest.mark.parametrize("status", [502, 503, 504])
def test_zimbra_unavailable_returns_502(logged_client: TestClient, zimbra: FakeZimbra, status: int):
    zimbra.unavailable_status = status

    response = logged_client.get("/api/folders")

    assert response.status_code == 502
    assert response.json() == {"detail": "Erreur Zimbra : Zimbra indisponible malgré 6 essais"}


def test_unexpected_zimbra_response_returns_502(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.root = {"name": "sans identifiant"}

    response = logged_client.get("/api/folders")

    assert response.status_code == 502
    assert response.json()["detail"].startswith("Erreur Zimbra : Réponse inattendue")


def test_address_suggestions_from_contacts_then_messages(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.contacts = [("Alain Térieur", "alain@test.fr"), ("Alice M.", "ALICE@test.fr")]

    response = logged_client.get("/api/addresses", params={"q": "al", "field": "from"})

    # alice@test.fr, aussi expéditrice de messages, n'apparaît qu'une fois
    assert response.json() == [
        {"address": "alain@test.fr", "name": "Alain Térieur"},
        {"address": "ALICE@test.fr", "name": "Alice M."},
    ]
    assert zimbra.searches[0]["query"] == "from:al*"
    assert zimbra.searches[0]["recip"] == "2"


def test_address_suggestions_from_message_senders(logged_client: TestClient):
    response = logged_client.get("/api/addresses", params={"q": "ali"})

    assert response.json() == [{"address": "alice@test.fr", "name": "Alice Martin"}]


def test_address_suggestions_for_recipients(logged_client: TestClient, zimbra: FakeZimbra):
    response = logged_client.get("/api/addresses", params={"q": "dir", "field": "to"})

    # L'expéditeur (compta@test.fr) du message trouvé n'est pas proposé
    assert response.json() == [{"address": "direction@test.fr", "name": ""}]
    assert zimbra.searches[0]["query"] == "tocc:dir*"


def test_address_suggestions_skip_contact_groups(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.contacts = [("Équipe", "Équipe"), ("Alice M.", "alice.m@test.fr")]

    response = logged_client.get("/api/addresses", params={"q": "éq"})

    assert response.json() == []


def test_address_suggestions_are_limited(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.contacts = [(f"Alex {i}", f"alex{i}@test.fr") for i in range(12)]

    response = logged_client.get("/api/addresses", params={"q": "alex"})

    assert len(response.json()) == 10
    assert zimbra.searches == []


def test_address_suggestions_without_word_search(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.contacts = [("Alice Martin", "alice@test.fr")]

    response = logged_client.get("/api/addresses", params={"q": "alice mar"})

    # Plusieurs mots : seule l'autocomplétion de Zimbra est utilisée
    assert response.status_code == 200
    assert zimbra.searches == []


def test_address_suggestions_ignore_failing_source(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.search_fault = "mail.TOO_MANY_QUERY_TERMS_EXPANDED"
    zimbra.autocomplete_fault = "service.FAILURE"

    response = logged_client.get("/api/addresses", params={"q": "al"})

    assert response.status_code == 200
    assert response.json() == []


def test_address_suggestions_require_two_characters(logged_client: TestClient):
    assert logged_client.get("/api/addresses", params={"q": "a"}).status_code == 422


def test_address_suggestions_with_expired_session(client: TestClient, settings: Settings):
    client.cookies.set("zimbra_session", seal(settings, "jeton-expire"), path="/api")

    assert client.get("/api/addresses", params={"q": "al"}).status_code == 401


def test_summary_with_subfolders(logged_client: TestClient):
    response = logged_client.get("/api/export/summary", params={"folder_id": "2"})

    assert response.json() == {
        "folder_count": 2,
        "message_count": 4,
        "total_size": sum(len(f"Subject: {c}\r\n\r\n{c.upper()}") for c in "abcd"),
    }


def test_summary_with_date_range(logged_client: TestClient, zimbra: FakeZimbra):
    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "start": "2024-01-11",
            "end": "2024-03-15",
        },
    )

    assert response.json()["message_count"] == 1
    assert zimbra.searches[0]["query"] == "inid:2 and after:10/01/24 and before:16/03/24"


@pytest.mark.parametrize(
    "start, end, expected",
    [
        # Bornes incluses : le premier et le dernier jour de la période comptent
        ("2024-01-10", "2024-01-10", 1),
        ("2024-03-15", "2024-03-15", 1),
        ("2024-01-11", "2024-03-14", 0),
        ("2024-03-16", None, 1),
        (None, "2024-01-09", 0),
    ],
)
def test_summary_date_bounds(logged_client: TestClient, start, end, expected):
    params = {"folder_id": "2", "include_subfolders": "false"}
    params |= {k: v for k, v in {"start": start, "end": end}.items() if v}

    response = logged_client.get("/api/export/summary", params=params)

    assert response.json()["message_count"] == expected


def test_summary_rejects_inverted_dates(logged_client: TestClient):
    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "start": "2024-02-01",
            "end": "2024-01-01",
        },
    )

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
    assert archive.testzip() is None
    # Messages dans l'ordre chronologique de chaque dossier, malgré les téléchargements parallèles
    assert archive.namelist() == [
        "Inbox/20240110_R_union d__quipe_101.eml",
        "Inbox/20240315_Budget_102.eml",
        "Inbox/20240601__t__103.eml",
        "Inbox/Projets_ 2024/20240301_Projet X_201.eml",
    ]
    assert archive.read("Inbox/20240315_Budget_102.eml") == b"Subject: b\r\n\r\nB"


def test_eml_names_use_the_configured_time_zone(logged_client: TestClient, zimbra: FakeZimbra):
    # 00:30 à Paris le 16 mars, soit le 15 mars à 23:30 UTC
    zimbra.messages.append(FakeMessage("104", "2", ms(2024, 3, 16, 0, 30), "Minuit", b"x"))

    response = logged_client.get("/api/export", params={"folder_id": "2", "include_subfolders": "false"})

    assert "Inbox/20240316_Minuit_104.eml" in exported_names(response)


def test_export_zip_without_subfolders_and_date_range(logged_client: TestClient):
    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "end": "2024-03-15",
        },
    )

    assert exported_names(response) == [
        "Inbox/20240110_R_union d__quipe_101.eml",
        "Inbox/20240315_Budget_102.eml",
    ]


def test_export_of_empty_folder_is_a_valid_zip(logged_client: TestClient, zimbra: FakeZimbra):
    response = logged_client.get("/api/export", params={"folder_id": "5"})

    assert exported_names(response) == []
    # Dossier vide d'après GetFolderRequest : aucune recherche n'est faite
    assert zimbra.searches == []


def test_export_progress(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.failing_message_ids = {"102"}

    response = logged_client.get("/api/export", params={"folder_id": "2", "export_id": "export-1234"})
    assert response.status_code == 200

    progress = logged_client.get("/api/export/progress", params={"export_id": "export-1234"})
    # Les messages en erreur comptent aussi comme traités
    assert progress.json() == {"done": 4, "total": 4, "finished": True, "error": None}


def test_export_progress_unknown(logged_client: TestClient):
    response = logged_client.get("/api/export/progress", params={"export_id": "inconnu-1234"})

    assert response.status_code == 404


def test_export_progress_of_another_session(logged_client: TestClient, settings: Settings):
    logged_client.get("/api/export", params={"folder_id": "2", "export_id": "export-1234"})
    logged_client.cookies.set("zimbra_session", seal(settings, "autre-jeton"), path="/api")

    response = logged_client.get("/api/export/progress", params={"export_id": "export-1234"})

    assert response.status_code == 404


def test_export_rejects_invalid_export_id(logged_client: TestClient):
    response = logged_client.get("/api/export", params={"folder_id": "2", "export_id": "a/b"})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "site, allowed",
    [
        ("same-origin", True),
        ("none", True),
        ("cross-site", False),
        ("same-site", False),
    ],
)
def test_export_cannot_be_started_from_another_site(logged_client: TestClient, site: str, allowed: bool):
    response = logged_client.get("/api/export", params={"folder_id": "5"}, headers={"Sec-Fetch-Site": site})

    assert (response.status_code == 200) is allowed
    if not allowed:
        assert response.status_code == 403


def test_concurrent_exports_are_limited(logged_client: TestClient, limiter: ExportLimiter):
    slots = [limiter.acquire(VALID_TOKEN) for _ in range(limiter.max_per_session)]

    assert logged_client.get("/api/export", params={"folder_id": "5"}).status_code == 429
    assert logged_client.get("/api/export/summary", params={"folder_id": "5"}).status_code == 429

    slots[0].release()
    assert logged_client.get("/api/export", params={"folder_id": "5"}).status_code == 200
    # La place de l'export terminé est rendue
    assert logged_client.get("/api/export", params={"folder_id": "5"}).status_code == 200


def test_export_slot_is_released_when_response_is_never_sent(
    zimbra: FakeZimbra, settings: Settings, registry: ProgressRegistry, limiter: ExportLimiter
):
    response = main.export(
        token=VALID_TOKEN,
        settings=settings,
        client_factory=lambda: ZimbraClient(settings, transport=zimbra.transport()),
        params=main.export_params(folder_id="2"),
        progress_registry=registry,
        limiter=limiter,
        export_id="export-1234",
    )
    assert registry.get(VALID_TOKEN, "export-1234").finished is False

    del response
    gc.collect()

    assert registry.get(VALID_TOKEN, "export-1234").finished is True
    slots = [limiter.acquire(VALID_TOKEN) for _ in range(limiter.max_per_session)]
    assert len(slots) == limiter.max_per_session


def test_export_lists_failed_downloads(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.failing_message_ids.add("102")

    response = logged_client.get("/api/export", params={"folder_id": "2", "include_subfolders": "false"})

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert "Inbox/20240315_Budget_102.eml" not in archive.namelist()
    errors = archive.read("erreurs.txt").decode()
    assert "Inbox/20240315_Budget_102.eml" in errors
    assert "code HTTP retourné = 500" in errors
    assert "INTERROMPU" not in errors


def test_network_error_during_download_leaves_no_truncated_entry(
    logged_client: TestClient, zimbra: FakeZimbra
):
    zimbra.broken_stream_ids.add("102")

    response = logged_client.get("/api/export", params={"folder_id": "2", "include_subfolders": "false"})

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.testzip() is None
    assert "Inbox/20240315_Budget_102.eml" not in archive.namelist()
    assert "Inbox/20240601__t__103.eml" in archive.namelist()
    errors = archive.read("erreurs.txt").decode()
    assert "Inbox/20240315_Budget_102.eml : Erreur réseau pendant le téléchargement" in errors
    # Le détail technique de l'erreur n'est pas recopié dans l'archive
    assert "coupure" not in errors


def test_session_expired_while_listing_messages_interrupts_export(
    logged_client: TestClient, zimbra: FakeZimbra, registry: ProgressRegistry
):
    zimbra.expire_after_searches = 1

    response = logged_client.get("/api/export", params={"folder_id": "2", "export_id": "export-1234"})

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["erreurs.txt"]
    errors = archive.read("erreurs.txt").decode().splitlines()
    assert errors[0].startswith("EXPORT INTERROMPU : session Zimbra expirée")
    assert registry.get(VALID_TOKEN, "export-1234").error.startswith("session Zimbra expirée")


def test_session_expired_while_downloading_interrupts_export(
    logged_client: TestClient, zimbra: FakeZimbra, registry: ProgressRegistry
):
    zimbra.rest_statuses["102"] = 401

    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "export_id": "export-1234",
        },
    )

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["Inbox/20240110_R_union d__quipe_101.eml", "erreurs.txt"]
    assert archive.read("erreurs.txt").decode().startswith("EXPORT INTERROMPU : session Zimbra expirée")
    progress = registry.get(VALID_TOKEN, "export-1234")
    assert progress.finished is True
    assert progress.error.startswith("session Zimbra expirée")


def test_export_gives_up_when_zimbra_stops_responding(
    logged_client: TestClient, zimbra: FakeZimbra, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(archive_module, "MAX_CONSECUTIVE_FAILURES", 2)
    for message_id in ("101", "102", "103"):
        zimbra.rest_statuses[message_id] = 503

    response = logged_client.get("/api/export", params={"folder_id": "2", "include_subfolders": "false"})

    errors = zipfile.ZipFile(io.BytesIO(response.content)).read("erreurs.txt").decode().splitlines()
    assert errors[0].startswith("EXPORT INTERROMPU : Zimbra ne répond plus")
    # Le troisième message n'est même pas traité
    assert len(errors) == 3


ATTACHMENT_EML = (
    b"From: alice@test.fr\r\n"
    b"Subject: Devis\r\n"
    b"MIME-Version: 1.0\r\n"
    b'Content-Type: multipart/mixed; boundary="sep"\r\n'
    b"\r\n"
    b"--sep\r\n"
    b"Content-Type: text/plain; charset=utf-8\r\n"
    b"\r\n"
    b"Voir le devis.\r\n"
    b"--sep\r\n"
    b'Content-Type: application/pdf; name="devis.pdf"\r\n'
    b'Content-Disposition: attachment; filename="../devis.pdf"\r\n'
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"JVBERi0xLjQ=\r\n"
    b"--sep\r\n"
    b"Content-Type: application/pdf\r\n"
    b"Content-Disposition: attachment; filename*=UTF-8''devis.pdf\r\n"
    b"\r\n"
    b"deux\r\n"
    b"--sep\r\n"
    b"Content-Type: image/png\r\n"
    b"Content-Disposition: attachment\r\n"
    b"Content-Transfer-Encoding: base64\r\n"
    b"\r\n"
    b"iVBORw==\r\n"
    b"--sep\r\n"
    b"Content-Type: message/rfc822\r\n"
    b"Content-Disposition: attachment\r\n"
    b"\r\n"
    b"Subject: Courriel joint\r\n"
    b"\r\n"
    b"Bonjour\r\n"
    b"--sep--\r\n"
)


def filter_param(*groups: list[dict], operator: str = "or", group_operator: str = "and") -> str:
    return json.dumps(
        {
            "operator": operator,
            "groups": [{"operator": group_operator, "conditions": g} for g in groups],
        }
    )


def exported_names(response) -> list[str]:
    assert response.status_code == 200
    return sorted(zipfile.ZipFile(io.BytesIO(response.content)).namelist())


def test_export_with_or_filter(logged_client: TestClient, zimbra: FakeZimbra):
    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "filter": filter_param(
                [{"field": "subject", "value": "budget"}],
                [{"field": "from", "value": "chef@test.fr"}],
            ),
        },
    )

    assert exported_names(response) == [
        "Inbox/20240315_Budget_102.eml",
        "Inbox/Projets_ 2024/20240301_Projet X_201.eml",
    ]
    assert zimbra.searches[0]["query"] == "inid:2 and ((subject:budget) or (from:chef@test.fr))"


def test_summary_with_and_filter(logged_client: TestClient):
    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "filter": filter_param(
                [
                    {"field": "from", "value": "alice"},
                    {"field": "to", "operator": "not_contains", "value": "tous@test.fr"},
                ],
            ),
        },
    )

    assert response.json()["message_count"] == 1


@pytest.mark.parametrize(
    "value, expected",
    [
        # Recherche par mots, comme Zimbra : un début de mot ne suffit pas sans joker
        ("budg", 0),
        ("budg*", 1),
        ("BUDGET", 1),
    ],
)
def test_filter_matches_words_not_substrings(logged_client: TestClient, value: str, expected: int):
    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "filter": filter_param(
                [{"field": "subject", "value": value}],
            ),
        },
    )

    assert response.json()["message_count"] == expected


def test_filter_combined_with_and_between_groups(logged_client: TestClient):
    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "filter": filter_param(
                [{"field": "to", "value": "bernard"}],
                [{"field": "subject", "value": "budget"}, {"field": "subject", "value": "projet"}],
                operator="and",
                group_operator="or",
            ),
        },
    )

    # (À bernard) ET (objet budget OU objet projet) : 102 et 201
    assert response.json()["message_count"] == 2


def test_invalid_filter(logged_client: TestClient):
    for value in ["pas du json", json.dumps({"groups": []})]:
        response = logged_client.get("/api/export/summary", params={"folder_id": "2", "filter": value})
        assert response.status_code == 422
        assert response.json()["detail"].startswith("Filtre invalide")


def test_invalid_filter_explains_the_problem(logged_client: TestClient):
    groups = [[{"field": "subject", "value": "x"}]] * 11

    response = logged_client.get(
        "/api/export/summary",
        params={
            "folder_id": "2",
            "filter": filter_param(*groups),
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("Filtre invalide (groups :")


def test_whole_mailbox_excludes_trash(logged_client: TestClient):
    response = logged_client.get("/api/export", params={"folder_id": "1"})

    assert response.headers["content-disposition"].startswith('attachment; filename="Boite_aux_lettres_')
    names = exported_names(response)
    assert len(names) == 4
    assert not any(name.startswith("Trash/") for name in names)


def test_whole_mailbox_always_includes_subfolders(logged_client: TestClient):
    response = logged_client.get("/api/export", params={"folder_id": "1", "include_subfolders": "false"})

    assert len(exported_names(response)) == 4


def test_whole_mailbox_excludes_trash_subfolders(logged_client: TestClient, zimbra: FakeZimbra):
    trash = zimbra.root["folder"][2]
    trash["folder"] = [{"id": "400", "name": "Vieux", "absFolderPath": "/Trash/Vieux", "view": "message"}]
    zimbra.messages.append(FakeMessage("401", "400", ms(2024, 1, 1, 0, 0), "Vieux", b"v"))

    response = logged_client.get("/api/export", params={"folder_id": "1"})

    assert not any(name.startswith("Trash/") for name in exported_names(response))


def test_trash_can_be_exported_on_its_own(logged_client: TestClient):
    response = logged_client.get("/api/export", params={"folder_id": "3"})

    assert exported_names(response) == ["Trash/20240201_Spam supprim__301.eml"]


def test_folder_paths_cannot_escape_the_archive(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.root["folder"].append(
        {"id": "500", "name": "..", "absFolderPath": "/../../etc", "view": "message"}
    )
    zimbra.messages.append(FakeMessage("501", "500", ms(2024, 1, 1, 12, 0), "x", b"x"))

    response = logged_client.get("/api/export", params={"folder_id": "500"})

    assert exported_names(response) == ["_/_/etc/20240101_x_501.eml"]
    assert response.headers["content-disposition"].startswith('attachment; filename="dossier_')


def test_archive_file_name_cannot_inject_headers(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.root["folder"].append(
        {"id": "500", "name": 'a"b\r\nSet-Cookie: x', "absFolderPath": "/a", "view": "message", "n": 0}
    )

    response = logged_client.get("/api/export", params={"folder_id": "500"})

    assert response.headers["content-disposition"].startswith('attachment; filename="a_b_Set-Cookie_x_')
    assert "set-cookie" not in response.headers


def test_folders_with_same_sanitized_name_keep_all_messages(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.root["folder"] += [
        {"id": "600", "name": "a:b", "absFolderPath": "/a:b", "view": "message"},
        {"id": "601", "name": "a?b", "absFolderPath": "/a?b", "view": "message"},
    ]
    zimbra.messages += [
        FakeMessage("1", "600", ms(2024, 1, 1, 12, 0), "x", b"premier"),
        FakeMessage("1", "601", ms(2024, 1, 1, 12, 0), "x", b"second"),
    ]

    response = logged_client.get("/api/export", params={"folder_id": "1"})

    names = exported_names(response)
    assert "a_b/20240101_x_1.eml" in names
    assert "a_b/20240101_x_1 (2).eml" in names


def test_export_only_messages_with_attachments(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.messages.append(FakeMessage("104", "2", ms(2024, 7, 1, 10, 0), "Devis", ATTACHMENT_EML))

    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "only_with_attachments": "true",
        },
    )

    assert exported_names(response) == ["Inbox/20240701_Devis_104.eml"]
    assert zimbra.searches[0]["query"] == "inid:2 and has:attachment"


def test_export_extracts_attachments(logged_client: TestClient, zimbra: FakeZimbra):
    zimbra.messages.append(FakeMessage("104", "2", ms(2024, 7, 1, 10, 0), "Devis", ATTACHMENT_EML))

    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "extract_attachments": "true",
            "filter": filter_param([{"field": "subject", "value": "devis"}]),
        },
    )

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == [
        "Inbox/20240701_Devis_104.eml",
        "Inbox/20240701_Devis_104/devis (2).pdf",
        "Inbox/20240701_Devis_104/devis.pdf",
        "Inbox/20240701_Devis_104/piece_jointe_3.png",
        "Inbox/20240701_Devis_104/piece_jointe_4.eml",
    ]
    assert archive.read("Inbox/20240701_Devis_104.eml") == ATTACHMENT_EML
    assert archive.read("Inbox/20240701_Devis_104/devis.pdf") == b"%PDF-1.4"
    assert archive.read("Inbox/20240701_Devis_104/devis (2).pdf") == b"deux"
    assert b"Subject: Courriel joint" in archive.read("Inbox/20240701_Devis_104/piece_jointe_4.eml")
    # Les formats déjà compressés ne sont pas recompressés
    assert archive.getinfo("Inbox/20240701_Devis_104/piece_jointe_3.png").compress_type == zipfile.ZIP_STORED
    assert archive.getinfo("Inbox/20240701_Devis_104.eml").compress_type == zipfile.ZIP_DEFLATED


def test_attachments_of_large_messages_are_not_extracted(
    logged_client: TestClient, zimbra: FakeZimbra, settings: Settings
):
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"max_attachment_extraction_size": 100}
    )
    zimbra.messages.append(FakeMessage("104", "2", ms(2024, 7, 1, 10, 0), "Devis", ATTACHMENT_EML))

    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "2",
            "include_subfolders": "false",
            "extract_attachments": "true",
            "only_with_attachments": "true",
        },
    )

    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["Inbox/20240701_Devis_104.eml", "erreurs.txt"]
    assert "courriel trop volumineux" in archive.read("erreurs.txt").decode()


def test_extract_attachments_of_simple_message(logged_client: TestClient):
    response = logged_client.get(
        "/api/export",
        params={
            "folder_id": "257",
            "extract_attachments": "true",
        },
    )

    assert exported_names(response) == ["Inbox/Projets_ 2024/20240301_Projet X_201.eml"]
