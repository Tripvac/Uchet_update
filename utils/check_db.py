import os
import psycopg2
from dotenv import load_dotenv

# Подгружаем доступы из твоего .env
load_dotenv()

try:
    # Подключаемся к базе партнера
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )
    cur = conn.cursor()
    
    print("--- Последние 5 событий из таблицы bot_events ---")
    # Достаем самые свежие записи
    cur.execute("SELECT id, screen_id, button_id, lang, telegram_user_id FROM bot_events ORDER BY ts DESC LIMIT 5;")
    
    rows = cur.fetchall()
    if not rows:
        print("Таблица пока пустая. Покликай бота в Телеграме!")
    else:
        for row in rows:
            print(f"ID: {row[0]} | Экран: {row[1]:<15} | Кнопка: {row[2]:<15} | Язык: {row[3]} | User ID: {row[4]}")
            
    conn.close()
except Exception as e:
    print("Не удалось подключиться или выполнить запрос:", e)