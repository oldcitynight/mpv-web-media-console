UPDATE profiles
SET enabled=0,
    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
WHERE id='video.vaapi' AND category='video' AND enabled=1;

INSERT OR IGNORE INTO profiles(id,name,category,enabled,created_at,updated_at)
VALUES
    ('display.vaapi-drm', 'Opt-in VAAPI with direct DRM GPU context', 'display', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));

INSERT OR IGNORE INTO profile_options(profile_id,option_name,value_json,precedence)
VALUES
    ('display.vaapi-drm', 'vo', '"gpu"', 40),
    ('display.vaapi-drm', 'gpu-context', '"drm"', 40),
    ('display.vaapi-drm', 'hwdec', '"vaapi"', 40);
