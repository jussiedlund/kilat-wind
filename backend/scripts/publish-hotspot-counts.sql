-- Publish collected daily counts without rebuilding unrelated feed data.
-- Import with wrangler d1 execute --remote --file (atomic file import).
DELETE FROM hotspot_summary_cache;
UPDATE snapshot SET
  body = json_set(body, '$.hotspots.days', json((
    SELECT json_group_array(json_set(day.value, '$.count', (
      SELECT count FROM hotspot_recent WHERE date = json_extract(day.value, '$.date')
    ))) FROM json_each(snapshot.body, '$.hotspots.days') AS day
  ))),
  etag = '"' || lower(hex(randomblob(32))) || '"'
WHERE id = 1 AND json_type(body, '$.hotspots.days') = 'array';
