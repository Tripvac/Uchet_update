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
    
    # Принудительно меняем ссылки в базе на твои тестовые
    cur.execute("UPDATE bot_links SET url = 'https://t.me/uchetp' WHERE key = 'channel_news';")
    cur.execute("UPDATE bot_links SET url = 'https://t.me/uchetq' WHERE key = 'chat_accountants';")
    
    # Сохраняем изменения
    conn.commit()
    print("✅ Ссылки в базе успешно обновлены на тестовые!")
    
    conn.close()
except Exception as e:
    print("Ошибка:", e)