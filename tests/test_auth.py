"""Вход, лимит попыток, инвалидация сессий."""
import asyncio

from conftest import ADMIN_PASS, ADMIN_USER, login

from app.auth import get_client_ip, verify_session_token, create_session_token
from app.config import config
from app.database import fetch_one


class FakeRequest:
    def __init__(self, headers=None, peer="10.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": peer})()


def test_login_and_logout(client):
    assert client.get("/", follow_redirects=False).status_code == 302
    assert login(client).status_code == 302
    assert client.get("/").status_code == 200
    assert client.post("/logout", follow_redirects=False).status_code == 302
    assert client.get("/", follow_redirects=False).status_code == 302


def test_wrong_password_is_401(client):
    assert login(client, password="wrong").status_code == 401


def test_unknown_user_is_401(client):
    assert login(client, username="nobody", password="whatever").status_code == 401


def test_rate_limit_blocks_after_n_failures(client):
    for _ in range(config.LOGIN_RATE_LIMIT):
        login(client, password="wrong")
    r = login(client, password="wrong")
    assert r.status_code == 429


def _login_through_proxy(client, forged, real_ip="203.0.113.5"):
    """Что реально приходит от nginx: заголовок клиента + дописанный им IP."""
    return client.post(
        "/login",
        data={"username": ADMIN_USER, "password": "wrong", "totp_code": ""},
        headers={"X-Forwarded-For": f"{forged}, {real_ip}"},
        follow_redirects=False,
    )


def test_rate_limit_cannot_be_bypassed_with_forged_xff(client):
    """Первый элемент X-Forwarded-For задаёт клиент — считать по нему нельзя."""
    for i in range(config.LOGIN_RATE_LIMIT):
        _login_through_proxy(client, forged=f"1.2.3.{i}")
    # Клиент меняет подставной адрес — лимит всё равно должен сработать.
    assert _login_through_proxy(client, forged="9.9.9.9").status_code == 429


def test_rate_limit_is_per_real_client(client):
    """Разные настоящие клиенты не блокируют друг друга."""
    for i in range(config.LOGIN_RATE_LIMIT):
        _login_through_proxy(client, forged="1.2.3.4", real_ip="203.0.113.5")
    assert _login_through_proxy(client, forged="1.2.3.4", real_ip="203.0.113.5").status_code == 429
    assert _login_through_proxy(client, forged="1.2.3.4", real_ip="198.51.100.9").status_code == 401


def test_get_client_ip_uses_last_hop():
    req = FakeRequest({"X-Forwarded-For": "1.2.3.4, 203.0.113.7"})
    assert get_client_ip(req) == "203.0.113.7"


def test_get_client_ip_without_proxy_trust(monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXY_HOPS", 0)
    req = FakeRequest({"X-Forwarded-For": "1.2.3.4"}, peer="198.51.100.2")
    assert get_client_ip(req) == "198.51.100.2"


def test_successful_login_clears_failures(client):
    for _ in range(config.LOGIN_RATE_LIMIT - 1):
        login(client, password="wrong")
    assert login(client).status_code == 302
    for _ in range(config.LOGIN_RATE_LIMIT - 1):
        login(client, password="wrong")
    assert login(client).status_code == 302


def test_password_change_revokes_other_sessions(client):
    login(client)
    stolen = client.cookies.get(config.SESSION_COOKIE_NAME)
    r = client.post(
        "/settings/change-password",
        data={"current_password": ADMIN_PASS, "new_password": "brandnewpass1", "confirm_password": "brandnewpass1"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    # Текущая вкладка остаётся в системе...
    assert client.get("/", follow_redirects=False).status_code == 200
    # ...а старая cookie больше не работает.
    client.cookies.set(config.SESSION_COOKIE_NAME, stolen)
    assert client.get("/", follow_redirects=False).status_code == 302


def test_revoke_all_invalidates_old_cookie(client):
    login(client)
    stolen = client.cookies.get(config.SESSION_COOKIE_NAME)
    assert client.post("/settings/sessions/revoke-all", follow_redirects=False).status_code == 302
    assert client.get("/", follow_redirects=False).status_code == 200
    client.cookies.set(config.SESSION_COOKIE_NAME, stolen)
    assert client.get("/", follow_redirects=False).status_code == 302


def test_forged_cookie_is_rejected(client):
    client.cookies.set(config.SESSION_COOKIE_NAME, "not-a-real-token")
    assert client.get("/", follow_redirects=False).status_code == 302


def test_session_token_roundtrip():
    t = create_session_token(7, "bob", 3)
    assert verify_session_token(t) == {"user_id": 7, "username": "bob", "epoch": 3}
    assert verify_session_token(t + "x") is None


def test_username_change_keeps_session(client):
    login(client)
    r = client.post(
        "/settings/change-username",
        data={"new_username": "newname", "current_password": ADMIN_PASS},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert client.get("/").status_code == 200
    row = asyncio.run(fetch_one("SELECT username FROM users WHERE username = ?", ("newname",)))
    assert row is not None


def test_username_change_requires_correct_password(client):
    login(client)
    r = client.post(
        "/settings/change-username",
        data={"new_username": "hacker", "current_password": "wrong"},
        follow_redirects=False,
    )
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE username = ?", ("hacker",))) is None
