"""Конфигурация приложения."""
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
# override=False — переменные окружения (Railway, systemd) важнее файла .env.
load_dotenv(BASE_DIR / ".env", override=False)


def _env_int(name, default):
    """int() из окружения: пустая строка или мусор не роняют приложение на импорте."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_bool(name, default):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    TG_API_ID = _env_int("TG_API_ID", 0)
    TG_API_HASH = os.getenv("TG_API_HASH", "").strip()
    SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()
    HOST = os.getenv("HOST", "127.0.0.1")
    PORT = _env_int("PORT", 8000)
    DOMAIN = os.getenv("DOMAIN", "localhost")
    ENV = os.getenv("ENV", "development")
    IS_PRODUCTION = ENV == "production"
    DATABASE_PATH = BASE_DIR / os.getenv("DATABASE_PATH", "./data/recovery.db")
    SESSIONS_DIR = BASE_DIR / os.getenv("SESSIONS_DIR", "./sessions")
    BACKUPS_DIR = (BASE_DIR / os.getenv("BACKUPS_DIR")) if os.getenv("BACKUPS_DIR", "").strip() else DATABASE_PATH.parent / "backups"
    TEMPLATES_DIR = str(APP_DIR / "templates")
    STATIC_DIR = str(APP_DIR / "static")
    AUTO_DELETE_CODES_DAYS = _env_int("AUTO_DELETE_CODES_DAYS", 30)
    BACKUP_RETENTION_DAYS = _env_int("BACKUP_RETENTION_DAYS", 30)
    LOGIN_RATE_LIMIT = _env_int("LOGIN_RATE_LIMIT", 5)
    LOGIN_RATE_WINDOW_MIN = _env_int("LOGIN_RATE_WINDOW_MIN", 1)
    SESSION_COOKIE_NAME = "tg_recovery_session"
    SESSION_MAX_AGE = _env_int("SESSION_MAX_AGE", 60 * 60 * 8)
    TELEGRAM_SERVICE_ID = 777000
    # Незавершённое добавление аккаунта живёт столько секунд, потом клиент отключается.
    PENDING_AUTH_TTL = _env_int("PENDING_AUTH_TTL", 15 * 60)
    # Сколько доверенных прокси стоит перед приложением (nginx, edge Railway).
    #   0 — не доверять X-Forwarded-For вообще (приложение смотрит в интернет напрямую);
    #   1 — брать предпоследний-с-конца элемент XFF: его подставил наш прокси, подделать нельзя.
    TRUSTED_PROXY_HOPS = _env_int("TRUSTED_PROXY_HOPS", 1)
    # Отдавать security-заголовки самим: нужно там, где перед нами нет nginx (Railway и т. п.).
    SEND_SECURITY_HEADERS = _env_bool("SEND_SECURITY_HEADERS", True)
    # Ставить Secure на cookie сессии. По умолчанию — в production (там всегда HTTPS).
    COOKIE_SECURE = _env_bool("COOKIE_SECURE", IS_PRODUCTION)

    @classmethod
    def validate(cls):
        e = []
        if not cls.TG_API_ID: e.append("TG_API_ID не задан")
        if not cls.TG_API_HASH: e.append("TG_API_HASH не задан")
        if not cls.SECRET_KEY or len(cls.SECRET_KEY) < 32: e.append("SECRET_KEY не задан или короче 32 символов")
        if not cls.ENCRYPTION_KEY or len(cls.ENCRYPTION_KEY) < 32: e.append("ENCRYPTION_KEY не задан или короче 32 символов")
        if cls.SECRET_KEY and cls.SECRET_KEY == cls.ENCRYPTION_KEY: e.append("SECRET_KEY и ENCRYPTION_KEY должны отличаться")
        if e: raise RuntimeError("Конфигурация:\n  - " + "\n  - ".join(e))


config = Config()
