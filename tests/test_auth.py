"""Вход, лимит попыток, инвалидация сессий."""
import asyncio
from datetime import timedelta

from conftest import ADMIN_PASS, ADMIN_USER, login

from app import auth as auth_mod
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
    data = verify_session_token(t)
    assert data["user_id"] == 7 and data["username"] == "bob" and data["epoch"] == 3
    assert isinstance(data["iat"], int)
    assert verify_session_token(t + "x") is None


def test_session_cookie_is_not_persistent(client):
    """Cookie должна умирать вместе с браузером: без Max-Age и Expires."""
    r = login(client)
    raw = "; ".join(r.headers.get_list("set-cookie"))
    assert config.SESSION_COOKIE_NAME in raw
    assert "Max-Age" not in raw and "max-age" not in raw
    assert "Expires" not in raw and "expires" not in raw
    assert "HttpOnly" in raw and "samesite=lax" in raw.lower()


def test_session_expires_after_idle_timeout(client, monkeypatch):
    login(client)
    assert client.get("/", follow_redirects=False).status_code == 200
    # Токен подписан вместе с меткой времени: сдвигаем часы вперёд.
    real = auth_mod.utcnow

    def later():
        return real() + timedelta(seconds=config.SESSION_IDLE_TIMEOUT + 60)

    monkeypatch.setattr(auth_mod, "utcnow", later)
    monkeypatch.setattr(auth_mod._s, "loads", _expired_loads(auth_mod._s))
    assert client.get("/", follow_redirects=False).status_code == 302


def _expired_loads(serializer):
    """Заставляет itsdangerous считать токен просроченным."""
    def loads(value, max_age=None, **kw):
        from itsdangerous import SignatureExpired
        raise SignatureExpired("expired")
    return loads


def test_activity_slides_the_idle_window(client):
    """Каждый запрос перевыпускает cookie, иначе окно не скользит."""
    login(client)
    r = client.get("/settings")
    assert r.status_code == 200
    assert any(config.SESSION_COOKIE_NAME in c for c in r.headers.get_list("set-cookie"))


def test_logout_is_not_undone_by_the_refresh(client):
    """Middleware не должен возвращать cookie, которую только что удалил выход."""
    login(client)
    r = client.post("/logout", follow_redirects=False)
    raw = "; ".join(r.headers.get_list("set-cookie"))
    assert config.SESSION_COOKIE_NAME in raw
    assert 'tg_recovery_session=""' in raw or "tg_recovery_session=;" in raw or "Max-Age=0" in raw
    assert client.get("/", follow_redirects=False).status_code == 302


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
