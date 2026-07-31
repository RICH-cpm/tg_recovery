"""Криптография."""
import base64, hashlib, re
from functools import lru_cache
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken
import pyotp
from .config import config

_ph = PasswordHasher()
# Хеш строки-заглушки: сверяемся с ним, когда пользователя нет, чтобы время
# ответа на несуществующий логин не отличалось от ответа на существующий.
DUMMY_HASH = _ph.hash("tg-recovery-dummy-password")
_DIGITS = re.compile(r"\D")


def hash_password(p): return _ph.hash(p)


def verify_password(p, h):
    if not p or not h: return False
    try:
        _ph.verify(h, p); return True
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except Exception:
        return False


def dummy_verify():
    """Сжечь столько же времени, сколько занимает настоящая проверка пароля."""
    try: _ph.verify(DUMMY_HASH, "wrong")
    except Exception: pass


@lru_cache(maxsize=1)
def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(config.ENCRYPTION_KEY.encode()).digest())
    return Fernet(key)


def encrypt_bytes(b): return _fernet().encrypt(b)
def decrypt_bytes(b): return _fernet().decrypt(b)
def encrypt_str(s): return base64.b64encode(encrypt_bytes(s.encode("utf-8"))).decode("ascii")
def decrypt_str(s): return decrypt_bytes(base64.b64decode(s.encode("ascii"))).decode("utf-8")


def try_decrypt_str(s):
    """Расшифровать или вернуть None (неверный ключ, битые данные) — без исключения."""
    if not s: return None
    try:
        return decrypt_str(s)
    except (InvalidToken, ValueError, TypeError, base64.binascii.Error):
        return None
    except Exception:
        return None


def generate_totp_secret(): return pyotp.random_base32()


def get_totp_uri(secret, username): return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name="TG Recovery")


def verify_totp(secret, code):
    """valid_window=1 — принимаем соседние 30-секундные окна (расхождение часов)."""
    if not secret or not code: return False
    code = _DIGITS.sub("", str(code))
    if len(code) != 6: return False
    try:
        return pyotp.TOTP(secret).verify(code, valid_window=1)
    except Exception:
        return False
