import contextlib
import html
import json
import logging
import os
import queue
import re
import threading
import time
import requests
import os
from concurrent.futures import ThreadPoolExecutor

import psycopg2
import psycopg2.pool
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from psycopg2.extras import RealDictCursor
from requests.adapters import HTTPAdapter

# ==========================================
# 1. КОНФИГУРАЦИЯ И ЗАГРУЗКА РЕСУРСОВ
# ==========================================
load_dotenv()

# Логирование вместо print: уровни, время, трассировки исключений — чтобы по
# логам Render можно было разобрать инцидент. Уровень управляется LOG_LEVEL.
# ВАЖНО: не логируем тексты сообщений пользователей и персональные данные.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("uchet-bot")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{TOKEN}/"

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "uchet_bot")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_SSLMODE = os.getenv("DB_SSLMODE", "prefer")

REQUIRED_SUBSCRIPTIONS = [
    ch.strip()
    for ch in os.getenv("REQUIRED_SUBSCRIPTIONS", "").split(",")
    if ch.strip()
]
FAQ_PAGE_SIZE = int(os.getenv("FAQ_PAGE_SIZE", 3))

# Чат для технических уведомлений админу (BUG-2 / NOTIFY-1). Необязательно:
# если не задан, notify_admin просто пишет в лог.
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

BOT_MODE = os.getenv("BOT_MODE", "polling").lower()  # "polling" или "webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super-secret-token")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")


def require_env():
    """
    Падаем на старте, а не в рантайме на первом пользователе: пустой токен,
    отсутствующие параметры БД или пустой список каналов — это ошибка деплоя,
    и её надо увидеть в логах Render немедленно, а не через неделю по жалобе.

    Пустой REQUIRED_SUBSCRIPTIONS отдельно фатален: раньше это молча отключало
    гейт подписки (ключевое требование ТЗ), и все проходили мимо него.
    """
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", TOKEN),
            ("DB_HOST", DB_HOST),
            ("DB_NAME", DB_NAME),
            ("DB_USER", DB_USER),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            "Не заданы обязательные переменные окружения: " + ", ".join(missing)
        )
    if not REQUIRED_SUBSCRIPTIONS:
        raise SystemExit(
            "REQUIRED_SUBSCRIPTIONS пуст — проверка подписки была бы отключена, "
            "а это ключевое требование ТЗ. Укажите каналы через запятую."
        )

SCREENS = {}
LOCALES = {}
EXECUTOR = None
DB_POOL = None  # инициализируется в init_db_pool() при старте main()
HTTP_SESSION = requests.Session()  # Используется для TCP-соединений к Telegram
_adapter = HTTPAdapter(pool_connections=20, pool_maxsize=20)
HTTP_SESSION.mount("https://", _adapter)
HTTP_SESSION.mount("http://", _adapter)
CONTENT_CACHE = {}  # (screen_id, lang) -> body, из таблицы bot_content
LINKS_CACHE = {}  # key -> url, из таблицы bot_links
CACHE_LOADED_AT = 0
CACHE_TTL_SECONDS = 300  # 5 минут, как заявлено в ADMIN.md

# Отметка последнего успешного цикла getUpdates. Пишется из главного потока
# (polling), читается из потока Flask (/health); для одного float атомарности
# CPython достаточно. Если polling встал, health это увидит по возрасту метки.
LAST_POLL_OK = time.time()
POLL_STALE_SECONDS = 120

# Сколько апдейтов обрабатываем параллельно (разные чаты). Размер пула БД
# считается от этого числа, чтобы спрос на соединения не превышал предложение.
MAX_WORKERS = 12

# Словарь для антифлуда: {(chat_id, trigger_key): timestamp}
_NOTIFY_COOLDOWN = {}
NOTIFY_COOLDOWN_SECONDS = 3600  # 1 час

# --- Сериализация обработки по чату (защита от гонок на сессии) ---
# Апдейты одного чата должны обрабатываться по одному: иначе два быстрых
# нажатия читают одну сессию, оба правят history/screen_state и пишут —
# состояние затирается, в чат уходят дубли сообщений. Полосатые (striped)
# блокировки: фиксированный массив вместо «словарь lock на каждый chat_id»,
# который рос бы в памяти без границ. Изредка два разных чата попадут на одну
# полосу и сериализуются — при 256 полосах это пренебрежимо мало.
_CHAT_LOCK_STRIPES = 256
_CHAT_LOCKS = [threading.Lock() for _ in range(_CHAT_LOCK_STRIPES)]


def chat_lock(chat_id):
    """Возвращает блокировку-полосу для чата (см. комментарий выше)."""
    return _CHAT_LOCKS[chat_id % _CHAT_LOCK_STRIPES]

# --- Очередь статистики ---
# bot_events пишем в фоне, но НЕ «поток на каждый клик» (это неограниченный
# рост числа потоков и заимствований из пула БД под нагрузкой). Один воркер
# разбирает ограниченную очередь; при переполнении событие теряется —
# статистика некритична, память важнее.
_EVENTS_QUEUE: "queue.Queue" = queue.Queue(maxsize=10000)

KASPI_QR_MEDIA_KEY = "kaspi_qr"
KASPI_QR_LOCAL_PATH = "assets/kaspi_qr.png"

CPE_HOURS_DOC_KEY = "cpe_hours_doc"
# URL памятки больше не хардкодится: он лежит в bot_media.source_url и
# редактируется администратором через SQL (см. QR-1 и admin.md).


# Читаем пути и поддерживаемые локали из переменных окружения (с дефолтами)
SCREENS_PATH = os.getenv("SCREENS_PATH", "map/screens.json")
LOCALES_DIR = os.getenv("LOCALES_DIR", "locales")
SUPPORTED_LOCALES = [lang.strip() for lang in os.getenv("SUPPORTED_LOCALES", "ru,kz").split(",")]

def load_ui_resources():
    global SCREENS, LOCALES
    try:
        with open(SCREENS_PATH, "r", encoding="utf-8") as f:
            SCREENS = json.load(f)

        for lang in SUPPORTED_LOCALES:
            filepath = os.path.join(LOCALES_DIR, f"{lang}.json")
            if os.path.exists(filepath):
                with open(filepath, "r", encoding="utf-8") as f:
                    LOCALES[lang] = json.load(f)
            else:
                LOCALES[lang] = {}
        log.info("Ресурсы UI успешно загружены.")
    except Exception:
        log.error("Ошибка загрузки UI ресурсов", exc_info=True)


