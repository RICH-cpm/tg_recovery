"""Настройки: учётная запись, 2FA, бэкапы, журнал."""
import io, base64, asyncio
from typing import List
from urllib.parse import quote
import qrcode
from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from ..templating import templates, nav_stats
from ..auth import get_current_user, get_client_ip, set_session_cookie, bump_session_epoch
from ..crypto import hash_password, verify_password, generate_totp_secret, get_totp_uri, verify_totp
from ..database import fetch_one, fetch_all, execute, log_action, get_setting, set_setting
from ..backup_service import perform_backup
from ..telegram_manager import tg_manager

router = APIRouter()

MIN_USERNAME_LEN = 3
MIN_PASSWORD_LEN = 8
BACKUP_PERIODS = ("day", "week", "month")


def _login_redirect():
    return RedirectResponse("/login", status_code=302)


def _back(message=None, error=None):
    """Redirect на /settings с корректно закодированным сообщением.

    Раньше текст подставлялся в URL как есть: кириллица и символы вроде «:»
    ломали ссылку, а сообщение приходило искажённым.
    """
    if message:
        return RedirectResponse(f"/settings?message={quote(message)}", status_code=302)
    if error:
        return RedirectResponse(f"/settings?error={quote(error)}", status_code=302)
    return RedirectResponse("/settings", status_code=302)


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, message: str = None, error: str = None):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    accounts = await fetch_all("SELECT * FROM telegram_accounts WHERE user_id = ? ORDER BY created_at DESC", (user["id"],))
    backup = {
        "enabled": await get_setting("backup_enabled", "1") == "1",
        "period": await get_setting("backup_period", "day"),
        "last_at": await get_setting("last_backup_at", ""),
        "last_size": await get_setting("last_backup_size", ""),
    }
    return templates.TemplateResponse(
        request, "settings.html",
        {"user": user, "full_user": fu, "accounts": accounts, "backup": backup,
         "message": message, "error": error, "nav": "settings", **await nav_stats(user)},
    )


@router.post("/settings/change-username")
async def change_username(request: Request, new_username: str = Form(...), current_password: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    new_username = (new_username or "").strip()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    if not verify_password(current_password, fu["password_hash"]):
        return _back(error="Неверный пароль")
    if len(new_username) < MIN_USERNAME_LEN:
        return _back(error=f"Логин не короче {MIN_USERNAME_LEN} символов")
    if new_username == fu["username"]:
        return _back(message="Логин не изменился")
    if await fetch_one("SELECT id FROM users WHERE username = ? AND id != ?", (new_username, user["id"])):
        return _back(error="Логин занят")
    await execute("UPDATE users SET username = ? WHERE id = ?", (new_username, user["id"]))
    await log_action(user["id"], "username_changed", f"new={new_username}", get_client_ip(request))
    resp = _back(message="Логин изменён")
    set_session_cookie(resp, user["id"], new_username, fu.get("session_epoch") or 0)
    return resp


@router.post("/settings/change-password")
async def change_password(request: Request, current_password: str = Form(...), new_password: str = Form(...), confirm_password: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    if not verify_password(current_password, fu["password_hash"]):
        return _back(error="Неверный текущий пароль")
    if new_password != confirm_password:
        return _back(error="Пароли не совпадают")
    if len(new_password) < MIN_PASSWORD_LEN:
        return _back(error=f"Пароль не короче {MIN_PASSWORD_LEN} символов")
    if verify_password(new_password, fu["password_hash"]):
        return _back(error="Новый пароль совпадает со старым")
    await execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new_password), user["id"]))
    # Смена пароля обязана разлогинивать чужие сессии — иначе тот, кто увёл
    # cookie, сохраняет доступ и после того, как пароль поменяли.
    epoch = await bump_session_epoch(user["id"])
    await log_action(user["id"], "password_changed", "", get_client_ip(request))
    resp = _back(message="Пароль изменён, другие сессии завершены")
    set_session_cookie(resp, user["id"], fu["username"], epoch)
    return resp


@router.post("/settings/sessions/revoke-all")
async def revoke_all(request: Request):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    epoch = await bump_session_epoch(user["id"])
    await log_action(user["id"], "sessions_revoked", "", get_client_ip(request))
    resp = _back(message="Все другие сессии завершены")
    set_session_cookie(resp, user["id"], user["username"], epoch)
    return resp


