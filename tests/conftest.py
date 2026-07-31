import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Конфиг читается на импорте, поэтому окружение готовим до импорта app.*
_TMP = tempfile.mkdtemp(prefix="tgr-tests-")
os.environ.setdefault("TG_API_ID", "12345")
os.environ.setdefault("TG_API_HASH", "0123456789abcdef0123456789abcdef")
os.environ.setdefault("SECRET_KEY", "a" * 48)
os.environ.setdefault("ENCRYPTION_KEY", "b" * 48)
os.environ.setdefault("ENV", "development")
os.environ.setdefault("DATABASE_PATH", str(Path(_TMP) / "recovery.db"))
os.environ.setdefault("SESSIONS_DIR", str(Path(_TMP) / "sessions"))
os.environ.setdefault("TRUSTED_PROXY_HOPS", "1")

from fastapi.testclient import TestClient  # noqa: E402

from app import telegram_manager as tm  # noqa: E402
from app.auth import create_initial_admin  # noqa: E402
from app.database import init_db_sync, close_db, execute  # noqa: E402

ADMIN_USER = "admin"
ADMIN_PASS = "password123"


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def client(monkeypatch):
    """Приложение без настоящих подключений к Telegram."""
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(tm.tg_manager, "start_all", noop)
    monkeypatch.setattr(tm.tg_manager, "stop_all", noop)
    monkeypatch.setattr(tm.tg_manager, "ensure_all_running", noop)

    import asyncio

    from app.main import app

    init_db_sync()
    asyncio.run(_reset())
    asyncio.run(create_initial_admin(ADMIN_USER, ADMIN_PASS))
    with TestClient(app) as c:
        yield c
    asyncio.run(close_db())


async def _reset():
    for t in ("received_codes", "telegram_accounts", "audit_log", "login_attempts", "users"):
        await execute(f"DELETE FROM {t}")


def login(client, username=ADMIN_USER, password=ADMIN_PASS, **extra):
    return client.post(
        "/login",
        data={"username": username, "password": password, "totp_code": "", **extra},
        follow_redirects=False,
    )