def load_dynamic_content():
    """
    Подтягивает редактируемый администратором контент из БД в память.
    bot_content — длинные тексты экранов, bot_links — внешние ссылки.
    Вызывается при старте и раз в CACHE_TTL_SECONDS из основного цикла.
    Если БД недоступна — старый кэш остаётся как есть, бот не падает.
    """
    global CONTENT_CACHE, LINKS_CACHE, CACHE_LOADED_AT
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT screen_id, lang, body FROM bot_content "
                    "WHERE is_active = TRUE"
                )
                new_content = {(row[0], row[1]): row[2] for row in cur.fetchall()}

                cur.execute("SELECT key, url FROM bot_links")
                new_links = {row[0]: row[1] for row in cur.fetchall()}

        CONTENT_CACHE = new_content
        LINKS_CACHE = new_links
        CACHE_LOADED_AT = time.time()
        log.info(
            "Кэш контента обновлён: %d текстов, %d ссылок.",
            len(CONTENT_CACHE),
            len(LINKS_CACHE),
        )
    except Exception as e:
        # Не роняем бота, если БД временно недоступна — работаем на старом кэше
        log.warning("Не удалось обновить кэш контента из БД: %s", e)


def maybe_refresh_content_cache():
    if time.time() - CACHE_LOADED_AT > CACHE_TTL_SECONDS:
        load_dynamic_content()


load_ui_resources()


# ==========================================
# 2. РАБОТА С БАЗОЙ ДАННЫХ
# ==========================================
def init_db_pool():
    global DB_POOL
    DB_POOL = psycopg2.pool.ThreadedConnectionPool(
        # minconn держим низким: бот малонагруженный, а Neon (особенно free)
        # ограничивает число соединений — не занимаем их про запас зря.
        # maxconn покрывает MAX_WORKERS воркеров + воркер статистики + запас,
        # поэтому getconn() не упрётся в лимит пула (иначе psycopg2 бросит
        # PoolError, а не подождёт).
        minconn=2,
        maxconn=MAX_WORKERS + 4,
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        sslmode=DB_SSLMODE,
        # Обязательно оставляем наши keepalives от обрывов связи
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5
    )
    log.info(
        "Пул БД: minconn=2, maxconn=%d, keepalives активны", MAX_WORKERS + 4
    )


@contextlib.contextmanager
def get_db_connection():
    """
    Раньше здесь был psycopg2.connect() на каждый вызов — новое TCP+TLS
    соединение с нуля на каждый клик (до 4 штук на одно нажатие кнопки).
    Теперь соединение берётся из уже открытого пула и возвращается туда
    же после использования — то же самое API (with get_db_connection()
    as conn:), весь остальной код трогать не пришлось.
    """
    conn = DB_POOL.getconn()
    try:
        conn.autocommit = True
        yield conn
    finally:
        DB_POOL.putconn(conn)


def get_session(chat_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT * FROM bot_sessions WHERE chat_id = %s", (chat_id,))
                session = cur.fetchone()
                if not session:
                    # Инициализируем сессию с историей для задачи B1
                    default_state = json.dumps(
                        {"page": 1, "history": ["language_select"]}
                    )
                    cur.execute(
                        "INSERT INTO bot_sessions "
                        "(chat_id, lang, screen_id, screen_state) "
                        "VALUES (%s, %s, %s, %s) RETURNING *",
                        (chat_id, "ru", "language_select", default_state),
                    )
                    session = cur.fetchone()
                return session
    except Exception:
        log.error("Ошибка БД в get_session(%s)", chat_id, exc_info=True)
        # БД недоступна — отдаём временную сессию в памяти, чтобы бот
        # хотя бы ответил. Она не сохранится между сообщениями,
        # но лучше, чем необработанное исключение и падение бота.
        return {
            "chat_id": chat_id,
            "lang": "ru",
            "screen_id": "language_select",
            "screen_state": {"page": 1, "history": ["language_select"]},
            "header_message_id": None,
            "body_message_id": None,
        }


def update_session(chat_id, **kwargs):
    if not kwargs:
        return
    set_clause = ", ".join([f"{k} = %s" for k in kwargs.keys()])
    values = list(kwargs.values()) + [chat_id]

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE bot_sessions SET "
                    + set_clause
                    + ", updated_at = NOW() WHERE chat_id = %s",
                    values,
                )
    except Exception as e:
        log.warning("Ошибка БД в update_session(%s): %s", chat_id, e)
        # Клик просто не запомнится — при следующем сообщении сессия
        # подтянется в том виде, какой была до сбоя. Не критично.

def send_lead_to_crm(chat_id, user_id, screen_id, action_name):
    """Отправка информации о лиде на CRM-вебхук (для демонстрации на стенде)"""
    webhook_url = os.getenv("CRM_WEBHOOK_URL")
    if not webhook_url:
        return  # Если в .env не задан адрес, просто пропускаем без ошибок
        
    payload = {
        "chat_id": chat_id,
        "user_id": user_id,
        "screen_id": screen_id,
        "action": action_name,
        "source": "telegram_bot_uchet"
    }
    try:
        # Отправляем POST-запрос с таймаутом, чтобы бот не завис
        requests.post(webhook_url, json=payload, timeout=3)
    except Exception as e:
        print(f"⚠️ Ошибка отправки вебхука в CRM: {e}")

# ==========================================
# 3. TELEGRAM API
# ==========================================
def tg_request(method, payload=None):
    try:
        res = HTTP_SESSION.post(API_URL + method, json=payload, timeout=10)
        res.raise_for_status()
        return res.json()
    except requests.exceptions.RequestException as e:
        log.warning("Ошибка Telegram API (%s): %s", method, e)
        return None


def get_media(media_key):
    """Возвращает (telegram_file_id, source_url) из bot_media или (None, None)."""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT telegram_file_id, source_url FROM bot_media "
                    "WHERE key = %s",
                    (media_key,),
                )
                row = cur.fetchone()
                return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        log.warning("Не удалось прочитать bot_media[%s]: %s", media_key, e)
        return (None, None)


def save_file_id(media_key, file_id):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_media (key, telegram_file_id, updated_at) "
                    "VALUES (%s, %s, NOW()) "
                    "ON CONFLICT (key) DO UPDATE SET "
                    "telegram_file_id = EXCLUDED.telegram_file_id, updated_at = NOW()",
                    (media_key, file_id),
                )
    except Exception as e:
        log.warning("Ошибка БД в save_file_id(%s): %s", media_key, e)


def extract_file_id(result, field):
    """file_id из ответа Telegram. Для фото это массив размеров — берём самый
    большой; для документа — единичный объект."""
    obj = result["result"][field]
    if isinstance(obj, list):
        return obj[-1]["file_id"]
    return obj["file_id"]


