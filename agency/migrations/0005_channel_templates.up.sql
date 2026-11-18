-- Channel template catalog and the calendar lookup index.
-- sqlite-fk:off
-- The channel-kind check is rebuilt. SQLite cannot drop it in place, and
-- content items point at agy_channels, so foreign keys are off for this script.

-- migrate-python:widen_channel_kind

CREATE TABLE agy_channel_templates (
    id          TEXT PRIMARY KEY,
    platform    TEXT NOT NULL CHECK (platform IN (
                    'instagram', 'youtube', 'linkedin', 'facebook', 'website', 'google_ads'
                )),
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    sort_order  INTEGER NOT NULL,
    UNIQUE (platform, slug)
);

CREATE INDEX idx_agy_channel_templates_platform ON agy_channel_templates (platform, sort_order);

CREATE TABLE agy_template_fields (
    id           TEXT PRIMARY KEY,
    template_id  TEXT NOT NULL REFERENCES agy_channel_templates(id) ON DELETE RESTRICT,
    field_key    TEXT NOT NULL,
    label        TEXT NOT NULL,
    max_chars    INTEGER NOT NULL CHECK (max_chars >= 1),
    min_count    INTEGER NOT NULL DEFAULT 1 CHECK (min_count >= 0),
    max_count    INTEGER NOT NULL CHECK (max_count >= 1),
    sort_order   INTEGER NOT NULL,
    UNIQUE (template_id, field_key),
    CHECK (min_count <= max_count)
);

CREATE INDEX idx_agy_template_fields_template ON agy_template_fields (template_id, sort_order);

CREATE INDEX idx_agy_slots_window ON agy_calendar_slots (brand_id, channel_id, starts_at);

-- migrate-python:seed_channel_templates
