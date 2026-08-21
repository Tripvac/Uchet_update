-- BUG-1. Ссылки на каналы должны храниться как полные URL: значение из
-- bot_links уходит в поле url inline-кнопки, а @username Telegram там не
-- принимает и роняет отправку всего сообщения (экран подписки не виден).
-- Код дополнительно нормализует @username на лету (normalize_link), но в базе
-- держим уже готовые URL, чтобы источник данных был корректным сам по себе.
-- Для getChatMember @username по-прежнему берётся из REQUIRED_SUBSCRIPTIONS.
--
-- ВНИМАНИЕ: точные адреса каналов подтвердить у заказчика перед боевым
-- применением. Здесь — приведение уже имеющихся в базе значений к формату URL.
UPDATE bot_links SET url = 'https://t.me/uchetp',  updated_at = CURRENT_TIMESTAMP WHERE key = 'channel_news';
UPDATE bot_links SET url = 'https://t.me/uchetq', updated_at = CURRENT_TIMESTAMP WHERE key = 'chat_accountants';