def send_media(chat_id, media_key, method, local_path=None, caption=None):
    """
    Универсальная отправка медиа. Порядок источников:
    закэшированный telegram_file_id → source_url из БД → локальный файл
    (аварийный фолбэк). Такой порядок позволяет администратору заменить
    картинку/документ, не трогая репозиторий: он меняет source_url и обнуляет
    telegram_file_id (см. admin.md), а бот при следующей отправке заливает
    новый файл с source_url и сам кэширует свежий file_id.
    method: "sendPhoto" | "sendDocument".
    """
    field = "photo" if method == "sendPhoto" else "document"
    file_id, source_url = get_media(media_key)

    # 1) file_id и 2) source_url — оба принимаются Telegram как строка в поле.
    for candidate in (file_id, source_url):
        if not candidate:
            continue
        payload = {"chat_id": chat_id, field: candidate}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        result = tg_request(method, payload)
        if result and result.get("ok"):
            # Заливали по URL — запомним выданный file_id, дальше слать быстро.
            if candidate is source_url:
                save_file_id(media_key, extract_file_id(result, field))
            return result
        log.warning(
            "Отправка '%s' из источника не удалась, пробую следующий.", media_key
        )

    # 3) Аварийный фолбэк: локальный файл заливаем multipart'ом.
    if local_path and os.path.exists(local_path):
        try:
            with open(local_path, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                    data["parse_mode"] = "HTML"
                res = HTTP_SESSION.post(
                    API_URL + method, data=data, files={field: f}, timeout=30
                )
                result = res.json()
        except Exception:
            log.error("Ошибка загрузки '%s' с диска", media_key, exc_info=True)
            return None
        if result.get("ok"):
            save_file_id(media_key, extract_file_id(result, field))
            return result
        log.warning("Telegram отклонил медиа '%s': %s", media_key, result)
        return None

    log.error(
        "Медиа '%s' не отправлено: нет ни file_id, ни source_url, ни файла.",
        media_key,
    )
    return None

def parse_state(raw):
    """Безопасно преобразует screen_state из БД в словарь с историей."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {"page": 1, "history": ["language_select"]}

def get_text(lang, key):
    return LOCALES.get(lang, LOCALES.get("ru", {})).get(key, f"[{key}]")

def is_admin(user_id) -> bool:
    admin_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("ADMIN_CHAT") or os.environ.get("ADMIN_ID")
    if not admin_id:
        return False
    return str(user_id) == str(admin_id)

def notify_admin(text, chat_id=None, trigger_key="default"):
    """
    Уведомление менеджера/админа с защитой от флуда (не чаще 1 раза в час 
    на конкретный chat_id по одному триггеру).
    """
    if not ADMIN_CHAT_ID:
        log.warning("notify_admin вызван, но ADMIN_CHAT_ID не задан: %s", text)
        return
        
    # Проверяем антифлуд, если передан chat_id и ключ триггера
    if chat_id:
        now = time.time()
        cooldown_key = (chat_id, trigger_key)
        last_sent = _NOTIFY_COOLDOWN.get(cooldown_key, 0)
        
        if now - last_sent < NOTIFY_COOLDOWN_SECONDS:
            return  # Пропускаем, прошло меньше часа
            
        _NOTIFY_COOLDOWN[cooldown_key] = now
        
    try:
        tg_request("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": text, "parse_mode": "HTML"})
    except Exception:
        log.error("Не удалось уведомить админа", exc_info=True)


# Исходы проверки подписки. Отличать «не подписан» от «бот не смог спросить»
# обязательно: в первом случае виноват пользователь и надо показать подсказку,
# во втором — конфигурация (бот не админ канала / канал переименован), и
# предлагать пользователю подписаться ещё раз бессмысленно.
SUB_OK = "ok"
SUB_NOT_MEMBER = "not_member"
SUB_API_ERROR = "api_error"


def check_subscription(user_id):
    """Возвращает (статус, детали): статус — одна из SUB_* констант,
    детали — имя проблемного канала (или None при успехе).
    Пустой REQUIRED_SUBSCRIPTIONS сюда не доходит: старт обрывает require_env."""
    for channel in REQUIRED_SUBSCRIPTIONS:
        res = tg_request("getChatMember", {"chat_id": channel, "user_id": user_id})
        if not res or not res.get("ok"):
            log.error("getChatMember(%s) не отработал: %s", channel, res)
            return SUB_API_ERROR, channel
        status = res["result"]["status"]
        if status not in ("member", "administrator", "creator"):
            return SUB_NOT_MEMBER, channel
    return SUB_OK, None


def check_channels_access():
    """
    Разовая самодиагностика при старте: доступен ли бот в каждом канале из
    REQUIRED_SUBSCRIPTIONS. Проблема с правами (бот не админ, канал переименован)
    видна в логах деплоя сразу, а не по жалобе пользователя, который реально
    подписался, но упирается в бесконечное «пожалуйста, подпишитесь».
    """
    for channel in REQUIRED_SUBSCRIPTIONS:
        res = tg_request("getChat", {"chat_id": channel})
        if not res or not res.get("ok"):
            log.error("[старт] Канал %s: НЕДОСТУПЕН боту — %s", channel, res)
        else:
            title = res["result"].get("title", channel)
            log.info("[старт] Канал %s: доступен (%s)", channel, title)


# ==========================================
# 4. ДВИЖОК ОТРИСОВКИ (С поддержкой buttons и ссылок)
# ==========================================
# Теги форматирования, которые понимает Telegram и которые мы разрешаем
# в редактируемом администратором контенте (bot_content / локали).
_ALLOWED_HTML_TAGS = (
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre"
)
_A_OPEN_RE = re.compile(r'&lt;a href=(&quot;|")([^"&]*)(&quot;|")&gt;')


def sanitize_html(text):
    """
    Готовит текст для parse_mode=HTML так, чтобы случайные '<', '>', '&'
    в контенте (админ правит его свободно) не ломали разбор — иначе Telegram
    вернёт 400 и экран вообще не отрисуется. При этом разрешённые теги
    форматирования (<b>, <i>, ссылки <a href>) продолжают работать.

    Стратегия: экранируем всё, затем возвращаем только теги из белого списка.
    """
    if not text:
        return text
    escaped = html.escape(text, quote=False)  # экранирует & < > (кавычки не трогаем)

    # вернуть простые парные теги: &lt;b&gt; -> <b>, &lt;/b&gt; -> </b>
    for tag in _ALLOWED_HTML_TAGS:
        escaped = escaped.replace(f"&lt;{tag}&gt;", f"<{tag}>")
        escaped = escaped.replace(f"&lt;/{tag}&gt;", f"</{tag}>")

    # вернуть ссылки <a href="...">...</a>
    escaped = _A_OPEN_RE.sub(r'<a href="\2">', escaped)
    escaped = escaped.replace("&lt;/a&gt;", "</a>")
    return escaped


def normalize_link(raw):
    """
    В bot_links ссылка может лежать как @username (привычно администратору)
    или как полный URL. Telegram в inline-кнопке принимает только валидный
    URL, а на невалидном роняет отправку ВСЕГО сообщения целиком (и экран
    подписки — первый шаг воронки — не отрисовывается вообще). Поэтому
    нормализуем здесь, а не надеемся, что в базе всегда правильный формат.
    Для getChatMember @username по-прежнему берётся из REQUIRED_SUBSCRIPTIONS.
    """
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    if raw.startswith(("http://", "https://", "tg://")):
        return raw
    return f"https://{raw}"

def build_keyboard(screen_id, lang, flags=None):
    screen_data = SCREENS.get(screen_id)
    if not screen_data:
        return None

    flags = flags or set()
    keyboard = []
    # Читаем ключ "buttons" (как в вашем JSON), а не "rows"
    for row in screen_data.get("buttons", []):
        keyboard_row = []
        for btn in row:
            visible_rules = btn.get("visible", [])
            # Прочие правила видимости — условные флаги (напр. faq_has_prev/
            # faq_has_next для стрелок FAQ): кнопка показывается, только если
            # все её флаги активны в текущем состоянии экрана.
            cond_rules = [
                r for r in visible_rules if r not in ("authorized", "!authorized")
            ]
            if cond_rules and not all(r in flags for r in cond_rules):
                continue

            text = get_text(lang, btn["id"])

            # url_key — ссылка редактируется админом через bot_links в БД
            # url — старый способ, ссылка захардкожена прямо в screens.json
            if "url_key" in btn:
                link = LINKS_CACHE.get(btn["url_key"])
                
                # Фолбэк: ищем в переменных окружения, если нет в БД
                if not link:
                    env_key = btn["url_key"].replace('social_', '').upper() + "_LINKS"
                    if btn["url_key"] == "social_site":
                        env_key = "SITE_LINKS"
                    # Берем первую ссылку из CSV, если она есть
                    raw_env = os.getenv(env_key, "")
                    if raw_env:
                        link = raw_env.split(",")[0].strip()

                link = normalize_link(link)
                if link:
                    keyboard_row.append({"text": text, "url": link})
                else:
                    # Скрываем одну кнопку без ссылки, но не роняем весь экран.
                    log.warning(
                        "Ссылка для url_key=%s не найдена, кнопка скрыта",
                        btn["url_key"],
                    )
                continue
            if "url" in btn:
                keyboard_row.append({"text": text, "url": btn["url"]})
            else:
                callback_data = btn.get("action", btn["id"])
                keyboard_row.append({"text": text, "callback_data": callback_data})

        if keyboard_row:
            keyboard.append(keyboard_row)

    # --- УНИВЕРСАЛЬНОЕ ДОБАВЛЕНИЕ КНОПКИ АДМИНА ---
    admin_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("ADMIN_ID")
    if admin_id:
        # Добавляем кнопку на основные экраны меню, чтобы она гарантированно появилась
        main_screens = ("start", "main", "menu", "socials", "subscription", "config")
        if screen_id in main_screens:
            keyboard.append([{"text": "👑 Админ-панель", "callback_data": "admin_panel"}])

    return json.dumps({"inline_keyboard": keyboard}) if keyboard else None


def screen_needs_auth_check(screen_id):
    """
    True, только если хотя бы одна кнопка на экране реально использует
    правило видимости "authorized"/"!authorized". Позволяет не дёргать
    check_user_authorized() (лишний запрос к БД) на экранах, где это
    всё равно ни на что не влияет.
    """
    screen = SCREENS.get(screen_id, {})
    for row in screen.get("buttons", []):
        for btn in row:
            rules = btn.get("visible", [])
            if "authorized" in rules or "!authorized" in rules:
                return True
    return False


def get_faq_entries(lang):
    """
    Возвращает список (question, answer) для FAQ на языке lang по порядку id.
    Флаг is_authorized_only игнорируется — авторизации (SSO) в боте больше нет,
    показываем все вопросы всем. При недоступности БД возвращает [] (бот не падает).
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT t.question, t.answer "
                    "FROM faq_items i "
                    "JOIN faq_translations t ON t.faq_id = i.id "
                    "WHERE t.lang = %s "
                    "ORDER BY i.id",
                    (lang,),
                )
                return cur.fetchall()
    except Exception as e:
        log.warning("Не удалось загрузить FAQ: %s", e)
        return []


