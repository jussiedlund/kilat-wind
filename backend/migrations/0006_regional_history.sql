CREATE TABLE IF NOT EXISTS regional_history (
  day TEXT PRIMARY KEY,
  body TEXT NOT NULL CHECK (json_valid(body)),
  captured_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS regional_air_history (
  slot TEXT PRIMARY KEY,
  body TEXT NOT NULL CHECK (json_valid(body))
);
