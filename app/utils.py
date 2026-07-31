"""Мелкие общие утилиты."""
from datetime import datetime, timezone


def utcnow():
    """Наивный datetime в UTC.

    datetime.utcnow() объявлен устаревшим начиная с Python 3.12, но весь формат
    хранения в базе — наивный ISO в UTC, поэтому tzinfo срезаем.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcnow_iso():
    return utcnow().isoformat()