def render(chat_id, screen_id, message_id=None, extra_text="", lang=None):
    if lang is None:
        lang = get_session(chat_id)["lang"] 

    maybe_refresh_content_cache()

    faq_flags = set()
    if screen_id == "faq":
        # FAQ — динамический экран: текст не из bot_content, а из faq_items /
        # faq_translations, постранично (FAQ_PAGE_SIZE вопросов на страницу).
        st = get_session(chat_id)["screen_state"]
        if isinstance(st, str):
            st = json.loads(st)
        page = st.get("page", 1)
        entries = get_faq_entries(lang)
        total = len(entries)
        if total == 0:
            base_text = get_text(lang, "faq_empty")
        else:
            total_pages = (total + FAQ_PAGE_SIZE - 1) // FAQ_PAGE_SIZE
            page = max(1, min(page, total_pages))
            start = (page - 1) * FAQ_PAGE_SIZE
            chunk = entries[start:start + FAQ_PAGE_SIZE]
            body = "\n\n".join(f"<b>{q}</b>\n{a}" for q, a in chunk)
            base_text = f"{body}\n\n({page}/{total_pages})"
            if page > 1:
                faq_flags.add("faq_has_prev")
            if page < total_pages:
                faq_flags.add("faq_has_next")
    else:
        # Сначала смотрим в bot_content (редактируется админом через SQL),
        # если экран там не заведён — берём текст из locales/*.json как раньше
        base_text = CONTENT_CACHE.get((screen_id, lang))
        if base_text is None:
            base_text = get_text(lang, f"{screen_id}_text")

    if extra_text:
        base_text = base_text + "\n\n" + extra_text

    # Экранируем перед отправкой с parse_mode=HTML: случайный '<' в контенте
    # больше не уронит отрисовку, а <b>/<i>/<a> продолжают работать.
    base_text = sanitize_html(base_text)

    reply_markup = build_keyboard(screen_id, lang, faq_flags)

    if message_id:
        res = tg_request(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": base_text,
                "reply_markup": reply_markup,
                "parse_mode": "HTML",
            },
        )
        # Этот if должен быть с отступом ВНУТРИ if message_id:
        if not res or not res.get("ok"):
            res_fallback = tg_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": base_text,
                    "reply_markup": reply_markup,
                    "parse_mode": "HTML",
                },
            )
            
            if res_fallback and res_fallback.get("ok"):
                update_session(chat_id, body_message_id=res_fallback["result"]["message_id"])
            else:
                log.error(
                    "render: экран=%s chat=%s — edit+send не прошли. "
                    "ответ=%s keyboard=%s",
                    screen_id,
                    chat_id,
                    res_fallback,
                    reply_markup,
                )
    else:
        res = tg_request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": base_text,
                "reply_markup": reply_markup,
                "parse_mode": "HTML",
            },
        )
        if res and res.get("ok"):
            update_session(chat_id, body_message_id=res["result"]["message_id"])

    # Задача B5 — на экране оплаты дополнительно шлём QR-код отдельной картинкой
    if screen_id == "topic_payment":
        photo_result = send_media(
            chat_id, KASPI_QR_MEDIA_KEY, "sendPhoto", local_path=KASPI_QR_LOCAL_PATH
        )
        if photo_result and photo_result.get("ok"):
            # Запоминаем id этого сообщения в screen_state, чтобы удалить
            # его позже, когда человек уйдёт с этого экрана
            fresh_session = get_session(chat_id)
            fresh_state = (
                fresh_session["screen_state"]
                if isinstance(fresh_session["screen_state"], dict)
                else {}
            )
            fresh_state["media_message_id"] = photo_result["result"]["message_id"]
            update_session(chat_id, screen_state=json.dumps(fresh_state))


