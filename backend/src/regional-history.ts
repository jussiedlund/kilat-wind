export const HISTORY_KEEP_DAYS = 30;
export const LIST_MAX_BYTES = 300_000;
const SGT_MS = 8 * 3600_000, SLOT_MS = 3 * 3600_000, DAY_MS = 86_400_000;
type DB = { DB: D1Database };

const iso = (ms: number) => new Date(ms).toISOString();
/** SGT calendar day (YYYY-MM-DD) of an instant. */
export function sgtDay(ms: number): string { return iso(ms + SGT_MS).slice(0, 10); }
/** Start of the 3-hour SGT slot containing the instant, e.g. 2026-10-01T12:00+08:00. */
export function sgtSlot(ms: number): string {
  const local = Math.floor((ms + SGT_MS) / SLOT_MS) * SLOT_MS;
  return `${iso(local).slice(0, 13)}:00+08:00`;
}
const slotKey = (slot: string) => slot.slice(0, 13); // sortable text, SGT wall clock
const json = (value: unknown, status: number, cache: string, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(value), { status, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': cache, 'x-content-type-options': 'nosniff', ...extra } });

export async function recordRegionalHistory(env: DB, body: string, now: number): Promise<void> {
  try {
    await env.DB.prepare('REPLACE INTO regional_history (day, body, captured_at) VALUES (?, ?, ?)').bind(sgtDay(now), body, iso(now)).run();
    await env.DB.prepare('DELETE FROM regional_history WHERE day < ?').bind(sgtDay(now - HISTORY_KEEP_DAYS * DAY_MS)).run();
  } catch { console.error('regional history write failed'); }
}
export async function recordAirHistory(env: DB, stations: { id: string; latitude: number; longitude: number; pm25_24hUgM3: number; observedAt: string }[], now: number): Promise<void> {
  try {
    const body = JSON.stringify(stations.map(s => ({ id: s.id, lat: s.latitude, lon: s.longitude, pm25_24hUgM3: s.pm25_24hUgM3, observedAt: s.observedAt })));
    await env.DB.prepare('REPLACE INTO regional_air_history (slot, body) VALUES (?, ?)').bind(sgtSlot(now), body).run();
    await env.DB.prepare('DELETE FROM regional_air_history WHERE slot < ?').bind(slotKey(sgtSlot(now - HISTORY_KEEP_DAYS * DAY_MS))).run();
  } catch { console.error('regional air history write failed'); }
}

async function digest(body: string): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(body));
  return `"${[...new Uint8Array(hash)].map(b => b.toString(16).padStart(2, '0')).join('')}"`;
}
async function reply(request: Request, out: string): Promise<Response> {
  const etag = await digest(out);
  const headers = { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'public, max-age=1800', etag, 'x-content-type-options': 'nosniff' };
  if (request.headers.get('if-none-match') === etag) return new Response(null, { status: 304, headers });
  return new Response(request.method === 'HEAD' ? null : out, { status: 200, headers });
}
function daysParam(url: URL, fallback: number): number | null {
  const raw = url.searchParams.get('days'); if (raw === null) return fallback;
  if (!/^\d{1,2}$/.test(raw)) return null;
  const n = Number(raw); return n >= 1 && n <= 30 ? n : null;
}

/** ?days=N lists full captures unless they exceed LIST_MAX_BYTES, then per-day summaries; ?day=YYYY-MM-DD returns one full capture. */
export async function handleRegionalHistory(request: Request, env: DB, now = Date.now()): Promise<Response> {
  const url = new URL(request.url), one = url.searchParams.get('day');
  if (one !== null) {
    if (!/^\d{4}-\d\d-\d\d$/.test(one)) return json({ error: 'bad day' }, 400, 'no-store');
    const row = await env.DB.prepare('SELECT day, body, captured_at FROM regional_history WHERE day=?').bind(one).first<{ day: string; body: string; captured_at: string }>();
    if (!row) return json({ error: 'no capture for day' }, 404, 'no-store');
    const { source: _s, ...rest } = JSON.parse(row.body);
    return reply(request, JSON.stringify({ day: row.day, capturedAt: row.captured_at, ...rest }));
  }
  const days = daysParam(url, 14);
  if (days === null) return json({ error: 'days must be 1-30' }, 400, 'no-store');
  const rows = (await env.DB.prepare('SELECT day, body, captured_at FROM regional_history WHERE day >= ? ORDER BY day ASC').bind(sgtDay(now - (days - 1) * DAY_MS)).all<{ day: string; body: string; captured_at: string }>()).results ?? [];
  const parsed = rows.map(r => ({ day: r.day, capturedAt: r.captured_at, ...JSON.parse(r.body) as { hotspots: { at: string; points: unknown[] }; haze: { at: string; polygons: unknown[] } } }));
  const size = rows.reduce((n, r) => n + r.body.length, 0);
  if (size <= LIST_MAX_BYTES) return reply(request, JSON.stringify({ days: parsed.map(({ source: _s, ...d }: any) => d) }));
  return reply(request, JSON.stringify({ summary: true, days: parsed.map(d => ({ day: d.day, capturedAt: d.capturedAt,
    hotspots: { at: d.hotspots.at, count: d.hotspots.points.length }, haze: { at: d.haze.at, polygonCount: d.haze.polygons.length } })) }));
}
export async function handleRegionalAirHistory(request: Request, env: DB, now = Date.now()): Promise<Response> {
  const days = daysParam(new URL(request.url), 7);
  if (days === null) return json({ error: 'days must be 1-30' }, 400, 'no-store');
  const from = slotKey(sgtSlot(now - (days - 1) * DAY_MS)).slice(0, 10);
  const rows = (await env.DB.prepare('SELECT slot, body FROM regional_air_history WHERE slot >= ? ORDER BY slot ASC').bind(from).all<{ slot: string; body: string }>()).results ?? [];
  return reply(request, JSON.stringify({ slots: rows.map(r => ({ slot: r.slot, stations: JSON.parse(r.body) })) }));
}
