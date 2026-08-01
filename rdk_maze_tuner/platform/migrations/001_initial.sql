CREATE TABLE users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT
);

CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    csrf_digest TEXT NOT NULL,
    expires_at_utc TEXT NOT NULL,
    created_at_utc TEXT NOT NULL,
    revoked_at_utc TEXT
);

CREATE TABLE control_lease (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    holder_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    lease_token_digest TEXT,
    acquired_at_utc TEXT,
    heartbeat_at_utc TEXT,
    expires_at_utc TEXT
);

CREATE TABLE devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_digest TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'offline',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT,
    revoked_at_utc TEXT
);

CREATE TABLE maps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at_utc TEXT NOT NULL,
    archived_at_utc TEXT
);

CREATE TABLE map_versions (
    id TEXT PRIMARY KEY,
    map_id TEXT NOT NULL REFERENCES maps(id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    digest TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (map_id, version_number)
);

CREATE TABLE param_versions (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES param_versions(id) ON DELETE RESTRICT,
    digest TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    diff_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    approval_json TEXT NOT NULL DEFAULT '{}',
    context_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('simulation', 'real')),
    status TEXT NOT NULL,
    map_version_id TEXT REFERENCES map_versions(id) ON DELETE RESTRICT,
    param_version_id TEXT REFERENCES param_versions(id) ON DELETE RESTRICT,
    device_id TEXT REFERENCES devices(id) ON DELETE RESTRICT,
    created_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at_utc TEXT NOT NULL,
    started_at_utc TEXT,
    ended_at_utc TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    monotonic_ns INTEGER NOT NULL CHECK (monotonic_ns >= 0),
    utc_timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    jsonl_written INTEGER NOT NULL DEFAULT 0 CHECK (jsonl_written IN (0, 1)),
    created_at_utc TEXT NOT NULL
);

CREATE INDEX events_run_time_idx
ON events (run_id, monotonic_ns, event_id);

CREATE TABLE scores (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
    profile_version TEXT NOT NULL,
    raw_metrics_json TEXT NOT NULL,
    breakdown_json TEXT NOT NULL,
    total_score REAL NOT NULL,
    created_at_utc TEXT NOT NULL,
    UNIQUE (run_id, profile_version)
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    sha256 TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    retained_until_utc TEXT,
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE advisor_candidates (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT,
    parent_param_version_id TEXT REFERENCES param_versions(id) ON DELETE RESTRICT,
    provider TEXT NOT NULL,
    status TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    expected_effect_json TEXT NOT NULL DEFAULT '{}',
    risk_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL,
    decided_at_utc TEXT,
    decided_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL
);
