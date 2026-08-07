#!/usr/bin/env python
"""Refresh churn.html from AppsFlyer installs and the guarded BigQuery helper.

The output contains no AppsFlyer IDs or BigQuery user IDs. It embeds one anonymous
summary row per matched install so campaign and install-date filters stay interactive.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import sys
from pathlib import Path

# afdata is installed in its managed desktop runtime rather than this project's
# virtual environment. Reuse that read-only client/runtime without copying secrets.
AF_HOME = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
) / "afdata"
for af_path in (AF_HOME / "app", AF_HOME / "runtime" / "lib"):
    if af_path.is_dir():
        sys.path.insert(0, str(af_path))
from afdata import api, keys
import keyring

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from bq_helper import PROJECT, dry_run_bytes, run  # noqa: E402

DATASET = "analytics_538412813"
TABLE = f"`{PROJECT}.{DATASET}.events_intraday_*`"
APP_ID = os.environ.get("TK_APPSFLYER_APP_ID", "com.game.tiny.knightfall.idle.rpg")
APP_START = "2026-06-29"
AF_LAG_DAYS = 1
INACTIVITY_HOURS = 72
AUTOMATIC_EVENTS = (
    "app_clear_data", "app_exception", "app_remove", "app_store_refund",
    "app_store_subscription_cancel", "app_store_subscription_convert",
    "app_store_subscription_renew", "app_update", "dynamic_link_app_open",
    "dynamic_link_first_open", "error", "firebase_campaign", "first_open",
    "first_visit", "notification_dismiss", "notification_foreground",
    "notification_open", "notification_receive", "os_update", "screen_view",
    "session_start", "user_engagement",
)


def _rows_from_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _normalise_installs(paid: list[dict], organic: list[dict], start: str, end: str) -> list[dict]:
    installs: dict[str, dict] = {}
    for row, force_organic in [(r, False) for r in paid] + [(r, True) for r in organic]:
        af_id = (row.get("AppsFlyer ID") or row.get("appsflyer_id") or "").strip()
        install_date = (
            row.get("Install Time") or row.get("install_time") or row.get("install_date") or ""
        )[:10]
        if not af_id or not (start <= install_date <= end):
            continue
        media = (
            row.get("Media Source") or row.get("Media Source (pid)")
            or row.get("media_source") or ""
        ).strip()
        campaign = (
            row.get("Campaign") or row.get("Campaign (c)") or row.get("campaign") or ""
        ).strip()
        if force_organic or not media or media.lower() == "organic":
            campaign = "Organic"
        else:
            campaign = campaign or media or "Unattributed"
        installs[af_id] = {"install_date": install_date, "campaign": campaign}
    return [{"af_id": af_id, **row} for af_id, row in installs.items()]


def pull_installs(start: str, end: str) -> list[dict]:
    """Prefer AFdata's local exports, then refresh through its credential store."""
    local_dirs = (ROOT / "dashboard" / "afdata_local", ROOT / "dashboard")
    for local_dir in local_dirs:
        paid_path = local_dir / "tk_installs.csv"
        organic_path = local_dir / "tk_organic.csv"
        if paid_path.exists() and organic_path.exists():
            return _normalise_installs(
                _rows_from_csv(paid_path), _rows_from_csv(organic_path), start, end
            )

    profile = os.environ.get("TK_APPSFLYER_KEY_PROFILE", "key-1")
    token = keyring.get_password("afdata-appsflyer", f"profile:{profile}")
    if not token:
        token = keys.get_key()
    if not token:
        raise RuntimeError(f"AppsFlyer credential profile {profile!r} is not available")

    def pull(report: str) -> tuple[list[dict], str]:
        response = api.pull_csv(
            token, APP_ID, report, start, end, "raw", timeout=180
        )
        return list(csv.DictReader(io.StringIO(response.csv_text))), response.csv_text

    paid, paid_csv = pull("installs_report")
    organic, organic_csv = pull("organic_installs_report")
    # Keep the raw ID mapping outside the Git repository. These files are local
    # refresh inputs only; churn.html receives anonymous aggregates.
    local_dir = local_dirs[0]
    paid_path = local_dir / "tk_installs.csv"
    organic_path = local_dir / "tk_organic.csv"
    local_dir.mkdir(parents=True, exist_ok=True)
    paid_path.write_text(paid_csv, encoding="utf-8", newline="")
    organic_path.write_text(organic_csv, encoding="utf-8", newline="")
    return _normalise_installs(paid, organic, start, end)


