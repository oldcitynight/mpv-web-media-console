-- Additive ownership journal. Submitted native side effects are never retried.
CREATE TABLE queue_play_intents (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 queue_id TEXT NOT NULL REFERENCES queues(id),
 -- Immutable requested ID survives removal, so it cannot borrow a duplicate.
 item_id TEXT,
 path TEXT,
 queue_revision INTEGER NOT NULL,
 source TEXT NOT NULL CHECK(source IN ('manual','direct','stop','auto','recovery')),
 phase TEXT NOT NULL CHECK(phase IN ('prepared','submitted','confirmed','failed','uncertain','superseded','removed','cancelled')),
 detail TEXT,
 created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE queue_playback_bindings (
 queue_id TEXT PRIMARY KEY REFERENCES queues(id),
 -- Preserve the original association as an audit tombstone after removal.
 item_id TEXT,
 intent_id INTEGER REFERENCES queue_play_intents(id),
 player_id TEXT NOT NULL,
 session_id TEXT NOT NULL,
 load_serial INTEGER NOT NULL,
 playlist_entry_id INTEGER NOT NULL,
 path TEXT NOT NULL,
 phase TEXT NOT NULL CHECK(phase IN ('playing','pending_cause','ended','revoked'))
);
CREATE TABLE queue_terminal_actions (
 origin TEXT PRIMARY KEY,
 queue_id TEXT NOT NULL REFERENCES queues(id),
 origin_item_id TEXT,
 intent_id INTEGER UNIQUE REFERENCES queue_play_intents(id),
 phase TEXT NOT NULL CHECK(phase IN ('claimed','submitted','confirmed','failed','uncertain','cancelled','end')),
 detail TEXT
);
CREATE INDEX idx_queue_intents_current ON queue_play_intents(queue_id,id DESC);
