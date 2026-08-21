-- 008_events_enrich.sql
-- Добавляем колонку языка и telegram_user_id в события (идемпотентно)
ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS lang VARCHAR(10);
ALTER TABLE bot_events ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT;

-- Ускоряем запросы по чатам и кнопкам
CREATE INDEX IF NOT EXISTS idx_bot_events_chat ON bot_events(chat_id);
CREATE INDEX IF NOT EXISTS idx_bot_events_button ON bot_events(button_id);

-- Создаем вьюшку воронки онбординга
CREATE OR REPLACE VIEW report_onboarding_funnel AS
WITH steps AS (
    SELECT chat_id,
           max((screen_id = 'language_select')::int) AS s1_start,
           max((screen_id = 'subscription')::int)    AS s2_subscription,
           max((screen_id = 'socials')::int)         AS s3_socials,
           max((screen_id = 'main')::int)            AS s4_main,
           max((button_id  = 'btn_join')::int)       AS s5_join_click
    FROM bot_events
    GROUP BY chat_id
)
SELECT sum(s1_start) AS "1_старт", sum(s2_subscription) AS "2_экран_подписки",
       sum(s3_socials) AS "3_прошли_гейт", sum(s4_main) AS "4_главное_меню",
       sum(s5_join_click) AS "5_клик_вступить"
FROM steps;