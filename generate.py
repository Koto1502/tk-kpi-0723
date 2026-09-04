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

import pandas as pd

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


# --- Segment cube: per-user dimension + per-(user, day, version) facts --------
# Everything the country/campaign filters and the version comparison need is
# derived in the browser from these two tables, so the filters stay honest
# (a filtered DAU is a real distinct-user count, not a re-scaled aggregate).
# Revenue is carried as integer micro-USD throughout to avoid float drift.
AF_DIR = HERE.parent / "dashboard" / "afdata_local"


def campaign_by_af() -> dict[str, str]:
    """appsflyer_id -> campaign, from the same local AppsFlyer exports the
    retention/LTV dashboards use. Raw IDs never leave this process: the payload
    only ever carries a campaign index."""
    import csv

    out: dict[str, str] = {}
    for fname, default in (("tk_installs.csv", None), ("tk_organic.csv", "Organic")):
        path = AF_DIR / fname
        if not path.exists():
            print(f"  ! {path.name} missing - campaign filter will be unattributed only")
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                af = (r.get("AppsFlyer ID") or "").strip()
                if not af:
                    continue
                camp = (r.get("Campaign") or "").strip() or default
                if not camp:
                    media = (r.get("Media Source") or "").strip()
                    camp = media or "Unattributed"
                if (r.get("Media Source") or "").strip().lower() == "organic":
                    camp = "Organic"
                out[af] = camp
    return out


