#!/usr/bin/env python3
"""Count recent VIIRS fire detections per day from the keyless FIRMS 7-day CSV and print SQL for D1.

Source: NOAA-21 VIIRS first, NOAA-20 if NOAA-21 is unreachable or returns no rows (Suomi NPP data delivery
ended 2 Nov 2026). The source used is logged to stderr; stdout stays pure SQL. Exits 1 if both fail.
The 2019-2024 baseline is SNPP-based, so counts are comparable but not identical.

Same filter as aggregate.py (confidence nominal/high, lon 94-120E, lat 12S-8N) so counts compare with the
2019-2024 baseline. Only complete UTC days are kept (the newest, still-filling day is skipped).
The FIRMS CSV is NRT and not clipped to national borders, so it runs slightly above the country archives.

Run daily (the 7-day file means a missed run is covered for up to a week):
  Scheduled daily by .github/workflows/hotspots.yml.
"""
import csv, datetime, io, sys, urllib.request, collections
SOURCES = [
    ("NOAA-21", "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_SouthEast_Asia_7d.csv"),
    ("NOAA-20", "https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_SouthEast_Asia_7d.csv"),
]
BOX = (94.0, 120.0, -12.0, 8.0)
KEEP = {"n", "nominal", "h", "high"}
def fetch(url):
    text = urllib.request.urlopen(url, timeout=120).read().decode("utf-8", "replace")
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows: raise ValueError("CSV has no rows")
    return rows
rows = None
for name, url in SOURCES:
    try:
        rows = fetch(url)
        print("firms_recent: using %s (%d rows)" % (name, len(rows)), file=sys.stderr)
        break
    except Exception as e:
        print("firms_recent: %s failed: %s" % (name, e), file=sys.stderr)
if rows is None: sys.exit("firms_recent: all FIRMS sources failed")
c = collections.Counter()
for r in rows:
    if r["confidence"].strip().lower() not in KEEP: continue
    lat, lon = float(r["latitude"]), float(r["longitude"])
    if BOX[0] <= lon <= BOX[1] and BOX[2] <= lat <= BOX[3]: c[r["acq_date"]] += 1
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for d in sorted(c):
    if d < today: print("REPLACE INTO hotspot_recent (date, count, updated_at) VALUES ('%s', %d, '%s');" % (d, c[d], now))
