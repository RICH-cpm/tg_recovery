"""Криптография, разбор кодов, телефоны, бэкапы."""
import sqlite3
import tarfile
import tempfile
from pathlib import Path

import pytest

from app import backup_service
from app.crypto import (decrypt_str, encrypt_str, hash_password, try_decrypt_str,
                        verify_password, verify_totp, generate_totp_secret)
from app.telegram_manager import extract_code, is_valid_phone, normalize_phone


# ------------------------------------------------------------------ crypto

def test_password_roundtrip():
    h = hash_password("s3cret-password")
    assert verify_password("s3cret-password", h)
    assert not verify_password("other", h)
    assert not verify_password("", h)
    assert not verify_password("s3cret-password", "")


def test_password_verify_survives_garbage_hash():
    assert verify_password("x", "not-a-hash") is False


def test_encrypt_roundtrip():
    for s in ["простой пароль", "", "a" * 500, "emoji 🔐 tail"]:
        assert decrypt_str(encrypt_str(s)) == s


def test_try_decrypt_returns_none_on_garbage():
    assert try_decrypt_str("not base64 !!!") is None
    assert try_decrypt_str(None) is None
    assert try_decrypt_str("") is None


def test_encryption_is_not_deterministic():
    assert encrypt_str("same") != encrypt_str("same")


def test_totp():
    import pyotp
    secret = generate_totp_secret()
    assert verify_totp(secret, pyotp.TOTP(secret).now())
    assert not verify_totp(secret, "000000")
    assert not verify_totp(secret, "")
    assert not verify_totp("", "123456")
    assert not verify_totp(secret, "12345")     # 5 цифр — не TOTP
    assert not verify_totp(secret, "abcdef")


def test_totp_ignores_spaces():
    import pyotp
    secret = generate_totp_secret()
    code = pyotp.TOTP(secret).now()
    assert verify_totp(secret, f"{code[:3]} {code[3:]}")


# ------------------------------------------------------------ разбор кодов

@pytest.mark.parametrize("text,expected", [
    ("Login code: 12345. Do not give this code to anyone.", "12345"),
    ("Код для входа: 54321. Никому его не сообщайте.", "54321"),
    ("Your login code is 987654", "987654"),
    # Число рядом со словом «код» приоритетнее любого другого в тексте.
    ("Ticket 99999 — ваш код: 24680, спасибо", "24680"),
    ("Никаких цифр здесь нет", None),
    ("Слишком короткое 1234", None),
    ("", None),
])
def test_extract_code(text, expected):
    assert extract_code(text) == expected


@pytest.mark.parametrize("raw,expected", [
    ("+7 (999) 123-45-67", "+79991234567"),
    ("79991234567", "+79991234567"),
    ("  +1 234 567 8900 ", "+12345678900"),
    ("", ""),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("phone,ok", [
    ("+79991234567", True),
    ("+1234567", True),
    ("+123", False),
    ("79991234567", False),
    ("", False),
    ("+" + "9" * 20, False),
])
def test_is_valid_phone(phone, ok):
    assert is_valid_phone(phone) is ok


# ----------------------------------------------------------------- бэкапы

def test_human_size():
    assert backup_service._human(0) == "0 Б"
    assert backup_service._human(512) == "512 Б"
    assert backup_service._human(2048) == "2.0 КБ"
    assert backup_service._human(5 * 1024 ** 2) == "5.0 МБ"


def test_backup_produces_readable_database(monkeypatch, tmp_path):
    """Архив должен содержать целую базу, а не срез файла посреди записи."""
    db = tmp_path / "recovery.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT)")
    conn.execute("INSERT INTO users (username) VALUES ('bob')")
    conn.commit()

    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", db)
    monkeypatch.setattr(backup_service.config, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", tmp_path / "backups")

    # Незакоммиченная запись в другом соединении не должна ломать бэкап.
    r = backup_service.perform_backup()
    conn.close()

    archive = Path(r["path"])
    assert archive.exists()
    assert oct(archive.stat().st_mode)[-3:] == "600"

    with tempfile.TemporaryDirectory() as out:
        with tarfile.open(archive) as tar:
            tar.extractall(out)
        restored = sqlite3.connect(Path(out) / "recovery.db")
        assert restored.execute("SELECT username FROM users").fetchone()[0] == "bob"
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        restored.close()


def test_backup_skips_partial_session_files(monkeypatch, tmp_path):
    db = tmp_path / "recovery.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit(); conn.close()

    sessions = tmp_path / "sessions"; sessions.mkdir()
    (sessions / "acc_1.enc").write_bytes(b"good")
    (sessions / "acc_2.enc.tmp").write_bytes(b"half-written")

    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", db)
    monkeypatch.setattr(backup_service.config, "SESSIONS_DIR", sessions)
    monkeypatch.setattr(backup_service.config, "BACKUPS_DIR", tmp_path / "backups")

    r = backup_service.perform_backup()
    with tarfile.open(r["path"]) as tar:
        names = tar.getnames()
    assert "sessions/acc_1.enc" in names
    assert not any(n.endswith(".tmp") for n in names)


def test_maybe_backup_returns_false_without_database(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_service.config, "DATABASE_PATH", tmp_path / "missing.db")
    assert backup_service.maybe_backup() is False


# --------------------------------------------------------- фильтры шаблонов

@pytest.mark.parametrize("phone,masked", [
    ("+79211234412", "+7 921 ••• 44 12"),
    ("+14155552290", "+1 415 ••• 22 90"),
    # Код страны из двух и трёх цифр не должен резаться по первой.
    ("+447700900118", "+44 770 ••• 01 18"),
    ("+4915277730", "+49 152 ••• 77 30"),
    ("+380501234567", "+380 501 ••• 45 67"),
    ("+998901234567", "+998 901 ••• 45 67"),
    ("", "—"),
    ("+123", "+123"),
])
def test_mask_phone(phone, masked):
    from app.templating import mask_phone
    assert mask_phone(phone) == masked


def test_mask_phone_never_leaks_the_middle():
    from app.templating import mask_phone
    assert "1234" not in mask_phone("+79211234412")


@pytest.mark.parametrize("name,letter", [("Личный", "Л"), ("work", "W"), ("  ёлка", "Ё"), ("", "•"), (None, "•")])
def test_initials(name, letter):
    from app.templating import initials
    assert initials(name) == letter