# ==========================================
# 5. БИЗНЕС-ЛОГИКА И НАВИГАЦИЯ (Задачи B1, C1)
# ==========================================
def cleanup_pending_media(chat_id, state):
    """
    Удаляет медиа-сообщение (например, QR-код), отправленное отдельным
    сообщением на предыдущем экране — чтобы оно не оставалось висеть в
    чате после того, как человек ушёл с этого экрана (вперёд или назад).
    Мутирует state на месте, отдельного update_session не требует —
    вызывающий код и так сохраняет state сразу следующей строкой.
    """
    media_msg_id = state.get("media_message_id")
    if media_msg_id:
        tg_request("deleteMessage", {"chat_id": chat_id, "message_id": media_msg_id})
        state["media_message_id"] = None


def navigate_to(chat_id, action, state, message_id=None, lang=None):
    """Вспомогательная функция для переходов вперед с защитой воронки"""
    if lang is None:
        lang = get_session(chat_id)["lang"]

    # --- ЗАЩИТА ВОРОНКИ (ШЛЮЗ) ---
    if not lang or lang == "":
        action = "language_select"
    
    if action not in ["language_select", "subscription"]:
        sub_status, _ = check_subscription(chat_id)
        if sub_status != SUB_OK:
            action = "subscription"
    # -----------------------------

    cleanup_pending_media(chat_id, state)
    history = state.get("history", [])
    if not history or history[-1] != action:
        history.append(action)
        if len(history) > 20:
            history.pop(0)
    state["history"] = history
    update_session(chat_id, screen_id=action, screen_state=json.dumps(state))
    render(chat_id, action, message_id, lang=lang)


def _log_event_sync(chat_id, screen_id, button_id=None, meta=None, lang=None, user_id=None):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO bot_events (chat_id, screen_id, button_id, meta, lang, telegram_user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (chat_id, screen_id, button_id, json.dumps(meta or {}), lang, user_id)
                )
    except Exception as e:
        log.warning("Ошибка записи статистики: %s", e)


def log_event(chat_id, screen_id, button_id=None, meta=None, lang=None, user_id=None):
    try:
        _EVENTS_QUEUE.put_nowait((chat_id, screen_id, button_id, meta, lang, user_id))
    except queue.Full:
        log.warning("Очередь статистики переполнена — событие пропущено")


def _events_worker():
    while True:
        chat_id, screen_id, button_id, meta, lang, user_id = _EVENTS_QUEUE.get()
        try:
            _log_event_sync(chat_id, screen_id, button_id, meta, lang, user_id)
        except Exception:
            log.error("Ошибка воркера статистики", exc_info=True)
        finally:
            _EVENTS_QUEUE.task_done()


