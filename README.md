# Wind publisher: setup to review

3 October 2026. Prepared, not published. Decision 037 requires a verified free publisher before iOS wind integration.

## What it does

A small automatic job downloads NOAA's regional wind four times a day, checks the complete dated grid and puts one JSON file on free Cloudflare Pages hosting. Kilat reads that file. If a download fails, the existing publication remains; its expiry prevents old wind from being presented as current.

Local NEA wind is a separate observation source. This publisher does not provide NEA history or make a new time scale.

## Recommended setup

Create a separate **public GitHub repository named `kilat-wind`**, containing only the wind exporter, contract checks, tests, public NEA test data, dependency list, scheduled workflow and hosting headers. The application source and the rest of this project are outside that publication package.

Public repository code is visible to anyone. Standard GitHub runners are free for public repositories ([GitHub billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions)). The existing four daily Pages deployments use about 120 of the Free plan's 500 monthly builds ([Pages limits](https://developers.cloudflare.com/pages/platform/limits/)). A private repository is possible, but its shared runner allowance and $0 spending stop must be checked before enabling it.

The Pages deployment credential goes into GitHub secret storage, not the repository or chat. The workflow remains disabled until configuration is complete. The package uses the existing `kilat-wind` Pages project name.

## Activation and acceptance

After the owner authorizes this concrete setup: create/connect the wind-only repository and free Pages project; configure the scoped deployment credential directly in secret storage; then enable the job. Verify the hosted file's contents and dates after a manual run and one later scheduled run. Only then proceed with the app integration and running interaction checks.

Local data checks establish compatibility and mechanics. They do not establish scheduled reliability, wind accuracy or acceptance of the app's feel. NEA's exact direction convention still needs source confirmation before rendering its flow.
