-- Content workflow.
--
-- sqlite-fk:off
-- SQLite cannot replace a CHECK constraint in place. The hook rebuilds
-- agy_content_items with the wider status list. Foreign keys are off for
-- this script on SQLite so that rebuild can drop the old table. Postgres
-- swaps the check. in_review becomes review.
--
-- published_url and published_on are a manual mark. Nothing fetches the URL.
-- agy_approvers is the allow-list for entering approved. The row is a name,
-- not a password or a token.

ALTER TABLE agy_content_items ADD COLUMN published_url TEXT NOT NULL DEFAULT '';
ALTER TABLE agy_content_items ADD COLUMN published_on TEXT;

-- migrate-python:widen_content_status

ALTER TABLE agy_content_revisions ADD COLUMN author TEXT NOT NULL DEFAULT '';
ALTER TABLE agy_content_revisions ADD COLUMN title TEXT NOT NULL DEFAULT '';

CREATE TABLE agy_approvers (
    id         TEXT PRIMARY KEY,
    brand_id   TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_agy_approvers_brand ON agy_approvers (brand_id);

ALTER TABLE agy_approvals ADD COLUMN approver_id TEXT REFERENCES agy_approvers(id) ON DELETE RESTRICT;
