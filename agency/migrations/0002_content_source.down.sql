-- Reverse of 0002. The index names the column, so it goes first.
DROP INDEX IF EXISTS idx_agy_items_source;
ALTER TABLE agy_content_items DROP COLUMN source_item_id;
