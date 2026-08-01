ALTER TABLE control_lease
ADD COLUMN holder_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL;

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    utc_timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX sessions_user_expiry_idx
ON sessions (user_id, expires_at_utc);

CREATE INDEX audit_events_time_idx
ON audit_events (utc_timestamp, id);

CREATE INDEX audit_events_actor_idx
ON audit_events (actor_user_id, id);
