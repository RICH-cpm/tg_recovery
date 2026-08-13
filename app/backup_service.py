"""Резервное копирование (веб-кнопка, cron и фоновая задача приложения)."""
import os, re, shutil, sqlite3, tarfile, tempfile, logging
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
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


BACKUP_NAME_RE = re.compile(r"^backup_\d{8}_\d{6}\.tar\.gz$")
MAX_RESTORE_BYTES = 512 * 1024 * 1024   # разумный потолок для распакованного архива


def list_backups():
    """Существующие архивы, свежие сверху."""
    bdir = Path(config.BACKUPS_DIR)
    if not bdir.exists():
        return []
    items = []
    for f in bdir.glob("backup_*.tar.gz"):
        try:
            st = f.stat()
        except OSError:
            continue
        items.append({
            "name": f.name,
            "size": _human(st.st_size),
            "bytes": st.st_size,
            "created_at": datetime.utcfromtimestamp(st.st_mtime).isoformat(),
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


def backup_path(name):
    """Путь к архиву по имени. None, если имя не наше или файла нет.

    Имя приходит из адресной строки, поэтому проверяем его по шаблону и
    сверяем итоговый путь с каталогом бэкапов — «..» не пройдёт.
    """
    if not name or not BACKUP_NAME_RE.match(name):
        return None
    bdir = Path(config.BACKUPS_DIR).resolve()
    p = (bdir / name).resolve()
    if p.parent != bdir or not p.is_file():
        return None
    return p


def _safe_members(tar):
    """Только ожидаемые файлы: сама база и содержимое sessions/.

    tarfile без проверок умеет писать по абсолютным путям и «..», то есть
    куда угодно на диске. Разрешаем строго обычные файлы внутри архива.
    """
    total = 0
    for m in tar.getmembers():
        # isfile() пропускает только обычные файлы: ссылки и устройства
        # из архива не извлекаются вовсе.
        if not m.isfile():
            continue
        name = m.name[2:] if m.name.startswith("./") else m.name
        parts = PurePosixPath(name).parts
        # lstrip("./") здесь не годится: он срезает точки посимвольно и
        # тихо превращает «../../evil.sh» в «evil.sh» вместо отказа.
        if name.startswith("/") or ".." in parts or not parts:
            raise ValueError(f"Недопустимый путь в архиве: {m.name}")
        if name != "recovery.db" and not name.startswith("sessions/"):
            continue
        total += m.size
        if total > MAX_RESTORE_BYTES:
            raise ValueError("Архив слишком большой")
        m.name = name
        yield m


def inspect_backup(archive: Path):
    """Проверить архив до восстановления. Возвращает сводку или бросает ValueError."""
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = list(_safe_members(tar))
    except (tarfile.TarError, OSError) as e:
        raise ValueError(f"Не удалось прочитать архив: {e}")
    if not any(m.name == "recovery.db" for m in members):
        raise ValueError("В архиве нет файла recovery.db — это не бэкап TG Recovery")
    sessions = [m for m in members if m.name.startswith("sessions/")]
    return {"sessions": len(sessions)}


def restore_backup(archive: Path):
    """Восстановить базу и сессии из архива.

    Перед вызовом приложение обязано закрыть своё соединение с базой и
    отключить Telegram-клиентов, иначе поверх открытых файлов ляжет чужая
    база. Прежнее состояние сохраняется рядом как *.replaced-<метка>.
    """
    summary = inspect_backup(archive)
    db_path = Path(config.DATABASE_PATH)
    sessions_dir = Path(config.SESSIONS_DIR)
    stamp = utcnow().strftime("%Y%m%d_%H%M%S")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(tmp, members=_safe_members(tar))

        new_db = tmp / "recovery.db"
        # Битую базу лучше отвергнуть до подмены, чем после.
        probe = sqlite3.connect(new_db)
        try:
            if probe.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("База в архиве повреждена")
            probe.execute("SELECT COUNT(*) FROM users").fetchone()
        except sqlite3.Error as e:
            raise ValueError(f"База в архиве нечитаема: {e}")
        finally:
            probe.close()

        db_path.parent.mkdir(parents=True, exist_ok=True)
        if db_path.exists():
            db_path.replace(db_path.with_name(db_path.name + f".replaced-{stamp}"))
        # WAL и SHM от старой базы к новой не относятся.
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                side.unlink()
        shutil.move(str(new_db), str(db_path))
        os.chmod(db_path, 0o600)

        restored_sessions = 0
        src_sessions = tmp / "sessions"
        if src_sessions.is_dir():
            if sessions_dir.exists():
                shutil.move(str(sessions_dir), str(sessions_dir.with_name(sessions_dir.name + f".replaced-{stamp}")))
            sessions_dir.mkdir(parents=True, exist_ok=True)
            for f in src_sessions.iterdir():
                if f.is_file():
                    shutil.move(str(f), str(sessions_dir / f.name))
                    os.chmod(sessions_dir / f.name, 0o600)
                    restored_sessions += 1

    log.warning("restored from %s: sessions=%s", archive.name, restored_sessions)
    return {"sessions": restored_sessions, "expected_sessions": summary["sessions"], "stamp": stamp}


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
