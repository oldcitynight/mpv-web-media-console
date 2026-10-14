INSERT OR IGNORE INTO profiles(id,name,category,enabled,created_at,updated_at)
VALUES
    ('video.vaapi', 'Opt-in VAAPI hardware video decoding', 'video', 1,
     strftime('%Y-%m-%dT%H:%M:%fZ','now'), strftime('%Y-%m-%dT%H:%M:%fZ','now'));

INSERT OR IGNORE INTO profile_options(profile_id,option_name,value_json,precedence)
VALUES ('video.vaapi', 'hwdec', '"vaapi"', 20);
