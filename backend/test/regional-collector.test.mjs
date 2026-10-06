import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { readFileSync } from 'node:fs';
import { buildRegionalSQL, collectRegional } from '../scripts/build-regional.mjs';

const sample = {
  source: 'haze.gov.sg',
  hotspots: { at: '2026-10-05T08:30:00.000Z', points: [[100.1, 2.2]] },
  haze: { at: '2026-10-05T09:00:00.000Z', polygons: [{ name: "NEA's Moderate", rings: [[[100, 1], [101, 1], [100, 1]]] }] },
};
const captureTime = Date.parse('2026-10-05T22:10:00Z');
function database() {
  const db = new DatabaseSync(':memory:');
  for (const file of ['0004_regional.sql', '0006_regional_history.sql']) {
    db.exec(readFileSync(new URL(`../migrations/${file}`, import.meta.url), 'utf8'));
  }
  return db;
}

test('collector SQL preserves source times, current digest/freshness and Singapore-day history', () => {
  const db = database();
  try {
    const result = buildRegionalSQL(sample, captureTime);
    db.exec(result.sql);
    const row = db.prepare('SELECT * FROM regional').get();
    assert.deepEqual(JSON.parse(row.body), sample);
    assert.equal(row.fetched_at, '2026-10-05T22:10:00.000Z');
    assert.equal(row.etag, `"${createHash('sha256').update(row.body).update('\n').update(row.fetched_at).digest('hex')}"`);
    const history = db.prepare('SELECT * FROM regional_history').get();
    assert.equal(history.day, '2026-10-06');
    assert.equal(history.body, row.body);
    assert.equal(history.captured_at, row.fetched_at);
  } finally { db.close(); }
});

test('successful refresh of identical source geometry changes freshness and ETag together', () => {
  const db = database();
  try {
    db.exec(buildRegionalSQL(sample, captureTime).sql);
    const before = db.prepare('SELECT * FROM regional').get();
    db.exec(buildRegionalSQL(sample, captureTime + 3600_000).sql);
    const after = db.prepare('SELECT * FROM regional').get();
    assert.equal(before.body, after.body);
    assert.notEqual(before.etag, after.etag);
    assert.notEqual(before.fetched_at, after.fetched_at);
  } finally { db.close(); }
});

test('delayed publication cannot regress current row or daily history; retention remains bounded', () => {
  const db = database();
  try {
    db.prepare('INSERT INTO regional_history VALUES (?, ?, ?)').run('2026-09-01', '{}', '2026-09-01T00:00:00Z');
    db.exec(buildRegionalSQL(sample, captureTime).sql);
    const before = db.prepare('SELECT * FROM regional').get();
    db.exec(buildRegionalSQL({ ...sample, hotspots: { ...sample.hotspots, points: [] } }, captureTime - 3600_000).sql);
    assert.deepEqual(db.prepare('SELECT * FROM regional').get(), before);
    assert.equal(db.prepare('SELECT COUNT(*) AS count FROM regional_history').get().count, 1);
    assert.equal(db.prepare('SELECT body FROM regional_history').get().body, before.body);
  } finally { db.close(); }
});

test('collection failure emits no refreshed publication; successful zero-hotspot source is retained', async () => {
  await assert.rejects(collectRegional(async () => new Response('unavailable', { status: 503 })), /regional HTTP 503/);
  let clockCalls = 0;
  const page = '<a href="https://www.haze.gov.sg/docs/default-source/haze_hotspots/Haze_Hotspots_20261005_163000.kml"></a>' +
    '<a href="https://www.haze.gov.sg/docs/default-source/hazeboundaries/HazeBoundaries_20261005_170000.kml"></a>';
  const fetcher = async url => new Response(url === 'https://www.haze.gov.sg' ? page : url.includes('haze_hotspots') ? '<kml></kml>' :
    '<Placemark><name>Moderate</name><Polygon><coordinates>100,1 101,1 100,1</coordinates></Polygon></Placemark>');
  const result = await collectRegional(fetcher, () => { clockCalls++; return captureTime; });
  assert.equal(clockCalls, 1);
  assert.equal(result.regional.hotspots.points.length, 0);
  assert.equal(result.regional.haze.polygons.length, 1);
  assert.equal(result.regional.hotspots.at, '2026-10-05T08:30:00.000Z');
});
