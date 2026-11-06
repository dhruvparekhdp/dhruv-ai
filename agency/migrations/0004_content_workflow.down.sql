-- Reverse of 0004.
--
-- sqlite-fk:off
-- Labels this workflow added fold back to draft before the old check returns.
-- The published mark and the approver link are dropped with the columns.

UPDATE agy_content_items
SET status = 'draft'
WHERE status NOT IN ('draft', 'in_review', 'approved', 'scheduled', 'archived');

-- migrate-python:narrow_content_status

ALTER TABLE agy_content_items DROP COLUMN published_on;
ALTER TABLE agy_content_items DROP COLUMN published_url;
ALTER TABLE agy_content_revisions DROP COLUMN title;
ALTER TABLE agy_content_revisions DROP COLUMN author;
ALTER TABLE agy_approvals DROP COLUMN approver_id;
DROP INDEX IF EXISTS idx_agy_approvers_brand;
DROP TABLE IF EXISTS agy_approvers;
