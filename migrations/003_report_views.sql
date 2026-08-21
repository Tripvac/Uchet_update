-- Отчёты для просмотра статистики из bot_events (задача D1).
-- Идея структуры отчётов — от напарника (его ветка feature/B4-db-cash,
-- файл REPORT.sql), адаптировано под нашу схему bot_events
-- (migrations/002_add_events.sql) — у неё уже подходящие имена колонок,
-- поэтому сами запросы почти не пришлось менять.
--
-- Применить: через SQL Editor в Neon, как и предыдущие миграции.

-- Сколько раз нажата каждая кнопка на каждом экране + сколько уникальных
-- пользователей до неё дошло. Самое полезное для вопроса
-- "на чём люди чаще всего останавливаются".
CREATE OR REPLACE VIEW report_stats_summary AS
SELECT
    screen_id,
    button_id,
    count(*) AS total_clicks,
    count(DISTINCT chat_id) AS unique_users
FROM bot_events
GROUP BY screen_id, button_id
ORDER BY total_clicks DESC;

-- Активность по дням — сколько всего действий пользователи совершили
-- за каждый день. Полезно для графика "растёт ли использование бота".
CREATE OR REPLACE VIEW report_daily_activity AS
SELECT
    date(ts) AS day,
    count(*) AS total_actions,
    count(DISTINCT chat_id) AS unique_users
FROM bot_events
GROUP BY date(ts)
ORDER BY day DESC;

-- Воронка по веткам главного меню — сколько уникальных пользователей
-- дошло хотя бы до одного клика в каждой из трёх веток сценария.
CREATE OR REPLACE VIEW report_funnel_by_branch AS
SELECT
    CASE
        WHEN screen_id IN ('categories_info', 'info_accountant', 'info_org', 'info_edu', 'info_outsource')
            THEN 'не член ПОБ'
        WHEN screen_id IN ('member_categories', 'member_topics_full', 'member_topics_assoc',
                            'topic_login', 'topic_hours', 'topic_payment')
            THEN 'член ПОБ'
        WHEN screen_id IN ('about_categories', 'competency_ladder')
            THEN 'узнать подробнее'
        ELSE 'прочее'
    END AS branch,
    count(DISTINCT chat_id) AS unique_users,
    count(*) AS total_clicks
FROM bot_events
GROUP BY branch
ORDER BY unique_users DESC;