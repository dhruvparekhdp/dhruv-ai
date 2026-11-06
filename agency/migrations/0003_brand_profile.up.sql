-- Brand profile and the per-platform access checklist.
--
-- A profile is the onboarding sheet for one brand. The checklist records
-- whether access was requested, granted, or not needed, plus a note.
-- There is no column for a password, a token, or an API key. A value that
-- looks like one is rejected in the application before it is written.
--
-- services_bought is a label of work the client already bought. It is not
-- an order and it does not move money.
--
-- Status and platform checks match AccessStatus and AccessPlatform.

CREATE TABLE agy_brand_profiles (
    id                  TEXT PRIMARY KEY,
    brand_id            TEXT NOT NULL UNIQUE REFERENCES agy_brands(id) ON DELETE RESTRICT,
    goals               TEXT NOT NULL DEFAULT '',
    audience            TEXT NOT NULL DEFAULT '',
    positioning         TEXT NOT NULL DEFAULT '',
    voice_description   TEXT NOT NULL DEFAULT '',
    words_to_use        TEXT NOT NULL DEFAULT '[]',
    words_to_avoid      TEXT NOT NULL DEFAULT '[]',
    sample_posts        TEXT NOT NULL DEFAULT '[]',
    competitors         TEXT NOT NULL DEFAULT '[]',
    active_channels     TEXT NOT NULL DEFAULT '[]',
    approval_contacts   TEXT NOT NULL DEFAULT '[]',
    services_bought     TEXT NOT NULL DEFAULT '[]',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE agy_access_items (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    platform    TEXT NOT NULL CHECK (platform IN (
                    'linkedin', 'instagram', 'x', 'youtube', 'facebook', 'tiktok',
                    'blog', 'newsletter', 'google_analytics', 'meta_ads', 'google_ads',
                    'website', 'email', 'other'
                )),
    status      TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'granted', 'not_needed')),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (brand_id, platform)
);

CREATE INDEX idx_agy_access_brand ON agy_access_items (brand_id);
