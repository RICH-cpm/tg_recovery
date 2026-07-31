"""Список аккаунтов, карточка аккаунта и API кодов."""
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from ..templating import templates
from ..auth import get_current_user
from ..database import fetch_all, fetch_one, execute

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = await get_current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    accounts = await fetch_all(
        """SELECT a.*,
                  (SELECT COUNT(*) FROM received_codes c WHERE c.account_id = a.id AND c.is_read = 0) AS unread_count,
                  (SELECT received_at FROM received_codes c WHERE c.account_id = a.id ORDER BY received_at DESC LIMIT 1) AS last_code_at
             FROM telegram_accounts a
            WHERE a.user_id = ?
         ORDER BY a.created_at DESC""",
        (user["id"],),
    )
    connected = sum(1 for a in accounts if a["is_connected"])
    unread_total = sum(a["unread_count"] or 0 for a in accounts)
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"user": user, "accounts": accounts, "connected": connected, "unread_total": unread_total},
    )


@router.get("/account/{account_id}", response_class=HTMLResponse)
async def account_detail(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    account = await fetch_one("SELECT * FROM telegram_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"]))
    if not account: raise HTTPException(404, "Аккаунт не найден")
    codes = await fetch_all("SELECT * FROM received_codes WHERE account_id = ? ORDER BY received_at DESC, id DESC LIMIT 100", (account_id,))
    await execute("UPDATE received_codes SET is_read = 1 WHERE account_id = ? AND is_read = 0", (account_id,))
    return templates.TemplateResponse(request, "account_detail.html", {"user": user, "account": account, "codes": codes})


@router.get("/api/account/{account_id}/codes")
async def api_codes(request: Request, account_id: int):
    user = await get_current_user(request)
    if not user: raise HTTPException(401)
    if not await fetch_one("SELECT id FROM telegram_accounts WHERE id = ? AND user_id = ?", (account_id, user["id"])):
        raise HTTPException(404)
    codes = await fetch_all(
        "SELECT id, code, full_text, received_at FROM received_codes WHERE account_id = ? ORDER BY received_at DESC, id DESC LIMIT 20",
        (account_id,),
    )
    return JSONResponse({"codes": codes})
