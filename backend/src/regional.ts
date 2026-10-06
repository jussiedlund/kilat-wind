import { recordRegionalHistory } from './regional-history.ts';
const BASE = 'https://www.haze.gov.sg';
const MAX_KML = 1_500_000;
export const REGIONAL_GATE_MS = 3 * 3600_000;
const RETRY_MS = 10 * 60_000;
let lastAttempt = 0; // isolate-local backoff so a failing source is not hit every minute

export type Ring = [number, number][];
export type RegionalBody = { source: 'haze.gov.sg'; hotspots: { at: string; points: [number, number][] }; haze: { at: string; polygons: { name: string; rings: Ring[] }[] } };

export function findUrls(page: string): { hotspots: string; haze: string } | null {
  const find = (folder: string) => {
    const all = page.match(new RegExp(`https://www\\.haze\\.gov\\.sg/docs/default-source/${folder}/[^"'?\\s<>]+\\.kml`, 'g'));
    return all ? [...all].sort().at(-1)! : null; // names embed YYYYMMDD_HHMMSS, so the largest is the newest
  };
  const hotspots = find('haze_hotspots'), haze = find('hazeboundaries');
  return hotspots && haze ? { hotspots, haze } : null;
}
export function stampOf(url: string): string {
  const m = /(\d{4})(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d)/.exec(url.split('/').pop() ?? '');
  const t = m ? Date.parse(`${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}+08:00`) : NaN;
  if (!Number.isFinite(t)) throw new Error('bad kml timestamp');
  return new Date(t).toISOString();
}
const r3 = (v: number) => Math.round(v * 1000) / 1000;
function coords(text: string): Ring {
  const out: Ring = [];
  for (const tok of text.trim().split(/\s+/)) {
    const [lon, lat] = tok.split(',').map(Number);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90) throw new Error('coordinate out of range');
    out.push([r3(lon), r3(lat)]);
  }
  return out;
}
const COORDS = /<coordinates>([\s\S]*?)<\/coordinates>/g;
export function parseHotspots(kml: string): [number, number][] {
  const points: [number, number][] = [];
  for (const p of kml.matchAll(/<Point>([\s\S]*?)<\/Point>/g)) {
    for (const c of p[1].matchAll(COORDS)) { const ring = coords(c[1]); if (ring[0]) points.push(ring[0]); }
  }
  return points;
}
export function simplifyRing(ring: Ring, max = 500, eps = 0.01): Ring {
  if (ring.length <= max) return ring;
  let out: Ring = [ring[0]];
  for (const p of ring.slice(1, -1)) { const q = out.at(-1)!; if (Math.hypot(p[0] - q[0], p[1] - q[1]) >= eps) out.push(p); }
  out.push(ring.at(-1)!);
  if (out.length > max) { const step = Math.ceil(out.length / max); out = out.filter((_, i) => i % step === 0 || i === out.length - 1); }
  return out;
}
export function parseHaze(kml: string): { name: string; rings: Ring[] }[] {
  const polygons: { name: string; rings: Ring[] }[] = [];
  for (const pm of kml.matchAll(/<Placemark\b[\s\S]*?<\/Placemark>/g)) {
    const raw = /<name>([\s\S]*?)<\/name>/.exec(pm[0])?.[1] ?? '';
    const name = raw.replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1').trim().slice(0, 80);
    for (const poly of pm[0].matchAll(/<Polygon>([\s\S]*?)<\/Polygon>/g)) {
      const rings = [...poly[1].matchAll(COORDS)].map(c => simplifyRing(coords(c[1]))).filter(r => r.length >= 3);
      if (rings.length) polygons.push({ name, rings });
    }
  }
  return polygons;
}
async function text(url: string, fetcher: typeof fetch, max: number): Promise<string> {
  const response = await fetcher(url, { headers: { 'User-Agent': 'SGMapLensBackend/1.0' }, signal: AbortSignal.timeout(12_000) });
  if (!response.ok) throw new Error(`regional HTTP ${response.status}`);
  if (Number(response.headers.get('content-length')) > max) throw new Error('regional body too large');
  const reader = response.body?.getReader(); if (!reader) throw new Error('empty body');
  const chunks: Uint8Array[] = []; let total = 0;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    total += value.byteLength;
    if (total > max) { await reader.cancel(); throw new Error('regional body too large'); }
    chunks.push(value);
  }
  const data = new Uint8Array(total); let pos = 0;
  for (const c of chunks) { data.set(c, pos); pos += c.byteLength; }
  return new TextDecoder().decode(data);
}
export async function fetchRegional(fetcher: typeof fetch = fetch): Promise<RegionalBody> {
  const urls = findUrls(await text(BASE, fetcher, 3_000_000));
  if (!urls) throw new Error('kml links not found');
  const [hs, hz] = await Promise.all([text(urls.hotspots, fetcher, MAX_KML), text(urls.haze, fetcher, MAX_KML)]);
  const points = parseHotspots(hs), polygons = parseHaze(hz);
  if (!points.length && !polygons.length) throw new Error('regional parse yielded nothing');
  return { source: 'haze.gov.sg', hotspots: { at: stampOf(urls.hotspots), points }, haze: { at: stampOf(urls.haze), polygons } };
}
async function digest(body: string): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body));
  return `"${[...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('')}"`;
}
export function regionalDue(fetchedAt: string | null | undefined, now: number): boolean {
  const t = fetchedAt ? Date.parse(fetchedAt) : NaN;
  return !Number.isFinite(t) || now - t >= REGIONAL_GATE_MS;
}
/** Refresh at most every 3 h; on any failure the previous row is kept. Returns true when a new row was written. */
export async function ingestRegional(env: { DB: D1Database }, now = Date.now(), fetcher: typeof fetch = fetch): Promise<boolean> {
  const row = await env.DB.prepare('SELECT fetched_at FROM regional WHERE id=1').first<{ fetched_at: string }>();
  if (!regionalDue(row?.fetched_at, now) || now - lastAttempt < RETRY_MS) return false;
  lastAttempt = now;
  try {
    const body = JSON.stringify(await fetchRegional(fetcher));
    const fetchedAt = new Date(now).toISOString();
    await env.DB.prepare('INSERT INTO regional (id, body, etag, fetched_at) VALUES (1, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body, etag=excluded.etag, fetched_at=excluded.fetched_at')
      .bind(body, await digest(`${body}\n${fetchedAt}`), fetchedAt).run();
    await recordRegionalHistory(env, body, now);
    return true;
  } catch { console.error('regional ingest failed; previous data retained'); return false; }
}
export function resetRegionalBackoff() { lastAttempt = 0; }
/** Isolate-local time of the last real fetch/parse attempt (0 = none); lets the scheduler see that heavy work just ran. */
export function regionalLastAttempt(): number { return lastAttempt; }
export async function handleRegional(request: Request, env: { DB: D1Database }): Promise<Response> {
  const row = await env.DB.prepare('SELECT body, etag, fetched_at FROM regional WHERE id=1').first<{ body: string; etag: string; fetched_at: string }>();
  if (!row) return new Response(JSON.stringify({ error: 'regional data not ready' }), { status: 503, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } });
  const headers = new Headers({ 'content-type': 'application/json; charset=utf-8', 'cache-control': 'public, max-age=1800', etag: row.etag, 'x-content-type-options': 'nosniff' });
  if (request.headers.get('if-none-match') === row.etag) return new Response(null, { status: 304, headers });
  // fetchedAt sits beside the stored body so the gate time and the payload stay in one row.
  const out = JSON.stringify({ source: 'haze.gov.sg', fetchedAt: row.fetched_at, ...JSON.parse(row.body) });
  return new Response(request.method === 'HEAD' ? null : out, { status: 200, headers });
}
