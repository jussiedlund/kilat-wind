"""Collect one dated snapshot of everything the haze briefing may draw on.

Each source is a small function registered in SOURCES. To add a source, write a
function that returns parsed JSON (or raises) and add it to the dict; the
digest decides what, if anything, the model sees from it.
"""
import json
import subprocess
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
        except (subprocess.CalledProcessError, ValueError, OSError) as error:
            detail = error.stderr.decode(errors='replace').strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            manifest['sources'][name] = {'status': 'failed', 'error': detail[:300]}
        print(f"  {name:22} {manifest['sources'][name]['status']}")
    (folder / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    return folder


if __name__ == '__main__':
    print(collect())
