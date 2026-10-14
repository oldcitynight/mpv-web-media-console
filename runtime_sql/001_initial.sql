PRAGMA foreign_keys=ON;
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL UNIQUE, applied_at TEXT NOT NULL);
CREATE TABLE media(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 canonical_path TEXT NOT NULL UNIQUE,
 size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
 mtime_ns INTEGER NOT NULL,
 duration_s REAL,
 container TEXT,
 video_codec TEXT,
 audio_codec TEXT,
 width INTEGER,
 height INTEGER,
 fps_num INTEGER,
 fps_den INTEGER,
 hdr_kind TEXT,
 probe_json TEXT CHECK(probe_json IS NULL OR json_valid(probe_json)),
 probed_at TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE playback_sessions(
 id TEXT PRIMARY KEY,
 player_id TEXT NOT NULL,
 media_id INTEGER REFERENCES media(id),
 state TEXT NOT NULL CHECK(state IN('idle','loading','playing','paused','stopped','error','recovering')),
 position_s REAL NOT NULL DEFAULT 0 CHECK(position_s>=0),
 duration_s REAL,
 selected_aid INTEGER,
 selected_sid INTEGER,
 selected_vid INTEGER,
 profile_set_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(profile_set_json)),
 started_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 ended_at TEXT,
 end_reason TEXT
) STRICT;
CREATE TABLE playback_history(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 session_id TEXT NOT NULL REFERENCES playback_sessions(id),
 media_id INTEGER NOT NULL REFERENCES media(id),
 started_at TEXT NOT NULL,
 ended_at TEXT,
 last_position_s REAL NOT NULL DEFAULT 0,
 completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN(0,1)),
 end_reason TEXT
) STRICT;
CREATE TABLE resume_points(
 media_id INTEGER PRIMARY KEY REFERENCES media(id) ON DELETE CASCADE,
 position_s REAL NOT NULL CHECK(position_s>=0),
 duration_s REAL,
 session_id TEXT REFERENCES playback_sessions(id),
 updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE queues(id TEXT PRIMARY KEY,name TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE queue_items(
 id TEXT PRIMARY KEY,
 queue_id TEXT NOT NULL REFERENCES queues(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL,
 media_id INTEGER NOT NULL REFERENCES media(id),
 added_at TEXT NOT NULL,
 UNIQUE(queue_id,ordinal)
) STRICT;
CREATE TABLE profiles(
 id TEXT PRIMARY KEY,
 name TEXT NOT NULL UNIQUE,
 category TEXT NOT NULL CHECK(category IN('base','video','audio','display','media_policy')),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE profile_options(
 profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
 option_name TEXT NOT NULL,
 value_json TEXT NOT NULL CHECK(json_valid(value_json)),
 precedence INTEGER NOT NULL DEFAULT 100,
 PRIMARY KEY(profile_id,option_name)
) STRICT;
CREATE TABLE settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL CHECK(json_valid(value_json)),updated_at TEXT NOT NULL) STRICT;
CREATE TABLE auth_tokens(
 id TEXT PRIMARY KEY,
 label TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL,
 revoked_at TEXT,
 last_used_at TEXT
) STRICT;
CREATE INDEX idx_history_media_started ON playback_history(media_id,started_at DESC);
CREATE INDEX idx_sessions_state ON playback_sessions(state);
CREATE INDEX idx_queue_items_order ON queue_items(queue_id,ordinal);
