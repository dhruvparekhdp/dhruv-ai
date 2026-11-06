-- Agency domain, migration 0001.
--
-- Every table except agy_brands has brand_id TEXT NOT NULL. Brand is the
-- root. Deleting a brand is refused while anything still points at it
-- (ON DELETE RESTRICT) so a cascade cannot wipe a client's history.
--
-- Timestamps are ISO-8601 text written by the application, not
-- CURRENT_TIMESTAMP. Second-precision database clocks already broke ordering
-- once in this repo (D-005).
--
-- Money columns are integer minor units (paise or cents). They are quote
-- inputs. Nothing in this schema is an order, a payout, or an ad balance.

CREATE TABLE agy_brands (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN ('personal', 'agency', 'client')),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE agy_clients (
    id              TEXT PRIMARY KEY,
    brand_id        TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    client_brand_id TEXT REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'archived')),
    website         TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX idx_agy_clients_brand ON agy_clients (brand_id);

CREATE TABLE agy_contacts (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    client_id   TEXT REFERENCES agy_clients(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    phone       TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_contacts_brand ON agy_contacts (brand_id);

CREATE INDEX idx_agy_contacts_client ON agy_contacts (client_id);

CREATE TABLE agy_leads (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    client_id   TEXT REFERENCES agy_clients(id) ON DELETE RESTRICT,
    contact_id  TEXT REFERENCES agy_contacts(id) ON DELETE RESTRICT,
    title       TEXT NOT NULL,
    stage       TEXT NOT NULL DEFAULT 'new' CHECK (stage IN ('new', 'qualified', 'proposal', 'won', 'lost')),
    source      TEXT NOT NULL DEFAULT '',
    value_cents BIGINT CHECK (value_cents IS NULL OR value_cents >= 0),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_leads_brand ON agy_leads (brand_id);

CREATE TABLE agy_lead_stage_history (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    lead_id     TEXT NOT NULL REFERENCES agy_leads(id) ON DELETE RESTRICT,
    from_stage  TEXT NOT NULL DEFAULT '',
    to_stage    TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    changed_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_lead_hist_lead ON agy_lead_stage_history (lead_id);

CREATE TABLE agy_projects (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    client_id   TEXT REFERENCES agy_clients(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused', 'done', 'cancelled')),
    description TEXT NOT NULL DEFAULT '',
    starts_on   TEXT,
    ends_on     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_projects_brand ON agy_projects (brand_id);

CREATE TABLE agy_tasks (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    project_id  TEXT NOT NULL REFERENCES agy_projects(id) ON DELETE RESTRICT,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'doing', 'blocked', 'done')),
    assignee    TEXT NOT NULL DEFAULT '',
    due_on      TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_tasks_brand ON agy_tasks (brand_id);

CREATE INDEX idx_agy_tasks_project ON agy_tasks (project_id);

CREATE TABLE agy_deliverables (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    project_id  TEXT NOT NULL REFERENCES agy_projects(id) ON DELETE RESTRICT,
    task_id     TEXT REFERENCES agy_tasks(id) ON DELETE RESTRICT,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_review', 'accepted', 'rejected')),
    due_on      TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_deliverables_brand ON agy_deliverables (brand_id);

CREATE INDEX idx_agy_deliverables_project ON agy_deliverables (project_id);

CREATE TABLE agy_channels (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('blog', 'newsletter', 'linkedin', 'instagram', 'x', 'youtube', 'podcast', 'other')),
    handle      TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_channels_brand ON agy_channels (brand_id);

CREATE TABLE agy_templates (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other' CHECK (kind IN ('caption', 'email', 'brief', 'proposal', 'other')),
    body        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_templates_brand ON agy_templates (brand_id);

CREATE TABLE agy_campaigns (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'paused', 'completed', 'cancelled')),
    objective   TEXT NOT NULL DEFAULT '',
    starts_on   TEXT,
    ends_on     TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_campaigns_brand ON agy_campaigns (brand_id);

CREATE TABLE agy_content_ideas (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'captured' CHECK (status IN ('captured', 'accepted', 'rejected', 'produced')),
    source      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_ideas_brand ON agy_content_ideas (brand_id);

CREATE TABLE agy_content_items (
    id           TEXT PRIMARY KEY,
    brand_id     TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    idea_id      TEXT REFERENCES agy_content_ideas(id) ON DELETE RESTRICT,
    channel_id   TEXT REFERENCES agy_channels(id) ON DELETE RESTRICT,
    campaign_id  TEXT REFERENCES agy_campaigns(id) ON DELETE RESTRICT,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'in_review', 'approved', 'scheduled', 'archived')),
    scheduled_at TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX idx_agy_items_brand ON agy_content_items (brand_id);

CREATE TABLE agy_content_revisions (
    id              TEXT PRIMARY KEY,
    brand_id        TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    content_item_id TEXT NOT NULL REFERENCES agy_content_items(id) ON DELETE RESTRICT,
    revision_no     INTEGER NOT NULL CHECK (revision_no >= 1),
    body            TEXT NOT NULL DEFAULT '',
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    UNIQUE (content_item_id, revision_no)
);

CREATE INDEX idx_agy_revisions_item ON agy_content_revisions (content_item_id);

CREATE TABLE agy_calendar_slots (
    id              TEXT PRIMARY KEY,
    brand_id        TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    channel_id      TEXT REFERENCES agy_channels(id) ON DELETE RESTRICT,
    content_item_id TEXT REFERENCES agy_content_items(id) ON DELETE RESTRICT,
    title           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'held', 'filled', 'cancelled')),
    starts_at       TEXT NOT NULL,
    ends_at         TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX idx_agy_slots_brand ON agy_calendar_slots (brand_id);

CREATE TABLE agy_approvals (
    id           TEXT PRIMARY KEY,
    brand_id     TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('content_item', 'content_revision', 'campaign', 'deliverable', 'estimate')),
    subject_id   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    note         TEXT NOT NULL DEFAULT '',
    decided_at   TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX idx_agy_approvals_brand ON agy_approvals (brand_id);

CREATE TABLE agy_kpi_definitions (
    id           TEXT PRIMARY KEY,
    brand_id     TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    campaign_id  TEXT REFERENCES agy_campaigns(id) ON DELETE RESTRICT,
    name         TEXT NOT NULL,
    unit         TEXT NOT NULL DEFAULT '',
    target_value REAL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX idx_agy_kpis_brand ON agy_kpi_definitions (brand_id);

CREATE TABLE agy_metric_entries (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    kpi_id      TEXT NOT NULL REFERENCES agy_kpi_definitions(id) ON DELETE RESTRICT,
    campaign_id TEXT REFERENCES agy_campaigns(id) ON DELETE RESTRICT,
    value       REAL NOT NULL,
    recorded_on TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_metrics_kpi ON agy_metric_entries (kpi_id);

CREATE TABLE agy_competitors (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    website     TEXT NOT NULL DEFAULT '',
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_competitors_brand ON agy_competitors (brand_id);

CREATE TABLE agy_research_notes (
    id            TEXT PRIMARY KEY,
    brand_id      TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    competitor_id TEXT REFERENCES agy_competitors(id) ON DELETE RESTRICT,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX idx_agy_research_brand ON agy_research_notes (brand_id);

CREATE TABLE agy_service_templates (
    id                       TEXT PRIMARY KEY,
    brand_id                 TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    name                     TEXT NOT NULL,
    description              TEXT NOT NULL DEFAULT '',
    unit                     TEXT NOT NULL DEFAULT 'item',
    default_unit_price_cents BIGINT NOT NULL DEFAULT 0 CHECK (default_unit_price_cents >= 0),
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE INDEX idx_agy_services_brand ON agy_service_templates (brand_id);

CREATE TABLE agy_estimates (
    id          TEXT PRIMARY KEY,
    brand_id    TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    client_id   TEXT REFERENCES agy_clients(id) ON DELETE RESTRICT,
    project_id  TEXT REFERENCES agy_projects(id) ON DELETE RESTRICT,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'accepted', 'declined', 'expired')),
    currency    TEXT NOT NULL DEFAULT 'INR' CHECK (length(currency) = 3 AND currency = upper(currency)),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE INDEX idx_agy_estimates_brand ON agy_estimates (brand_id);

CREATE TABLE agy_estimate_lines (
    id                  TEXT PRIMARY KEY,
    brand_id            TEXT NOT NULL REFERENCES agy_brands(id) ON DELETE RESTRICT,
    estimate_id         TEXT NOT NULL REFERENCES agy_estimates(id) ON DELETE RESTRICT,
    service_template_id TEXT REFERENCES agy_service_templates(id) ON DELETE RESTRICT,
    description         TEXT NOT NULL,
    quantity            REAL NOT NULL DEFAULT 1 CHECK (quantity > 0),
    unit_price_cents    BIGINT NOT NULL DEFAULT 0 CHECK (unit_price_cents >= 0),
    sort_order          INTEGER NOT NULL DEFAULT 0 CHECK (sort_order >= 0),
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE INDEX idx_agy_estimate_lines_estimate ON agy_estimate_lines (estimate_id);
