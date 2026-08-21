-- migration: 009_links_fix.sql
-- Замена стороннего сокращателя WhatsApp на прямую официальную ссылку wa.me (LINK-1)

UPDATE bot_links 
SET url = 'https://wa.me/77750392413' 
WHERE key = 'whatsapp_manager';