def bq_window() -> tuple[str, str]:
    result = run(f"SELECT MIN(event_date) lo, MAX(event_date) hi FROM {TABLE}", gib=1)
    return str(result.lo.iloc[0]), str(result.hi.iloc[0])


def player_summaries(lo: str, hi: str):
    excluded = ",".join(f"'{name}'" for name in AUTOMATIC_EVENTS)
    sql = f"""
    WITH raw AS (
      SELECT user_pseudo_id AS user_id, event_name, event_timestamp,
        (SELECT value.string_value FROM UNNEST(user_properties)
          WHERE key='appsflyer_id') AS af_id,
        (SELECT COALESCE(
            CAST(value.int_value AS FLOAT64), value.double_value,
            SAFE_CAST(value.string_value AS FLOAT64))
          FROM UNNEST(user_properties) WHERE key='online_time') AS online_time,
        (SELECT COALESCE(
            CAST(value.int_value AS FLOAT64), value.double_value,
            SAFE_CAST(value.string_value AS FLOAT64))
          FROM UNNEST(event_params) WHERE key='engagement_time_msec') AS engagement_ms,
        (SELECT COALESCE(
            CAST(value.int_value AS FLOAT64), value.double_value,
            SAFE_CAST(value.string_value AS FLOAT64))
          FROM UNNEST(event_params) WHERE key='level') AS level,
        IF(event_name='earn',
          (SELECT value.string_value FROM UNNEST(event_params) WHERE key='position'),
          NULL) AS quest_position
      FROM {TABLE}
      WHERE _TABLE_SUFFIX BETWEEN '{lo}' AND '{hi}'
        AND event_timestamp BETWEEN
          UNIX_MICROS(TIMESTAMP_SUB(PARSE_TIMESTAMP('%Y%m%d','{lo}'), INTERVAL 1 DAY))
          AND UNIX_MICROS(TIMESTAMP_ADD(PARSE_TIMESTAMP('%Y%m%d','{hi}'), INTERVAL 2 DAY))
    ),
    main_quest AS (
      SELECT user_id, MAX(quest_number) AS main_quest_claims
      FROM (
        SELECT user_id,
          ROW_NUMBER() OVER (
            PARTITION BY user_id ORDER BY event_timestamp
          ) AS quest_number
        FROM raw
        WHERE event_name='earn' AND quest_position='main_quest'
      )
      GROUP BY user_id
    ),
    users AS (
      SELECT user_id,
        ARRAY_AGG(af_id IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)[SAFE_OFFSET(0)] af_id,
        MAX(event_timestamp) AS last_event_ts,
        MAX(IF(online_time IS NOT NULL OR event_name='user_engagement',
          event_timestamp, NULL)) AS last_heartbeat_ts,
        MAX(online_time) AS max_online_time,
        SUM(IF(event_name='user_engagement', COALESCE(engagement_ms,0), 0))/1000
          AS engagement_seconds,
        MAX(IF(event_name='level_start', level, NULL)) AS max_level,
        ARRAY_AGG(IF(event_name NOT IN ({excluded})
            AND NOT STARTS_WITH(event_name,'firebase_'),
          event_name, NULL) IGNORE NULLS ORDER BY event_timestamp DESC LIMIT 1)
          [SAFE_OFFSET(0)] AS last_custom_event
      FROM raw GROUP BY user_id
    ),
    bounds AS (
      SELECT MAX(event_timestamp) AS data_max_ts FROM raw
    )
    SELECT u.af_id, u.user_id,
      COALESCE(u.last_heartbeat_ts,u.last_event_ts)
        < b.data_max_ts-{INACTIVITY_HOURS}*60*60*1000000 AS inactive,
      CAST(COALESCE(NULLIF(u.max_online_time,0),u.engagement_seconds,0) AS INT64)
        AS playtime_seconds,
      CAST(COALESCE(u.max_level,0) AS INT64) AS max_level,
      CAST(COALESCE(q.main_quest_claims,0) AS INT64) AS main_quest_claims,
      u.last_custom_event,
      COALESCE(u.last_heartbeat_ts,u.last_event_ts) AS last_activity_ts
    FROM users u
    LEFT JOIN main_quest q USING (user_id)
    CROSS JOIN bounds b
    WHERE u.af_id IS NOT NULL
    """
    dry_run_bytes(sql)
    return run(sql, gib=3, max_rows=100_000)


