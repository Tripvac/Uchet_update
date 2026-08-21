-- 000_base_tables.sql
-- Базовые таблицы, существовавшие до нового сценария (ранее создавались
-- скриптом init_db.py). Вынесены в миграцию, чтобы вся схема поднималась
-- одной цепочкой миграций, без отдельного шага. Все определения —
-- CREATE TABLE IF NOT EXISTS, поэтому файл безопасно применять и на уже
-- существующей базе (например, на текущей Neon, где таблицы созданы руками).

CREATE TABLE IF NOT EXISTS bot_sessions (
    chat_id BIGINT PRIMARY KEY,
    lang VARCHAR(10) NOT NULL DEFAULT 'ru',
    screen_id VARCHAR(50) NOT NULL,
    screen_state JSONB DEFAULT '{}'::jsonb,
    header_message_id BIGINT,
    body_message_id BIGINT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS telegram_users (
    telegram_user_id BIGINT PRIMARY KEY,
    user_id VARCHAR(100),
    is_active BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS faq_items (
    id SERIAL PRIMARY KEY,
    is_authorized_only BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS faq_translations (
    id SERIAL PRIMARY KEY,
    faq_id INTEGER REFERENCES faq_items(id),
    lang VARCHAR(10) NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL
);
