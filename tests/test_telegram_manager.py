"""Логика менеджера сессий без реального Telegram."""
import asyncio

from app.config import config
from app.telegram_manager import TelegramSessionManager
from app.utils import utcnow


class FakeClient:
    def __init__(self, authorized=True):
        self.disconnected = False
        self.logged_out = False
        self._authorized = authorized

    async def disconnect(self):
        self.disconnected = True

    async def log_out(self):
        self.logged_out = True
        self.disconnected = True

    async def is_user_authorized(self):
        return self._authorized

    def is_connected(self):
        return not self.disconnected


def test_pending_auth_is_scoped_per_user():
    """Разные пользователи с одним номером не должны видеть чужую попытку."""
    m = TelegramSessionManager()
    m.pending_auth[(1, "+79991234567")] = {"client": FakeClient(), "created_at": utcnow(), "twofa_enc": None}

    r = asyncio.run(m.submit_code(2, "+79991234567", "12345"))
    assert r["success"] is False
    r = asyncio.run(m.finalize_account(2, "+79991234567", "Чужой"))
    assert r["success"] is False


def test_prune_pending_disconnects_stale_clients(monkeypatch):
    m = TelegramSessionManager()
    monkeypatch.setattr(config, "PENDING_AUTH_TTL", 60)
    fresh, stale = FakeClient(), FakeClient()
    m.pending_auth[(1, "+70000000001")] = {"client": fresh, "created_at": utcnow(), "twofa_enc": None}
    m.pending_auth[(1, "+70000000002")] = {
        "client": stale,
        "created_at": utcnow().replace(year=utcnow().year - 1),
        "twofa_enc": None,
    }

    removed = asyncio.run(m.prune_pending())
    assert removed == 1
    assert stale.disconnected and not fresh.disconnected
    assert (1, "+70000000001") in m.pending_auth
    assert (1, "+70000000002") not in m.pending_auth


def test_cancel_pending_disconnects_and_forgets():
    m = TelegramSessionManager()
    c = FakeClient()
    m.pending_auth[(1, "+79990000000")] = {"client": c, "created_at": utcnow(), "twofa_enc": None}
    asyncio.run(m.cancel_pending(1, "+79990000000"))
    assert c.disconnected
    assert not m.pending_auth


def test_request_code_rejects_bad_phone():
    m = TelegramSessionManager()
    r = asyncio.run(m.request_code(1, "12345"))
    assert r["success"] is False and "номер" in r["error"].lower()


def test_twofa_password_stays_on_server():
    """Шифротекст пароля не возвращается наружу — он живёт в pending_auth."""
    m = TelegramSessionManager()
    client = FakeClient()

    async def sign_in(**kwargs):
        return None

    client.sign_in = sign_in
    m.pending_auth[(1, "+79991112233")] = {"client": client, "created_at": utcnow(), "twofa_enc": None}

    r = asyncio.run(m.submit_password(1, "+79991112233", "cloudpass", "ciphertext"))
    assert r == {"success": True}
    assert "ciphertext" not in str(r)
    assert m.pending_auth[(1, "+79991112233")]["twofa_enc"] == "ciphertext"


def test_session_path_ignores_traversal():
    m = TelegramSessionManager()
    p = m._session_path("../../etc/passwd")
    assert p.parent == config.SESSIONS_DIR
    assert p.name == "passwd"


def test_run_does_not_clobber_a_newer_client():
    """Гонка reconnect: завершение старой задачи не должно гасить новое соединение."""
    m = TelegramSessionManager()
    old, new = FakeClient(), FakeClient()

    async def scenario():
        m.clients[7] = new  # переподключение уже поставило новый клиент
        await m._run(7, old)  # старая задача завершается позже

    asyncio.run(scenario())
    assert m.clients.get(7) is new


def test_stop_all_clears_everything():
    m = TelegramSessionManager()
    c1, c2, pending = FakeClient(), FakeClient(), FakeClient()
    m.clients[1] = c1
    m.clients[2] = c2
    m.pending_auth[(1, "+70001112233")] = {"client": pending, "created_at": utcnow(), "twofa_enc": None}

    asyncio.run(m.stop_all())
    assert c1.disconnected and c2.disconnected and pending.disconnected
    assert not m.clients and not m.pending_auth
