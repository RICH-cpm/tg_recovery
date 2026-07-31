"""Создание учётной записи администратора.

Интерактивно:      python -m scripts.init_admin
Неинтерактивно:    TGR_ADMIN_USER=admin TGR_ADMIN_PASS=... python -m scripts.init_admin
"""
import asyncio, os, sys, getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config
from app.database import init_db_sync, fetch_one, close_db
from app.auth import create_initial_admin

MIN_USER = 3
MIN_PASS = 8


async def main():
    try:
        config.validate()
    except RuntimeError as e:
        print(f"[ERROR] {e}")
        return 1

    init_db_sync()
    config.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    env_user, env_pass = os.getenv("TGR_ADMIN_USER"), os.getenv("TGR_ADMIN_PASS")
    if env_user and env_pass:
        env_user = env_user.strip()
        if len(env_user) < MIN_USER:
            print(f"[ERROR] логин короче {MIN_USER} символов")
            return 1
        if len(env_pass) < MIN_PASS:
            print(f"[ERROR] пароль короче {MIN_PASS} символов")
            return 1
        if await fetch_one("SELECT id FROM users WHERE username = ?", (env_user,)):
            print(f"[OK] '{env_user}' уже есть.")
            return 0
        uid = await create_initial_admin(env_user, env_pass)
        print(f"[OK] Администратор создан: {env_user} (id={uid})")
        return 0

    if not sys.stdin.isatty():
        print("[ERROR] Нет TTY. Задайте TGR_ADMIN_USER и TGR_ADMIN_PASS.")
        return 1

    if await fetch_one("SELECT id FROM users WHERE is_admin = 1 LIMIT 1"):
        if input("Админ уже есть. Создать ещё одного? (y/N): ").strip().lower() != "y":
            return 0

    while True:
        username = input("Логин: ").strip()
        if len(username) < MIN_USER:
            print(f"  логин короче {MIN_USER} символов")
            continue
        if await fetch_one("SELECT id FROM users WHERE username = ?", (username,)):
            print("  такой логин уже занят")
            continue
        break

    while True:
        password = getpass.getpass("Пароль: ")
        if len(password) < MIN_PASS:
            print(f"  пароль короче {MIN_PASS} символов")
            continue
        if password != getpass.getpass("Повторите пароль: "):
            print("  пароли не совпадают")
            continue
        break

    uid = await create_initial_admin(username, password)
    print(f"[OK] Создан: {username} (id={uid})")
    return 0


async def _run():
    try:
        return await main()
    finally:
        # Соединение aiosqlite живёт в обычном (не daemon) потоке —
        # без закрытия процесс не завершится.
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
