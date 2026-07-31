"""Менеджер Telegram-сессий."""
import asyncio, re, os, logging
from typing import Dict, Optional, Tuple
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError, PhoneCodeInvalidError, PhoneCodeExpiredError,
    PhoneCodeEmptyError, PhoneNumberInvalidError, PhoneNumberBannedError,
    PasswordHashInvalidError, FloodWaitError, ApiIdInvalidError,
)
from telethon.sessions import StringSession
from .config import config
from .database import fetch_all, fetch_one, execute
from .crypto import encrypt_bytes, decrypt_bytes
from .utils import utcnow, utcnow_iso

log = logging.getLogger("tg_recovery.telegram")

# Сначала ищем число рядом со словом «код», и только потом любое 5-6-значное:
# в тексте вроде «Ваш код: 12345, сообщение №98765» не выберется лишнее число.
CODE_PATTERNS = (
    re.compile(r"(?:code|код)\W{0,20}?(\d{5,6})\b", re.IGNORECASE),
    re.compile(r"\b(\d{5,6})\b"),
)
PHONE_RE = re.compile(r"^\+\d{7,15}$")


def extract_code(text: str) -> Optional[str]:
    for pat in CODE_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1)
    return None


def normalize_phone(raw: str) -> str:
    """+7 (999) 123-45-67 -> +79991234567."""
    digits = re.sub(r"\D", "", raw or "")
    return "+" + digits if digits else ""


def is_valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone or ""))


def _friendly_error(e: Exception) -> str:
    if isinstance(e, FloodWaitError):
        return f"Слишком много попыток. Подождите {e.seconds} сек."
    if isinstance(e, PhoneNumberInvalidError):
        return "Неверный номер телефона."
    if isinstance(e, PhoneNumberBannedError):
        return "Этот номер заблокирован в Telegram."
    if isinstance(e, ApiIdInvalidError):
        return "Неверные TG_API_ID / TG_API_HASH."
    if isinstance(e, (PhoneCodeInvalidError, PhoneCodeEmptyError)):
        return "Неверный код."
    if isinstance(e, PhoneCodeExpiredError):
        return "Код истёк. Запросите новый."
    if isinstance(e, PasswordHashInvalidError):
        return "Неверный облачный пароль."
    return str(e) or e.__class__.__name__


