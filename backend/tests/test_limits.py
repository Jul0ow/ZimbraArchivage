import time

import pytest
from cryptography.fernet import Fernet

from app.limits import LOGIN_WINDOW_SECONDS, ExportLimiter, LoginThrottle, TooManyExports
from app.sessions import SessionCipher


def test_exports_are_limited_per_session_and_in_total():
    limiter = ExportLimiter(max_total=3, max_per_session=2)

    first = limiter.acquire("a")
    limiter.acquire("a")
    with pytest.raises(TooManyExports):
        limiter.acquire("a")
    limiter.acquire("b")
    with pytest.raises(TooManyExports):
        limiter.acquire("c")

    first.release()
    # Libérer deux fois la même place ne rend qu'une place
    first.release()
    limiter.acquire("c")
    with pytest.raises(TooManyExports):
        limiter.acquire("d")


def test_login_throttle_per_user_and_ip():
    now = 0.0
    throttle = LoginThrottle(max_per_user=2, max_per_ip=3, clock=lambda: now)

    throttle.record_failure("Bernard@test.fr", "10.0.0.1")
    assert not throttle.is_blocked("bernard@test.fr", "10.0.0.2")
    throttle.record_failure("bernard@test.fr ", "10.0.0.2")
    # Bloqué quelle que soit l'adresse IP, et quelle que soit la casse de l'identifiant
    assert throttle.is_blocked("BERNARD@test.fr", "10.0.0.3")

    throttle.record_failure("alice@test.fr", "10.0.0.1")
    throttle.record_failure("claire@test.fr", "10.0.0.1")
    # L'adresse IP a atteint sa limite pour tous les identifiants
    assert throttle.is_blocked("denis@test.fr", "10.0.0.1")
    assert not throttle.is_blocked("denis@test.fr", "10.0.0.9")

    now = LOGIN_WINDOW_SECONDS + 1
    assert not throttle.is_blocked("bernard@test.fr", "10.0.0.1")


def test_successful_login_resets_the_user_counter():
    throttle = LoginThrottle(max_per_user=2, max_per_ip=10)
    throttle.record_failure("bernard@test.fr", None)

    throttle.reset("bernard@test.fr")
    throttle.record_failure("bernard@test.fr", None)

    assert not throttle.is_blocked("bernard@test.fr", None)


def test_session_cookie_roundtrip_and_tampering():
    cipher = SessionCipher(Fernet.generate_key().decode(), max_age=3600)

    sealed = cipher.seal("jeton")

    assert sealed != "jeton"
    assert cipher.open(sealed) == "jeton"
    assert cipher.open(sealed[:-2] + "AA") is None
    assert cipher.open("jeton") is None
    # Un cookie chiffré avec une autre clé est refusé
    assert SessionCipher(Fernet.generate_key().decode(), 3600).open(sealed) is None


def test_session_cookie_expires(monkeypatch: pytest.MonkeyPatch):
    cipher = SessionCipher(Fernet.generate_key().decode(), max_age=60)
    sealed = cipher.seal("jeton")

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + 120)

    assert cipher.open(sealed) is None


def test_session_cipher_without_secret_generates_a_key():
    cipher = SessionCipher("", max_age=60)

    assert cipher.open(cipher.seal("jeton")) == "jeton"
