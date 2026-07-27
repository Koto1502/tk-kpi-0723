# Tiny Knight Fall — KPI Dashboard

Static, self-contained dashboards served from GitHub Pages:

- [`index.html`](index.html) — KPI overview (DAU, revenue, retention, ad funnel, levels, geo)
- [`campaigns.html`](campaigns.html) — LTV and retention by campaign cohort
- [`churn.html`](churn.html) — campaign-filtered churn, level exits, and last custom event

All numbers are baked into each page as a single `const DATA = {…}` object, so the
pages need no backend and load instantly.

## Refreshing the data

`index.html` is generated from [`template.html`](template.html) (the page with a
`/*__DATA__*/` placeholder) by [`generate.py`](generate.py), which re-runs every query
against the GA4/Firebase BigQuery export and re-injects the data. The header, tile
dates, and "partial day" notes derive from the data itself, so a refresh is one
command:

```bash
uv run --with google-cloud-bigquery --with db-dtypes --with pandas --with pyarrow generate.py
```

Then commit the updated `index.html`.

The churn dashboard refreshes AppsFlyer paid + organic install reports and joins
them to GA4 by `user_property.appsflyer_id`:

```bash
refresh_churn.bat
```

It writes an anonymous, self-contained `churn.html`; AppsFlyer IDs and BigQuery
user IDs are never embedded in the published page.

**Prerequisites:** queries run through [`bq_helper.py`](bq_helper.py) as **your own
Google identity** (read-only, byte-capped) using the project bound in **Mirror
Connect** (`~/.mirror/session.json`). Bind Mirror to `tiny-knightfall-idle-rpg-game`
before running. The generator auto-detects the export window from the tables present,
so it always picks up newly landed days.

## Source & definitions

- **Source:** GA4/Firebase BigQuery export
  `tiny-knightfall-idle-rpg-game.analytics_538412813.events_intraday_*` (streaming
  tables only). The last day in the window is always partial. All figures **UTC**.
- **Users** = `user_pseudo_id` (device-scoped; `player_id` coverage is too low to use).
- **Installs / cohorts** = `first_open`; **DAU** = distinct users/day; **sessions** =
  `session_start` count.
- **IAA revenue** = `ad_impression_MAX` `value` param only (AppLovin MAX). The
  `ad_impression` and `ad_impression_rocket` events are duplicate logs of the same
  impressions and are **not** additive.
- **IAP revenue** = `event_value_in_usd` on `in_app_purchase`, which only began
  reporting with build 15 (~Jul 20); earlier real IAP revenue, if any, is not in
  BigQuery.
- **Retention** = classic day-N (active exactly N days after `first_open`), cohorted
  by install day.
- **Ad funnel:** fill rate = `ad_request_status` with `status='success'` ÷
  `ad_request`, split by `ad_type` (`inter`/`rewarded`).
- **Levels:** `starters` = distinct users on `level_start`; `wins`/`losses` = counts
  of `level_win`/`level_lose`; `avg_win_time` = mean `time_play` (s) on `level_win`.
