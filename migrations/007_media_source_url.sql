-- QR-1. Медиа должно быть управляемым администратором: он меняет source_url
-- (ссылку на файл на сайте ПОБ), обнуляет telegram_file_id — и бот при
-- следующей отправке заливает новый файл с этого URL и сам его закэширует.
-- Раньше и QR, и памятка были захардкожены в коде: заменить их можно было
-- только коммитом и деплоем, что нарушало критерий приёмки №12 ТЗ.
ALTER TABLE bot_media ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE bot_media ALTER COLUMN telegram_file_id DROP NOT NULL;

-- Памятка (PDF) — публичный URL известен, заполняем сразу.
INSERT INTO bot_media (key, source_url) VALUES
  ('cpe_hours_doc',
   'https://pob.uchet.kz/local/templates/pob_personal/images/files/pamyatka-buh.pdf')
ON CONFLICT (key) DO UPDATE SET source_url = EXCLUDED.source_url;

-- Kaspi QR: боевой URL картинки на сайте ПОБ запросить у заказчика. До его
-- получения source_url не задаём — бот отправляет QR из локального файла
-- assets/kaspi_qr.png (аварийный фолбэк). Когда URL пришлют, админ выполнит:
--   UPDATE bot_media SET source_url = '<url>', telegram_file_id = NULL,
--          updated_at = NOW() WHERE key = 'kaspi_qr';
