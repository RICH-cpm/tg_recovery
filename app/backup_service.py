"""Резервное копирование (веб-кнопка, cron и фоновая задача приложения)."""
import os, sqlite3, tarfile, tempfile, logging
from datetime import datetime, timedelta
from pathlib import Path
from .config import config
from .utils import utcnow

log = logging.getLogger("tg_recovery.backup")

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}
BUSY_TIMEOUT_S = 10


def _connect():
    return sqlite3.connect(config.DATABASE_PATH, timeout=BUSY_TIMEOUT_S)


def _get(c, k, d=""):
    try:
        r = c.execute("SELECT value FROM app_settings WHERE key = ?", (k,)).fetchone()
    except sqlite3.Error:
        return d
    return r[0] if r and r[0] is not None else d


def _set(c, k, v):
    c.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))


def _human(n):
    n = float(n)
    for u in ["Б", "КБ", "МБ", "ГБ"]:
        if n < 1024:
            return f"{n:.0f} {u}" if u == "Б" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ТБ"


def _snapshot_db(dest: Path):
    """Согласованная копия базы через backup API SQLite.

    Просто положить файл базы в архив нельзя: при включённом WAL часть данных
    лежит в -wal, а сам файл может меняться прямо во время чтения — из такого
    архива восстанавливается битая или неполная база.
    """
    src = sqlite3.connect(config.DATABASE_PATH, timeout=BUSY_TIMEOUT_S)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def perform_backup():
    if not Path(config.DATABASE_PATH).exists():
        raise FileNotFoundError(f"База не найдена: {config.DATABASE_PATH}")

    bdir = Path(config.BACKUPS_DIR)
    bdir.mkdir(parents=True, exist_ok=True)
    bf = bdir / f"backup_{utcnow().strftime('%Y%m%d_%H%M%S')}.tar.gz"

    with tempfile.TemporaryDirectory(dir=str(bdir)) as tmpdir:
        snap = Path(tmpdir) / "recovery.db"
        _snapshot_db(snap)
        # Пишем во временный файл: прерванный бэкап не должен оставлять
        # обрезанный архив, который выглядит как валидный.
        tmp_archive = bf.with_suffix(bf.suffix + ".part")
        with tarfile.open(tmp_archive, "w:gz") as tar:
            tar.add(snap, arcname="recovery.db")
            if Path(config.SESSIONS_DIR).exists():
                # .tmp — недописанные сессии, в архиве они не нужны.
                tar.add(config.SESSIONS_DIR, arcname="sessions",
                        filter=lambda ti: None if ti.name.endswith(".tmp") else ti)
        os.replace(tmp_archive, bf)

    # Архив содержит базу и ключевые сессии — читать его должен только владелец.
    os.chmod(bf, 0o600)
    size = bf.stat().st_size

    retention = max(1, config.BACKUP_RETENTION_DAYS)
    cutoff = utcnow().timestamp() - retention * 24 * 3600
    for old in bdir.glob("backup_*.tar.gz"):
        try:
            if old != bf and old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError as e:
            log.warning("cannot remove old backup %s: %s", old, e)

    c = _connect()
    try:
        _set(c, "last_backup_at", utcnow().isoformat())
        _set(c, "last_backup_size", _human(size))
        c.commit()
    finally:
        c.close()
    return {"filename": bf.name, "size": _human(size), "path": str(bf)}


def maybe_backup():
    """Сделать бэкап, если по расписанию пора. Возвращает True, если сделали."""
    if not Path(config.DATABASE_PATH).exists():
        return False
    c = _connect()
    try:
        enabled = _get(c, "backup_enabled", "1")
        period = _get(c, "backup_period", "day")
        last = _get(c, "last_backup_at", "")
    finally:
        c.close()
    if enabled != "1":
        return False
    days = PERIOD_DAYS.get(period, 1)
    if last:
        try:
            if utcnow() - datetime.fromisoformat(last) < timedelta(days=days):
                return False
        except (ValueError, TypeError):
            pass  # битая метка времени — считаем, что бэкапа не было
    perform_backup()
    return True
