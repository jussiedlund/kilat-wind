"""Collect one dated snapshot of everything the haze briefing may draw on.

Each source is a small function registered in SOURCES. To add a source, write a
function that returns parsed JSON (or raises) and add it to the dict; the
digest decides what, if anything, the model sees from it.
"""
import json
import math
import subprocess
import tempfile
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / 'scripts'))
import wind_contract  # noqa: E402  (shared GFS decoder, same contract as the app)

SGT = timezone(timedelta(hours=8))
WORKER = 'https://sg-map-lens-data.sgenv.workers.dev'
DATA_GOV = 'https://api-open.data.gov.sg/v2/real-time/api/'
GFS = 'https://kilat-wind.pages.dev/latest-v2.json'

# Keppel Bay, the experiments' reference place. Region is a caller choice.
PLACE = {'name': 'Keppel Bay', 'lat': 1.2645, 'lon': 103.8210, 'region': 'south'}

# Points where the GFS surface wind is sampled: Singapore plus the usual smoke
# source areas and the corridor between. Grid decoding stays in wind_contract.
WIND_POINTS = {
    'Singapore': (1.35, 103.82),
    'Riau (Pekanbaru)': (0.51, 101.45),
    'Jambi': (-1.61, 103.61),
    'South Sumatra (Palembang)': (-2.98, 104.76),
    'Malacca Strait (mid)': (1.9, 101.9),
    'Johor (north of Singapore)': (2.0, 103.8),
    'West Kalimantan (Pontianak)': (-0.03, 109.33),
}
WIND_HOURS = (0, 3, 6, 9, 12, 18, 24)


def fetch(url, retries=0):
    """curl, not urllib: data.gov.sg answered urllib with 403 in earlier trials."""
    for attempt in range(retries + 1):
        out = subprocess.run(['curl', '-sS', '--fail', '-m', '40', '-A', 'SGMapLens-experiment/1.0', url],
                             capture_output=True)
        if out.returncode == 0:
            return json.loads(out.stdout)
        if attempt < retries and b'429' in out.stderr:
            time.sleep(8 * (attempt + 1))
            continue
        out.check_returncode()


def worker(path):
    return lambda now: fetch(WORKER + path)


def data_gov(name, days_back=None):
    # The unkeyed real-time API rate-limits bursts; pace calls and retry a 429.
    # days_back selects one SGT day of history (one page for PSI and PM2.5).
    def get(now):
        time.sleep(2)
        query = ''
        if days_back is not None:
            query = '?date=' + (now.astimezone(SGT) - timedelta(days=days_back)).strftime('%Y-%m-%d')
        return fetch(DATA_GOV + name + query, retries=2)
    return get


def gfs_wind(now):
    """Sample the 10 m model wind at fixed points instead of keeping the 700 KB grid."""
    raw = subprocess.run(['curl', '-sS', '--fail', '-m', '60', GFS], capture_output=True, check=True).stdout
    parsed = wind_contract.parse_model(raw)
    if parsed.status != 'available':
        raise ValueError(f'GFS product {parsed.status}: {parsed.reason}')
    model = parsed.data
    start = now.replace(minute=0, second=0, microsecond=0)
    samples = {}
    for name, (lat, lon) in WIND_POINTS.items():
        rows = []
        for hours in WIND_HOURS:
            at = start + timedelta(hours=hours)
            got = wind_contract.sample_model(model, at.isoformat(), lat, lon, now=now.isoformat())
            if got.status == 'available':
                rows.append({'at': at.isoformat(), 'u': round(got.data.u_ms, 2), 'v': round(got.data.v_ms, 2)})
            else:
                rows.append({'at': at.isoformat(), 'status': got.status, 'reason': got.reason})
        samples[name] = {'lat': lat, 'lon': lon, 'samples': rows}
    return {'source': model.source, 'modelRunAt': model.model_run_at.isoformat(),
            'expiresAt': model.expires_at.isoformat(), 'points': samples}


# Smoke travels at about 750 m, so the briefing also reads 925 hPa wind: the last 24 h from model
# analyses (f000/f003 of recent cycles) and the next 24 h from the newest forecast cycle.
NOMADS = 'https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl'
TRANSPORT_BOX = {'leftlon': 98, 'rightlon': 110, 'toplat': 6, 'bottomlat': -6}
TRANSPORT_PAUSE = 0.4  # seconds between NOMADS requests
MAX_GRIB_BYTES = 200_000


def nomads_url(cycle, hour):
    query = {'file': f'gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}', 'lev_925_mb': 'on', 'var_UGRD': 'on', 'var_VGRD': 'on',
             'subregion': '', **TRANSPORT_BOX, 'dir': f'/gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos'}
    return NOMADS + '?' + '&'.join(f'{k}={v}' for k, v in query.items())


