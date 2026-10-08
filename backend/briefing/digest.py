"""Turn a raw snapshot into the evidence digest the model reads.

All arithmetic happens here: trends, bands, distances, bearings and whether the
modelled wind points from the fires toward Singapore. The model only has to
explain. Every digest line carries an evidence ID (N1, L3, W2...) so claims in
an answer can be traced back.

Each section is a function (raw, ctx) -> list of (text, facts). Reorder,
drop or add sections in SECTIONS.
"""
import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

SGT = timezone(timedelta(hours=8))
SG = (1.35, 103.82)
REGIONS = ('north', 'south', 'east', 'west', 'central')


# ---------- shared helpers ----------

def t(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def sgt(value, fmt='%a %d %b %H:%M'):
    return (t(value) if isinstance(value, str) else value).astimezone(SGT).strftime(fmt)


def ago(then, now):
    minutes = round((now - (t(then) if isinstance(then, str) else then)).total_seconds() / 60)
    return f'{minutes} min ago' if minutes < 90 else f'{minutes / 60:.0f} h ago' if minutes < 48 * 60 else f'{minutes / 1440:.0f} days ago'


def km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = math.sin((la2 - la1) * p / 2) ** 2 + math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def bearing(a, b):
    (la1, lo1), (la2, lo2) = [(x * math.pi / 180, y * math.pi / 180) for x, y in (a, b)]
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def relative_day(day, now):
    """'today (Thu 8 Oct)' style, so the model never has to work out which day a bare date is."""
    target = datetime.fromisoformat(day).date()
    gap = (target - now.astimezone(SGT).date()).days
    word = {-1: 'yesterday', 0: 'today', 1: 'tomorrow'}.get(gap, '')
    label = target.strftime('%a %-d %b')
    return f'{word} ({label})' if word else label


def compass(deg):
    names = ['N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE', 'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW']
    return names[round(deg / 22.5) % 16]


def angle_gap(a, b):
    return abs((a - b + 180) % 360 - 180)


def pm25_band(v):
    return 'Normal (Band I)' if v <= 55 else 'Elevated (Band II)' if v <= 150 else 'High (Band III)' if v <= 250 else 'Very High (Band IV)'


def psi_band(v):
    return 'Good' if v <= 50 else 'Moderate' if v <= 100 else 'Unhealthy' if v <= 200 else 'Very Unhealthy' if v <= 300 else 'Hazardous'


def signed(v, unit=''):
    return f'{v:+.0f}{unit}' if v else f'±0{unit}'


NEA_FIELD = {'pm25': 'pm25_one_hourly', 'psi': 'psi_twenty_four_hourly'}


def series(raw, metric, region):
    """Hourly points for today and yesterday, keyed by observation time.
    Direct NEA captures win; the app's Worker snapshot fills gaps."""
    points = {}
    details = (raw.get('snapshot') or {}).get('details', {})
    for block in (details.get('prevDay', {}).get(metric, {}), details.get(metric, {})):
        for p in block.get(region) or []:
            if p.get('v') is not None and p.get('observedAt'):
                points[t(p['observedAt'])] = p['v']
    for day in ('yesterday', 'today'):
        for item in ((raw.get(f'{metric}_{day}') or {}).get('data') or {}).get('items', []):
            v = item['readings'].get(NEA_FIELD[metric], {}).get(region)
            if v is not None:
                points[t(item['timestamp'])] = v
    return sorted(points.items())


def value_at(points, when, tolerance=timedelta(minutes=40)):
    best = min(points, key=lambda p: abs(p[0] - when), default=None)
    return best[1] if best and abs(best[0] - when) <= tolerance else None


def inside(point, ring):
    """Ray casting; ring is [[lon, lat], ...]."""
    lat, lon = point
    hit = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def area(lat, lon):
    """Coarse named areas for hotspot grouping. Boundaries are approximate on purpose."""
    if lat >= 1.25 and lon <= 104.6 and lon >= (102.0 if lat < 2.6 else 100.8 if lat < 4 else 99.6):
        return 'Peninsular Malaysia'
    if lon >= 108.5 and lat >= -4.5:
        return 'Borneo (Kalimantan, Sarawak, Sabah)'
    if 100 <= lon <= 106.5 and -6 <= lat < -0.8:
        return 'Southern Sumatra (Jambi, South Sumatra, Lampung, Bangka)'
    if 95 <= lon <= 104.6 and -0.8 <= lat <= 6:
        return 'Central and northern Sumatra (Riau, West/North Sumatra)'
    if lat < -5.5 and 105 <= lon <= 116:
        return 'Java and Bali'
    return 'Elsewhere in the region'


# ---------- sections ----------

def clock(raw, ctx):
    return [(f"Briefing time: {sgt(ctx['now'], '%A %d %B %Y, %H:%M')} SGT. One briefing for all of Singapore; "
             "readers are anywhere on the island.", {'now': ctx['now'].isoformat()})]


def nea_outlook(raw, ctx):
    data = raw.get('haze_outlook')
    if not data or not data.get('report'):
        return [('NEA haze outlook: unavailable in this snapshot.', None)]
    r, now = data['report'], ctx['now']
    lines = [(f"NEA haze outlook issued {sgt(r['issuedAt'])} SGT ({ago(r['issuedAt'], now)}): 24-hr PSI forecast for "
              f"{relative_day(r['forecastDay'], now) if r.get('forecastDay') else 'the stated period'} is \"{r['forecast']}\"."
              + (' This outlook has EXPIRED.' if data.get('expired') else f" Valid until {sgt(r['validUntil'])} SGT."), r)]
    if r.get('context'):
        lines.append((f"NEA's situation note: {r['context']}", None))
    for p in r.get('paragraphs', []):
        if 'forecast' in p.lower() and 'psi' in p.lower() or p.startswith('Singapore,'):
            lines.append((f'NEA bulletin text: "{p.lstrip("0123456789. ")}"', None))
    return lines


def singapore_now(raw, ctx):
    d, now = raw, ctx['now']
    lines, facts = [], {}
    for region in REGIONS:
        pm, psi = series(d, 'pm25', region), series(d, 'psi', region)
        if not pm:
            continue
        at, v = pm[-1]
        change = {h: (v - old) for h in (1, 3, 6) if (old := value_at(pm, at - timedelta(hours=h))) is not None}
        last24 = [(w, x) for w, x in pm if w > at - timedelta(hours=24)]
        peak_at, peak = max(last24, key=lambda p: p[1])
        psi_now = psi[-1][1] if psi else None
        psi_6h = value_at(psi, psi[-1][0] - timedelta(hours=6)) if psi else None
        facts[region] = {'pm25': v, 'at': at.isoformat(), 'change': change, 'peak24': peak, 'psi': psi_now}
        trend = ', '.join(f'{signed(c)} vs {h}h ago' for h, c in change.items())
        text = (f"{region.title()}: 1-hr PM2.5 {v} µg/m³ at {sgt(at, '%H:%M')} [{pm25_band(v)}] ({trend}); "
                f"24h peak {peak} at {sgt(peak_at, '%a %H:%M')}")
        if psi_now is not None:
            text += f"; 24-hr PSI {psi_now} [{psi_band(psi_now)}]" + (f" ({signed(psi_now - psi_6h)} vs 6h ago)" if psi_6h is not None else '')
        lines.append((text, None))
    if not facts:
        return [('Singapore air readings: unavailable in this snapshot.', None)]
    latest = max(t(f['at']) for f in facts.values())
    values = [f['pm25'] for f in facts.values()]
    lines.insert(0, (f"Singapore, latest hour {sgt(latest, '%H:%M')} ({ago(latest, now)}): 1-hr PM2.5 ranges "
                     f"{min(values)}–{max(values)} µg/m³ across 5 regions. NEA bands: Normal 0–55, Elevated 56–150, "
                     f"High 151–250, Very High 251+. 24-hr PSI: Good ≤50, Moderate 51–100, Unhealthy 101–200, "
                     f"Very Unhealthy 201–300, Hazardous 300+.", facts))
    hours = [latest - timedelta(hours=h) for h in range(8, -1, -1)]
    rows = [f"{r.title()} " + ' '.join(str(value_at(series(d, 'pm25', r), h) or '–') for h in hours) for r in REGIONS]
    lines.append((f"Hourly 1-hr PM2.5, {sgt(hours[0], '%H:%M')} to {sgt(hours[-1], '%H:%M')}: " + '; '.join(rows), None))
    island = [max(series(d, 'pm25', r)[-24:], key=lambda p: p[1], default=(None, 0))[1] for r in REGIONS]
    lines.append((f"Highest 1-hr PM2.5 anywhere in Singapore over the last 24 h: {max(island)} µg/m³.", None))
    return lines


ADVICE = [  # Mirrors AirDetails.rules in app/SGMapLens/DetailsContent.swift
    ('Normal activities are fine for most people.', 'Outdoor exercise as usual; windows open; sensitive groups as usual.'),
    ('Reduce prolonged or strenuous outdoor activity.', 'Outdoor exercise shorter and lighter; facemask not needed for short trips; '
     'air purifier on at home; windows closed; sensitive groups minimise time outdoors.'),
    ('Avoid prolonged or strenuous outdoor activity.', 'Exercise indoors; N95 for long outdoor stays; air purifier on; windows closed; '
     'sensitive groups stay indoors if possible.'),
    ('Minimise outdoor activity; stay indoors where possible.', 'Exercise indoors; N95 outdoors; air purifier on; windows closed; '
     'sensitive groups stay indoors.'),
]


def advice_level(raw, region):
    """Bands.adviceLevel: the stricter of the 1-hr PM2.5 level and the 24-hr PSI level."""
    pm, psi = series(raw, 'pm25', region), series(raw, 'psi', region)
    if not pm or not psi:
        return None
    pm_level = 0 if pm[-1][1] < 56 else 1 if pm[-1][1] < 151 else 2 if pm[-1][1] < 251 else 3
    psi_level = 0 if psi[-1][1] <= 100 else 1 if psi[-1][1] <= 200 else 2 if psi[-1][1] <= 300 else 3
    driver = 'the 24-hr PSI' if psi_level > pm_level else 'the 1-hr PM2.5' if pm_level > psi_level else 'both readings'
    return max(pm_level, psi_level), driver


def advisory(raw, ctx):
    """The app shows each reader the advisory card for their own region beside this briefing.
    One island-wide briefing must be consistent with every region's card."""
    levels = {r: advice_level(raw, r) for r in REGIONS}
    levels = {r: v for r, v in levels.items() if v}
    if not levels:
        return [('App health advisory: cannot be computed, readings are missing.', None)]
    groups = {}
    for region, (level, driver) in levels.items():
        groups.setdefault(level, []).append((region, driver))
    lines = [("Each reader sees the app's health advisory card for their own region right beside this briefing. "
              "It is context only: the summary must not repeat or paraphrase this advice, but must never contradict it.", None)]
    for level in sorted(groups, reverse=True):
        headline, rows = ADVICE[level]
        regions = ', '.join(r for r, _ in groups[level])
        drivers = sorted({d for _, d in groups[level]})
        lines.append((f"{regions}: \"{headline}\" ({rows}) Set by {' / '.join(drivers)}.",
                      {'level': level, 'regions': [r for r, _ in groups[level]]}))
    return lines


def regional_stations(raw, ctx):
    data, hist = raw.get('regional_air'), raw.get('regional_air_history')
    if not data:
        return [('Regional (Malaysia, Indonesia) stations: unavailable in this snapshot.', None)]
    now = ctx['now']
    past = {}
    for slot in (hist or {}).get('slots', []):
        age = now - t(slot['slot'].replace('+08:00', '+08:00'))
        if timedelta(hours=20) <= age <= timedelta(hours=30):
            for s in slot['stations']:
                past.setdefault(s['id'], s['pm25_24hUgM3'])
    rows = []
    for s in data['stations']:
        pos = (s['latitude'], s['longitude'])
        rows.append({'name': s['name'], 'country': s['country'], 'km': s['distanceKm'], 'dir': compass(bearing(SG, pos)),
                     'pm25': s['pm25_24hUgM3'], 'at': s['observedAt'], 'was': past.get(s['id']),
                     'estimated': s.get('measurement') != 'reportedConcentration'})
    rows.sort(key=lambda r: r['km'])
    lines = [("Regional stations report 24-hour MEAN PM2.5 (µg/m³), so they lag; they are not comparable to "
              "Singapore's 1-hr readings. Some Malaysian values are estimated from the API index.", None)]
    for r in rows[:8]:
        change = f", {signed(r['pm25'] - r['was'])} vs ~24h earlier" if r['was'] is not None else ''
        lines.append((f"{r['name']} ({r['country']}), {r['km']:.0f} km {r['dir']} of Singapore: {r['pm25']:.0f} "
                      f"at {sgt(r['at'], '%a %H:%M')}{change}", None))
    for label, lo, hi in (('100–250 km', 100, 250), ('250–400 km', 250, 400)):
        band = [r for r in rows if lo <= r['km'] < hi]
        if band:
            worst = max(band, key=lambda r: r['pm25'])
            rising = sum(1 for r in band if r['was'] is not None and r['pm25'] > r['was'] + 5)
            falling = sum(1 for r in band if r['was'] is not None and r['pm25'] < r['was'] - 5)
            lines.append((f"Stations {label} away: {len(band)}, median {statistics.median(r['pm25'] for r in band):.0f}, "
                          f"highest {worst['pm25']:.0f} at {worst['name']} ({worst['dir']}); vs ~24h earlier "
                          f"{rising} rising, {falling} falling.", None))
    return lines


def hotspots(raw, ctx):
    lines, now = [], ctx['now']
    snap = (raw.get('snapshot') or {}).get('hotspots')
    if snap:
        days = [d for d in snap['days'] if d.get('count') is not None][-7:]
        if days:
            last = days[-1]
            lines.append((f"Satellite fire detections (NASA FIRMS VIIRS, regional box) on {last['date']}: "
                          f"{last['count']:,}; the 2019–2024 median for that date is {last['median']:.0f} "
                          f"(~{last['count'] / max(last['median'], 1):.0f}× usual). Last 7 days: "
                          + ', '.join(f"{d['date'][5:]} {d['count']:,}" for d in days), None))
    reg = raw.get('regional')
    if reg and reg.get('hotspots'):
        pts, at = reg['hotspots']['points'], reg['hotspots']['at']
        groups = {}
        for lon, lat in pts:
            groups.setdefault(area(lat, lon), []).append((lat, lon))
        lines.append((f"haze.gov.sg hotspot map, captured {sgt(at)} SGT ({ago(at, now)}): {len(pts)} hotspots. "
                      "Cloud can hide hotspots, so low counts are not proof of few fires.", None))
        for name, members in sorted(groups.items(), key=lambda g: -len(g[1])):
            near = min(members, key=lambda p: km(SG, p))
            c = (statistics.mean(p[0] for p in members), statistics.mean(p[1] for p in members))
            lines.append((f"  {name}: {len(members)} hotspots; centre ~{km(SG, c):.0f} km {compass(bearing(SG, c))} "
                          f"of Singapore; nearest {km(SG, near):.0f} km {compass(bearing(SG, near))}.",
                          {'area': name, 'count': len(members), 'centre': c}))
        ctx['hotspot_groups'] = {n: m for n, m in groups.items() if len(m) >= 20}
    hist = raw.get('regional_history')
    if hist:
        seen = {}
        for d in hist['days']:
            h = d.get('hotspots') or {}
            if h.get('at'):
                seen[h['at']] = len(h.get('points', []))
        lines.append(("haze.gov.sg hotspot counts by capture: " + ', '.join(f"{sgt(a, '%d %b')} {n}" for a, n in sorted(seen.items())), None))
    if reg and reg.get('haze'):
        at = reg['haze']['at']
        for poly in reg['haze']['polygons']:
            ring = poly['rings'][0]
            near = min(km(SG, (la, lo)) for lo, la in ring)
            centre = (statistics.mean(p[1] for p in ring), statistics.mean(p[0] for p in ring))
            where = 'COVERS Singapore' if inside(SG, ring) else f"edge ~{near:.0f} km from Singapore, centred {compass(bearing(SG, centre))}"
            lines.append((f"Satellite haze outline \"{poly['name']}\" ({sgt(at)} SGT, {ago(at, now)}): {where}.", None))
    return lines or [('Hotspot data: unavailable in this snapshot.', None)]


def wind(raw, ctx):
    lines, now = [], ctx['now']
    sp, di = raw.get('wind_speed'), raw.get('wind_direction')
    if sp and di:
        s_read, d_read = sp['data']['readings'][0], di['data']['readings'][0]
        speeds = {x['stationId']: x['value'] for x in s_read['data']}
        dirs = {x['stationId']: x['value'] for x in d_read['data']}
        both = [(speeds[k], dirs[k]) for k in speeds if k in dirs]
        if both:
            u = sum(s * math.sin(math.radians(d)) for s, d in both)
            v = sum(s * math.cos(math.radians(d)) for s, d in both)
            mean_dir = (math.degrees(math.atan2(u, v)) + 360) % 360
            lines.append((f"Measured station wind in Singapore at {sgt(s_read['timestamp'], '%H:%M')} ({len(both)} stations, "
                          f"10-min means): median {statistics.median(s for s, _ in both):.1f} knots, mostly from the "
                          f"{compass(mean_dir)} ({mean_dir:.0f}°). Light winds mean smoke disperses slowly.", None))
    g = raw.get('gfs_wind')
    if g:
        lines.append((f"Modelled surface (10 m) wind, NOAA GFS run {sgt(g['modelRunAt'])} SGT. This is a model, not an "
                      "observation, and surface wind is only a rough guide to how smoke moves higher up.", None))
        groups = ctx.get('hotspot_groups', {})
        for name, point in g['points'].items():
            parts, aligned = [], []
            for s in point['samples']:
                if 'u' not in s:
                    continue
                speed = math.hypot(s['u'], s['v']) * 3.6
                to = (math.degrees(math.atan2(s['u'], s['v'])) + 360) % 360
                parts.append(f"{sgt(s['at'], '%H:%M')} from {compass((to + 180) % 360)} {speed:.0f} km/h")
                if name != 'Singapore':
                    gap = angle_gap(to, bearing((point['lat'], point['lon']), SG))
                    aligned.append(gap)
            text = f"{name}: " + '; '.join(parts)
            if aligned:
                toward = sum(1 for a in aligned if a <= 30)
                text += (f". Wind here points toward Singapore (within 30°) in {toward} of {len(aligned)} time steps."
                         if toward else '. Wind here does not point toward Singapore in the next 24 h.')
            lines.append((text, None))
        if groups:
            lines.append(("Hotspot areas with 20+ hotspots: " + ', '.join(groups) + ". Compare each with the nearest wind point above.", None))
    f = raw.get('forecast_24h')
    if f:
        gen = f['data']['records'][0]['general']
        w = gen['wind']
        lines.append((f"NEA 24-hour forecast ({gen['validPeriod']['text']}): wind {w['direction']} "
                      f"{w['speed']['low']}–{w['speed']['high']} km/h; {gen['forecast']['text']}.", None))
    return lines or [('Wind data: unavailable in this snapshot.', None)]


def weather(raw, ctx):
    lines = []
    f = raw.get('forecast_24h')
    if f:
        for p in f['data']['records'][0]['periods']:
            texts = sorted({r['text'] for r in p['regions'].values()})
            lines.append((f"Forecast {p['timePeriod']['text']}: {' / '.join(texts)}.", None))
    snap = raw.get('snapshot')
    if snap:
        for o in snap['details'].get('outlook', [])[:3]:
            lines.append((f"4-day outlook {o['day']}: {o['summary']}, {o['low']}–{o['high']}°C.", None))
    if lines:
        lines.append(('Rain can wash particles out locally for a while; it does not stop the fires.', None))
    return lines


# ---------- what stands out this hour ----------

WIND_FOR_AREA = {  # Which sampled wind point speaks for each hotspot area (sources.WIND_POINTS)
    'Southern Sumatra (Jambi, South Sumatra, Lampung, Bangka)': ['Jambi', 'South Sumatra (Palembang)'],
    'Central and northern Sumatra (Riau, West/North Sumatra)': ['Riau (Pekanbaru)'],
    'Borneo (Kalimantan, Sarawak, Sabah)': ['West Kalimantan (Pontianak)'],
}


def band_index(value, bounds):
    return sum(value >= b for b in bounds)


def signals(raw, ctx, w):
    """Yield (signal name, strength 1-2, text). Each test reads raw data directly so it stays
    independent of how the other sections word things."""
    now = ctx['now']
    out = raw.get('haze_outlook') or {}
    r = out.get('report')
    if r:
        age_h = (now - t(r['issuedAt'])).total_seconds() / 3600
        if age_h <= w['outlook_new']['within_hours']:
            yield 'outlook_new', 1, (f"NEA published a new haze outlook {ago(r['issuedAt'], now)}: \"{r['forecast']}\" for "
                                     f"{relative_day(r['forecastDay'], now) if r.get('forecastDay') else 'the coming period'}.")
        prev = ((ctx.get('previous') or {}).get('haze_outlook') or {}).get('report')
        if prev and prev['issuedAt'] != r['issuedAt'] and prev['forecast'] != r['forecast']:
            yield 'outlook_changed', 1, f"NEA's forecast changed from \"{prev['forecast']}\" to \"{r['forecast']}\"."
    pm_bounds, psi_bounds = (56, 151, 251), (51, 101, 201, 301)
    latest_values, moves, crossings = {}, {'rose': [], 'fell': []}, []
    for region in REGIONS:
        pm, psi = series(raw, 'pm25', region), series(raw, 'psi', region)
        if not pm:
            continue
        at, v = pm[-1]
        latest_values[region] = v
        for h in (1, 3):
            old = value_at(pm, at - timedelta(hours=h))
            if old is not None and abs(v - old) >= w['pm25_jump']['delta']:
                moves['rose' if v > old else 'fell'].append((abs(v - old), f"{region} {old}→{v} in {h} h"))
                break
        old3 = value_at(pm, at - timedelta(hours=3))
        if old3 is not None and band_index(v, pm_bounds) != band_index(old3, pm_bounds):
            crossings.append(f"{region} {pm25_band(old3).split(' (')[0]}→{pm25_band(v).split(' (')[0]}")
        if psi:
            old6 = value_at(psi, psi[-1][0] - timedelta(hours=6))
            if old6 is not None and band_index(psi[-1][1], psi_bounds) != band_index(old6, psi_bounds):
                yield 'psi_band_change', 1, f"{region.title()}: 24-hr PSI moved from {psi_band(old6)} to {psi_band(psi[-1][1])} within 6 h."
            # Advice level three hours ago, from the same rule the app uses.
            pm3, psi3 = value_at(pm, at - timedelta(hours=3)), value_at(psi, psi[-1][0] - timedelta(hours=3))
            now_level = advice_level(raw, region)
            if pm3 is not None and psi3 is not None and now_level:
                then = max(band_index(pm3, pm_bounds), [0, 0, 1, 2, 3][band_index(psi3, psi_bounds)])
                if then != now_level[0]:
                    yield 'advice_change', 1, f"{region.title()}: the app's advice changed from \"{ADVICE[then][0]}\" to \"{ADVICE[now_level[0]][0]}\" within 3 h."
        if (now - at) > timedelta(minutes=w['stale_data']['minutes']) and region == REGIONS[0]:
            yield 'stale_data', 1, f"Latest Singapore reading is {ago(at, now)}; describe it as of {sgt(at, '%H:%M')}, not 'now'."
    for direction, items in moves.items():
        if items:
            items.sort(reverse=True)
            breadth = 'across most of the island' if len(items) >= 3 else 'in ' + ' and '.join(i[1].split()[0] for i in items)
            # Strength grows with the size of the largest move and with how many regions moved.
            strength = min(items[0][0] / w['pm25_jump']['delta'] + 0.25 * (len(items) - 1), 2)
            yield 'pm25_jump', strength, f"1-hr PM2.5 {direction} sharply {breadth}: " + '; '.join(i[1] for i in items) + '.'
    if crossings:
        yield 'pm25_band_change', 1, '1-hr PM2.5 crossed an NEA band within 3 h: ' + '; '.join(crossings) + '.'
    if latest_values:
        hi, lo = max(latest_values, key=latest_values.get), min(latest_values, key=latest_values.get)
        if latest_values[hi] - latest_values[lo] >= w['regional_spread']['delta']:
            yield 'regional_spread', 1, (f"Big difference across the island: {hi} {latest_values[hi]} vs {lo} {latest_values[lo]} "
                                        "(1-hr PM2.5); where you are matters.")
    reg = raw.get('regional') or {}
    for poly in (reg.get('haze') or {}).get('polygons', []):
        ring = poly['rings'][0]
        if inside(SG, ring):
            yield 'haze_over_sg', 1, f"A satellite \"{poly['name']}\" outline covers Singapore ({ago(reg['haze']['at'], now)})."
        elif 'dense' in poly['name'].lower():
            near = min(km(SG, (la, lo)) for lo, la in ring)
            if near <= w['dense_haze_near']['km']:
                yield 'dense_haze_near', 1, f"Dense haze outline about {near:.0f} km away ({ago(reg['haze']['at'], now)})."
    days = [d for d in ((raw.get('snapshot') or {}).get('hotspots') or {}).get('days', []) if d.get('count') is not None]
    if days and days[-1]['median']:
        ratio = days[-1]['count'] / days[-1]['median']
        if ratio >= w['fires_anomaly']['ratio']:
            yield 'fires_anomaly', min(ratio / w['fires_anomaly']['ratio'], 2), \
                f"Regional fire detections are about {ratio:.0f}× the 2019–2024 norm for the date."
    gfs, groups = raw.get('gfs_wind'), ctx.get('hotspot_groups', {})
    if gfs:
        for area_name, points in WIND_FOR_AREA.items():
            if area_name not in groups:
                continue
            best = 0
            for name in points:
                p = gfs['points'].get(name)
                steps = [s for s in (p or {}).get('samples', []) if 'u' in s]
                if steps:
                    toward = sum(angle_gap((math.degrees(math.atan2(s['u'], s['v'])) + 360) % 360,
                                           bearing((p['lat'], p['lon']), SG)) <= 30 for s in steps)
                    best = max(best, toward / len(steps))
            if best >= w['wind_from_fires']['share']:
                yield 'wind_from_fires', 1, (f"Modelled wind over {area_name} ({len(groups[area_name])} hotspots) points toward "
                                            f"Singapore {best:.0%} of the next 24 h.")
    f2 = raw.get('forecast_2h')
    if f2:
        item = f2['data']['items'][0]
        wet = [x['area'] for x in item['forecasts'] if any(k in x['forecast'] for k in ('Shower', 'Rain', 'Thunder'))]
        if wet:
            yield 'rain_now', 1, f"Showers forecast {item['valid_period']['text']} in {len(wet)} areas (e.g. {', '.join(wet[:3])})."


def standout(raw, ctx):
    weights = json.loads((Path(__file__).parent / 'weights.json').read_text())
    fired = sorted(((weights[name]['weight'] * strength, name, text) for name, strength, text in signals(raw, ctx, weights)),
                   key=lambda x: -x[0])
    changes = [f for f in fired if weights[f[1]]['kind'] == 'change']
    conditions = [f for f in fired if weights[f[1]]['kind'] == 'condition']
    if changes and changes[0][0] >= weights['lead_threshold']:
        lead = 'Something changed this hour: lead with the top change, and use conditions only to explain it.'
    elif conditions:
        lead = 'No big change this hour: lead with the top standing condition and keep the tone steady.'
    else:
        lead = 'Quiet hour: nothing notable. Keep the briefing short and calm.'
    lines = [(lead, None)]
    for label, group in (('Change', changes), ('Condition', conditions)):
        for score, name, text in group:
            lines.append((f"{label} (score {score:.1f}, {name}): {text}", {'signal': name, 'score': round(score, 2)}))
    return lines


SECTIONS = [
    ('N', 'Official NEA haze outlook', nea_outlook),
    ('L', 'Singapore air right now', singapore_now),
    ('A', 'Health advisory shown in the app', advisory),
    ('R', 'Regional stations (Malaysia, Indonesia)', regional_stations),
    ('H', 'Fires and haze across the region', hotspots),
    ('W', 'Wind', wind),
    ('F', 'Weather forecast', weather),
]


def load_raw(folder):
    manifest = json.loads((folder / 'manifest.json').read_text())
    raw = {name: json.loads((folder / 'raw' / f'{name}.json').read_text())
           for name, info in manifest['sources'].items() if info['status'] == 'ok'}
    return manifest, raw


def previous_raw(folder, now):
    """The most recent earlier snapshot within 6 h, for 'changed since last time' signals."""
    best = None
    for other in folder.parent.iterdir():
        if other == folder or not (other / 'manifest.json').exists():
            continue
        at = t(json.loads((other / 'manifest.json').read_text())['collectedAt'])
        if timedelta(minutes=20) <= now - at <= timedelta(hours=6) and (best is None or at > best[0]):
            best = (at, other)
    return load_raw(best[1])[1] if best else None


def build(folder):
    folder = Path(folder)
    manifest, raw = load_raw(folder)
    ctx = {'now': t(manifest['collectedAt']), 'place': manifest['place']}
    ctx['previous'] = previous_raw(folder, ctx['now'])
    facts, blocks = {}, {}
    # Standout runs last (it uses hotspot groups from the H section) but is printed first.
    for prefix, title, section in SECTIONS + [('S', 'What stands out this hour', standout)]:
        lines = [f'## {title}']
        items = section(raw, ctx)
        if prefix == 'S':
            lines.append(items[0][0])
            items = items[1:]
        for n, (text, fact) in enumerate(items, 1):
            lines.append(f'[{prefix}{n}] {text}')
            if fact is not None:
                facts[f'{prefix}{n}'] = fact
        blocks[prefix] = lines + ['']
    out = [clock(raw, ctx)[0][0]] + [''] + blocks.pop('S') + [line for b in blocks.values() for line in b]
    missing = [n for n, i in manifest['sources'].items() if i['status'] != 'ok']
    if missing:
        out.append('Sources that failed in this snapshot: ' + ', '.join(missing) + '.')
    text = '\n'.join(out).strip() + '\n'
    (folder / 'digest.md').write_text(text)
    (folder / 'digest.json').write_text(json.dumps(facts, indent=1, default=str, ensure_ascii=False))
    return text


if __name__ == '__main__':
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else max((Path(__file__).parent / 'snapshots').iterdir())
    print(build(target))
