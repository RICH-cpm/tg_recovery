"""Вход и выход."""
from fastapi import APIRouter, Request, Form
from fastapi.responses import RedirectResponse
from ..templating import templates
from ..auth import (
    authenticate_user, check_login_rate_limit, record_login_attempt, clear_login_attempts,
    get_client_ip, get_current_user, set_session_cookie, clear_session_cookie,
)
from ..crypto import verify_totp
from ..database import execute, log_action
from ..utils import utcnow_iso

router = APIRouter()


def _login_page(request, status_code=200, **ctx):
    return templates.TemplateResponse(request, "login.html", ctx, status_code=status_code)


@router.get("/login")
async def login_page(request: Request, error: str = None):
    if await get_current_user(request):
        return RedirectResponse("/", status_code=302)
    return _login_page(request, error=error)


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...), totp_code: str = Form("")):
    ip = get_client_ip(request)
    username = (username or "").strip()
    if not await check_login_rate_limit(ip):
        # Заблокированную попытку не записываем: иначе счётчик неудач подпитывает
        # сам себя и окно блокировки никогда не заканчивается.
        return _login_page(request, 429, error="Слишком много попыток. Подождите минуту.", username=username)

    user = await authenticate_user(username, password)
    if not user:
        await record_login_attempt(ip, username, False)
        await log_action(None, "login_failed", f"username={username}", ip)
        return _login_page(request, 401, error="Неверный логин или пароль.", username=username)

    if user["totp_enabled"]:
        if not totp_code:
            return _login_page(request, 401, error="Введите код 2FA.", username=username, show_totp=True)
        if not verify_totp(user["totp_secret"], totp_code):
            await record_login_attempt(ip, username, False)
            await log_action(user["id"], "login_failed_totp", "", ip)
            return _login_page(request, 401, error="Неверный код 2FA.", username=username, show_totp=True)

    await record_login_attempt(ip, username, True)
    await clear_login_attempts(ip)
    await execute("UPDATE users SET last_login_at = ? WHERE id = ?", (utcnow_iso(), user["id"]))
    await log_action(user["id"], "login_success", "", ip)
    resp = RedirectResponse("/", status_code=302)
    set_session_cookie(resp, user["id"], user["username"], user.get("session_epoch") or 0)
    return resp


@router.post("/logout")
async def logout(request: Request):
    user = await get_current_user(request)
    if user:
        await log_action(user["id"], "logout", "", get_client_ip(request))
    resp = RedirectResponse("/login", status_code=302)
    clear_session_cookie(resp)
    return resp
