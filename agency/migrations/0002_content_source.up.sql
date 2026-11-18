-- Link a duplicated content item to the item it was copied from.
--
-- The new row has its own id and its own brand_id. source_item_id points at
-- the original and does not move it. Deleting the original is refused while
-- a copy still points at it, so the link cannot dangle.

ALTER TABLE agy_content_items
    ADD COLUMN source_item_id TEXT REFERENCES agy_content_items(id) ON DELETE RESTRICT;

CREATE INDEX idx_agy_items_source ON agy_content_items (source_item_id);
