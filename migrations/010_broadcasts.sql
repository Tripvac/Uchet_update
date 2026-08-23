-- Миграция 010: Таблица рассылок и флаг блокировки бота пользователем (CAST-1)

-- 1. Добавляем поле блокировки в сессии пользователей, если его еще нет
ALTER TABLE bot_sessions 
ADD COLUMN IF NOT EXISTS is_blocked BOOLEAN DEFAULT FALSE;

-- 2. Создаем таблицу для хранения истории и очередей рассылок
CREATE TABLE IF NOT EXISTS bot_broadcasts (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    message_text TEXT NOT NULL,
    status VARCHAR(50) DEFAULT 'pending', -- pending, processing, completed, cancelled
    total_recipients INT DEFAULT 0,
    sent_count INT DEFAULT 0,
    failed_count INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

COMMENT ON TABLE bot_broadcasts IS 'История и статусы массовых рассылок';