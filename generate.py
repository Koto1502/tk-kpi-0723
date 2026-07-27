#!/usr/bin/env python
"""Regenerate the Tiny Knight Fall KPI dashboard (index.html) from BigQuery.

The dashboard is a single self-contained HTML file whose data lives in one baked-in
`const DATA = {...}` object. This script re-runs every underlying query against the
GA4/Firebase export and rewrites that object, so refreshing the dashboard is:

    uv run --with google-cloud-bigquery --with db-dtypes --with pandas \
           --with pyarrow generate.py

Queries go through `bq_helper.run` (read-only, byte-capped, your own Google identity
via Mirror). `template.html` holds the page with a `/*__DATA__*/` placeholder and
self-updating date logic; it is created from index.html on first run and reused after.

All metric definitions were validated to reproduce the prior dashboard exactly on
overlapping days. See README.md for the full definitions and caveats.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bq_helper import run, PROJECT

DATASET = "analytics_538412813"
TBL = f"`{PROJECT}.{DATASET}.events_intraday_*`"
HERE = Path(__file__).parent

# Reusable SQL fragments -------------------------------------------------------
# scalar extract of an event_param by key
P_STR = lambda k: f"(SELECT p.value.string_value FROM UNNEST(event_params) p WHERE p.key='{k}')"
P_NUM = lambda k: (
    "(SELECT COALESCE(p.value.double_value, p.value.float_value, "
    f"CAST(p.value.int_value AS FLOAT64)) FROM UNNEST(event_params) p WHERE p.key='{k}')"
)
P_INT = lambda k: f"(SELECT p.value.int_value FROM UNNEST(event_params) p WHERE p.key='{k}')"


def window() -> tuple[str, str]:
    """First and last event_date present in the export (drives the whole run)."""
    df = run(f"SELECT MIN(event_date) lo, MAX(event_date) hi FROM {TBL}")
    return str(df.lo.iloc[0]), str(df.hi.iloc[0])


def build_data(lo: str, hi: str) -> dict:
    suffix = f"_TABLE_SUFFIX BETWEEN '{lo}' AND '{hi}'"
    data: dict = {}

    # 1. DAU by app version --------------------------------------------------
    data["dau_version"] = js(run(f"""
        SELECT event_date d, SAFE_CAST(app_info.version AS INT64) v,
               COUNT(DISTINCT user_pseudo_id) users
        FROM {TBL}
        WHERE {suffix} AND SAFE_CAST(app_info.version AS INT64) IS NOT NULL
        GROUP BY d, v ORDER BY d, v"""))

    # 2. Daily KPIs ----------------------------------------------------------
    data["daily"] = js(run(f"""
        WITH base AS (
          SELECT event_date d, event_name e, user_pseudo_id u,
                 event_value_in_usd ev_usd, {P_NUM('value')} v_val
          FROM {TBL} WHERE {suffix}
        )
        SELECT d,
          COUNT(DISTINCT u) dau,
          COUNT(DISTINCT IF(e='first_open', u, NULL)) new_users,
          COUNTIF(e='session_start') sessions,
          ROUND(SUM(IF(e='ad_impression_MAX', v_val, 0)), 4) iaa_rev,
          ROUND(SUM(IF(e='in_app_purchase', ev_usd, 0)), 2) iap_rev,
          COUNTIF(e='ad_impression_MAX') impressions,
          COUNT(DISTINCT IF(e='ad_impression_MAX', u, NULL)) ad_viewers,
          COUNTIF(e='in_app_purchase') iap_events,
          COUNT(DISTINCT IF(e='in_app_purchase', u, NULL)) payers
        FROM base GROUP BY d ORDER BY d"""))

    # first_open cohort + activity CTE reused by retention + curve
    cohort_cte = f"""
        fo AS (
          SELECT user_pseudo_id u, MIN(PARSE_DATE('%Y%m%d', event_date)) cohort
          FROM {TBL} WHERE {suffix} AND event_name='first_open' GROUP BY u
        ),
        act AS (
          SELECT DISTINCT user_pseudo_id u, PARSE_DATE('%Y%m%d', event_date) ad
          FROM {TBL} WHERE {suffix}
        )"""

    # 3. Retention by cohort (classic day-N) ---------------------------------
    dn = lambda n: (f"ROUND(100*COUNT(DISTINCT IF(DATE_DIFF(a.ad,f.cohort,DAY)={n},"
                    f"f.u,NULL))/COUNT(DISTINCT f.u),1)")
    data["retention"] = js(run(f"""
        WITH {cohort_cte}
        SELECT FORMAT_DATE('%Y-%m-%d', f.cohort) cohort, COUNT(DISTINCT f.u) size,
          {dn(1)} d1, {dn(3)} d3, {dn(7)} d7, {dn(14)} d14
        FROM fo f JOIN act a USING(u)
        GROUP BY cohort ORDER BY cohort"""))

    # 4. Retention curve (day-0..20) -----------------------------------------
    data["ret_curve"] = js(run(f"""
        WITH {cohort_cte}
        SELECT dd, SUM(is_active) active, SUM(is_eligible) eligible FROM (
          SELECT f.u, dd,
            IF(DATE_ADD(f.cohort, INTERVAL dd DAY) <= DATE '{hi[:4]}-{hi[4:6]}-{hi[6:]}',1,0) is_eligible,
            MAX(IF(a.ad IS NOT NULL AND DATE_DIFF(a.ad,f.cohort,DAY)=dd,1,0)) is_active
          FROM fo f CROSS JOIN UNNEST(GENERATE_ARRAY(0,20)) dd
          LEFT JOIN act a ON a.u=f.u
          GROUP BY f.u, f.cohort, dd
        ) GROUP BY dd ORDER BY dd"""))

    # 5. IAA revenue by ad format --------------------------------------------
    data["iaa_by_format"] = js(run(f"""
        SELECT {P_STR('ad_format')} fmt, COUNT(*) imps, ROUND(SUM({P_NUM('value')}),4) rev
        FROM {TBL} WHERE {suffix} AND event_name='ad_impression_MAX'
        GROUP BY fmt ORDER BY rev DESC"""))

    # 6. IAA revenue by rewarded placement -----------------------------------
    data["iaa_by_placement"] = js(run(f"""
        SELECT COALESCE(pl,'(none)') pl, COUNT(*) imps, ROUND(SUM(val),4) rev FROM (
          SELECT {P_STR('ad_placement')} pl, {P_STR('ad_format')} fmt, {P_NUM('value')} val
          FROM {TBL} WHERE {suffix} AND event_name='ad_impression_MAX'
        ) WHERE fmt='REWARDED' GROUP BY pl ORDER BY rev DESC"""))

    # 7. IAP revenue by product ----------------------------------------------
    data["iap_by_product"] = js(run(f"""
        SELECT {P_STR('product_id')} product, COUNT(*) buys,
               COUNT(DISTINCT user_pseudo_id) buyers, ROUND(SUM(event_value_in_usd),2) rev
        FROM {TBL} WHERE {suffix} AND event_name='in_app_purchase'
        GROUP BY product ORDER BY rev DESC"""))

    # 8. Ad request/fill/show funnel -----------------------------------------
    data["ad_funnel"] = js(run(f"""
        SELECT d, ad_type,
          COUNTIF(e='ad_request') requests,
          COUNTIF(e='ad_request_status' AND status='success') fills,
          COUNTIF(e='ad_show') shows
        FROM (
          SELECT event_date d, event_name e, {P_STR('ad_type')} ad_type, {P_STR('status')} status
          FROM {TBL} WHERE {suffix} AND event_name IN ('ad_request','ad_request_status','ad_show')
        ) WHERE ad_type IS NOT NULL GROUP BY d, ad_type ORDER BY d, ad_type"""))

    # 9. Level funnel & difficulty -------------------------------------------
    data["levels"] = js(run(f"""
        SELECT lvl,
          COUNT(DISTINCT IF(e='level_start',u,NULL)) starters,
          COUNTIF(e='level_win') wins,
          COUNTIF(e='level_lose') losses,
          ROUND(AVG(IF(e='level_win', tp, NULL)),1) avg_win_time
        FROM (
          SELECT event_name e, user_pseudo_id u, {P_INT('level')} lvl, {P_NUM('time_play')} tp
          FROM {TBL} WHERE {suffix} AND event_name IN ('level_start','level_win','level_lose')
        ) WHERE lvl BETWEEN 1 AND 60 GROUP BY lvl ORDER BY lvl"""))

    # 10. Users vs revenue by country ----------------------------------------
    data["geo"] = js(run(f"""
        SELECT country, COUNT(DISTINCT u) users, ROUND(SUM(rev),2) rev FROM (
          SELECT geo.country country, user_pseudo_id u,
            IF(event_name='in_app_purchase', COALESCE(event_value_in_usd,0),0)
            + IF(event_name='ad_impression_MAX', COALESCE({P_NUM('value')},0),0) rev
          FROM {TBL} WHERE {suffix}
        ) GROUP BY country ORDER BY users DESC LIMIT 12"""))

    return data


def js(df):
    """DataFrame -> list of JSON-safe dicts (NaN -> null, numpy -> native)."""
    return json.loads(df.to_json(orient="records"))


def make_template(index_html: str) -> str:
    """Turn the current index.html into a reusable template: swap the DATA object for
    a placeholder and make the header/tile dates derive from the data itself."""
    lines = index_html.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("const DATA = "):
            lines[i] = "const DATA = /*__DATA__*/;"
            break
    else:
        raise SystemExit("could not find `const DATA = ` line in index.html")
    t = "\n".join(lines)

    def sub(old, new, t):
        if old not in t:
            raise SystemExit(f"template anchor not found: {old[:60]!r}")
        return t.replace(old, new)

    # self-updating dates: define helpers once, then point everything at them
    t = sub(
        "const peakDAU = Math.max(...daily.map(r=>r.dau));",
        "const peakDAU = Math.max(...daily.map(r=>r.dau));\r\n"
        "const peakRow = daily.reduce((a,b)=>b.dau>a.dau?b:a, daily[0]);\r\n"
        "const LAST = new Date(String(dates[dates.length-1]).replace(/(\\d{4})(\\d{2})(\\d{2})/,'$1-$2-$3'));\r\n"
        "const isoD = s => String(s).replace(/(\\d{4})(\\d{2})(\\d{2})/,'$1-$2-$3');\r\n"
        "const monthDay = s => ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][+String(s).slice(4,6)-1]+' '+ +String(s).slice(6,8);",
        t)
    t = t.replace("new Date('2026-07-23')", "LAST")  # both cutoffs -> data-driven
    t = sub(
        "full export 2026-06-29 → 2026-07-23 (last day partial, intraday) · generated 2026-07-23",
        "full export ${isoD(dates[0])} → ${isoD(dates[dates.length-1])} "
        "(last day partial, intraday) · generated ${isoD(dates[dates.length-1])}",
        t)
    t = sub(
        "l:'DAU (Jul 22, last full day)', n:`peak ${fmtK(peakDAU)} on Jul 19`",
        "l:`DAU (${monthDay(lastFull.d)}, last full day)`, n:`peak ${fmtK(peakDAU)} on ${monthDay(peakRow.d)}`",
        t)
    t = sub("l:'ARPDAU (Jul 22)'", "l:`ARPDAU (${monthDay(lastFull.d)})`", t)
    t = sub(
        "'Bars = new users (first_open) per day; line = DAU. Jul 23 is a partial day.'",
        "`Bars = new users (first_open) per day; line = DAU. "
        "${monthDay(dates[dates.length-1])} is a partial day.`",
        t)
    t = sub(
        "the export begins 2026-06-29 and Jul 23 is a partial day). ",
        "the export begins ${isoD(dates[0])} and ${monthDay(dates[dates.length-1])} is a partial day). ",
        t)
    return t


def main():
    index = HERE / "index.html"
    template = HERE / "template.html"
    if not template.exists():
        template.write_text(make_template(index.read_text(encoding="utf-8")),
                            encoding="utf-8", newline="")
        print(f"wrote {template.name}")

    lo, hi = window()
    print(f"export window: {lo} -> {hi}")
    data = build_data(lo, hi)
    for k, v in data.items():
        print(f"  {k:16s} {len(v):4d} rows")

    payload = json.dumps(data, separators=(", ", ": "))
    html = template.read_text(encoding="utf-8").replace("/*__DATA__*/", payload)
    index.write_text(html, encoding="utf-8", newline="")
    print(f"wrote {index.name} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
