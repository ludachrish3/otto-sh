-- Frozen monitor-database v2 sample (dump spec §13.5). Historical schema, rows
-- and user_version exactly as a v2 archive holds them; never regenerated from
-- today's writer. Removing 2 from MONITOR_DB_READ_VERSIONS deletes this file
-- in the same, marked commit.
PRAGMA user_version = 2;
CREATE TABLE sessions (
    id             TEXT    PRIMARY KEY,
    label          TEXT,
    note           TEXT,
    start          TEXT    NOT NULL,
    end            TEXT,
    lab_json       TEXT    NOT NULL DEFAULT '{}',
    meta_json      TEXT    NOT NULL DEFAULT '{}',
    chart_map_json TEXT    NOT NULL DEFAULT '{}',
    tunnels_json   TEXT    NOT NULL DEFAULT '[]'
);
CREATE TABLE metrics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    label      TEXT    NOT NULL,
    value      REAL    NOT NULL,
    source     TEXT
);
CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    end_ts     TEXT,
    label      TEXT    NOT NULL,
    source     TEXT    NOT NULL DEFAULT 'manual',
    color      TEXT    NOT NULL DEFAULT '#888888',
    dash       TEXT    NOT NULL DEFAULT 'dash'
);
CREATE TABLE log_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL REFERENCES sessions(id),
    ts         TEXT    NOT NULL,
    host       TEXT    NOT NULL DEFAULT '',
    tab        TEXT    NOT NULL DEFAULT '',
    fields     TEXT    NOT NULL DEFAULT '{}'
);
INSERT INTO sessions VALUES (
    '2026-07-01T08-00-00Z', 'first run', 'finalized session',
    '2026-07-01T08:00:00+00:00', '2026-07-01T08:05:00+00:00',
    '{"elements":[],"hosts":[{"id":"h1","element":"h1","os_type":"unix","ip":"10.0.0.1","interfaces":{"eth0":"10.0.0.1"}}],"links":[]}',
    '{"interval":30.0,"charts":[{"label":"CPU","y_title":"CPU %","unit":"%","command":"sample:cpu","chart":"CPU","interval":30.0,"max_series":8}],"tabs":[{"id":"overview","label":"Overview","metrics":["CPU"],"kind":"charts"}]}',
    '{"Overall CPU":"CPU","core 0":"CPU"}',
    '[{"id":"tun-000000000001-5000","protocol":"udp","service_port":5000,"hops":["h1","h2"],"status":"ok","carriers_present":1,"carriers_expected":1}]'
);
INSERT INTO sessions VALUES (
    '2026-07-01T09-00-00Z', NULL, NULL,
    '2026-07-01T09:00:00+00:00', NULL,
    '{}', '{}', '{}', '[]'
);
INSERT INTO metrics (session_id, ts, host, label, value, source) VALUES
    ('2026-07-01T08-00-00Z', '2026-07-01T08:00:00+00:00', 'h1', 'Overall CPU', 12.5, NULL),
    ('2026-07-01T08-00-00Z', '2026-07-01T08:00:30+00:00', 'h1', 'core 0', 25.0, 'sample'),
    ('2026-07-01T09-00-00Z', '2026-07-01T09:00:30+00:00', 'h1', 'Overall CPU', 7.0, NULL);
INSERT INTO events (session_id, ts, end_ts, label, source, color, dash) VALUES
    ('2026-07-01T08-00-00Z', '2026-07-01T08:01:00+00:00', NULL, 'deploy', 'manual', '#888888', 'dash'),
    ('2026-07-01T08-00-00Z', '2026-07-01T08:02:00+00:00', '2026-07-01T08:03:00+00:00', 'outage', 'auto', '#ff0000', 'solid');
INSERT INTO log_events (session_id, ts, host, tab, fields) VALUES
    ('2026-07-01T08-00-00Z', '2026-07-01T08:01:30+00:00', 'h1', 'kernel', '{"level":"error","message":"link flap"}');
