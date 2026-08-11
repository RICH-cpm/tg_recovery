"""Аутентификация и сессии."""
from datetime import timedelta
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from .config import config
from .database import fetch_one, execute
from .crypto import verify_password, hash_password, dummy_verify
from .utils import utcnow, utcnow_iso

_s = URLSafeTimedSerializer(config.SECRET_KEY, salt="tg-recovery-session")


def create_session_token(uid, uname, epoch=0, issued_at=None):
    """issued_at — момент настоящего входа.

    Он переносится при каждом продлении, поэтому скользящее окно простоя не
    может продлевать сессию бесконечно: SESSION_MAX_AGE считается от входа.
    """
    return _s.dumps({
        "user_id": uid,
        "username": uname,
        "epoch": epoch,
        "iat": int(issued_at if issued_at is not None else utcnow().timestamp()),
    })


def verify_session_token(t):
    """Проверяет подпись и простой. Метка времени внутри токена обновляется
    при каждом запросе, поэтому max_age здесь — именно таймаут бездействия."""
    try:
        data = _s.loads(t, max_age=config.SESSION_IDLE_TIMEOUT)
    except (BadSignature, SignatureExpired):
        return None
    except Exception:
        return None
    if not isinstance(data, dict) or "user_id" not in data:
        return None
    # Абсолютный предел от момента входа.
    iat = data.get("iat")
    if iat is not None and utcnow().timestamp() - iat > config.SESSION_MAX_AGE:
        return None
    return data


def set_session_cookie(resp, uid, uname, epoch=0, issued_at=None):
    """Единая точка выдачи cookie сессии — чтобы флаги нигде не разъезжались.

    max_age и expires намеренно не задаются: cookie становится сеансовой и
    исчезает вместе с закрытием браузера. Раньше она жила 8 часов на диске,
    и после закрытия вкладки любой на том же устройстве попадал в панель
    без пароля. Браузеры с восстановлением вкладок могут сеансовую cookie
    вернуть — от этого страхует таймаут бездействия в самом токене.
    """
    resp.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=create_session_token(uid, uname, epoch, issued_at),
        httponly=True,
        secure=config.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(resp):
    # path обязателен: без него браузер может не удалить cookie, выставленную с path="/".
    resp.delete_cookie(config.SESSION_COOKIE_NAME, path="/")


async def authenticate_user(u, p):
    user = await fetch_one("SELECT * FROM users WHERE username = ?", (u,))
    if not user:
        dummy_verify()  # не даём отличить «нет такого логина» от «неверный пароль» по времени ответа
        return None
    return user if verify_password(p, user["password_hash"]) else None


async def check_login_rate_limit(ip):
    cutoff = (utcnow() - timedelta(minutes=max(1, config.LOGIN_RATE_WINDOW_MIN))).isoformat()
    r = await fetch_one("SELECT COUNT(*) AS c FROM login_attempts WHERE ip_address = ? AND attempted_at > ? AND success = 0", (ip, cutoff))
    return (r["c"] if r else 0) < config.LOGIN_RATE_LIMIT


async def record_login_attempt(ip, u, ok):
    await execute("INSERT INTO login_attempts (ip_address, username, success, attempted_at) VALUES (?, ?, ?, ?)", (ip, u, 1 if ok else 0, utcnow_iso()))


async def clear_login_attempts(ip):
    """После удачного входа обнуляем счётчик неудач, чтобы не блокировать себя же."""
    await execute("DELETE FROM login_attempts WHERE ip_address = ? AND success = 0", (ip,))


def get_client_ip(request):
    """IP клиента с учётом доверенных прокси.

    X-Forwarded-For заполняется клиентом и дополняется каждым прокси, поэтому
    первый элемент подделывается тривиально: раньше это позволяло обойти
    лимит попыток входа, просто меняя заголовок. Берём элемент, который
    подставил ближайший к нам доверенный прокси, — его подменить нельзя.
    """
    hops = config.TRUSTED_PROXY_HOPS
    peer = request.client.host if request.client else "unknown"
    if hops <= 0:
        return peer
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        parts = [x.strip() for x in fwd.split(",") if x.strip()]
        if parts:
            idx = max(0, len(parts) - hops)
            return parts[idx]
    real = request.headers.get("X-Real-IP")
    if real:
        return real.strip()
    return peer


async def get_current_user(request):
    t = request.cookies.get(config.SESSION_COOKIE_NAME)
    if not t: return None
    data = verify_session_token(t)
    if not data: return None
    u = await fetch_one("SELECT id, username, totp_enabled, is_admin, session_epoch FROM users WHERE id = ?", (data["user_id"],))
    if not u: return None
    if data.get("epoch", 0) != (u.get("session_epoch") or 0): return None
    u["session_iat"] = data.get("iat")
    return u


async def require_admin(request):
    """Пользователь с правами администратора или None."""
    user = await get_current_user(request)
    return user if user and user.get("is_admin") else None


async def bump_session_epoch(user_id):
    """Инвалидировать все выданные сессии пользователя. Возвращает новую эпоху."""
    row = await fetch_one("SELECT session_epoch FROM users WHERE id = ?", (user_id,))
    new_epoch = (row.get("session_epoch") or 0) + 1 if row else 1
    await execute("UPDATE users SET session_epoch = ? WHERE id = ?", (new_epoch, user_id))
    return new_epoch


async def create_initial_admin(u, p):
    ex = await fetch_one("SELECT id FROM users WHERE username = ?", (u,))
    if ex: return ex["id"]
    return await execute(
        "INSERT INTO users (username, password_hash, is_admin, session_epoch, created_at) VALUES (?, ?, 1, 0, ?)",
        (u, hash_password(p), utcnow_iso()),
    )
