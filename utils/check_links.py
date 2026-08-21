import os
import psycopg2
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from dotenv import load_dotenv

load_dotenv()

def check_links():
    print("🔍 Подключение к базе данных для проверки ссылок...")
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            host=os.getenv("DB_HOST", "localhost"),
            port=os.getenv("DB_PORT", "5432"),
            sslmode=os.getenv("DB_SSLMODE", "prefer")
        )
        cur = conn.cursor()
        cur.execute("SELECT key, url FROM bot_links;")
        links = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"❌ Ошибка подключения к базе данных: {e}")
        exit(1)

    print(f"Найдено ссылок в таблице bot_links: {len(links)}. Проверяем статус...\n")
    all_ok = True
    
    for key, url in links:
        try:
            # Передаем User-Agent, чтобы сайты не блокировали стандартный пинг Python
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Compatible; UchetBotLinkChecker/1.0)"})
            with urlopen(req, timeout=10) as response:
                code = response.getcode()
                if 200 <= code < 400:
                    print(f"✅ [{code}] {key} -> {url}")
                else:
                    print(f"⚠️ [HTTP {code}] {key} -> {url}")
                    all_ok = False
        except HTTPError as e:
            print(f"❌ [HTTP Error {e.code}] {key} -> {url}")
            all_ok = False
        except URLError as e:
            print(f"❌ [URL Error] {key} -> {url} (Причина: {e.reason})")
            all_ok = False
        except Exception as e:
            print(f"❌ [Error] {key} -> {url} (Детали: {e})")
            all_ok = False

    if not all_ok:
        print("\n⚠️ Некоторые ссылки вернули ошибки или недоступны!")
        exit(1)
    else:
        print("\n🎉 Все ссылки из таблицы bot_links успешно прошли проверку (2xx/3xx)!")

if __name__ == "__main__":
    check_links()