def build_segments(lo: str, hi: str) -> dict:
    suffix = f"_TABLE_SUFFIX BETWEEN '{lo}' AND '{hi}'"
    rev_expr = (
        f"IF(event_name='in_app_purchase', COALESCE(event_value_in_usd,0), 0) "
        f"+ IF(event_name='ad_impression_MAX', COALESCE({P_NUM('value')},0), 0)"
    )
    iap_expr = "IF(event_name='in_app_purchase', COALESCE(event_value_in_usd,0), 0)"

    users = run(f"""
        WITH ev AS (
          SELECT user_pseudo_id u, event_timestamp ts, event_name e, event_date d,
                 SAFE_CAST(app_info.version AS INT64) v,
                 NULLIF(geo.country,'') c,
                 (SELECT value.string_value FROM UNNEST(user_properties)
                    WHERE key='appsflyer_id') af,
                 {iap_expr} iap, {rev_expr} rev
          FROM {TBL} WHERE {suffix}
        )
        SELECT u,
          MIN(ts) ft, MAX(ts) lt,
          MIN(IF(rev>0, ts, NULL)) frt, MAX(IF(rev>0, ts, NULL)) lrt,
          MIN(d) fd, MAX(IF(e='first_open',1,0)) fo,
          SUM(iap) iap, SUM(rev-iap) adr,
          ARRAY_AGG(c  IGNORE NULLS ORDER BY ts LIMIT 1)[SAFE_OFFSET(0)] country,
          ARRAY_AGG(v  IGNORE NULLS ORDER BY ts LIMIT 1)[SAFE_OFFSET(0)] fv,
          ARRAY_AGG(af IGNORE NULLS ORDER BY ts LIMIT 1)[SAFE_OFFSET(0)] af
        FROM ev GROUP BY u""", gib=6)

    facts = run(f"""
        SELECT user_pseudo_id u, event_date d, SAFE_CAST(app_info.version AS INT64) v,
               SUM({iap_expr}) iap, SUM({rev_expr} - ({iap_expr})) adr,
               COUNTIF(event_name='session_start') sess,
               COUNTIF(event_name='ad_impression_MAX') imp
        FROM {TBL} WHERE {suffix}
        GROUP BY u, d, v""", gib=6)

    camp_map = campaign_by_af()

    days = sorted({str(d) for d in facts["d"].unique()} | {str(d) for d in users["fd"].unique()})
    day_ix = {d: i for i, d in enumerate(days)}

    # dimension vocabularies, ordered by user count so the pickers list big ones first
    def vocab(series, unknown):
        vals = [unknown if not pd.notna(v) or v == "" else str(v) for v in series]
        counts: dict[str, int] = {}
        for v in vals:
            counts[v] = counts.get(v, 0) + 1
        names = sorted(counts, key=lambda n: (n == unknown, -counts[n], n))
        return names, {n: i for i, n in enumerate(names)}, vals

    countries, c_ix, u_country = vocab(users["country"].tolist(), "(unknown)")
    camp_vals = [
        camp_map.get(a) if isinstance(a, str) and a in camp_map else None
        for a in users["af"].tolist()
    ]
    campaigns, p_ix, u_camp = vocab(camp_vals, "(unattributed)")

    versions = sorted({int(v) for v in facts["v"].dropna().unique()})
    v_ix = {v: i for i, v in enumerate(versions)}

    t0 = int(users["ft"].min())  # microseconds; all timestamps become minutes from here
    mins = lambda ts: int((int(ts) - t0) // 60_000_000)
    micro = lambda x: int(round(float(x) * 1_000_000)) if pd.notna(x) else 0

    u_ix = {u: i for i, u in enumerate(users["u"].tolist())}
    U = {
        "co": [c_ix[v] for v in u_country],
        "ca": [p_ix[v] for v in u_camp],
        "fv": [v_ix.get(int(v), -1) if pd.notna(v) else -1 for v in users["fv"]],
        "fd": [day_ix[str(d)] for d in users["fd"]],
        "fo": [int(v) for v in users["fo"]],
        "ft": [mins(t) for t in users["ft"]],
        "lt": [mins(t) for t in users["lt"]],
        "frt": [mins(t) if pd.notna(t) else -1 for t in users["frt"]],
        "lrt": [mins(t) if pd.notna(t) else -1 for t in users["lrt"]],
        "iap": [micro(x) for x in users["iap"]],
        "adr": [micro(x) for x in users["adr"]],
    }

    # The intraday table keeps growing while we query, so `facts` can contain users
    # the dimension query never saw. Drop those rather than emitting dangling indices.
    known = facts["u"].isin(u_ix)
    if not known.all():
        print(f"  segments: dropped {(~known).sum():,} fact rows for "
              f"{facts.loc[~known, 'u'].nunique():,} users that landed mid-run")
        facts = facts[known]

    facts = facts.sort_values(["u", "d"])  # sorted so the u column compresses well
    F = {
        "u": [u_ix[u] for u in facts["u"]],
        "d": [day_ix[str(d)] for d in facts["d"]],
        "v": [v_ix.get(int(v), -1) if pd.notna(v) else -1 for v in facts["v"]],
        "iap": [micro(x) for x in facts["iap"]],
        "adr": [micro(x) for x in facts["adr"]],
        "sess": [int(x) for x in facts["sess"]],
        "imp": [int(x) for x in facts["imp"]],
    }

    attributed = sum(1 for v in u_camp if v != "(unattributed)")
    print(f"  segments: {len(U['co']):,} users ({attributed:,} campaign-attributed, "
          f"{100*attributed/max(len(u_camp),1):.0f}%), {len(F['u']):,} user-day-version facts")
    print(f"  segments: {len(countries)} countries, {len(campaigns)} campaigns, "
          f"{len(versions)} versions, {len(days)} days")

    return {
        "seg": {
            "days": days, "countries": countries, "campaigns": campaigns,
            "versions": versions, "t0": t0, "users": U, "facts": F,
        }
    }


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
    data.update(build_segments(lo, hi))

    payload = json.dumps(data, separators=(",", ":"))  # compact: the segment cube adds ~1 MB of separators otherwise
    html = template.read_text(encoding="utf-8").replace("/*__DATA__*/", payload)
    index.write_text(html, encoding="utf-8", newline="")
    print(f"wrote {index.name} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