def main() -> None:
    if PROJECT != "tiny-knightfall-idle-rpg-game":
        raise RuntimeError(f"Refusing unexpected Mirror project: {PROJECT!r}")
    af_end = (dt.date.today() - dt.timedelta(days=AF_LAG_DAYS)).isoformat()
    installs = pull_installs(APP_START, af_end)
    af_by_id = {r["af_id"]: r for r in installs}
    bq_lo, bq_hi = bq_window()
    summaries = player_summaries(bq_lo, bq_hi)

    # One anonymous record per AppsFlyer ID. If GA4 has more than one
    # user_pseudo_id, treat the install as active when any identity is recent.
    joined: dict[str, dict] = {}
    for row in summaries.to_dict("records"):
        af_id = str(row.get("af_id") or "").strip()
        install = af_by_id.get(af_id)
        if not install:
            continue
        record = {
            "install_date": install["install_date"],
            "campaign": install["campaign"],
            "inactive": bool(row.get("inactive")),
            "playtime_seconds": int(row.get("playtime_seconds") or 0),
            "max_level": int(row.get("max_level") or 0),
            "main_quest_claims": int(row.get("main_quest_claims") or 0),
            "last_custom_event": row.get("last_custom_event") or None,
            "last_activity_ts": int(row.get("last_activity_ts") or 0),
        }
        old = joined.get(af_id)
        if old is None:
            joined[af_id] = record
        else:
            old["inactive"] = old["inactive"] and record["inactive"]
            old["playtime_seconds"] = max(
                old["playtime_seconds"], record["playtime_seconds"]
            )
            old["max_level"] = max(old["max_level"], record["max_level"])
            old["main_quest_claims"] += record["main_quest_claims"]
            if record["last_activity_ts"] > old["last_activity_ts"]:
                old["last_activity_ts"] = record["last_activity_ts"]
                old["last_custom_event"] = (
                    record["last_custom_event"] or old["last_custom_event"]
                )

    players = sorted(
        (
            {key: value for key, value in row.items() if key != "last_activity_ts"}
            for row in joined.values()
        ),
        key=lambda r: (r["install_date"], r["campaign"]),
    )
    campaigns = sorted({r["campaign"] for r in players})
    payload = {
        "meta": {
            "project": PROJECT,
            "dataset": DATASET,
            "generated_at": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "min_date": min((r["install_date"] for r in players), default=APP_START),
            "max_date": max((r["install_date"] for r in players), default=af_end),
            "af_installs": len(installs),
            "matched_players": len(players),
            "campaigns": campaigns,
            "tutorial_available": False,
            "inactivity_hours": INACTIVITY_HOURS,
            "lag_days": AF_LAG_DAYS,
            "bq_window": f"{bq_lo}..{bq_hi}",
        },
        "players": players,
    }
    template = (HERE / "churn_template.html").read_text(encoding="utf-8")
    output = template.replace(
        "/*__DATA__*/", json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    )
    (HERE / "churn.html").write_text(output, encoding="utf-8", newline="")
    print(
        f"Wrote churn.html: {len(installs):,} AF installs, "
        f"{len(players):,} matched BigQuery players, {len(campaigns):,} campaigns"
    )


if __name__ == "__main__":
    main()
