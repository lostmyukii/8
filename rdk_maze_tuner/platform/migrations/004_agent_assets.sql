CREATE TRIGGER param_versions_no_update
BEFORE UPDATE ON param_versions
BEGIN
    SELECT RAISE(ABORT, 'param version is immutable');
END;

CREATE TRIGGER param_versions_no_delete
BEFORE DELETE ON param_versions
BEGIN
    SELECT RAISE(ABORT, 'param version is immutable');
END;

CREATE INDEX devices_status_idx
ON devices (status, revoked_at_utc);