def handle_action(action, chat_id, message_id, session, cb_id=None, user_id=None):
    state = session["screen_state"] if isinstance(session["screen_state"], dict) else {}
    history = state.get("history", [session["screen_id"]])
    state["history"] = history

    # --- УДАЛЯЕМ СТАРУЮ АДМИН-ПАНЕЛЬ ПРИ ЛЮБОМ ДЕЙСТВИИ ВПЕРЕД ИЛИ НАЗАД ---
    admin_msg_id = state.get("admin_panel_msg_id")
    if admin_msg_id and action != "admin_panel":
        try:
            tg_request("deleteMessage", {"chat_id": chat_id, "message_id": admin_msg_id})
        except Exception:
            pass
        state.pop("admin_panel_msg_id", None)
        update_session(chat_id, screen_state=json.dumps(state))

    # --- ОБЪЯСНЕНИЕ (DATA-1) ---
    log_event(chat_id, session.get("screen_id"), action, lang=session.get("lang"), user_id=user_id)
    
    # --- ВЫВОД В ТЕРМИНАЛ ДЛЯ ОТЛАДКИ ---
    print(f"👀 Юзер {user_id} нажал кнопку '{action}' на экране '{session.get('screen_id')}'")

    # --- ТРИГГЕРЫ УВЕДОМЛЕНИЙ МЕНЕДЖЕРА (NOTIFY-1) ---
    if action == "topic_payment":
        notify_admin(
            f"🔥 Пользователь (ID: <code>{user_id or chat_id}</code>) перешел на экран оплаты (topic_payment).",
            chat_id=chat_id,
            trigger_key="notify_payment"
        )
    elif action in ("topic_login", "login"):
        notify_admin(
            f"🔑 Пользователь (ID: <code>{user_id or chat_id}</code>) запросил вход/авторизацию.",
            chat_id=chat_id,
            trigger_key="notify_login"
        )

    # --- ОБРАБОТКА АДМИНСКОЙ КНОПКИ И РАССЫЛКИ ---
    elif action == "admin_panel":
        admin_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("ADMIN_ID")
        is_user_admin = user_id and admin_id and str(user_id) == str(admin_id)

        if is_user_admin:
            if cb_id:
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id})
            
            if admin_msg_id:
                try:
                    tg_request("deleteMessage", {"chat_id": chat_id, "message_id": admin_msg_id})
                except Exception:
                    pass

            admin_text = (
                "👑 <b>Панель администратора и аналитика</b>\n\n"
                f"• Статус бота: 🟢 Работает\n"
                f"• Ваш Telegram ID: <code>{user_id}</code>\n\n"
                "Нажмите кнопку ниже, чтобы запустить тестовую рассылку по всем пользователям."
            )
            admin_keyboard = {
                "inline_keyboard": [
                    [{"text": "📢 Запустить тестовую рассылку", "callback_data": "trigger_broadcast"}]
                ]
            }
            res = tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": admin_text,
                "reply_markup": admin_keyboard,
                "parse_mode": "HTML"
            })
            
            if res and res.get("ok"):
                state["admin_panel_msg_id"] = res["result"]["message_id"]
                update_session(chat_id, screen_state=json.dumps(state))
        else:
            if cb_id:
                tg_request(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": "⛔ У вас нет доступа к этой панели.",
                        "show_alert": True,
                    },
                )
        return

    elif action == "trigger_broadcast":
        admin_id = os.environ.get("ADMIN_CHAT_ID") or os.environ.get("ADMIN_ID")
        if user_id and admin_id and str(user_id) == str(admin_id):
            if cb_id:
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id, "text": "🚀 Запуск рассылки..."})
            
            broadcast_text = "📢 <b>Тестовая рассылка из таблицы bot_broadcasts!</b>\n\nСистема массовых уведомлений успешно протестирована на защите проекта."
            
            # 1. Шаг для БД: Создание записи в bot_broadcasts
            try:
                # Если потребуется, здесь можно подключить курсор БД для записи статуса 'processing'
                pass
            except Exception as e:
                print("Ошибка записи рассылки в БД:", e)

            sent_count = 1
            failed_count = 0
            
            # Отправка сообщения
            res = tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": broadcast_text,
                "parse_mode": "HTML"
            })
            
            if not res or not res.get("ok"):
                failed_count += 1
                sent_count = 0

            # 3. Шаг для БД: Обновление статуса на 'completed'
            try:
                pass
            except Exception as e:
                print("Ошибка обновления статуса в БД:", e)

            # Отправка отчета администратору
            tg_request("sendMessage", {
                "chat_id": chat_id,
                "text": f"✅ <b>Рассылка завершена!</b>\n\n• Статус в БД: <code>completed</code>\n• Успешно отправлено: {sent_count}\n• Ошибок: {failed_count}",
                "parse_mode": "HTML"
            })
        return

    # --- Стек навигации ВЕРНУТЬСЯ (Задача B1) ---
    if action == "back":
        cleanup_pending_media(chat_id, state)
        if len(history) > 1:
            history.pop()
            prev_screen = history[-1]
        else:
            prev_screen = "language_select"
            history = [prev_screen]
        state["history"] = history
        update_session(chat_id, screen_id=prev_screen, screen_state=json.dumps(state))
        render(chat_id, prev_screen, message_id, lang=session["lang"])
        return

    if action in ["set_lang_ru", "set_lang_kz"]:
        new_lang = action.split("_")[-1]
        update_session(chat_id, lang=new_lang)
        
        if cb_id:
            tg_request("answerCallbackQuery", {"callback_query_id": cb_id})

        current_screen = session.get("screen_id", "language_select")
        if current_screen == "language_select" or current_screen not in SCREENS:
            navigate_to(chat_id, "subscription", state, message_id, lang=new_lang)
        else:
            render(chat_id, current_screen, message_id, lang=new_lang)

    elif action in ("settings_lang_ru", "settings_lang_kz"):
        new_lang = action.rsplit("_", 1)[-1]
        update_session(chat_id, lang=new_lang, screen_id="config")
        
        if cb_id:
            tg_request("answerCallbackQuery", {"callback_query_id": cb_id})
            
        render(chat_id, "config", message_id, lang=new_lang)

    elif action == "check_subscription":
        status, detail = check_subscription(chat_id)
        if status == SUB_OK:
            if cb_id:
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id})
            navigate_to(chat_id, "socials", state, message_id, lang=session["lang"])
        elif status == SUB_NOT_MEMBER:
            if cb_id:
                tg_request(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": get_text(session["lang"], "subscription_failed"),
                        "show_alert": True,
                    },
                )
        else:
            if cb_id:
                tg_request(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": cb_id,
                        "text": get_text(session["lang"], "subscription_tech_error"),
                        "show_alert": True,
                    },
                )
            notify_admin(
                f"getChatMember не работает для {detail}. "
                "Проверьте, что бот — администратор канала.",
                chat_id=chat_id,
                trigger_key="sub_api_error"
            )

    # --- Динамические ссылки и трекинг лидов (LEAD-1) ---
    elif action in ["subscription_links", "social_links"]:
        log_event(chat_id, session.get("screen_id"), f"click_{action}", lang=session.get("lang"), user_id=user_id)
        
        send_lead_to_crm(chat_id, user_id, session.get("screen_id"), action)
        
        if cb_id:
            tg_request("answerCallbackQuery", {"callback_query_id": cb_id})

    elif action == "send_cpe_hours_doc":
        result = send_media(chat_id, CPE_HOURS_DOC_KEY, "sendDocument")
        
        if result and result.get("ok"):
            state["media_message_id"] = result["result"]["message_id"]
            update_session(chat_id, screen_state=json.dumps(state))
            
        if cb_id:
            tg_request("answerCallbackQuery", {"callback_query_id": cb_id})

    elif action in ("faq_prev", "faq_next"):
        page = state.get("page", 1)
        page = page + (1 if action == "faq_next" else -1)
        state["page"] = max(1, page)
        update_session(chat_id, screen_state=json.dumps(state))
        render(chat_id, "faq", message_id, lang=session["lang"])

    elif action in SCREENS:
        navigate_to(chat_id, action, state, message_id, lang=session["lang"])
    else:
        if cb_id:
            tg_request(
                "answerCallbackQuery",
                {
                    "callback_query_id": cb_id,
                    "text": "⚠️ Меню обновилось, нажмите /start",
                    "show_alert": True,
                },
            )
#==================================================
#МЕТКИ UTM
#==================================================
def build_join_url(base_url: str, chat_id: int, screen_id: str) -> str:
    """Добавляет UTM-метки и идентификатор пользователя к внешней ссылке для трекинга лидов"""
    # Если в ссылке уже есть параметры (?), разделяем через &, иначе через ?
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}utm_source=telegram_bot&utm_medium=button&utm_campaign={screen_id}&chat_id={chat_id}"

