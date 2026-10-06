CREATE TABLE IF NOT EXISTS regional (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  body TEXT NOT NULL CHECK (json_valid(body)),
  etag TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
