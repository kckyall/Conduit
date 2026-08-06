-- Conduit lead-generation pipeline — canonical SQLite schema.
-- Apply with:  python init_db.py   (or)  sqlite3 leads.db < schema.sql
--
-- This is the single source of truth for the data model. Every script reads/writes
-- only the columns defined here. Geography and business-type targeting are stored as
-- JSON arrays in Campaigns.target_geography / target_business_types.

CREATE TABLE IF NOT EXISTS Campaigns (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT UNIQUE,
    slug                  TEXT UNIQUE,
    campaign_type         TEXT DEFAULT 'b2b_outreach'
                              CHECK (campaign_type IN ('shared_mailer','solo_mail','b2b_outreach','custom')),
    status                TEXT DEFAULT 'active'
                              CHECK (status IN ('draft','active','paused','completed')),
    description           TEXT,
    value_proposition     TEXT,
    target_description    TEXT,
    target_business_types TEXT,          -- JSON array of Google Place types, e.g. ["restaurant","cafe"]
    target_geography      TEXT,          -- JSON array of ZIP codes, e.g. ["33701","33704"]
    -- Apollo sequencing config (IDs are tenant-specific; never commit real values)
    apollo_sequence_id    TEXT,
    apollo_sequence_name  TEXT,
    apollo_ab_enabled     INTEGER DEFAULT 0,
    sync_enabled          INTEGER NOT NULL DEFAULT 0,   -- live-sync master gate
    max_batch_size        INTEGER NOT NULL DEFAULT 50,
    whitelisted_sequences TEXT DEFAULT NULL,            -- JSON allowlist of sequence IDs
    sender_inbox_id       TEXT,
    -- Personalization guidance for snippet generation
    personalization_context TEXT,
    personalization_angles  TEXT,        -- JSON array
    sender_identity       TEXT,
    created_at            TEXT DEFAULT (datetime('now')),
    updated_at            TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS PipelineContacts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id          INTEGER REFERENCES Campaigns(id),
    -- identity
    google_place_id      TEXT UNIQUE,
    business_name        TEXT,
    display_name         TEXT,
    primary_type         TEXT,
    -- location
    address              TEXT,
    city                 TEXT,
    state                TEXT,
    zip_code             TEXT,
    neighborhood         TEXT,
    -- contact
    phone                TEXT,
    website              TEXT,
    email                TEXT,
    contact_name         TEXT,
    contact_title        TEXT,
    display_contact      TEXT,
    -- social
    social_facebook      TEXT,
    social_instagram     TEXT,
    social_linkedin      TEXT,
    has_social_presence  INTEGER,
    -- google
    google_rating        REAL,
    review_count         INTEGER,
    -- enrichment
    years_in_business    INTEGER,
    website_quality      TEXT CHECK (website_quality IN ('none','basic','professional','strong') OR website_quality IS NULL),
    fit_score            INTEGER,        -- 1-10
    disqualify_reason    TEXT,
    -- personalization snippets (for Apollo mail-merge custom fields)
    personalization_angle TEXT,
    snippet_subject      TEXT,
    snippet_hook         TEXT,
    snippet_body         TEXT,
    -- Apollo tracking
    apollo_contact_id    TEXT,
    apollo_sequence_id   TEXT,
    apollo_ab_variant    TEXT,
    -- pipeline status
    status               TEXT DEFAULT 'new'
                             CHECK (status IN ('new','researching','qualified','disqualified',
                                               'enriched','synced','opted_out',
                                               'customer','warm_contact','do_not_contact')),
    -- source / provenance
    source_type          TEXT CHECK (source_type IN ('scrape','manual_single','manual_batch','manual_sheet') OR source_type IS NULL),
    source_scrape_id     INTEGER,
    source_reference     TEXT,
    intake_context       TEXT,
    created_at           TEXT DEFAULT (datetime('now')),
    updated_at           TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ScrapeHistory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id     INTEGER REFERENCES Campaigns(id),
    zip_codes       TEXT,
    place_types     TEXT,
    started_at      TEXT,
    completed_at    TEXT,
    total_results   INTEGER,
    new_leads       INTEGER,
    duplicate_count INTEGER,
    api_calls_used  INTEGER,
    status          TEXT
);

CREATE TABLE IF NOT EXISTS OutreachLog (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id           INTEGER REFERENCES PipelineContacts(id),
    campaign_id          INTEGER REFERENCES Campaigns(id),
    apollo_sequence_name TEXT,
    enrolled_at          TEXT DEFAULT (datetime('now')),
    current_step         INTEGER,
    status               TEXT DEFAULT 'active'
                             CHECK (status IN ('active','completed','bounced','replied','opted_out')),
    ab_variant           TEXT,
    last_activity_at     TEXT,
    sync_status          TEXT NOT NULL DEFAULT 'pending',
    sync_error           TEXT,
    sync_batch_id        TEXT,
    sender_inbox_id      TEXT
);

CREATE TABLE IF NOT EXISTS ApiUsageLog (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service       TEXT,
    endpoint      TEXT,
    calls_made    INTEGER DEFAULT 1,
    billing_month TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS PipelineRuns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id   INTEGER REFERENCES Campaigns(id),
    run_type      TEXT,
    status        TEXT DEFAULT 'running'
                      CHECK (status IN ('running','completed','failed','cancelled')),
    started_at    TEXT DEFAULT (datetime('now')),
    finished_at   TEXT,
    error_message TEXT,
    metadata_json TEXT
);
