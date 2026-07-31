"""Резервное копирование по расписанию (вызывается из cron).

    python -m scripts.backup        — сделать бэкап, если пора по расписанию
    python -m scripts.backup --now  — сделать бэкап немедленно
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backup_service import maybe_backup, perform_backup


def main():
    force = "--now" in sys.argv or "-f" in sys.argv
    try:
        if force:
            r = perform_backup()
            print(f"[backup] создан {r['filename']} ({r['size']})")
            return 0
        print("[backup] выполнено" if maybe_backup() else "[backup] пропущено (ещё не время или выключено)")
        return 0
    except Exception as e:
        print(f"[backup] ОШИБКА: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
