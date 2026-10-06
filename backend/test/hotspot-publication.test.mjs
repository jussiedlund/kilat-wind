import test from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';

test('count publication restores dates and null gaps without refreshing other feeds', () => {
  const db = new DatabaseSync(':memory:');
  try {
    db.exec('CREATE TABLE snapshot(id INTEGER PRIMARY KEY, body TEXT, etag TEXT, generated_at TEXT); CREATE TABLE hotspot_recent(date TEXT, count INTEGER); CREATE TABLE hotspot_summary_cache(body TEXT);');
    const old = { generatedAt: 'old', weather: { observedAt: 'old', temperature: 30 }, hotspots: { years: [2019], days: [
      { date: '2026-10-04', count: 1, median: 123 }, { date: '2026-10-05', count: null, median: 456 }, { date: '2026-10-06', count: null, median: 789 }
    ] } };
    db.prepare('INSERT INTO snapshot VALUES(1, ?, ?, ?)').run(JSON.stringify(old), 'old-tag', 'old-time');
    db.exec("INSERT INTO hotspot_recent VALUES('2026-10-04', 4691), ('2026-10-05', 0); INSERT INTO hotspot_summary_cache VALUES('{}');");
    db.exec(readFileSync(new URL('../scripts/publish-hotspot-counts.sql', import.meta.url), 'utf8'));
    const row = db.prepare('SELECT * FROM snapshot').get();
    const expected = structuredClone(old); expected.hotspots.days[0].count = 4691; expected.hotspots.days[1].count = 0;
    assert.deepEqual(JSON.parse(row.body), expected);
    assert.equal(row.generated_at, 'old-time');
    assert.notEqual(row.etag, 'old-tag');
    assert.match(row.etag, /^"[a-f0-9]{64}"$/);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM hotspot_summary_cache').get().n, 0);
  } finally { db.close(); }
});
