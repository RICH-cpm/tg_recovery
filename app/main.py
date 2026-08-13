"""Точка входа приложения."""
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
# Обработчик вешаем на starlette-версию HTTPException: FastAPI-версия наследуется
# от неё, но 404 для несуществующего маршрута роутер бросает именно базовым классом.
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import config
from .templating import templates, nav_stats
from .auth import get_current_user, verify_session_token, set_session_cookie
from .database import init_db_sync, execute, close_db
from .telegram_manager import tg_manager
from .backup_service import maybe_backup
from .utils import utcnow
from .routes import auth_routes, dashboard, accounts, settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tg_recovery")

CLEANUP_INTERVAL = 3600
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


async def housekeeping_task():
    """Чистка старых записей, автобэкап и присмотр за Telegram-клиентами."""
    while True:
        try:
            if config.AUTO_DELETE_CODES_DAYS > 0:
                await execute(
                    "DELETE FROM received_codes WHERE received_at < ?",
                    ((utcnow() - timedelta(days=config.AUTO_DELETE_CODES_DAYS)).isoformat(),),
                )
            await execute("DELETE FROM login_attempts WHERE attempted_at < ?", ((utcnow() - timedelta(days=7)).isoformat(),))
            await execute("DELETE FROM audit_log WHERE created_at < ?", ((utcnow() - timedelta(days=180)).isoformat(),))
            await tg_manager.prune_pending()
            # Клиенты, которым Telethon не смог восстановить соединение, поднимаем сами:
            # иначе аккаунт молча перестаёт получать коды до ручного переподключения.
            await tg_manager.ensure_all_running()
            # Автобэкап по расписанию. На VPS это делает и cron, но там, где cron нет
            # (Railway и подобные), без этого бэкапы не создавались бы вовсе.
            await asyncio.to_thread(maybe_backup)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("housekeeping: %s", e)
        await asyncio.sleep(CLEANUP_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    init_db_sync()
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    await tg_manager.start_all()
    task = asyncio.create_task(housekeeping_task())
    log.info("started on %s:%s (env=%s)", config.HOST, config.PORT, config.ENV)
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await tg_manager.stop_all()
        await close_db()
        log.info("stopped")


app = FastAPI(title="TG Recovery", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(accounts.router)
app.include_router(settings.router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    if config.SEND_SECURITY_HEADERS:
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if config.IS_PRODUCTION:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Страницы не кэшируем: они персональные, а ещё несут ссылки на статику
    # с отпечатком версии — из кэша пришли бы ссылки на старые файлы.
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers.setdefault("Cache-Control", "no-store, must-revalidate")
    _refresh_session(request, resp)
    return resp


def _refresh_session(request: Request, resp) -> None:
    """Продлить окно бездействия, пока пользователь пользуется панелью.

    Метка времени лежит внутри подписанного токена, поэтому продление —
    это перевыпуск cookie. Проверяется только подпись, без обращения к базе.
    """
    token = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not token:
        return
    # Вход и выход уже выставили свою cookie — второй раз не трогаем,
    # иначе выход тут же вернул бы сессию обратно.
    for header in resp.headers.getlist("set-cookie"):
        if header.startswith(config.SESSION_COOKIE_NAME + "="):
            return
    data = verify_session_token(token)
    if not data:
        return
    set_session_cookie(resp, data["user_id"], data.get("username", ""), data.get("epoch", 0), data.get("iat"))


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return PlainTextResponse("ok")


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """404 — красивая страница, а для /api/* и любых других кодов — JSON."""
    if request.url.path.startswith("/api/") or exc.status_code != 404:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
    # Вошедшему 404 показываем внутри панели, гостю — отдельной страницей.
    user = None
    with suppress(Exception):
        user = await get_current_user(request)
    if not user:
        return templates.TemplateResponse(request, "404_guest.html", {"user": None}, status_code=404)
    return templates.TemplateResponse(
        request, "404.html", {"user": user, **await nav_stats(user)}, status_code=404
    )


def run():
    import uvicorn
    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, workers=1, log_level="info")


if __name__ == "__main__":
    run()
