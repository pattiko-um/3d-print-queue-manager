-- 0001_initial.sql
-- Initial schema for PrintQueue
BEGIN TRANSACTION;

CREATE TABLE IF NOT EXISTS tickets (
    id                  INTEGER PRIMARY KEY,
    title               TEXT NOT NULL,
    requester           TEXT DEFAULT '',
    username            TEXT DEFAULT '',
    external_ticket_id  INTEGER,
    ticket_url          TEXT,
    notes               TEXT DEFAULT '',
    status              TEXT NOT NULL DEFAULT 'received'
                        CHECK(status IN ('received','awaiting_input','queued','in_process','complete','closed')),
    priority            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    closed_at          TEXT
);

CREATE TABLE IF NOT EXISTS prints (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id           INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    filename            TEXT NOT NULL,
    filepath            TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'to_do'
                        CHECK(status IN ('to_do','awaiting_input','queued','printing','complete')),
    quantity            INTEGER NOT NULL DEFAULT 1,
    quantity_completed  INTEGER NOT NULL DEFAULT 0,
    size_x_mm           REAL,
    size_y_mm           REAL,
    size_z_mm           REAL,
    volume_mm3          REAL,
    triangle_count      INTEGER,
    has_overhangs       INTEGER DEFAULT 0,
    overhang_area_mm2   REAL,
    support_vol_mm3     REAL,
    layer_count         INTEGER,
    filament_length_m   REAL,
    filament_mass_g     REAL,
    time_minutes        REAL,
    time_formatted      TEXT,
    config_json         TEXT,
    issues_json         TEXT,
    parse_error         TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

COMMIT;
