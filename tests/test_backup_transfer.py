"""Скачивание и восстановление резервных копий."""
import asyncio
import io
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from conftest import ADMIN_PASS, login

from app import backup_service
from app.crypto import hash_password
from app.database import execute, fetch_one
from app.utils import utcnow_iso


def _make_archive(path, db_rows=("alice",), sessions=("acc_1.enc",), extra=None):
    """Собрать архив в формате бэкапа."""
    work = path.parent / "work"
    work.mkdir(exist_ok=True)
    db = work / "recovery.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    for name in db_rows:
        conn.execute("INSERT INTO users (username) VALUES (?)", (name,))
    conn.commit(); conn.close()

    with tarfile.open(path, "w:gz") as tar:
        tar.add(db, arcname="recovery.db")
        for s in sessions:
            f = work / s
            f.write_bytes(b"session-bytes")
            tar.add(f, arcname=f"sessions/{s}")
        for name, data in (extra or {}).items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


# ------------------------------------------------------------- список/путь

def test_list_backups_newest_first(monkeypatch, tmp_path):
    bdir = tmp_path / "backups"; bdir.mkdir()
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", bdir)
    for name in ("backup_20260101_000000.tar.gz", "backup_20260202_000000.tar.gz"):
        (bdir / name).write_bytes(b"x" * 10)
        time.sleep(0.01)
    names = [b["name"] for b in backup_service.list_backups()]
    assert names == ["backup_20260202_000000.tar.gz", "backup_20260101_000000.tar.gz"]


@pytest.mark.parametrize("name", [
    "../../etc/passwd",
    "/etc/passwd",
    "backup_x.tar.gz",
    "notabackup.tar.gz",
    "",
    "backup_20260101_000000.tar.gz.evil",
])
def test_backup_path_rejects_bad_names(monkeypatch, tmp_path, name):
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", tmp_path)
    assert backup_service.backup_path(name) is None


