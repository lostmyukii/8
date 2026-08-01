CREATE TABLE physical_profiles (
    profile_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE CHECK (length(digest) = 64),
    random_seed INTEGER NOT NULL CHECK (random_seed >= 0),
    snapshot_json TEXT NOT NULL CHECK (json_valid(snapshot_json)),
    created_at_utc TEXT NOT NULL
);

ALTER TABLE runs
ADD COLUMN physical_profile_id TEXT REFERENCES physical_profiles(profile_id) ON DELETE RESTRICT;

ALTER TABLE runs
ADD COLUMN physical_profile_digest TEXT;

ALTER TABLE runs
ADD COLUMN physical_profile_snapshot_json TEXT;

ALTER TABLE runs
ADD COLUMN random_seed INTEGER;

ALTER TABLE runs
ADD COLUMN controller_version TEXT;

ALTER TABLE runs
ADD COLUMN webots_version TEXT;

CREATE INDEX runs_physical_profile_idx
ON runs (physical_profile_id, physical_profile_digest);

CREATE TRIGGER physical_profiles_no_update
BEFORE UPDATE ON physical_profiles
BEGIN
    SELECT RAISE(ABORT, 'physical profile is immutable');
END;

CREATE TRIGGER physical_profiles_no_delete
BEFORE DELETE ON physical_profiles
BEGIN
    SELECT RAISE(ABORT, 'physical profile is immutable');
END;

CREATE TRIGGER runs_physical_identity_immutable
BEFORE UPDATE OF
    physical_profile_id,
    physical_profile_digest,
    physical_profile_snapshot_json,
    random_seed,
    controller_version,
    webots_version
ON runs
WHEN
    OLD.physical_profile_id IS NOT NEW.physical_profile_id
    OR OLD.physical_profile_digest IS NOT NEW.physical_profile_digest
    OR OLD.physical_profile_snapshot_json IS NOT NEW.physical_profile_snapshot_json
    OR OLD.random_seed IS NOT NEW.random_seed
    OR OLD.controller_version IS NOT NEW.controller_version
    OR OLD.webots_version IS NOT NEW.webots_version
BEGIN
    SELECT RAISE(ABORT, 'run physical identity is immutable');
END;
