"""Несколько пользователей: изоляция данных и права администратора."""
import asyncio

from conftest import ADMIN_PASS, ADMIN_USER, login

from app.crypto import hash_password, verify_password
from app.database import execute, fetch_all, fetch_one
from app.utils import utcnow_iso

FRIEND_PASS = "friendpass1"


async def _make_user(username, password=FRIEND_PASS, admin=0):
    return await execute(
        "INSERT INTO users (username, password_hash, is_admin, session_epoch, created_at) VALUES (?, ?, ?, 0, ?)",
        (username, hash_password(password), admin, utcnow_iso()),
    )


async def _make_account(user_id, name="Аккаунт", phone="+79991234567"):
    return await execute(
        "INSERT INTO telegram_accounts (user_id, display_name, phone_number, session_filename, is_active, created_at)"
        " VALUES (?, ?, ?, 'x.enc', 1, ?)",
        (user_id, name, phone, utcnow_iso()),
    )


def _user_id(username):
    return asyncio.run(fetch_one("SELECT id FROM users WHERE username = ?", (username,)))["id"]


# ------------------------------------------------------------- создание

def test_admin_creates_user(client):
    login(client)
    r = client.post("/settings/users/create", data={
        "username": "friend", "password": FRIEND_PASS, "confirm_password": FRIEND_PASS,
    }, follow_redirects=False)
    assert r.status_code == 302 and "message" in r.headers["location"]
    row = asyncio.run(fetch_one("SELECT username, is_admin FROM users WHERE username = 'friend'"))
    assert row is not None and row["is_admin"] == 0


def test_created_user_can_sign_in(client):
    login(client)
    client.post("/settings/users/create", data={
        "username": "friend", "password": FRIEND_PASS, "confirm_password": FRIEND_PASS,
    }, follow_redirects=False)
    client.post("/logout", follow_redirects=False)
    assert login(client, username="friend", password=FRIEND_PASS).status_code == 302
    assert client.get("/").status_code == 200


def test_create_rejects_duplicate_and_weak_input(client):
    login(client)
    bad = [
        {"username": "ab", "password": FRIEND_PASS, "confirm_password": FRIEND_PASS},        # короткий логин
        {"username": "friend", "password": "short", "confirm_password": "short"},            # короткий пароль
        {"username": "friend", "password": FRIEND_PASS, "confirm_password": "otherpass1"},   # не совпали
        {"username": "плохой", "password": FRIEND_PASS, "confirm_password": FRIEND_PASS},    # не латиница
        {"username": ADMIN_USER, "password": FRIEND_PASS, "confirm_password": FRIEND_PASS},  # занят
    ]
    for data in bad:
        r = client.post("/settings/users/create", data=data, follow_redirects=False)
        assert "error" in r.headers["location"], data
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'friend'")) is None