class TelegramSessionManager:
    def __init__(self):
        self.clients: Dict[int, TelegramClient] = {}
        self._tasks: Dict[int, asyncio.Task] = {}
        # Ключ — (user_id, phone): раньше ключом был только телефон, и любой
        # залогиненный пользователь мог перехватить чужое незавершённое добавление.
        self.pending_auth: Dict[Tuple[int, str], dict] = {}
        self._lock = asyncio.Lock()
        self._closing = False

    # ---------------------------------------------------------------- запуск

    async def start_all(self):
        self._closing = False
        for a in await fetch_all("SELECT * FROM telegram_accounts WHERE is_active = 1"):
            try:
                await self._start_client(a)
            except Exception as e:
                log.warning("start %s: %s", a["id"], e)

    async def ensure_all_running(self):
        """Сторож: поднимает аккаунты, чьё соединение оборвалось окончательно."""
        if self._closing:
            return
        for a in await fetch_all("SELECT * FROM telegram_accounts WHERE is_active = 1"):
            c = self.clients.get(a["id"])
            if c is not None and c.is_connected():
                continue
            try:
                await self._start_client(a)
            except Exception as e:
                log.warning("watchdog %s: %s", a["id"], e)

    async def stop_all(self):
        self._closing = True
        async with self._lock:
            tasks = list(self._tasks.values()); self._tasks.clear()
            clients = list(self.clients.values()); self.clients.clear()
        for c in clients:
            try:
                await asyncio.wait_for(c.disconnect(), timeout=10)
            except Exception:
                pass
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for p in list(self.pending_auth.values()):
            try:
                await asyncio.wait_for(p["client"].disconnect(), timeout=5)
            except Exception:
                pass
        self.pending_auth.clear()

    # ------------------------------------------------------------- хранилище

    def _session_path(self, fn):
        # Имя файла приходит только из нашего кода, но защититься от «..» дёшево.
        return config.SESSIONS_DIR / os.path.basename(fn)

    async def _load(self, fn):
        p = self._session_path(fn)
        if not p.exists():
            raise FileNotFoundError(fn)
        with open(p, "rb") as f:
            return decrypt_bytes(f.read()).decode("utf-8")

    async def _save(self, fn, s):
        config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        p = self._session_path(fn)
        # Пишем во временный файл и подменяем: обрыв на середине не оставит
        # полузаписанную (нерасшифровываемую) сессию вместо рабочей.
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "wb") as f:
            f.write(encrypt_bytes(s.encode("utf-8")))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)

    def _new(self, s=""):
        return TelegramClient(
            StringSession(s), config.TG_API_ID, config.TG_API_HASH,
            device_model="Recovery Tool", system_version="1.0", app_version="1.0",
        )

    # ------------------------------------------------------------- соединения

    async def _start_client(self, a):
        if a is None or self._closing:
            return
        aid = a["id"]
        async with self._lock:
            existing = self.clients.get(aid)
            if existing is not None and existing.is_connected():
                return
            if existing is not None:
                self.clients.pop(aid, None)
                try:
                    await asyncio.wait_for(existing.disconnect(), timeout=10)
                except Exception:
                    pass

            try:
                s = await self._load(a["session_filename"])
            except FileNotFoundError:
                log.warning("session file missing for account %s", aid)
                await execute("UPDATE telegram_accounts SET is_connected = 0 WHERE id = ?", (aid,))
                return
            except Exception as e:  # битый файл или сменился ENCRYPTION_KEY
                log.error("cannot decrypt session for account %s: %s", aid, e)
                await execute("UPDATE telegram_accounts SET is_connected = 0 WHERE id = ?", (aid,))
                return

            c = self._new(s)

            @c.on(events.NewMessage(from_users=config.TELEGRAM_SERVICE_ID))
            async def handler(event):
                await self._msg(aid, event)

            try:
                await c.connect()
                if not await c.is_user_authorized():
                    log.warning("account %s is no longer authorized", aid)
                    await execute("UPDATE telegram_accounts SET is_connected = 0 WHERE id = ?", (aid,))
                    await c.disconnect()
                    return
                self.clients[aid] = c
                await execute("UPDATE telegram_accounts SET is_connected = 1, last_seen_at = ? WHERE id = ?", (utcnow_iso(), aid))
                # Ссылку на задачу держим в словаре: без неё сборщик мусора может
                # забрать задачу, и клиент тихо перестанет слушать сообщения.
                self._tasks[aid] = asyncio.create_task(self._run(aid, c))
            except Exception as e:
                log.error("connect %s: %s", aid, e)
                self.clients.pop(aid, None)
                try:
                    await c.disconnect()
                except Exception:
                    pass
                await execute("UPDATE telegram_accounts SET is_connected = 0 WHERE id = ?", (aid,))

    async def _run(self, aid, c):
        try:
            await c.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("run %s: %s", aid, e)
        finally:
            # Снимаем регистрацию, только если в словаре всё ещё наш клиент:
            # иначе гонка с reconnect_account погасит только что поднятое соединение.
            if self.clients.get(aid) is c:
                self.clients.pop(aid, None)
                try:
                    await execute("UPDATE telegram_accounts SET is_connected = 0 WHERE id = ?", (aid,))
                except Exception:
                    pass
            if self._tasks.get(aid) is asyncio.current_task():
                self._tasks.pop(aid, None)

    async def _msg(self, aid, event):
        try:
            text = event.message.message or ""
            now = utcnow_iso()
            await execute(
                "INSERT INTO received_codes (account_id, code, full_text, received_at) VALUES (?, ?, ?, ?)",
                (aid, extract_code(text), text, now),
            )
            await execute("UPDATE telegram_accounts SET last_seen_at = ? WHERE id = ?", (now, aid))
        except Exception as e:
            log.error("save message for %s: %s", aid, e)

    # ---------------------------------------------------- добавление аккаунта

    async def prune_pending(self):
        """Отключить клиентов брошенных на полпути добавлений."""
        now = utcnow()
        stale = [k for k, v in self.pending_auth.items()
                 if (now - v["created_at"]).total_seconds() > config.PENDING_AUTH_TTL]
        for k in stale:
            p = self.pending_auth.pop(k, None)
            if p:
                try:
                    await asyncio.wait_for(p["client"].disconnect(), timeout=5)
                except Exception:
                    pass
        return len(stale)

    async def request_code(self, user_id, phone):
        if not is_valid_phone(phone):
            return {"success": False, "error": "Неверный номер телефона. Формат: +79991234567"}
        # Предыдущая попытка для этого же номера — закрываем, иначе клиент утечёт.
        await self.cancel_pending(user_id, phone)
        c = self._new()
        try:
            await c.connect()
            sent = await c.send_code_request(phone)
            self.pending_auth[(user_id, phone)] = {
                "client": c, "phone_code_hash": sent.phone_code_hash,
                "created_at": utcnow(), "twofa_enc": None,
            }
            return {"success": True}
        except Exception as e:
            try:
                await c.disconnect()
            except Exception:
                pass
            return {"success": False, "error": _friendly_error(e)}

    async def submit_code(self, user_id, phone, code):
        p = self.pending_auth.get((user_id, phone))
        if not p:
            return {"success": False, "error": "Сессия добавления истекла. Начните заново."}
        c = p["client"]
        try:
            await c.sign_in(phone=phone, code=code, phone_code_hash=p["phone_code_hash"])
            return {"success": True, "needs_password": False}
        except SessionPasswordNeededError:
            return {"success": True, "needs_password": True}
        except PhoneCodeExpiredError:
            await self.cancel_pending(user_id, phone)
            return {"success": False, "error": "Код истёк. Запросите новый."}
        except Exception as e:
            return {"success": False, "error": _friendly_error(e)}

    async def submit_password(self, user_id, phone, password, twofa_enc=None):
        p = self.pending_auth.get((user_id, phone))
        if not p:
            return {"success": False, "error": "Сессия добавления истекла."}
        try:
            await p["client"].sign_in(password=password)
            # Зашифрованный пароль храним на сервере, а не гоняем скрытым полем формы.
            p["twofa_enc"] = twofa_enc
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": _friendly_error(e)}

    async def finalize_account(self, user_id, phone, display_name):
        key = (user_id, phone)
        p = self.pending_auth.get(key)
        if not p:
            return {"success": False, "error": "Сессия добавления истекла."}
        c = p["client"]
        fn = None
        try:
            if not await c.is_user_authorized():
                return {"success": False, "error": "Авторизация не завершена. Начните заново."}
            s = c.session.save()
            fn = f"acc_{user_id}_{int(utcnow().timestamp())}.enc"
            await self._save(fn, s)
            aid = await execute(
                "INSERT INTO telegram_accounts (user_id, display_name, phone_number, session_filename, twofa_enc, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (user_id, display_name, phone, fn, p.get("twofa_enc") or None, utcnow_iso()),
            )
        except Exception as e:
            # Если строка в базе не создалась — не оставляем осиротевший файл сессии.
            if fn:
                try:
                    self._session_path(fn).unlink(missing_ok=True)
                except Exception:
                    pass
            return {"success": False, "error": _friendly_error(e)}

        self.pending_auth.pop(key, None)
        try:
            await c.disconnect()
        except Exception:
            pass
        try:
            await self._start_client(await fetch_one("SELECT * FROM telegram_accounts WHERE id = ?", (aid,)))
        except Exception as e:
            log.warning("start after finalize %s: %s", aid, e)
        return {"success": True, "account_id": aid}

    async def cancel_pending(self, user_id, phone):
        p = self.pending_auth.pop((user_id, phone), None)
        if p:
            try:
                await asyncio.wait_for(p["client"].disconnect(), timeout=5)
            except Exception:
                pass

    # ----------------------------------------------------- удаление / рестарт

    async def remove_account(self, aid):
        """Строку и файл сессии убираем сразу, отзыв в Telegram — в фоне."""
        a = await fetch_one("SELECT session_filename FROM telegram_accounts WHERE id = ?", (aid,))
        await execute("DELETE FROM telegram_accounts WHERE id = ?", (aid,))
        if a:
            try:
                self._session_path(a["session_filename"]).unlink(missing_ok=True)
            except Exception:
                pass
        async with self._lock:
            c = self.clients.pop(aid, None)
            t = self._tasks.pop(aid, None)
        if t:
            t.cancel()
        if c:
            async def _kill(client):
                try:
                    await asyncio.wait_for(client.log_out(), timeout=10)
                except Exception:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=10)
                    except Exception:
                        pass
            asyncio.create_task(_kill(c))

    async def reconnect_account(self, aid):
        async with self._lock:
            old = self.clients.pop(aid, None)
            t = self._tasks.pop(aid, None)
        if t:
            t.cancel()
        if old:
            try:
                await asyncio.wait_for(old.disconnect(), timeout=10)
            except Exception:
                pass
        a = await fetch_one("SELECT * FROM telegram_accounts WHERE id = ? AND is_active = 1", (aid,))
        if a:
            await self._start_client(a)


tg_manager = TelegramSessionManager()
