"""Страницы, доступ к чужим данным, обработка ошибок."""
import asyncio

import pytest
from conftest import ADMIN_PASS, login

from app.crypto import encrypt_str, hash_password
from app.database import execute, fetch_one
from app.utils import utcnow_iso

PROTECTED = ["/", "/settings", "/audit", "/accounts/add", "/settings/totp/setup"]


async def _make_account(user_id, name="Тест", phone="+79991234567", twofa=None):
    return await execute(
        "INSERT INTO telegram_accounts (user_id, display_name, phone_number, session_filename, twofa_enc, is_active, created_at)"
        " VALUES (?, ?, ?, ?, ?, 1, ?)",
        (user_id, name, phone, "missing.enc", twofa, utcnow_iso()),
    )


async def _make_user(username="mallory", password="password123"):
    return await execute(
        "INSERT INTO users (username, password_hash, is_admin, session_epoch, created_at) VALUES (?, ?, 0, 0, ?)",
        (username, hash_password(password), utcnow_iso()),
    )


@pytest.mark.parametrize("path", PROTECTED)
def test_pages_require_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/login"


@pytest.mark.parametrize("path", PROTECTED)
def test_pages_render_for_logged_in_user(client, path):
    login(client)
    assert client.get(path).status_code == 200


def test_api_requires_login(client):
    assert client.get("/api/account/1/codes").status_code == 401
    assert client.get("/api/account/1/twofa").status_code == 401


def test_404_page_is_html(client):
    r = client.get("/no-such-page")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]


def test_api_404_is_json(client):
    login(client)
    r = client.get("/api/account/424242/codes")
    assert r.status_code == 404
    assert "application/json" in r.headers["content-type"]


def test_security_headers_present(client):
    r = client.get("/login")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in r.headers


def test_html_is_not_cached(client):
    """Иначе браузер отдаст старую страницу со ссылками на старую статику."""
    assert "no-store" in client.get("/login").headers["Cache-Control"]


def test_static_links_carry_a_version(client):
    """Nginx держит /static/ семь дней — без отпечатка обновление не доедет."""
    import re
    page = client.get("/login").text
    css = re.search(r'href="(/static/css/style\.css[^"]*)"', page)
    assert css and "?v=" in css.group(1)
    for name in ("i18n", "theme", "time"):
        m = re.search(rf'src="(/static/js/{name}\.js[^"]*)"', page)
        assert m and "?v=" in m.group(1), name


def test_static_version_changes_with_the_file(tmp_path, monkeypatch):
    from app import templating
    monkeypatch.setattr(templating.config, "STATIC_DIR", str(tmp_path))
    f = tmp_path / "x.css"
    f.write_text("a")
    first = templating.static_url("x.css")
    import os, time
    time.sleep(0.01)
    os.utime(f, None)
    f.write_text("b")
    assert templating.static_url("x.css") != first


def test_static_url_survives_missing_file(monkeypatch, tmp_path):
    from app import templating
    monkeypatch.setattr(templating.config, "STATIC_DIR", str(tmp_path))
    assert templating.static_url("nope.css") == "/static/nope.css"


def test_healthz(client):
    assert client.get("/healthz").text == "ok"


def test_cannot_read_foreign_account(client):
    mallory = asyncio.run(_make_user())
    foreign = asyncio.run(_make_account(mallory, twofa=encrypt_str("secret")))
    login(client)
    assert client.get(f"/account/{foreign}", follow_redirects=False).status_code == 404
    assert client.get(f"/api/account/{foreign}/codes").status_code == 404
    assert client.get(f"/api/account/{foreign}/twofa").status_code == 404


def test_cannot_delete_foreign_account(client):
    mallory = asyncio.run(_make_user())
    foreign = asyncio.run(_make_account(mallory))
    login(client)
    assert client.post(f"/accounts/{foreign}/delete", follow_redirects=False).status_code == 404
    assert asyncio.run(fetch_one("SELECT id FROM telegram_accounts WHERE id = ?", (foreign,))) is not None


def test_cannot_overwrite_foreign_twofa(client):
    mallory = asyncio.run(_make_user())
    foreign = asyncio.run(_make_account(mallory))
    login(client)
    r = client.post(f"/accounts/{foreign}/twofa/save", data={"twofa_password": "pwned"}, follow_redirects=False)
    assert r.status_code == 404
    row = asyncio.run(fetch_one("SELECT twofa_enc FROM telegram_accounts WHERE id = ?", (foreign,)))
    assert row["twofa_enc"] is None


def test_bulk_delete_ignores_foreign_ids(client):
    mallory = asyncio.run(_make_user())
    foreign = asyncio.run(_make_account(mallory))
    login(client)
    r = client.post("/settings/accounts/delete", data={"account_ids": [foreign]}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM telegram_accounts WHERE id = ?", (foreign,))) is not None


def test_twofa_save_reveal_delete_cycle(client):
    login(client)
    me = asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'admin'"))
    aid = asyncio.run(_make_account(me["id"]))

    client.post(f"/accounts/{aid}/twofa/save", data={"twofa_password": "мой пароль"}, follow_redirects=False)
    r = client.get(f"/api/account/{aid}/twofa")
    assert r.json() == {"has": True, "value": "мой пароль"}

    client.post(f"/accounts/{aid}/twofa/delete", follow_redirects=False)
    assert client.get(f"/api/account/{aid}/twofa").json()["has"] is False


def test_twofa_reveal_handles_undecryptable_value(client):
    login(client)
    me = asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'admin'"))
    aid = asyncio.run(_make_account(me["id"], twofa="!!! не шифротекст !!!"))
    r = client.get(f"/api/account/{aid}/twofa")
    assert r.status_code == 200 and r.json()["error"] == "decrypt"


def test_codes_are_marked_read_on_view(client):
    login(client)
    me = asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'admin'"))
    aid = asyncio.run(_make_account(me["id"]))
    asyncio.run(execute(
        "INSERT INTO received_codes (account_id, code, full_text, received_at) VALUES (?, ?, ?, ?)",
        (aid, "12345", "Login code: 12345", utcnow_iso()),
    ))
    assert "12345" in client.get("/").text
    assert client.get(f"/account/{aid}").status_code == 200
    row = asyncio.run(fetch_one("SELECT COUNT(*) AS c FROM received_codes WHERE account_id = ? AND is_read = 0", (aid,)))
    assert row["c"] == 0


def test_password_change_rejects_mismatch(client):
    login(client)
    r = client.post(
        "/settings/change-password",
        data={"current_password": ADMIN_PASS, "new_password": "abcdefgh1", "confirm_password": "different1"},
        follow_redirects=False,
    )
    assert "error" in r.headers["location"]


def test_password_change_rejects_short(client):
    login(client)
    r = client.post(
        "/settings/change-password",
        data={"current_password": ADMIN_PASS, "new_password": "short", "confirm_password": "short"},
        follow_redirects=False,
    )
    assert "error" in r.headers["location"]


def test_backup_settings_reject_unknown_period(client):
    login(client)
    client.post("/settings/backup/save", data={"backup_enabled": "on", "backup_period": "century"}, follow_redirects=False)
    row = asyncio.run(fetch_one("SELECT value FROM app_settings WHERE key = 'backup_period'"))
    assert row["value"] in ("day", "week", "month")


def test_add_phone_rejects_invalid_number(client):
    login(client)
    r = client.post("/accounts/add/phone", data={"phone_number": "не телефон"})
    assert r.status_code == 400
    assert "Неверный номер" in r.text


def test_audit_shows_failed_logins(client):
    login(client, password="wrong")
    login(client)
    assert "login_failed" in client.get("/audit").text
