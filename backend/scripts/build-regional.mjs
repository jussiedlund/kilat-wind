#!/usr/bin/env node
/** Collect NEA's regional layers outside the Worker's CPU budget; emit a D1 import. */
import { createHash } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import { pathToFileURL } from 'node:url';
import { fetchRegional } from '../src/regional.ts';
import { HISTORY_KEEP_DAYS, sgtDay } from '../src/regional-history.ts';

const quote = value => `'${value.replaceAll("'", "''")}'`;

export function buildRegionalSQL(regional, now = Date.now()) {
  if (!Number.isFinite(now)) throw new Error('invalid capture time');
  const body = JSON.stringify(regional);
  const fetchedAt = new Date(now).toISOString();
  // fetchedAt is delivered by the API too: unchanged source geometry with a
  // successful refresh must return 200 so a client can clear stale status.
  const etag = `"${createHash('sha256').update(body).update('\n').update(fetchedAt).digest('hex')}"`;
  // The entire current payload, digest and successful-fetch time move in one row.
  // A delayed collector must never replace a more recent successful publication.
  // Import this complete file with wrangler d1 execute --remote --file. D1's
  // file importer commits atomically or rolls back; BEGIN/COMMIT are unsupported.
  const sql = [
    `INSERT INTO regional (id, body, etag, fetched_at) VALUES (1, ${quote(body)}, ${quote(etag)}, ${quote(fetchedAt)}) ` +
      'ON CONFLICT(id) DO UPDATE SET body=excluded.body, etag=excluded.etag, fetched_at=excluded.fetched_at ' +
      'WHERE regional.fetched_at <= excluded.fetched_at;',
    `INSERT INTO regional_history (day, body, captured_at) SELECT ${quote(sgtDay(now))}, body, fetched_at FROM regional ` +
      `WHERE id=1 AND fetched_at=${quote(fetchedAt)} ` +
      'ON CONFLICT(day) DO UPDATE SET body=excluded.body, captured_at=excluded.captured_at ' +
      'WHERE regional_history.captured_at <= excluded.captured_at;',
    `DELETE FROM regional_history WHERE day < ${quote(sgtDay(now - HISTORY_KEEP_DAYS * 86_400_000))};`,
  ].join('\n') + '\n';
  return { sql, body, etag, fetchedAt };
}

export async function collectRegional(fetcher = fetch, clock = Date.now) {
  // Only a fully fetched and parsed source earns a refreshed freshness timestamp.
  const regional = await fetchRegional(fetcher);
  return { ...buildRegionalSQL(regional, clock()), regional };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = process.argv.slice(2);
  if (args.length !== 2 || args[0] !== '--output') {
    console.error('Usage: node --experimental-strip-types backend/scripts/build-regional.mjs --output /tmp/regional.sql');
    process.exitCode = 1;
  } else {
    try {
      const result = await collectRegional();
      await writeFile(args[1], result.sql);
      console.log(`Prepared ${result.regional.hotspots.points.length} hotspots and ${result.regional.haze.polygons.length} haze polygons; fetched ${result.fetchedAt}`);
    } catch (error) {
      console.error(`Regional collection failed: ${error.message}`);
      process.exitCode = 1;
    }
  }
}
