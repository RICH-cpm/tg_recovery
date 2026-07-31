"""База данных SQLite (+ миграции, настройки).

Все запросы идут через одно долгоживущее соединение aiosqlite. Прежняя версия
открывала и закрывала соединение на каждый запрос: под нагрузкой (веб-запросы +
входящие сообщения Telegram одновременно) это давало "database is locked" и
заметно тормозило. Одно соединение + busy_timeout + WAL снимают проблему.
"""
import asyncio
import aiosqlite, sqlite3
from pathlib import Path
from .config import config
from .utils import utcnow_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, totp_secret TEXT, totp_enabled INTEGER DEFAULT 0, is_admin INTEGER DEFAULT 0, session_epoch INTEGER DEFAULT 0, created_at TEXT NOT NULL, last_login_at TEXT);
CREATE TABLE IF NOT EXISTS telegram_accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, display_name TEXT NOT NULL, phone_number TEXT NOT NULL, session_filename TEXT NOT NULL, twofa_enc TEXT, is_active INTEGER DEFAULT 1, is_connected INTEGER DEFAULT 0, last_seen_at TEXT, created_at TEXT NOT NULL, FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS received_codes (id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, code TEXT, full_text TEXT NOT NULL, received_at TEXT NOT NULL, is_read INTEGER DEFAULT 0, FOREIGN KEY (account_id) REFERENCES telegram_accounts(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, action TEXT NOT NULL, details TEXT, ip_address TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS login_attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, ip_address TEXT NOT NULL, username TEXT, success INTEGER DEFAULT 0, attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_codes_account ON received_codes(account_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_codes_received ON received_codes(received_at);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_ip ON login_attempts(ip_address, attempted_at DESC);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON telegram_accounts(user_id, created_at DESC);
"""
DEFAULT_SETTINGS = {"backup_enabled": "1", "backup_period": "day", "last_backup_at": "", "last_backup_size": ""}
BUSY_TIMEOUT_MS = 10_000

_db = None
_db_lock = asyncio.Lock()


def _columns(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def init_db_sync():
    p = Path(config.DATABASE_PATH); p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        # journal_mode нужно ставить до создания таблиц и вне транзакции.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        # Миграции для баз, созданных ранними версиями.
        if "session_epoch" not in _columns(conn, "users"):
            conn.execute("ALTER TABLE users ADD COLUMN session_epoch INTEGER DEFAULT 0")
        if "twofa_enc" not in _columns(conn, "telegram_accounts"):
            conn.execute("ALTER TABLE telegram_accounts ADD COLUMN twofa_enc TEXT")
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (k, v))
        # Флаг подключения живёт только в памяти процесса: после рестарта он всегда 0,
        # иначе UI показывает "подключён" для аккаунтов, которые ещё не поднялись.
        conn.execute("UPDATE telegram_accounts SET is_connected = 0")
        conn.commit()
    finally:
        conn.close()


async def get_db():
    """Одно общее соединение на процесс, создаётся лениво."""
    global _db
    if _db is None:
        async with _db_lock:
            if _db is None:
                Path(config.DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
                # isolation_level=None — autocommit: каждый запрос фиксируется сразу,
                # поэтому параллельные корутины не могут «склеить» чужие транзакции.
                db = await aiosqlite.connect(config.DATABASE_PATH, isolation_level=None)
                db.row_factory = aiosqlite.Row
                await db.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA synchronous = NORMAL")
                _db = db
    return _db


async def close_db():
    global _db
    async with _db_lock:
        if _db is not None:
            try:
                await _db.close()
            except Exception:
                pass
            _db = None


async def fetch_one(q, p=()):
    db = await get_db()
    async with db.execute(q, p) as c:
        r = await c.fetchone()
    return dict(r) if r else None


async def fetch_all(q, p=()):
    db = await get_db()
    async with db.execute(q, p) as c:
        rs = await c.fetchall()
    return [dict(r) for r in rs]


async def execute(q, p=()):
    """INSERT/UPDATE/DELETE. Возвращает lastrowid (осмысленен только для INSERT)."""
    db = await get_db()
    async with db.execute(q, p) as c:
        return c.lastrowid


async def execute_count(q, p=()):
    """Как execute, но возвращает количество затронутых строк."""
    db = await get_db()
    async with db.execute(q, p) as c:
        return c.rowcount


async def get_setting(k, d=""):
    r = await fetch_one("SELECT value FROM app_settings WHERE key = ?", (k,))
    return r["value"] if r and r["value"] is not None else d


async def set_setting(k, v):
    await execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))


async def log_action(user_id, action, details="", ip=""):
    await execute("INSERT INTO audit_log (user_id, action, details, ip_address, created_at) VALUES (?, ?, ?, ?, ?)", (user_id, action, details, ip, utcnow_iso()))
