# Kilat wind publisher

A scheduled NOAA GFS exporter for regional wind. It validates a complete, dated vector grid and publishes one static JSON document to Cloudflare Pages.

**Hosted file:** [latest.json](https://kilat-wind.pages.dev/latest.json). The first manual CLI publication was checked on 3 October 2026. Automatic publishing stays disabled until the deployment credential is configured and the workflow is verified.

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