def test_non_admin_cannot_create_users(client):
    asyncio.run(_make_user("friend"))
    login(client, username="friend", password=FRIEND_PASS)
    r = client.post("/settings/users/create", data={
        "username": "intruder", "password": FRIEND_PASS, "confirm_password": FRIEND_PASS,
    }, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'intruder'")) is None


# ------------------------------------------------------------- изоляция

def test_users_do_not_see_each_others_accounts(client):
    friend = asyncio.run(_make_user("friend"))
    mine = _user_id(ADMIN_USER)
    asyncio.run(_make_account(mine, "Мой личный"))
    theirs = asyncio.run(_make_account(friend, "Чужой аккаунт"))

    login(client, username="friend", password=FRIEND_PASS)
    page = client.get("/").text
    assert "Чужой аккаунт" in page
    assert "Мой личный" not in page

    # И прямой заход по id тоже закрыт.
    asyncio.run(_make_account(mine, "Секретный"))
    secret = asyncio.run(fetch_one("SELECT id FROM telegram_accounts WHERE display_name = 'Секретный'"))["id"]
    assert client.get(f"/account/{secret}", follow_redirects=False).status_code == 404
    assert client.get(f"/api/account/{secret}/codes").status_code == 404
    assert client.post(f"/accounts/{secret}/delete", follow_redirects=False).status_code == 404
    assert theirs  # аккаунт друга остался на месте


def test_nav_counters_are_per_user(client):
    friend = asyncio.run(_make_user("friend"))
    mine = _user_id(ADMIN_USER)
    for n in range(3):
        asyncio.run(_make_account(mine, f"Мой {n}"))
    asyncio.run(_make_account(friend, "Один"))

    login(client, username="friend", password=FRIEND_PASS)
    assert "Один" in client.get("/").text
    rows = asyncio.run(fetch_all("SELECT display_name FROM telegram_accounts WHERE user_id = ?", (friend,)))
    assert len(rows) == 1


def test_audit_is_per_user(client):
    friend = asyncio.run(_make_user("friend"))
    asyncio.run(execute(
        "INSERT INTO audit_log (user_id, action, details, ip_address, created_at) VALUES (?, 'secret_admin_action', 'тайна', '', ?)",
        (_user_id(ADMIN_USER), utcnow_iso()),
    ))
    login(client, username="friend", password=FRIEND_PASS)
    assert "secret_admin_action" not in client.get("/audit").text
    assert friend


# --------------------------------------------------------- права и бэкап

def test_non_admin_cannot_run_backup(client):
    asyncio.run(_make_user("friend"))
    login(client, username="friend", password=FRIEND_PASS)
    r = client.post("/settings/backup/now", follow_redirects=False)
    assert "error" in r.headers["location"]
    r = client.post("/settings/backup/save", data={"backup_enabled": "off"}, follow_redirects=False)
    assert "error" in r.headers["location"]


def test_non_admin_settings_page_hides_admin_sections(client):
    asyncio.run(_make_user("friend"))
    login(client, username="friend", password=FRIEND_PASS)
    page = client.get("/settings").text
    assert "/settings/users/create" not in page
    assert "/settings/backup/now" not in page
    # Свои разделы на месте.
    assert "/settings/change-password" in page


def test_admin_settings_page_shows_users(client):
    asyncio.run(_make_user("friend"))
    login(client)
    page = client.get("/settings").text
    assert "/settings/users/create" in page
    assert "friend" in page


def test_sidebar_role_label_matches_rights(client):
    asyncio.run(_make_user("friend"))
    login(client)
    assert 'data-i18n="user_role"' in client.get("/").text
    client.post("/logout", follow_redirects=False)
    login(client, username="friend", password=FRIEND_PASS)
    page = client.get("/").text
    assert 'data-i18n="role_user"' in page
    assert 'data-i18n="user_role"' not in page


# ------------------------------------------------------------ управление

def test_admin_resets_password_and_kicks_the_user(client):
    friend = asyncio.run(_make_user("friend"))
    login(client)
    r = client.post(f"/settings/users/{friend}/password", data={"new_password": "brandnew12"}, follow_redirects=False)
    assert "message" in r.headers["location"]
    row = asyncio.run(fetch_one("SELECT password_hash, session_epoch FROM users WHERE id = ?", (friend,)))
    assert verify_password("brandnew12", row["password_hash"])
    assert row["session_epoch"] == 1  # прежние cookie больше не подойдут


def test_admin_deletes_user_with_their_accounts(client):
    friend = asyncio.run(_make_user("friend"))
    asyncio.run(_make_account(friend, "Друг-аккаунт"))
    login(client)
    r = client.post(f"/settings/users/{friend}/delete",
                    data={"confirm_password": ADMIN_PASS}, follow_redirects=False)
    assert "message" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE id = ?", (friend,))) is None
    assert asyncio.run(fetch_one("SELECT id FROM telegram_accounts WHERE user_id = ?", (friend,))) is None


def test_delete_requires_own_password(client):
    friend = asyncio.run(_make_user("friend"))
    login(client)
    r = client.post(f"/settings/users/{friend}/delete",
                    data={"confirm_password": "wrong"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE id = ?", (friend,))) is not None


def test_admin_cannot_delete_self(client):
    login(client)
    me = _user_id(ADMIN_USER)
    r = client.post(f"/settings/users/{me}/delete",
                    data={"confirm_password": ADMIN_PASS}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE id = ?", (me,))) is not None


def test_last_admin_keeps_rights(client):
    login(client)
    me = _user_id(ADMIN_USER)
    r = client.post(f"/settings/users/{me}/admin", data={"make_admin": "off"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT is_admin FROM users WHERE id = ?", (me,)))["is_admin"] == 1


def test_admin_can_promote_and_demote_when_another_admin_exists(client):
    friend = asyncio.run(_make_user("friend"))
    login(client)
    client.post(f"/settings/users/{friend}/admin", data={"make_admin": "on"}, follow_redirects=False)
    assert asyncio.run(fetch_one("SELECT is_admin FROM users WHERE id = ?", (friend,)))["is_admin"] == 1
    client.post(f"/settings/users/{friend}/admin", data={"make_admin": "off"}, follow_redirects=False)
    assert asyncio.run(fetch_one("SELECT is_admin FROM users WHERE id = ?", (friend,)))["is_admin"] == 0


def test_non_admin_cannot_manage_users(client):
    friend = asyncio.run(_make_user("friend"))
    victim = asyncio.run(_make_user("victim"))
    login(client, username="friend", password=FRIEND_PASS)
    for url, data in [
        (f"/settings/users/{victim}/password", {"new_password": "hacked12345"}),
        (f"/settings/users/{victim}/admin", {"make_admin": "on"}),
        (f"/settings/users/{victim}/delete", {"confirm_password": FRIEND_PASS}),
    ]:
        r = client.post(url, data=data, follow_redirects=False)
        assert "error" in r.headers["location"], url
    row = asyncio.run(fetch_one("SELECT is_admin, password_hash FROM users WHERE id = ?", (victim,)))
    assert row is not None and row["is_admin"] == 0
    assert verify_password(FRIEND_PASS, row["password_hash"])
    assert friend