def fetch_grib(cycle, hour):
    out = subprocess.run(['curl', '-sS', '--fail', '-m', '40', '-A', 'SGMapLens-experiment/1.0', nomads_url(cycle, hour)],
                         capture_output=True)
    out.check_returncode()
    if not out.stdout.startswith(b'GRIB') or len(out.stdout) > MAX_GRIB_BYTES:
        raise ValueError(f'NOMADS gave no usable GRIB for {cycle:%Y-%m-%d %Hz} f{hour:03d}')
    return out.stdout


def decode_uv(data):
    """Return {'u': grid, 'v': grid} where grid = (lats, lons, values by row) for the 925 hPa fields."""
    import eccodes as ec  # imported here so the other sources run without it
    fields = {}
    with tempfile.TemporaryFile() as handle:
        handle.write(data)
        handle.seek(0)
        while (gid := ec.codes_grib_new_from_file(handle)) is not None:
            try:
                name = ec.codes_get(gid, 'shortName')
                if name in ('u', 'v') and ec.codes_get(gid, 'level') == 925 and ec.codes_get(gid, 'gridType') == 'regular_ll':
                    ni, nj = ec.codes_get(gid, 'Ni'), ec.codes_get(gid, 'Nj')
                    la1, la2 = ec.codes_get(gid, 'latitudeOfFirstGridPointInDegrees'), ec.codes_get(gid, 'latitudeOfLastGridPointInDegrees')
                    lo1, lo2 = ec.codes_get(gid, 'longitudeOfFirstGridPointInDegrees'), ec.codes_get(gid, 'longitudeOfLastGridPointInDegrees')
                    values = ec.codes_get_values(gid)
                    if len(values) != ni * nj or ni < 2 or nj < 2:
                        raise ValueError('unexpected 925 hPa grid size')
                    lats = [la1 + (la2 - la1) * j / (nj - 1) for j in range(nj)]
                    lons = [lo1 + (lo2 - lo1) * i / (ni - 1) for i in range(ni)]
                    fields[name] = (lats, lons, [list(values[j * ni:(j + 1) * ni]) for j in range(nj)])
            finally:
                ec.codes_release(gid)
    if set(fields) != {'u', 'v'}:
        raise ValueError('925 hPa U and V fields not both present')
    return fields


def bilinear(grid, lat, lon):
    lats, lons, rows = grid

    def locate(axis, x):
        for k in range(len(axis) - 1):
            lo, hi = axis[k], axis[k + 1]
            if min(lo, hi) <= x <= max(lo, hi):
                return k, (x - lo) / (hi - lo)
        raise ValueError('point outside the transport grid')
    j, fy = locate(lats, lat)
    i, fx = locate(lons, lon)
    value = (rows[j][i] * (1 - fx) * (1 - fy) + rows[j][i + 1] * fx * (1 - fy)
             + rows[j + 1][i] * (1 - fx) * fy + rows[j + 1][i + 1] * fx * fy)
    if not math.isfinite(value) or abs(value) > 100:
        raise ValueError('implausible 925 hPa wind value')
    return value


