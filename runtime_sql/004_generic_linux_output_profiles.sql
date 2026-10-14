INSERT OR IGNORE INTO profiles(id,name,category,enabled,created_at,updated_at)
VALUES
    ('linux.desktop', 'Generic Linux desktop video', 'display', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('linux.headless', 'Generic Linux headless video', 'display', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('linux.drm-direct', 'Generic Linux direct DRM video', 'display', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('linux.audio-pipewire', 'Generic Linux PipeWire audio', 'audio', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    ('linux.audio-alsa', 'Generic Linux ALSA audio', 'audio', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));

INSERT OR IGNORE INTO profile_options(profile_id,option_name,value_json,precedence)
VALUES
    ('linux.desktop', 'vo', '"gpu"', 40),
    ('linux.headless', 'vo', '"null"', 40),
    ('linux.drm-direct', 'vo', '"drm"', 40),
    ('linux.audio-pipewire', 'ao', '"pipewire"', 30),
    ('linux.audio-alsa', 'ao', '"alsa"', 30);
