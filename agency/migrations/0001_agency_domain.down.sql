-- Reverse of 0001. Children first so foreign keys can stay enforced.
-- The bookkeeping table agy_schema_migrations is owned by the runner, not
-- by this script, and is dropped only once nothing remains applied.

DROP TABLE IF EXISTS agy_estimate_lines;
DROP TABLE IF EXISTS agy_estimates;
DROP TABLE IF EXISTS agy_service_templates;
DROP TABLE IF EXISTS agy_research_notes;
DROP TABLE IF EXISTS agy_competitors;
DROP TABLE IF EXISTS agy_metric_entries;
DROP TABLE IF EXISTS agy_kpi_definitions;
DROP TABLE IF EXISTS agy_approvals;
DROP TABLE IF EXISTS agy_calendar_slots;
DROP TABLE IF EXISTS agy_content_revisions;
DROP TABLE IF EXISTS agy_content_items;
DROP TABLE IF EXISTS agy_content_ideas;
DROP TABLE IF EXISTS agy_templates;
DROP TABLE IF EXISTS agy_channels;
DROP TABLE IF EXISTS agy_campaigns;
DROP TABLE IF EXISTS agy_deliverables;
DROP TABLE IF EXISTS agy_tasks;
DROP TABLE IF EXISTS agy_projects;
DROP TABLE IF EXISTS agy_lead_stage_history;
DROP TABLE IF EXISTS agy_leads;
DROP TABLE IF EXISTS agy_contacts;
DROP TABLE IF EXISTS agy_clients;
DROP TABLE IF EXISTS agy_brands;
