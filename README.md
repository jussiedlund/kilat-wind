# Kilat wind publisher

## Regional fire and haze publication

`regional.yml` collects the official haze.gov.sg homepage/KML layers hourly at minute 23 UTC, validates them with the app backend parser, and atomically imports the current capture plus 30-day history into the existing `sg-map-lens-data` D1 database. Node 24 runs parsing outside Workers Free's 10 ms CPU budget. `CLOUDFLARE_D1_TOKEN` is restricted to D1 and `CLOUDFLARE_ACCOUNT_ID` selects the existing account; no new credential is needed. Failures preserve the previous capture; its four-hour app expiry still applies. Source observation time is distinct from successful fetch time. Each successful refresh changes the ETag so unchanged source geometry still delivers the fresh fetch timestamp.

`hotspots.yml` collects complete UTC-day FIRMS counts at 02:43 / 08:43 / 14:43 / 20:43 UTC. Repeated attempts recover missed GitHub schedules. The atomic import also clears the seasonal summary cache and updates only the snapshot's actual hotspot counts and ETag; unrelated feed timestamps remain unchanged. Missing dates remain null. GitHub schedules can be delayed or dropped; these retries reduce that risk, without guaranteeing delivery.

Check `node --experimental-strip-types --test backend/test/regional-collector.test.mjs backend/test/hotspot-publication.test.mjs` before publication. Trigger either workflow manually for recovery, then verify `/v1/regional` fetch freshness and `/v1/snapshot` hotspot dates. The regional Worker cron should be disabled once this collector is activated.

A scheduled NOAA GFS exporter for regional wind. It validates a complete, dated vector grid and publishes one static JSON document to Cloudflare Pages.

**Hosted file:** [latest.json](https://kilat-wind.pages.dev/latest.json). Publishing is enabled. Manual workflow [37101817627](https://github.com/jussiedlund/kilat-wind/actions/runs/37101817627) passed on 3 October 2026, including the hosted-byte comparison. A successful scheduled event has not yet been observed; activation verification remains open.

## Data contract

- NOAA GFS 10 m wind: 0.25 degree grid, 95–120 degrees E and 8 degrees S–12 degrees N, 101 x 81 cells.
- One model cycle with +0 / +3 / +6 / +9 / +12 hour frames; rows run south to north.
- U points east; V points north. Components are base64 little-endian signed 16-bit integers, divided by 10 to obtain m/s.
- Model-run, frame-valid, generation and expiry times are distinct. Consumers check expiry against the actual clock, including for past frames.
- Failed downloads or contract checks preserve the previous publication; its expiry still applies.
- This is modelled wind, not station observations or proof of smoke transport. One cycle does not provide a complete historical archive.

The public NEA fixture checks station/time pairing and partial coverage. This publisher does not provide NEA wind or history. NEA direction stays raw degrees pending official confirmation of its orientation convention.

## Run locally

Use Python 3.12 or later, preferably in a virtual environment:

    python -m pip install -r backend/scripts/wind-requirements.txt
    python -m unittest discover -s backend/test -p 'test_wind_contract.py'
    python backend/scripts/build-wind.py --output wind-public/latest.json

Generated documents are deployed separately. Commit only publisher source and tests.

## Activate

Create the static Pages project `kilat-wind` before enabling deployment. Configure these repository secrets:

- `CLOUDFLARE_ACCOUNT_ID`: hosting account ID.
- `CLOUDFLARE_API_TOKEN`: Cloudflare Pages Edit token restricted to the hosting account.

Set repository variable `WIND_PUBLISH_ENABLED` to `1`, then manually run **Publish GFS wind**. Check the hosted JSON and dates after that run and one later scheduled run. Production cadence is 04:17 / 10:17 / 16:17 / 22:17 UTC; scheduled starts can be delayed.

Standard public GitHub runners and static Pages hosting support the zero-cost design. Four daily deployments use about 120 builds monthly. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions) and [Pages limits](https://developers.cloudflare.com/pages/platform/limits/).
