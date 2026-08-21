-- 005_faq_seed.sql
-- Наполнение FAQ (команда /faq). Таблицы faq_items / faq_translations уже
-- созданы в 000_base_tables.sql. Здесь — примеры-заглушки, чтобы механика
-- листалки работала сразу; РЕАЛЬНЫЕ вопросы/ответы администратор вносит через
-- SQL по инструкции admin.md (таблица faq_translations).
--
-- FAQ_PAGE_SIZE=3, поэтому 4 вопроса дают 2 страницы — видно, что стрелки
-- «◀ / ▶» работают.

-- Уникальность (faq_id, lang) — чтобы правки FAQ были upsert-ом. Добавляем
-- идемпотентно (ADD CONSTRAINT IF NOT EXISTS в старых PG нет).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'faq_translations_faq_lang_uniq'
    ) THEN
        ALTER TABLE faq_translations
            ADD CONSTRAINT faq_translations_faq_lang_uniq UNIQUE (faq_id, lang);
    END IF;
END $$;

INSERT INTO faq_items (id, is_authorized_only) VALUES
    (1, FALSE), (2, FALSE), (3, FALSE), (4, FALSE)
ON CONFLICT (id) DO NOTHING;

-- Явные id выше не двигают SERIAL-последовательность — поправим, чтобы
-- последующие INSERT без id не столкнулись с занятыми номерами.
SELECT setval(
    pg_get_serial_sequence('faq_items', 'id'),
    (SELECT COALESCE(MAX(id), 1) FROM faq_items)
);

INSERT INTO faq_translations (faq_id, lang, question, answer) VALUES
(1, 'ru', 'Как вступить в ПОБ «Учёт»?', '[ЗАГЛУШКА] Впишите ответ через admin.md.'),
(1, 'kz', 'ПОБ «Учёт»-ке қалай кіруге болады?', '[ЫЛҒА] Жауапты admin.md арқылы жазыңыз.'),
(2, 'ru', 'Как оплатить членский взнос?', '[ЗАГЛУШКА] Впишите ответ через admin.md.'),
(2, 'kz', 'Мүшелік жарнаны қалай төлеймін?', '[ЫЛҒА] Жауапты admin.md арқылы жазыңыз.'),
(3, 'ru', 'Не могу войти в личный кабинет — что делать?', '[ЗАГЛУШКА] Впишите ответ через admin.md.'),
(3, 'kz', 'Жеке кабинетке кіре алмаймын — не істеу керек?', '[ЫЛҒА] Жауапты admin.md арқылы жазыңыз.'),
(4, 'ru', 'Где посмотреть часы повышения квалификации?', '[ЗАГЛУШКА] Впишите ответ через admin.md.'),
(4, 'kz', 'Біліктілікті арттыру сағаттарын қайдан көремін?', '[ЫЛҒА] Жауапты admin.md арқылы жазыңыз.')
ON CONFLICT (faq_id, lang) DO UPDATE
    SET question = EXCLUDED.question, answer = EXCLUDED.answer;
