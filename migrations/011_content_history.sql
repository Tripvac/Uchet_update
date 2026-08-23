-- Миграция 011: Создание таблицы bot_content_history для отката правок администратора (ADM-1)
-- Идемпотентна: использует IF NOT EXISTS

CREATE TABLE IF NOT EXISTS bot_content_history (
    id SERIAL PRIMARY KEY,
    screen_id VARCHAR(64) NOT NULL,
    lang VARCHAR(10) NOT NULL,
    body TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Индекс для быстрого поиска истории по экрану и языку
CREATE INDEX IF NOT EXISTS idx_content_history_screen_lang 
ON bot_content_history (screen_id, lang, updated_at DESC);