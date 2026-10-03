#!/usr/bin/env python3
"""Build one static, dated NOAA GFS 10 m wind document outside the Worker.

With --input, decode local GRIB files (one per forecast hour). Without it, fetch
the newest complete NOMADS cycle. An incomplete or expired cycle is never published.
"""
import argparse
import base64
import datetime as dt
import json
import math
import os
from pathlib import Path
import struct
import tempfile
import urllib.parse
import urllib.request

import eccodes as ec

UTC = dt.timezone.utc
HOURS = (0, 3, 6, 9, 12)
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX, STEP = -8.0, 12.0, 95.0, 120.0, 0.25
WIDTH, HEIGHT = 101, 81
MAX_GRIB_BYTES = 1_000_000
SOURCE = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


def instant(date, time):
    return dt.datetime.strptime(f"{int(date):08d}{int(time):04d}", "%Y%m%d%H%M").replace(tzinfo=UTC)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def grid_component(gid, expected):
    value = lambda key: ec.codes_get(gid, key)
    if (value("shortName") != expected or value("typeOfLevel") != "heightAboveGround" or
            value("level") != 10 or value("gridType") != "regular_ll"):
        raise ValueError("GRIB is not a 10 m U/V regular latitude-longitude field")
    grid = (value("Ni"), value("Nj"), value("latitudeOfFirstGridPointInDegrees"),
            value("longitudeOfFirstGridPointInDegrees"), value("latitudeOfLastGridPointInDegrees"),
            value("longitudeOfLastGridPointInDegrees"), value("iDirectionIncrementInDegrees"),
            value("jDirectionIncrementInDegrees"))
    want = (WIDTH, HEIGHT, LAT_MIN, LON_MIN, LAT_MAX, LON_MAX, STEP, STEP)
    if any(abs(a - b) > 0.00001 for a, b in zip(grid, want)) or any(
            value(key) != expected_value for key, expected_value in
            (("iScansNegatively", 0), ("jScansPositively", 1),
             ("jPointsAreConsecutive", 0), ("alternativeRowScanning", 0))):
        raise ValueError("unexpected GFS grid or scan direction")
    run = instant(value("dataDate"), value("dataTime"))
    valid = instant(value("validityDate"), value("validityTime"))
    numbers = ec.codes_get_values(gid)
    if len(numbers) != WIDTH * HEIGHT or any(not math.isfinite(x) or abs(x) > 100 for x in numbers):
        raise ValueError("missing, invalid or implausible wind component")
    # j grows northwards; each row runs west to east. 0.1 m/s resolution.
    packed = struct.pack("<" + "h" * len(numbers), *(round(float(x) * 10) for x in numbers))
    return run, valid, base64.b64encode(packed).decode("ascii")


def decode_grib(data, hour):
    if len(data) > MAX_GRIB_BYTES or not data.startswith(b"GRIB"):
        raise ValueError("GRIB response is missing or too large")
    with tempfile.TemporaryFile() as source:
        source.write(data)
        source.seek(0)
        fields = {}
        while (gid := ec.codes_grib_new_from_file(source)) is not None:
            try:
                name = ec.codes_get(gid, "shortName")
                if name in ("10u", "10v"):
                    if name in fields:
                        raise ValueError("duplicate wind field")
                    fields[name] = grid_component(gid, name)
                else:
                    raise ValueError("unexpected extra GRIB field")
            finally:
                ec.codes_release(gid)
    if set(fields) != {"10u", "10v"}:
        raise ValueError("both U and V components are required")
    run, valid, u = fields["10u"]
    v_run, v_valid, v = fields["10v"]
    if (run, valid) != (v_run, v_valid) or valid != run + dt.timedelta(hours=hour):
        raise ValueError("U/V cycles or forecast hours do not match")
    return run, {"forecastHour": hour, "validAt": iso(valid), "u": u, "v": v}


def gfs_url(run, hour):
    query = urllib.parse.urlencode({
        "file": f"gfs.t{run.hour:02d}z.pgrb2.0p25.f{hour:03d}",
        "lev_10_m_above_ground": "on", "var_UGRD": "on", "var_VGRD": "on",
        "subregion": "", "leftlon": LON_MIN, "rightlon": LON_MAX,
        "toplat": LAT_MAX, "bottomlat": LAT_MIN,
        "dir": f"/gfs.{run:%Y%m%d}/{run:%H}/atmos"
    })
    return f"{SOURCE}?{query}"


def fetch_cycle(run):
    files = {}
    for hour in HOURS:
        request = urllib.request.Request(gfs_url(run, hour), headers={"User-Agent": "KilatWind/1.0 (static public data export)"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(MAX_GRIB_BYTES + 1)
        files[hour] = data
    return files


def document(files, now):
    frames = []
    runs = set()
    for hour in HOURS:
        if hour not in files:
            raise ValueError(f"missing forecast hour {hour}")
        run, frame = decode_grib(files[hour], hour)
        runs.add(run)
        frames.append(frame)
    if len(runs) != 1:
        raise ValueError("forecast hours span different model cycles")
    run = runs.pop()
    expiry = run + dt.timedelta(hours=max(HOURS))
    if run > now + dt.timedelta(minutes=5) or now >= expiry:
        raise ValueError("model cycle is future-dated or has expired")
    return {"schemaVersion": 1, "source": "NOAA/NCEP GFS 0.25 degree, 10 m wind",
            "modelRunAt": iso(run), "generatedAt": iso(now), "expiresAt": iso(expiry),
            "grid": {"south": LAT_MIN, "north": LAT_MAX, "west": LON_MIN, "east": LON_MAX,
                     "stepDegrees": STEP, "width": WIDTH, "height": HEIGHT,
                     "order": "south-to-north rows, west-to-east columns"},
            "encoding": "base64 of little-endian signed int16; divide by 10 for m/s",
            "frames": frames}


def latest_complete(now):
    candidate = now - dt.timedelta(hours=3)
    candidate = candidate.replace(hour=(candidate.hour // 6) * 6, minute=0, second=0, microsecond=0)
    for offset in (0, 6):
        run = candidate - dt.timedelta(hours=offset)
        if now >= run + dt.timedelta(hours=max(HOURS)):
            continue
        try:
            return document(fetch_cycle(run), now)
        except (OSError, ValueError) as error:
            print(f"GFS {iso(run)} unavailable: {error}", file=__import__("sys").stderr)
    raise RuntimeError("no complete unexpired GFS cycle; keep the previous static publication")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", metavar="HOUR=FILE", help="use a local GRIB file; repeat for 0,3,6,9,12")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    now = dt.datetime.now(UTC)
    if args.input:
        files = {}
        for item in args.input:
            hour_text, path = item.split("=", 1)
            hour = int(hour_text)
            if hour not in HOURS or hour in files:
                parser.error("input forecast hours must be unique and among 0,3,6,9,12")
            files[hour] = Path(path).read_bytes()
        result = document(files, now)
    else:
        result = latest_complete(now)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(result, separators=(",", ":")).encode("utf-8")
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(body)
    os.replace(temporary, args.output)
    print(f"wrote {args.output} ({len(body)} bytes), model {result['modelRunAt']}, expires {result['expiresAt']}")


if __name__ == "__main__":
    main()
