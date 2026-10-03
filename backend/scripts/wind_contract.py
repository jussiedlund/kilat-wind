"""Off-app wind contract; no fetching, publishing, rendering or source blending.

NEA directions stay raw degrees: a direction-from convention is not asserted here.
GFS u/v are eastward/northward m/s as supplied by the schema1 producer.
All query instants require timezone-aware full ISO dates. Bounds are defensive
contract limits, not claims about station sensor range or source freshness.
"""
import base64
import bisect
from dataclasses import dataclass
import datetime as dt
import json
import math
import re
import struct

UTC = dt.timezone.utc
MAX_BYTES = 2_000_000
MAX_STATIONS = 500
MAX_ROWS = 2000
MAX_VALUES = 100_000
ORDER = 'south-to-north rows, west-to-east columns'
ENCODING = 'base64 of little-endian signed int16; divide by 10 for m/s'
HOURS = (0, 3, 6, 9, 12)


@dataclass(frozen=True)
class Result:
    status: str
    data: object = None
    reason: str = ''


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Observation:
    station: Station
    observed_at: dt.datetime
    speed_source_at: str
    direction_source_at: str
    speed_knots: float
    speed_ms: float
    direction_degrees: float
    speed_unit: str
    direction_unit: str
    speed_reading_type: str | None
    direction_reading_type: str | None
    identity: str = 'NEA station observation'


@dataclass(frozen=True)
class Frame:
    valid_at: dt.datetime
    u: tuple
    v: tuple


@dataclass(frozen=True)
class Model:
    source: str
    model_run_at: dt.datetime
    generated_at: dt.datetime
    expires_at: dt.datetime
    frames: tuple
    width: int = 101
    height: int = 81
    south: float = -8.0
    west: float = 95.0
    step: float = 0.25
    identity: str = 'GFS model forecast'


@dataclass(frozen=True)
class ModelSample:
    u_ms: float
    v_ms: float
    target_at: dt.datetime
    valid_from: dt.datetime
    valid_to: dt.datetime
    model_run_at: dt.datetime
    generated_at: dt.datetime
    expires_at: dt.datetime
    source: str
    identity: str = 'GFS model forecast'


