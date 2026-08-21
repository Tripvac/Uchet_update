CREATE TABLE IF NOT EXISTS bot_events (
    id SERIAL PRIMARY KEY,
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    chat_id BIGINT NOT NULL,
    telegram_user_id BIGINT,
    screen_id VARCHAR(50),
    button_id VARCHAR(50),
    meta JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_bot_events_ts_screen ON bot_events(ts, screen_id);