def test_backup_path_accepts_real_file(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", tmp_path)
    good = tmp_path / "backup_20260101_000000.tar.gz"
    good.write_bytes(b"x")
    assert backup_service.backup_path(good.name) == good.resolve()


# --------------------------------------------------------------- проверка

def test_inspect_accepts_our_archive(tmp_path):
    a = _make_archive(tmp_path / "b.tar.gz", sessions=("acc_1.enc", "acc_2.enc"))
    assert backup_service.inspect_backup(a)["sessions"] == 2


def test_inspect_rejects_foreign_archive(tmp_path):
    a = tmp_path / "foreign.tar.gz"
    with tarfile.open(a, "w:gz") as tar:
        info = tarfile.TarInfo("readme.txt"); info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    with pytest.raises(ValueError, match="recovery.db"):
        backup_service.inspect_backup(a)


def test_inspect_rejects_garbage(tmp_path):
    a = tmp_path / "garbage.tar.gz"
    a.write_bytes(b"not an archive at all")
    with pytest.raises(ValueError):
        backup_service.inspect_backup(a)


def test_inspect_rejects_path_traversal(tmp_path):
    """Архив не должен уметь писать за пределы каталогов приложения."""
    a = tmp_path / "evil.tar.gz"
    with tarfile.open(a, "w:gz") as tar:
        info = tarfile.TarInfo("../../evil.sh"); info.size = 2
        tar.addfile(info, io.BytesIO(b"hi"))
    with pytest.raises(ValueError, match="Недопустимый путь"):
        backup_service.inspect_backup(a)


# ---------------------------------------------------------- восстановление

def test_restore_replaces_db_and_sessions(monkeypatch, tmp_path):
    db = tmp_path / "recovery.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users (username) VALUES ('старый')")
    conn.commit(); conn.close()

    sessions = tmp_path / "sessions"; sessions.mkdir()
    (sessions / "old.enc").write_bytes(b"old")

    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", db)
    monkeypatch.setattr(backup_service.config, "SESSIONS_DIR", sessions)

    archive = _make_archive(tmp_path / "b.tar.gz", db_rows=("восстановленный",), sessions=("new.enc",))
    result = backup_service.restore_backup(archive)

    assert result["sessions"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "восстановленный"
    conn.close()
    assert (sessions / "new.enc").exists()
    assert not (sessions / "old.enc").exists()
    # Прежнее состояние отложено, а не стёрто.
    assert any(p.name.startswith("recovery.db.replaced-") for p in tmp_path.iterdir())
    assert any(p.name.startswith("sessions.replaced-") for p in tmp_path.iterdir())


def test_restore_refuses_corrupt_database(monkeypatch, tmp_path):
    db = tmp_path / "recovery.db"
    db.write_bytes(b"original")
    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", db)
    monkeypatch.setattr(backup_service.config, "SESSIONS_DIR", tmp_path / "sessions")

    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("recovery.db"); info.size = 9
        tar.addfile(info, io.BytesIO(b"not a db!"))
    with pytest.raises(ValueError):
        backup_service.restore_backup(archive)
    assert db.read_bytes() == b"original"  # исходная база не тронута


def test_backup_then_restore_roundtrip(monkeypatch, tmp_path):
    """Полный круг: сделали бэкап, испортили базу, вернули из архива."""
    db = tmp_path / "recovery.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users (username) VALUES ('важный')")
    conn.commit(); conn.close()

    sessions = tmp_path / "sessions"; sessions.mkdir()
    (sessions / "acc.enc").write_bytes(b"secret-session")

    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", db)
    monkeypatch.setattr(backup_service.config, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", tmp_path / "backups")

    made = backup_service.perform_backup()

    db.unlink()
    (sessions / "acc.enc").unlink()

    backup_service.restore_backup(Path(made["path"]))

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT username FROM users").fetchone()[0] == "важный"
    conn.close()
    assert (sessions / "acc.enc").read_bytes() == b"secret-session"


# ------------------------------------------------------------- маршруты

async def _make_user(username, password="friendpass1"):
    return await execute(
        "INSERT INTO users (username, password_hash, is_admin, session_epoch, created_at) VALUES (?, ?, 0, 0, ?)",
        (username, hash_password(password), utcnow_iso()),
    )


def test_download_requires_admin(client):
    asyncio.run(_make_user("friend"))
    login(client, username="friend", password="friendpass1")
    r = client.get("/settings/backup/download/backup_20260101_000000.tar.gz")
    assert r.status_code == 403


def test_download_rejects_traversal(client):
    login(client)
    r = client.get("/settings/backup/download/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code == 404


def test_admin_downloads_a_real_backup(client):
    login(client)
    r = client.post("/settings/backup/now", follow_redirects=False)
    assert "message" in r.headers["location"]
    files = backup_service.list_backups()
    assert files
    r = client.get(f"/settings/backup/download/{files[0]['name']}")
    assert r.status_code == 200
    assert r.headers["content-type"] in ("application/gzip", "application/x-gzip")
    # Скачанное действительно распаковывается.
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        assert "recovery.db" in tar.getnames()


def test_restore_requires_admin(client, tmp_path):
    asyncio.run(_make_user("friend"))
    login(client, username="friend", password="friendpass1")
    a = _make_archive(tmp_path / "b.tar.gz")
    with open(a, "rb") as fh:
        r = client.post("/settings/backup/restore",
                        files={"archive": ("b.tar.gz", fh, "application/gzip")},
                        data={"confirm_password": "friendpass1"}, follow_redirects=False)
    assert "error" in r.headers["location"]


def test_restore_requires_correct_password(client, tmp_path):
    login(client)
    a = _make_archive(tmp_path / "b.tar.gz")
    with open(a, "rb") as fh:
        r = client.post("/settings/backup/restore",
                        files={"archive": ("b.tar.gz", fh, "application/gzip")},
                        data={"confirm_password": "wrong"}, follow_redirects=False)
    assert "error" in r.headers["location"]
    # Пользователь на месте — база не подменена.
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'admin'")) is not None


def test_restore_rejects_foreign_archive(client, tmp_path):
    login(client)
    a = tmp_path / "foreign.tar.gz"
    with tarfile.open(a, "w:gz") as tar:
        info = tarfile.TarInfo("readme.txt"); info.size = 3
        tar.addfile(info, io.BytesIO(b"abc"))
    with open(a, "rb") as fh:
        r = client.post("/settings/backup/restore",
                        files={"archive": ("foreign.tar.gz", fh, "application/gzip")},
                        data={"confirm_password": ADMIN_PASS}, follow_redirects=False)
    assert "error" in r.headers["location"]
    assert asyncio.run(fetch_one("SELECT id FROM users WHERE username = 'admin'")) is not None
