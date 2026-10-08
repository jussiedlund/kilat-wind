-- Hourly AI haze briefing, one row per attempt (including skipped, rejected and failed ones) so a shadow week can be reviewed.
CREATE TABLE haze_briefing (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'skipped', 'error')),
  model TEXT NOT NULL,
  prompt_sha TEXT NOT NULL,
  weights_sha TEXT NOT NULL,
  card_json TEXT,
  raw_text TEXT,
  reasons TEXT NOT NULL DEFAULT '[]',
  warnings TEXT NOT NULL DEFAULT '[]',
  digest_md TEXT,
  sources_json TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cost_usd REAL,
  seconds REAL
);
CREATE INDEX idx_haze_briefing_created ON haze_briefing (created_at);
