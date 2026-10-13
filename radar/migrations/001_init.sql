-- Initial schema. Timestamps are UTC ISO-8601 text.
CREATE TABLE projects (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    norm_name             TEXT,
    x_handle              TEXT,
    chain                 TEXT,
    status                TEXT NOT NULL,
    verification          TEXT NOT NULL DEFAULT 'unverified',
    price_text            TEXT,
    price_status          TEXT NOT NULL DEFAULT 'unknown',
    launch_at             TEXT,
    launch_date           TEXT,
    launch_precision      TEXT NOT NULL DEFAULT 'unknown',
    launch_text           TEXT,
    launch_tz             TEXT,
    supply                INTEGER,
    eligibility           TEXT NOT NULL DEFAULT '[]',
    website_url           TEXT,
    mint_url              TEXT,
    mint_platform         TEXT,
    first_source_url      TEXT,
    confidence            REAL NOT NULL DEFAULT 0,
    risk_flags            TEXT NOT NULL DEFAULT '[]',
    notes                 TEXT NOT NULL DEFAULT '[]',
    snoozed_until         TEXT,
    live_since            TEXT,
    last_nag_at           TEXT,
    resolved_at           TEXT,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);
CREATE INDEX idx_projects_status ON projects(status);
CREATE INDEX idx_projects_launch ON projects(launch_at);

-- Identity keys used for dedupe/merge: handle:<x>, domain:<d>, name:<n>
CREATE TABLE project_keys (
    key         TEXT PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE posts (
    id               TEXT PRIMARY KEY,
    project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    author_username  TEXT,
    text             TEXT,
    url              TEXT,
    created_at       TEXT,
    fetched_at       TEXT NOT NULL,
    relevant         INTEGER NOT NULL,
    raw              TEXT
);
CREATE INDEX idx_posts_project ON posts(project_id);

-- Every claim we act on keeps its source.
CREATE TABLE evidence (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    field        TEXT NOT NULL,
    value        TEXT,
    source_url   TEXT,
    source_kind  TEXT NOT NULL,   -- project_account | third_party | official_site | llm | user
    snippet      TEXT,
    observed_at  TEXT NOT NULL
);
CREATE INDEX idx_evidence_project ON evidence(project_id);

-- Outbox for discovery/update alerts. dedupe_key makes alerts idempotent.
CREATE TABLE alerts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    dedupe_key           TEXT NOT NULL UNIQUE,
    message              TEXT NOT NULL,
    with_buttons         INTEGER NOT NULL DEFAULT 1,
    created_at           TEXT NOT NULL,
    sent_at              TEXT,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    telegram_message_id  INTEGER
);
CREATE INDEX idx_alerts_unsent ON alerts(sent_at);

-- Persistent reminders. Survive restarts; dedupe_key prevents duplicates.
CREATE TABLE reminders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,   -- pre_1440 | pre_60 | live | repeat | digest
    due_at        TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL UNIQUE,
    created_at    TEXT NOT NULL,
    sent_at       TEXT,
    cancelled_at  TEXT,
    cancel_reason TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
);
CREATE INDEX idx_reminders_due ON reminders(sent_at, cancelled_at, due_at);

CREATE TABLE status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    reason      TEXT,
    at          TEXT NOT NULL
);

CREATE TABLE query_state (
    query           TEXT PRIMARY KEY,
    since_id        TEXT,
    last_polled_at  TEXT
);

CREATE TABLE api_usage (
    day       TEXT NOT NULL,
    provider  TEXT NOT NULL,
    reads     INTEGER NOT NULL DEFAULT 0,
    requests  INTEGER NOT NULL DEFAULT 0,
    errors    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider)
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);