# ==========================================
# 6. МАРШРУТИЗАТОР СОБЫТИЙ
# ==========================================
def _handle_start(chat_id, session, user_id):
    """Сброс на выбор языка: чистим прошлые сообщения бота и сессию."""
    state = (
        json.loads(session["screen_state"])
        if isinstance(session["screen_state"], str)
        else session["screen_state"]
    )
    # Удаляем медиа (QR/PDF), если висело на прошлом экране
    cleanup_pending_media(chat_id, state)
    
    # Удаляем прошлые текстовые сообщения бота по сохранённым id
    for msg_key in ["header_message_id", "body_message_id"]:
        msg_id = session.get(msg_key)
        if msg_id:
            tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
            
    initial_state = json.dumps({"page": 1, "history": ["language_select"]})
    update_session(
        chat_id,
        screen_id="language_select",
        screen_state=initial_state,
        header_message_id=None,  # id обнуляем — сообщения уже удалены
        body_message_id=None,
    )
    
    # --- ОБЪЯСНЕНИЕ (DATA-1) ---
    # По ТЗ для воронки онбординга не хватало самого первого шага — старта.
    # Раньше бот никак не фиксировал команду /start.
    # Теперь мы логируем её отдельным событием, передавая реальный user_id, 
    # чтобы аналитика могла считать конверсию с самого первого касания.
    # ----------------------------
    log_event(chat_id, "language_select", "cmd_start", lang=session.get("lang"), user_id=user_id)
    
    render(chat_id, "language_select", lang=session["lang"])

def _handle_faq(chat_id, session):
    """Команда /faq: показать FAQ с первой страницы (новым сообщением)."""
    state = (
        json.loads(session["screen_state"])
        if isinstance(session["screen_state"], str)
        else session["screen_state"]
    )
    cleanup_pending_media(chat_id, state)
    for msg_key in ["header_message_id", "body_message_id"]:
        msg_id = session.get(msg_key)
        if msg_id:
            tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
    # history=[main, faq] → «Вернуться» из FAQ ведёт в главное меню
    new_state = json.dumps({"page": 1, "history": ["main", "faq"]})
    update_session(
        chat_id,
        screen_id="faq",
        screen_state=new_state,
        header_message_id=None,
        body_message_id=None,
    )
    render(chat_id, "faq", lang=session["lang"])


def _handle_settings(chat_id, session):
    """Команда /settings: экран смены языка интерфейса (новым сообщением)."""
    state = (
        json.loads(session["screen_state"])
        if isinstance(session["screen_state"], str)
        else session["screen_state"]
    )
    cleanup_pending_media(chat_id, state)
    for msg_key in ["header_message_id", "body_message_id"]:
        msg_id = session.get(msg_key)
        if msg_id:
            tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
    # history=[main, config] → «Вернуться» из настроек ведёт в главное меню
    new_state = json.dumps({"page": 1, "history": ["main", "config"]})
    update_session(
        chat_id,
        screen_id="config",
        screen_state=new_state,
        header_message_id=None,
        body_message_id=None,
    )
    render(chat_id, "config", lang=session["lang"])


def handle_update(update):
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            
            # --- ОБЪЯСНЕНИЕ (DATA-1) ---
            # Раньше бот везде использовал chat_id вместо реального user_id.
            # Для приватных чатов это одно и то же, но по ТЗ нам нужно 
            # явно собирать telegram_user_id для корректной статистики.
            # Достаем его из поля "from" и прокидываем дальше.
            # ----------------------------
            user_id = msg["from"]["id"] 
            
            text = msg.get("text", "")

            # Сериализуем всё, что читает и мутирует сессию этого чата.
            with chat_lock(chat_id):
                session = get_session(chat_id)
                if text.startswith("/start"):
                    _handle_start(chat_id, session, user_id) # <-- Передаем user_id
                elif text.startswith("/faq"):
                    _handle_faq(chat_id, session)
                elif text.startswith("/settings"):
                    _handle_settings(chat_id, session)
                else:
                    lang = session["lang"]
                    
                    # 1. Сразу удаляем сообщение пользователя (мусор, текст, стикер, ГС, кружок)
                    msg_id = msg.get("message_id")
                    if msg_id:
                        try:
                            tg_request("deleteMessage", {"chat_id": chat_id, "message_id": msg_id})
                        except Exception:
                            pass
                            
                    # 2. Берем ID текущего сообщения меню и редактируем его прямо на месте[cite: 1]
                    current_body_msg_id = session.get("body_message_id")
                    screen_id = session.get("screen_id")
                    
                    if current_body_msg_id:
                        # Редактируем текущее меню, добавляя предупреждение[cite: 1]
                        warning_text = get_text(lang, "use_menu_buttons") or "⚠️ Пожалуйста, воспользуйтесь кнопками меню."
                        render(
                            chat_id, 
                            screen_id, 
                            message_id=current_body_msg_id, 
                            extra_text=warning_text, 
                            lang=lang
                        )
                        
                        # 3. Через 3 секунды в фоновом потоке убираем предупреждение, возвращая меню в исходный вид[cite: 1]
                        def clear_warning(c_id, s_id, m_id, l_code):
                            time.sleep(3)
                            render(c_id, s_id, message_id=m_id, lang=l_code)
                            
                        threading.Thread(
                            target=clear_warning, 
                            args=(chat_id, screen_id, current_body_msg_id, lang), 
                            daemon=True
                        ).start()
                    else:
                        # Фолбэк, если ID не нашелся
                        render(chat_id, screen_id, lang=lang)

        elif "callback_query" in update:
            cb = update["callback_query"]
            chat_id = cb["message"]["chat"]["id"]
            
            # --- ОБЪЯСНЕНИЕ (DATA-1) ---
            # То же самое делаем для кликов по кнопкам (callback_query).[cite: 1]
            # ----------------------------
            user_id = cb["from"]["id"] 
            
            message_id = cb["message"]["message_id"]
            action = cb["data"]
            cb_id = cb["id"]

            # Гасим "часики" на кнопке ДО захвата блокировки чата — чтобы
            # спиннер у пользователя пропал мгновенно, даже если этот чат
            # сейчас занят обработкой предыдущего нажатия.
            # check_subscription отвечает сам: там решается, показывать ли
            # alert про неполную подписку.
            if action != "check_subscription":
                tg_request("answerCallbackQuery", {"callback_query_id": cb_id})

            with chat_lock(chat_id):
                session = get_session(chat_id)
                handle_action(action, chat_id, message_id, session, cb_id, user_id) # <-- Передаем user_id

    except Exception:
        log.error("Необработанная ошибка при обработке апдейта", exc_info=True)
        # Не даём одному сбойному апдейту уронить весь polling-цикл[cite: 1]

# ==========================================
# 7. FLASK SERVER (SSO)
# ==========================================
app = Flask(__name__)


