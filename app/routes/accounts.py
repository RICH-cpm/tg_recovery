"""Добавление, удаление и облачный пароль Telegram-аккаунтов."""
from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from ..templating import templates, nav_stats
from ..auth import get_current_user, get_client_ip
from ..database import fetch_one, execute, log_action
from ..crypto import encrypt_str, try_decrypt_str
from ..telegram_manager import tg_manager, normalize_phone

router = APIRouter()

MAX_NAME_LEN = 50


def _login_redirect():
    return RedirectResponse("/login", status_code=302)


async def _render(request, user, step, status_code=200, **ctx):
    return templates.TemplateResponse(
        request, "add_account.html",
        {"user": user, "step": step, "nav": "accounts", **await nav_stats(user), **ctx},
        status_code=status_code,
    )


async def _owned_account(user, account_id):
    a = await fetch_one("SELECT * FROM telegram_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"]))
    if not a:
        raise HTTPException(404, "Аккаунт не найден")
    return a


@router.get("/accounts/add", response_class=HTMLResponse)
async def add_page(request: Request):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    return await _render(request, user, "phone")


@router.post("/accounts/add/phone", response_class=HTMLResponse)
async def submit_phone(request: Request, phone_number: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    phone = normalize_phone(phone_number)
    r = await tg_manager.request_code(user["id"], phone)
    if not r["success"]:
        return await _render(request, user, "phone", 400, error=r.get("error"), phone_number=phone_number)
    # В журнал пишем только последние цифры: полный номер там не нужен.
    await log_action(user["id"], "account_add_phone", f"…{phone[-4:]}", get_client_ip(request))
    return await _render(request, user, "code", phone_number=phone)


@router.post("/accounts/add/code", response_class=HTMLResponse)
async def submit_code(request: Request, phone_number: str = Form(...), code: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    phone = normalize_phone(phone_number)
    r = await tg_manager.submit_code(user["id"], phone, code.strip())
    if not r["success"]:
        return await _render(request, user, "code", 400, phone_number=phone, error=r.get("error"))
    if r.get("needs_password"):
        return await _render(request, user, "password", phone_number=phone)
    return await _render(request, user, "name", phone_number=phone)


@router.post("/accounts/add/password", response_class=HTMLResponse)
async def submit_pw(request: Request, phone_number: str = Form(...), password: str = Form(...), save_twofa: str = Form("off")):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    phone = normalize_phone(phone_number)
    # Шифротекст больше не путешествует скрытым полем формы — он остаётся на сервере.
    twofa_enc = encrypt_str(password) if save_twofa == "on" and password else None
    r = await tg_manager.submit_password(user["id"], phone, password, twofa_enc)
    if not r["success"]:
        return await _render(request, user, "password", 400, phone_number=phone, error=r.get("error"))
    return await _render(request, user, "name", phone_number=phone)


@router.post("/accounts/add/finalize")
async def finalize(request: Request, phone_number: str = Form(...), display_name: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    phone = normalize_phone(phone_number)
    display_name = (display_name or "").strip()[:MAX_NAME_LEN] or phone
    r = await tg_manager.finalize_account(user["id"], phone, display_name)
    if not r["success"]:
        return await _render(request, user, "name", 400, phone_number=phone, error=r.get("error"))
    await log_action(user["id"], "account_added", f"account_id={r['account_id']}", get_client_ip(request))
    return RedirectResponse(f"/account/{r['account_id']}", status_code=302)


@router.post("/accounts/add/cancel")
async def cancel(request: Request, phone_number: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await tg_manager.cancel_pending(user["id"], normalize_phone(phone_number))
    return RedirectResponse("/", status_code=302)


@router.post("/accounts/{account_id}/delete")
async def delete_acc(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await _owned_account(user, account_id)
    await tg_manager.remove_account(account_id)
    await log_action(user["id"], "account_removed", f"account_id={account_id}", get_client_ip(request))
    return RedirectResponse("/", status_code=302)


@router.post("/accounts/{account_id}/reconnect")
async def reconnect(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await _owned_account(user, account_id)
    await tg_manager.reconnect_account(account_id)
    return RedirectResponse(f"/account/{account_id}", status_code=302)


@router.post("/accounts/{account_id}/twofa/save")
async def twofa_save(request: Request, account_id: int, twofa_password: str = Form(...)):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await _owned_account(user, account_id)
    pw = (twofa_password or "").strip()
    if not pw:
        return RedirectResponse(f"/account/{account_id}", status_code=302)
    await execute("UPDATE telegram_accounts SET twofa_enc = ? WHERE id = ? AND user_id = ?", (encrypt_str(pw), account_id, user["id"]))
    await log_action(user["id"], "twofa_saved", f"account_id={account_id}", get_client_ip(request))
    return RedirectResponse(f"/account/{account_id}", status_code=302)


@router.post("/accounts/{account_id}/twofa/delete")
async def twofa_delete(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: return _login_redirect()
    await _owned_account(user, account_id)
    await execute("UPDATE telegram_accounts SET twofa_enc = NULL WHERE id = ? AND user_id = ?", (account_id, user["id"]))
    await log_action(user["id"], "twofa_removed", f"account_id={account_id}", get_client_ip(request))
    return RedirectResponse(f"/account/{account_id}", status_code=302)


@router.get("/api/account/{account_id}/twofa")
async def twofa_reveal(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: raise HTTPException(401)
    a = await fetch_one("SELECT twofa_enc FROM telegram_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"]))
    if not a: raise HTTPException(404)
    if not a["twofa_enc"]:
        return JSONResponse({"has": False, "value": ""})
    value = try_decrypt_str(a["twofa_enc"])
    if value is None:
        return JSONResponse({"has": False, "value": "", "error": "decrypt"})
    await log_action(user["id"], "twofa_revealed", f"account_id={account_id}", get_client_ip(request))
    return JSONResponse({"has": True, "value": value})
