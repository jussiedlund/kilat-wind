#!/usr/bin/env python3
"""Count recent VIIRS SNPP fire detections per day from the keyless FIRMS 7-day CSV and print SQL for D1.

Same filter as aggregate.py (confidence nominal/high, lon 94-120E, lat 12S-8N) so counts compare with the
2019-2024 baseline. Only complete UTC days are kept (the newest, still-filling day is skipped).
The FIRMS CSV is NRT and not clipped to national borders, so it runs slightly above the country archives.

Run daily (the 7-day file means a missed run is covered for up to a week):
  Scheduled daily by .github/workflows/hotspots.yml.
"""
import csv, datetime, io, urllib.request, collections
URL = "https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_SouthEast_Asia_7d.csv"
BOX = (94.0, 120.0, -12.0, 8.0)
KEEP = {"n", "nominal", "h", "high"}
text = urllib.request.urlopen(URL, timeout=120).read().decode("utf-8", "replace")
c = collections.Counter()
for r in csv.DictReader(io.StringIO(text)):
    if r["confidence"].strip().lower() not in KEEP: continue
    lat, lon = float(r["latitude"]), float(r["longitude"])
    if BOX[0] <= lon <= BOX[1] and BOX[2] <= lat <= BOX[3]: c[r["acq_date"]] += 1
today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
for d in sorted(c):
    if d < today: print("REPLACE INTO hotspot_recent (date, count, updated_at) VALUES ('%s', %d, '%s');" % (d, c[d], now))
