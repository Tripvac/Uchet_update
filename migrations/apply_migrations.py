"""
Применение SQL-миграций на голом psycopg2 — без Alembic и ORM (по ТЗ).

Что делает:
  1. Создаёт служебную таблицу schema_migrations (учёт применённого).
  2. Находит все файлы migrations/NNN_*.sql, сортирует по имени.
  3. Применяет только те, которых ещё нет в schema_migrations, каждый —
     в отдельной транзакции; при успехе записывает имя файла.

Идемпотентность: сами SQL-файлы написаны через CREATE ... IF NOT EXISTS /
INSERT ... ON CONFLICT / CREATE OR REPLACE VIEW, поэтому повторный прогон
(в т.ч. на уже существующей вручную базе Neon) ничего не ломает — первый
запуск просто отметит их как применённые.

Запуск из корня репозитория:
    python migrations/apply_migrations.py
Параметры БД берутся из .env / переменных окружения (те же, что у bot.py).
"""

import glob
import os
import sys

import psycopg2
from dotenv import load_dotenv

load_dotenv()

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "5432"),
    "dbname": os.getenv("DB_NAME", "uchet_bot"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "sslmode": os.getenv("DB_SSLMODE", "prefer"),
}

_TRACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def discover_migrations() -> list[str]:
    """Все *.sql в каталоге миграций, отсортированные по имени (000, 001, ...)."""
    paths = glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))
    return sorted(os.path.basename(p) for p in paths)


def applied_set(cur) -> set[str]:
    cur.execute("SELECT filename FROM schema_migrations")
    return {row[0] for row in cur.fetchall()}


def apply_one(conn, filename: str) -> None:
    """Применяет один файл в отдельной транзакции и фиксирует его в учёте."""
    path = os.path.join(MIGRATIONS_DIR, filename)
    with open(path, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn:  # транзакция: commit при успехе, rollback при исключении
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,)
            )


def main() -> int:
    print(f"Подключение к БД {DB_CONFIG['dbname']}@{DB_CONFIG['host']}...")
    try:
        conn = psycopg2.connect(**DB_CONFIG)
    except Exception as e:
        print(f"Не удалось подключиться к БД: {e}")
        return 1

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(_TRACK_TABLE_SQL)

        with conn.cursor() as cur:
            done = applied_set(cur)

        pending = [f for f in discover_migrations() if f not in done]
        if not pending:
            print("Все миграции уже применены — делать нечего.")
            return 0

        print(f"К применению: {', '.join(pending)}")
        for filename in pending:
            print(f"  -> {filename}", end=" ... ")
            try:
                apply_one(conn, filename)
                print("ok")
            except Exception as e:
                print("ОШИБКА")
                print(f"Миграция {filename} не применена, откат: {e}")
                return 1

        print("Готово.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
