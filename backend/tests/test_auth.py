"""Тесты входа по общему паролю (FastAPI TestClient).

Запуск: `cd backend && .venv/bin/python -m pytest tests/test_auth.py`
или без pytest: `.venv/bin/python -m tests.test_auth`.
"""

import os
import tempfile

# БД — во временной папке, чтобы тест не трогал data/easy_week.db. До импорта app!
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "test.db"))

from fastapi.testclient import TestClient  # noqa: E402

from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402

PASSWORD = "test-password"
# Защищённый роут без внешних вызовов (лимиты Claude — просто чтение БД).
PROTECTED = "/api/limits"


def _client() -> TestClient:
    settings.app_password = PASSWORD
    settings.app_secret = ""
    auth.login_limiter.reset("testclient")
    return TestClient(app)


def test_health_open() -> None:
    with _client() as c:
        assert c.get("/api/health").status_code == 200


def test_status_anonymous() -> None:
    with _client() as c:
        assert c.get("/api/auth/status").json() == {"authenticated": False, "required": True}


def test_protected_401_without_cookie() -> None:
    with _client() as c:
        r = c.get(PROTECTED)
        assert r.status_code == 401
        assert r.json()["detail"]


def test_login_wrong_password() -> None:
    with _client() as c:
        r = c.post("/api/auth/login", json={"password": "nope"})
        assert r.status_code == 401
        assert auth.COOKIE_NAME not in r.cookies


def test_login_ok_then_protected_200_then_logout() -> None:
    with _client() as c:
        r = c.post("/api/auth/login", json={"password": PASSWORD})
        assert r.status_code == 200
        set_cookie = r.headers["set-cookie"].lower()
        assert "httponly" in set_cookie and "samesite=lax" in set_cookie
        # по http без X-Forwarded-Proto — Secure не ставим (auto)
        assert "secure" not in set_cookie
        assert c.get(PROTECTED).status_code == 200
        assert c.get("/api/auth/status").json()["authenticated"] is True

        c.post("/api/auth/logout")
        assert c.get(PROTECTED).status_code == 401


def test_secure_cookie_behind_https_proxy() -> None:
    with _client() as c:
        r = c.post(
            "/api/auth/login",
            json={"password": PASSWORD},
            headers={"X-Forwarded-Proto": "https"},
        )
        assert "secure" in r.headers["set-cookie"].lower()


def test_password_change_invalidates_session() -> None:
    with _client() as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        assert c.get(PROTECTED).status_code == 200
        settings.app_password = "another"
        assert c.get(PROTECTED).status_code == 401


def test_forged_cookie_rejected() -> None:
    with _client() as c:
        c.cookies.set(auth.COOKIE_NAME, "v1.9999999999.deadbeef")
        assert c.get(PROTECTED).status_code == 401


def test_rate_limit() -> None:
    with _client() as c:
        for _ in range(auth.RATE_LIMIT_ATTEMPTS):
            assert c.post("/api/auth/login", json={"password": "x"}).status_code == 401
        # лимит исчерпан — даже верный пароль ждёт окна
        assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429
    auth.login_limiter.reset("testclient")


def test_auth_disabled_when_no_password() -> None:
    with _client() as c:
        settings.app_password = ""
        assert c.get(PROTECTED).status_code == 200
        assert c.get("/api/auth/status").json() == {"authenticated": True, "required": False}


def _req(peer: str, xff: str | None) -> "auth.Request":
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return auth.Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_from_forwarded_for() -> None:
    # За nginx (peer = loopback): самый правый не-loopback адрес из XFF
    assert auth.client_ip(_req("127.0.0.1", "1.2.3.4, 192.168.1.50")) == "192.168.1.50"
    # funnel → nginx: tailscale дописал клиента, nginx — 127.0.0.1
    assert auth.client_ip(_req("127.0.0.1", "8.8.8.8, 127.0.0.1")) == "8.8.8.8"
    # Прямое подключение не через прокси — XFF не доверяем
    assert auth.client_ip(_req("10.0.0.7", "1.1.1.1")) == "10.0.0.7"


if __name__ == "__main__":
    # Запуск без pytest: прогоняем все test_* по порядку.
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