@router.post("/settings/backup/save")
async def backup_save(request: Request, backup_enabled: str = Form("off"), backup_period: str = Form("day")):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await set_setting("backup_enabled", "1" if backup_enabled == "on" else "0")
    if backup_period in BACKUP_PERIODS:
        await set_setting("backup_period", backup_period)
    await log_action(user["id"], "backup_settings_changed", f"enabled={backup_enabled},period={backup_period}", get_client_ip(request))
    return _back(message="Настройки бэкапа сохранены")


@router.post("/settings/backup/now")
async def backup_now(request: Request):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    try:
        r = await asyncio.to_thread(perform_backup)
    except Exception as e:
        return _back(error=f"Ошибка бэкапа: {str(e)[:80]}")
    await log_action(user["id"], "backup_manual", r["filename"], get_client_ip(request))
    return _back(message=f"Бэкап создан ({r['size']})")


@router.post("/settings/accounts/delete")
async def delete_accounts(request: Request, account_ids: List[int] = Form([])):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    removed = 0
    for aid in account_ids:
        if await fetch_one("SELECT id FROM telegram_accounts WHERE id = ? AND user_id = ?", (aid, user["id"])):
            await tg_manager.remove_account(aid)
            removed += 1
    if not removed:
        return _back(error="Ничего не выбрано")
    await log_action(user["id"], "accounts_bulk_removed", f"count={removed}", get_client_ip(request))
    return _back(message=f"Удалено: {removed}")


@router.get("/settings/totp/setup", response_class=HTMLResponse)
async def totp_setup(request: Request, error: str = None):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    if fu["totp_enabled"]:
        return RedirectResponse("/settings", status_code=302)
    secret = fu["totp_secret"] or generate_totp_secret()
    if not fu["totp_secret"]:
        await execute("UPDATE users SET totp_secret = ? WHERE id = ?", (secret, user["id"]))
    qr = qrcode.QRCode(version=1, box_size=8, border=2)
    qr.add_data(get_totp_uri(secret, user["username"]))
    qr.make(fit=True)
    buf = io.BytesIO()
    qr.make_image(fill_color="#111", back_color="white").save(buf, format="PNG")
    return templates.TemplateResponse(
        request, "totp_setup.html",
        {"user": user, "secret": secret, "qr_base64": base64.b64encode(buf.getvalue()).decode(),
         "error": error, "nav": "settings", **await nav_stats(user)},
    )


@router.post("/settings/totp/enable")
async def totp_enable(request: Request, code: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    if not fu["totp_secret"]:
        return RedirectResponse("/settings/totp/setup", status_code=302)
    if fu["totp_enabled"]:
        return _back(message="2FA уже включена")
    if not verify_totp(fu["totp_secret"], code):
        return RedirectResponse(f"/settings/totp/setup?error={quote('Неверный код. Попробуйте ещё раз.')}", status_code=302)
    await execute("UPDATE users SET totp_enabled = 1 WHERE id = ?", (user["id"],))
    await log_action(user["id"], "totp_enabled", "", get_client_ip(request))
    return _back(message="2FA включена")


@router.post("/settings/totp/disable")
async def totp_disable(request: Request, password: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    fu = await fetch_one("SELECT * FROM users WHERE id = ?", (user["id"],))
    if not fu: return _login_redirect()
    if not verify_password(password, fu["password_hash"]):
        return _back(error="Неверный пароль")
    await execute("UPDATE users SET totp_enabled = 0, totp_secret = NULL WHERE id = ?", (user["id"],))
    await log_action(user["id"], "totp_disabled", "", get_client_ip(request))
    return _back(message="2FA отключена")


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(request: Request):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    # Неудачные входы пишутся с user_id = NULL, но по имени пользователя они наши —
    # без этого условия попытки подбора пароля в журнале не видны.
    logs = await fetch_all(
        """SELECT * FROM audit_log
            WHERE user_id = ? OR (user_id IS NULL AND details = ?)
         ORDER BY created_at DESC, id DESC LIMIT 200""",
        (user["id"], f"username={user['username']}"),
    )
    return templates.TemplateResponse(
        request, "audit.html",
        {"user": user, "logs": logs, "nav": "audit", **await nav_stats(user)},
    )