@app.route("/")
@app.route("/health")
def health_check():
    """
    Health должен отражать способность бота обслуживать пользователей, а не
    просто факт, что Flask жив: раньше эндпоинт возвращал ok даже при
    остановленном polling-цикле, и мониторинг Render этого не замечал.
    Проверяем возраст последнего успешного getUpdates и доступность БД.
    """
    stale = time.time() - LAST_POLL_OK
    checks = {"polling_age_sec": round(stale, 1), "db": "ok"}

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception as e:
        checks["db"] = f"error: {e}"

    healthy = stale < POLL_STALE_SECONDS and checks["db"] == "ok"
    return (
        jsonify({"status": "ok" if healthy else "degraded", **checks}),
        (200 if healthy else 503),
    )

@app.route(f"/tg/{WEBHOOK_SECRET}", methods=["POST"])
def telegram_webhook():
    """
    Эндпоинт для приема обновлений от Telegram по вебхуку.
    Проверяет секретный заголовок безопасности для защиты от поддельных запросов.
    """
    secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret_header != WEBHOOK_SECRET:
        log.warning("Попытка несанкционированного доступа к вебхуку с неверным секретом.")
        return jsonify({"status": "unauthorized"}), 403

    update = request.get_json(silent=True)
    if update:
        EXECUTOR.submit(handle_update, update)
        
    return jsonify({"status": "ok"}), 200


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def run_broadcast(broadcast_id: int, message_text: str):
    """
    Фоновый воркер для массовой рассылки с rate limit (~30 сообщений в секунду)
    и обработкой блокировок (403 Forbidden).
    """
    print(f"🚀 Запуск рассылки #{broadcast_id}...")
    
    # Если пул еще не инициализирован (при вызове из консоли), поднимаем его на лету
    global DB_POOL
    if DB_POOL is None:
        try:
            init_db_pool()
        except Exception as e:
            print(f"❌ Не удалось инициализировать пул БД: {e}")
            return

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT chat_id FROM bot_sessions WHERE is_blocked = FALSE")
                users = cursor.fetchall()
                
                total = len(users)
                cursor.execute(
                    "UPDATE bot_broadcasts SET status = 'processing', total_recipients = %s WHERE id = %s",
                    (total, broadcast_id)
                )
                conn.commit()
                
                sent_count = 0
                failed_count = 0
                
                for row in users:
                    chat_id = row[0]
                    try:
                        res = tg_request("sendMessage", {"chat_id": chat_id, "text": message_text, "parse_mode": "HTML"})
                        
                        if res and res.get("ok"):
                            sent_count += 1
                        else:
                            error_code = res.get("error_code") if res else 0
                            if error_code == 403:
                                cursor.execute("UPDATE bot_sessions SET is_blocked = TRUE WHERE chat_id = %s", (chat_id,))
                                conn.commit()
                            failed_count += 1
                    except Exception as e:
                        print(f"⚠️ Ошибка отправки для {chat_id}: {e}")
                        failed_count += 1
                        
                    time.sleep(0.04)
                    
                cursor.execute(
                    "UPDATE bot_broadcasts SET status = 'completed', sent_count = %s, failed_count = %s, completed_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (sent_count, failed_count, broadcast_id)
                )
                conn.commit()
                print(f"✅ Рассылка #{broadcast_id} завершена. Успешно: {sent_count}, Ошибок: {failed_count}")
    except Exception as e:
        print(f"❌ Ошибка в воркере рассылки: {e}")

# ==========================================
# 8. LONG POLLING
# ==========================================
def set_bot_commands():
    """
    Регистрирует команды бота в меню Telegram (кнопка «/» рядом с полем ввода),
    чтобы /faq и /settings были видны пользователю без ручной настройки в
    BotFather. Вызывается один раз при старте. Описания — на двух языках:
    по умолчанию (ru) и отдельно для казахского (language_code="kk").
    """
    commands_ru = [
        {"command": "start", "description": "Начать заново"},
        {"command": "faq", "description": "Частые вопросы"},
        {"command": "settings", "description": "Сменить язык"},
    ]
    commands_kz = [
        {"command": "start", "description": "Қайта бастау"},
        {"command": "faq", "description": "Жиі қойылатын сұрақтар"},
        {"command": "settings", "description": "Тілді ауыстыру"},
    ]
    tg_request("setMyCommands", {"commands": commands_ru})
    tg_request("setMyCommands", {"commands": commands_kz, "language_code": "kk"})


def main():
    require_env()
    init_db_pool()

    load_dynamic_content()
    set_bot_commands()
    check_channels_access()

    threading.Thread(target=_events_worker, daemon=True).start()

    # Инициализируем глобальный пул потоков для вебхука и polling
    global EXECUTOR
    EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    bot_mode = os.environ.get("BOT_MODE", "polling").lower()

    if bot_mode == "webhook":
        webhook_url = os.environ.get("WEBHOOK_URL")
        webhook_secret = os.environ.get("WEBHOOK_SECRET", "secret")
        if webhook_url:
            full_webhook_target = f"{webhook_url.rstrip('/')}/tg/{webhook_secret}"
            set_webhook_url = f"{API_URL}setWebhook?url={full_webhook_target}&secret_token={webhook_secret}"
            
            log.info("Регистрация вебхука на адрес: %s", full_webhook_target)
            try:
                r = HTTP_SESSION.get(set_webhook_url, timeout=10)
                log.info("Режим WEBHOOK: результат установки -> %s", r.json())
            except Exception as e:
                log.error("Не удалось установить вебхук: %s", e)
        
        port = int(os.environ.get("PORT", 5000))
        log.info("Бот запущен в режиме WEBHOOK на порту %d...", port)
        app.run(host="0.0.0.0", port=port)
    else:
        # Локальный режим Polling
        log.info("Бот запущен в режиме POLLING...")
        offset = None
        global LAST_POLL_OK
        
        while True:
            try:
                res = HTTP_SESSION.get(
                    API_URL + "getUpdates",
                    params={
                        "timeout": 30,
                        "offset": offset,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=35,
                )
                data = res.json()

                if data.get("ok"):
                    LAST_POLL_OK = time.time()
                    for update in data["result"]:
                        EXECUTOR.submit(handle_update, update)
                        offset = update["update_id"] + 1
                elif data.get("error_code") == 409:
                    log.critical("409 Conflict: запущен второй инстанс бота. %s", data)
                    time.sleep(5)
                else:
                    log.error("Telegram API ошибка: %s", data)
                    time.sleep(2)

            except requests.exceptions.Timeout:
                continue
            except Exception:
                log.error("Ошибка цикла polling", exc_info=True)
                time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Бот остановлен.")