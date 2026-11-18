-- sqlite-fk:off
-- New channel kinds fold to other before the check narrows, or the rebuild
-- would reject the rows it is trying to keep. The catalog goes away with them.

DROP INDEX IF EXISTS idx_agy_slots_window;
DROP TABLE IF EXISTS agy_template_fields;
DROP TABLE IF EXISTS agy_channel_templates;

UPDATE agy_channels SET kind = 'other'
WHERE kind NOT IN ('blog', 'newsletter', 'linkedin', 'instagram', 'x', 'youtube', 'podcast', 'other');

-- migrate-python:narrow_channel_kind
