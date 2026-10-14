ALTER TABLE playback_sessions
ADD COLUMN state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(state_json));

CREATE TABLE player_events(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 player_id TEXT NOT NULL,
 session_id TEXT NOT NULL REFERENCES playback_sessions(id) ON DELETE CASCADE,
 sequence INTEGER NOT NULL CHECK(sequence>0),
 event_type TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 correlation_id TEXT NOT NULL,
 payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
 UNIQUE(player_id,session_id,sequence)
) STRICT;

CREATE INDEX idx_player_events_session_sequence
ON player_events(player_id,session_id,sequence DESC);

CREATE INDEX idx_player_events_occurred_at
ON player_events(occurred_at DESC);