def instant(value):
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(
            r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        raise ValueError('full timezone-aware ISO timestamp required')
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timezone required')
    return parsed.astimezone(UTC)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('finite numeric value required')
    return value


def load_payload(payload):
    if isinstance(payload, (str, bytes)):
        if len(payload) > MAX_BYTES:
            raise ValueError('payload too large')
        payload = json.loads(payload)
    # Also bound already-decoded inputs; reject non-JSON types and NaN.
    if len(json.dumps(payload, allow_nan=False).encode('utf-8')) > MAX_BYTES:
        raise ValueError('payload too large')
    if not isinstance(payload, dict):
        raise ValueError('object required')
    return payload


def bounded_list(value, limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError('invalid or oversized array')
    return value


def unique(mapping, key, value):
    """Identical duplicates collapse; any conflict stays invalid, order independently."""
    if key not in mapping:
        mapping[key] = value
    elif mapping[key] != value:
        mapping[key] = None


def parse_feed(payload, kind):
    payload = load_payload(payload)
    if 'data' in payload:
        if payload.get('code') != 0 or not isinstance(payload['data'], dict):
            raise ValueError('unsuccessful source response')
        payload = payload['data']
    unit = payload.get('readingUnit')
    if unit not in ({'knots'} if kind == 'speed' else {'degrees'}):
        raise ValueError('unsupported source unit')
    stations = {}
    for raw in bounded_list(payload.get('stations'), MAX_STATIONS):
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not raw['id']:
            continue
        key = raw['id']
        try:
            loc = raw['location']
            lat, lon = number(loc['latitude']), number(loc['longitude'])
            name = raw['name']
            if abs(lat) > 90 or abs(lon) > 180 or not isinstance(name, str) or not name:
                raise ValueError('invalid metadata')
            station = Station(key, name, lat, lon)
        except (KeyError, TypeError, ValueError):
            station = None
        unique(stations, key, station)
    values = {}
    count = 0
    for raw in bounded_list(payload.get('readings'), MAX_ROWS):
        if not isinstance(raw, dict):
            continue
        try:
            at = instant(raw.get('timestamp'))
        except (ValueError, OverflowError):
            continue
        rows = bounded_list(raw.get('data'), MAX_STATIONS)
        count += len(rows)
        if count > MAX_VALUES:
            raise ValueError('too many readings')
        for reading in rows:
            if not isinstance(reading, dict) or not isinstance(reading.get('stationId'), str):
                continue
            key = (reading['stationId'], at)
            try:
                value = number(reading.get('value'))
                if not 0 <= value <= (200 if kind == 'speed' else 360):
                    raise ValueError('out of bounds')
                # Raw source timestamps are preserved; equivalent ISO representations
                # count as duplicate values, with lexicographic timestamp selection.
                entry = (value, raw['timestamp'])
            except ValueError:
                entry = None
            if key in values and values[key] is not None and entry is not None and values[key][0] == entry[0]:
                values[key] = min(values[key], entry)
            else:
                unique(values, key, entry)
    reading_type = payload.get('readingType')
    if reading_type is not None and not isinstance(reading_type, str):
        raise ValueError('invalid source reading type')
    return stations, values, unit, reading_type


def pair_observations(speed_payload, direction_payload):
    """Parse one NEA page per feed; retain only same-station, same-instant pairs."""
    try:
        speeds, sv, su, st = parse_feed(speed_payload, 'speed')
        directions, dv, du, dtyp = parse_feed(direction_payload, 'direction')
        observations = []
        for (station_id, at), speed in sorted(sv.items()):
            direction = dv.get((station_id, at))
            station = speeds.get(station_id)
            if station is None or station != directions.get(station_id) or speed is None or direction is None:
                continue
            observations.append(Observation(station, at, speed[1], direction[1], speed[0],
                                            speed[0] * 1852 / 3600, direction[0], su, du, st, dtyp))
        return Result('available' if observations else 'missing', tuple(observations))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        return Result('invalid', reason=str(error))


def sample_observations(observations, target_at, *, max_age_seconds):
    """Latest paired observation at/before target per station, inclusive age bound."""
    try:
        target = instant(target_at)
        age = number(max_age_seconds)
        if not 0 <= age <= 86400:
            raise ValueError('observation age must be between 0 and 86400 seconds')
        selected = {}
        for obs in observations:
            if 0 <= (target - obs.observed_at).total_seconds() <= age:
                old = selected.get(obs.station.id)
                if old is None or old.observed_at < obs.observed_at:
                    selected[obs.station.id] = obs
        return Result('available' if selected else 'missing', tuple(selected[key] for key in sorted(selected)))
    except (ValueError, TypeError, OverflowError) as error:
        return Result('invalid', reason=str(error))


def parse_model(payload):
    """Validate exactly the existing build-wind.py schema1 product, fail closed."""
    try:
        raw = load_payload(payload)
        if type(raw.get('schemaVersion')) is not int or raw['schemaVersion'] != 1 or raw.get('encoding') != ENCODING:
            raise ValueError('unsupported schema or encoding')
        if raw.get('source') != 'NOAA/NCEP GFS 0.25 degree, 10 m wind':
            raise ValueError('unsupported model source')
        want = {'south': -8.0, 'north': 12.0, 'west': 95.0, 'east': 120.0,
                'stepDegrees': 0.25, 'width': 101, 'height': 81, 'order': ORDER}
        grid = raw.get('grid')
        if not isinstance(grid, dict) or any(grid.get(k) != v for k, v in want.items()):
            raise ValueError('unsupported grid or order')
        if type(grid['width']) is not int or type(grid['height']) is not int:
            raise ValueError('grid dimensions must be integers')
        run, generated, expiry = (instant(raw[k]) for k in ('modelRunAt', 'generatedAt', 'expiresAt'))
        if run.minute or run.second or run.microsecond or run.hour % 6:
            raise ValueError('unsupported model cycle')
        if not run <= generated < expiry or expiry != run + dt.timedelta(hours=12):
            raise ValueError('invalid publication times')
        frames = []
        for hour, frame in zip(HOURS, bounded_list(raw.get('frames'), len(HOURS))):
            if not isinstance(frame, dict) or type(frame.get('forecastHour')) is not int or frame['forecastHour'] != hour:
                raise ValueError('invalid frame sequence')
            valid = instant(frame['validAt'])
            if valid != run + dt.timedelta(hours=hour):
                raise ValueError('invalid frame time')
            components = []
            for key in ('u', 'v'):
                encoded = frame.get(key)
                if not isinstance(encoded, str) or len(encoded) != 21816:
                    raise ValueError('invalid encoded component length')
                packed = base64.b64decode(encoded, validate=True)
                if len(packed) != 101 * 81 * 2:
                    raise ValueError('invalid decoded component length')
                values = struct.unpack('<8181h', packed)
                if any(abs(value) > 1000 for value in values):
                    raise ValueError('out of bounds wind component')
                components.append(tuple(value / 10 for value in values))
            frames.append(Frame(valid, *components))
        if len(frames) != len(HOURS):
            raise ValueError('incomplete frame sequence')
        return Result('available', Model(raw['source'], run, generated, expiry, tuple(frames)))
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as error:
        return Result('invalid', reason=str(error))


def sample_model(model, target_at, latitude, longitude, *, now):
    """Bilinear spatial and adjacent-frame vector interpolation; no extrapolation.

Publication expiry uses actual now, independently of the requested map time.
A historically valid forecast is still a model forecast, never an observation.
"""
    try:
        target, actual = instant(target_at), instant(now)
        lat, lon = number(latitude), number(longitude)
        if actual >= model.expires_at:
            return Result('expired')
        if actual < model.generated_at:
            return Result('unavailable', reason='publication is future-dated')
        if not model.south <= lat <= 12 or not model.west <= lon <= 120:
            return Result('unsupported', reason='outside grid')
        times = [f.valid_at for f in model.frames]
        if target < times[0] or target > times[-1]:
            return Result('unsupported', reason='outside valid frames')
        index = bisect.bisect_left(times, target)
        first = last = model.frames[index]
        weight = 0.0
        if first.valid_at != target:
            last = first
            first = model.frames[index - 1]
            weight = (target - first.valid_at) / (last.valid_at - first.valid_at)
        x, y = (lon - model.west) / model.step, (lat - model.south) / model.step
        x0, y0 = min(math.floor(x), model.width - 2), min(math.floor(y), model.height - 2)
        dx, dy = x - x0, y - y0
        def spatial(values):
            a = y0 * model.width + x0
            return ((values[a] * (1 - dx) + values[a + 1] * dx) * (1 - dy) +
                    (values[a + model.width] * (1 - dx) + values[a + model.width + 1] * dx) * dy)
        u = spatial(first.u) * (1 - weight) + spatial(last.u) * weight
        v = spatial(first.v) * (1 - weight) + spatial(last.v) * weight
        return Result('available', ModelSample(u, v, target, first.valid_at, last.valid_at,
                      model.model_run_at, model.generated_at, model.expires_at, model.source))
    except (ValueError, TypeError, OverflowError) as error:
        return Result('invalid', reason=str(error))
