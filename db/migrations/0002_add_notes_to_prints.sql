-- 0002_add_notes_to_prints.sql
BEGIN TRANSACTION;

ALTER TABLE prints ADD COLUMN notes TEXT DEFAULT '';

COMMIT;
