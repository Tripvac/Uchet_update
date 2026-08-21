import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

try:
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST"),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )
    cur = conn.cursor()
    
    cur.execute("SELECT screen_id, lang, left(body, 50) FROM bot_content ORDER BY screen_id, lang;")
    rows = cur.fetchall()
    
    print(f"--- Найдено записей контента в базе: {len(rows)} ---")
    for row in rows:
        print(f"Экран: {row[0]:<20} | Язык: {row[1]} | Текст (начало): {row[2]}...")
        
    conn.close()
except Exception as e:
    print("Ошибка подключения к базе:", e)