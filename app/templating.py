"""Jinja-окружение и вспомогательные фильтры представления.

Здесь только оформление: никакой бизнес-логики, чтобы шаблоны могли
показывать замаскированный телефон или инициалы, не трогая маршруты.
"""
import re
from pathlib import Path

from fastapi.templating import Jinja2Templates

from .config import config
from .database import fetch_one

templates = Jinja2Templates(directory=config.TEMPLATES_DIR)

_static_versions = {}


def static_url(path):
    """Ссылка на статику с отпечатком файла: /static/css/style.css?v=1a2b3c4d.

    Nginx отдаёт /static/ с заголовком «expires 7d», поэтому после обновления
    браузер ещё неделю показывал старые CSS и JS вместе с новой разметкой:
    вёрстка разъезжалась, а неизвестные строки перевода выводились ключами.
    Отпечаток меняется вместе с файлом и заставляет браузер скачать свежий.
    """
    path = path.lstrip("/")
    full = Path(config.STATIC_DIR) / path
    try:
        stamp = full.stat().st_mtime_ns
    except OSError:
        return f"/static/{path}"
    cached = _static_versions.get(path)
    if not cached or cached[0] != stamp:
        cached = (stamp, format(stamp & 0xFFFFFFFF, "08x"))
        _static_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


templates.env.globals["static_url"] = static_url


# Длина телефонного кода страны. Всё, чего нет в списках, считаем трёхзначным —
# иначе маска режет номер не по границе кода и выглядит как чужая страна
# (+447700900118 превращался в «+4 477 ••• 01 18»).
_CC1 = {"1", "7"}
_CC2 = {
    "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47",
    "48", "49", "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65",
    "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98",
}


def _cc_len(digits):
    if digits[:1] in _CC1:
        return 1
    if digits[:2] in _CC2:
        return 2
    return 3


def mask_phone(phone):
    """+79991234567 -> +7 999 ••• 45 67 (в списках полный номер не нужен)."""
    p = (phone or "").strip()
    digits = re.sub(r"\D", "", p)
    if len(digits) < 7:
        return p or "—"
    n = _cc_len(digits)
    cc, rest = digits[:n], digits[n:]
    if len(rest) < 6:
        return f"+{cc} ••• {rest[-2:]}"
    return f"+{cc} {rest[:3]} ••• {rest[-4:-2]} {rest[-2:]}"


def initials(name):
    """Первая буква названия для аватарки-плашки."""
    s = (name or "").strip()
    return s[0].upper() if s else "•"


templates.env.filters["mask_phone"] = mask_phone
templates.env.filters["initials"] = initials


async def nav_stats(user):
    """Счётчики для боковой панели: сколько аккаунтов, сколько на связи, сколько новых кодов.

    Оформительские данные — их показывает шапка на каждой странице, поэтому
    считаем одним запросом и передаём в шаблон рядом с основным контекстом.
    """
    if not user:
        return {"nav_total": 0, "nav_connected": 0, "nav_unread": 0, "nav_pct": 0}
    row = await fetch_one(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(is_connected), 0) AS connected,
                  (SELECT COUNT(*) FROM received_codes c
                     JOIN telegram_accounts t ON t.id = c.account_id
                    WHERE t.user_id = ? AND c.is_read = 0) AS unread
             FROM telegram_accounts WHERE user_id = ?""",
        (user["id"], user["id"]),
    ) or {}
    total = row.get("total") or 0
    connected = row.get("connected") or 0
    return {
        "nav_total": total,
        "nav_connected": connected,
        "nav_unread": row.get("unread") or 0,
        "nav_pct": round(connected * 100 / total) if total else 0,
    }
