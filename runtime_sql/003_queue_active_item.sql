ALTER TABLE queues
ADD COLUMN active_item_id TEXT REFERENCES queue_items(id) ON DELETE SET NULL;

CREATE INDEX idx_queue_items_media
ON queue_items(queue_id,media_id,ordinal);