def transport_plan(now, cycle):
    """(valid time, cycle, forecast hour, kind) for each 3-hourly instant from 24 h back to 24 h ahead.
    Instants the newest cycle's analysis (f000/f003) covers use analyses; later ones use its forecast."""
    first = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=24)
    first -= timedelta(hours=first.hour % 3)
    plan, at = [], first
    while at <= now + timedelta(hours=24):
        if at <= cycle + timedelta(hours=3):
            base = at if at.hour % 6 == 0 else at - timedelta(hours=3)
            plan.append((at, base, int((at - base).total_seconds() // 3600), 'analysis'))
        else:
            plan.append((at, cycle, int((at - cycle).total_seconds() // 3600), 'forecast'))
        at += timedelta(hours=3)
    return plan


def newest_cycle(now):
    """The newest 6-hourly cycle whose analysis file is already published; a cycle takes about 4 h."""
    cycle = now - timedelta(hours=3, minutes=30)
    cycle = cycle.replace(hour=cycle.hour // 6 * 6, minute=0, second=0, microsecond=0)
    for _ in range(2):
        try:
            return cycle, fetch_grib(cycle, 0)
        except subprocess.CalledProcessError:
            cycle -= timedelta(hours=6)
    raise ValueError('no published GFS cycle found on NOMADS')


def gfs_transport(now):
    """925 hPa U/V at the WIND_POINTS: about 9 analysis and 9 forecast instants, 3-hourly, newest cycle first.
    One NOMADS request per distinct file, at most about 20 per run, sequential with a short pause."""
    cycle, first_file = newest_cycle(now)
    files = {(cycle, 0): first_file}
    plan = transport_plan(now, cycle)
    if len(set((c, h) for _, c, h, _ in plan)) > 22:
        raise ValueError('transport plan needs too many requests')
    grids = {}
    for _, base, hour, _ in plan:
        if (base, hour) not in files:
            time.sleep(TRANSPORT_PAUSE)
            files[(base, hour)] = fetch_grib(base, hour)
        if (base, hour) not in grids:
            grids[(base, hour)] = decode_uv(files[(base, hour)])
    points = {}
    for name, (lat, lon) in WIND_POINTS.items():
        rows = []
        for at, base, hour, kind in plan:
            grid = grids[(base, hour)]
            rows.append({'at': at.isoformat(), 'u': round(float(bilinear(grid['u'], lat, lon)), 2),
                         'v': round(float(bilinear(grid['v'], lat, lon)), 2), 'kind': kind})
        points[name] = {'lat': lat, 'lon': lon, 'samples': rows}
    return {'source': 'NOAA GFS 0.25 degree, 925 hPa wind', 'level': '925 hPa', 'cycleAt': cycle.isoformat(),
            'requests': len(files), 'points': points}


def kilat_outlook(now):
    """The experimental haze outlook without its map frames and trajectories, which the digest never reads."""
    data = fetch(WORKER + '/v1/haze')
    for heavy in ('smokeMap', 'air'):
        data.pop(heavy, None)
    return data


SOURCES = {
    # Singapore hourly PM2.5 and PSI straight from NEA, today and yesterday (SGT days).
    'pm25_today': data_gov('pm25', 0),
    'pm25_yesterday': data_gov('pm25', 1),
    'psi_today': data_gov('psi', 0),
    'psi_yesterday': data_gov('psi', 1),
    # The app's own snapshot: hotspot baseline, 4-day outlook, and a fallback for the readings above.
    'snapshot': worker(f"/v1/snapshot?lat={PLACE['lat']}&lon={PLACE['lon']}"),
    # NEA's daily haze bulletin, parsed by the backend.
    'haze_outlook': worker('/v1/haze-outlook'),
    # Kilat's experimental smoke and PM2.5 outlook (research model, issued 06 and 18 UTC).
    'kilat_outlook': kilat_outlook,
    # Latest hotspot points and haze polygons from haze.gov.sg.
    'regional': worker('/v1/regional'),
    # Seven days of hotspot captures, for "more or fewer than recent days".
    'regional_history': worker('/v1/regional/history'),
    # Malaysian and Indonesian PM2.5 stations within 400 km.
    'regional_air': worker('/v1/regional/air-quality'),
    'regional_air_history': worker('/v1/regional/air-quality/history?days=2'),
    # Measured station wind (10-minute means) and the official 24-hour outlook.
    'wind_speed': data_gov('wind-speed'),
    'wind_direction': data_gov('wind-direction'),
    'forecast_24h': data_gov('twenty-four-hr-forecast'),
    'forecast_2h': data_gov('two-hr-forecast'),
    # Modelled surface wind along the smoke corridor.
    'gfs_wind': gfs_wind,
    # Modelled wind at 925 hPa (about 750 m, where smoke travels): last 24 h and next 24 h.
    'gfs_transport': gfs_transport,
}


def collect(out_root=ROOT / 'snapshots', only=None):
    now = datetime.now(timezone.utc)
    folder = out_root / now.astimezone(SGT).strftime('%Y-%m-%dT%H%M')
    raw_dir = folder / 'raw'
    raw_dir.mkdir(parents=True, exist_ok=True)
    manifest = {'collectedAt': now.isoformat(), 'place': PLACE, 'sources': {}}
    for name, source in SOURCES.items():
        if only and name not in only:
            continue
        started = datetime.now(timezone.utc)
        try:
            data = source(now)
            (raw_dir / f'{name}.json').write_text(json.dumps(data, ensure_ascii=False, indent=1))
            manifest['sources'][name] = {'status': 'ok', 'seconds': round((datetime.now(timezone.utc) - started).total_seconds(), 1)}
        except (subprocess.CalledProcessError, ValueError, OSError, ImportError) as error:
            detail = error.stderr.decode(errors='replace').strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            manifest['sources'][name] = {'status': 'failed', 'error': detail[:300]}
        print(f"  {name:22} {manifest['sources'][name]['status']}")
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    return folder


if __name__ == '__main__':
    print(collect())
