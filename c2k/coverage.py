"""Coverage and gap report (SQL over the Parquet tables + tagged events).

Grids are built with CROSS JOINs against the full universe of cells
(vehicle x time-of-day bucket, vehicle x speed band, vehicle x event type), so
an empty cell shows up as an explicit zero instead of silently being absent.
"""
from __future__ import annotations

from pathlib import Path

from .convert import DONGLE_VEHICLE, MANIFEST, TABLE_NAMES
from .db import connect
from .events import BIN_S, KEYS

EVENT_TYPES = [
    "moderate_braking", "hard_braking", "steering_rate_high",
    "close_lead_caution", "close_lead_critical", "car_following",
]
TOD_BUCKETS = ["00-06", "06-12", "12-18", "18-24"]
SPEED_BANDS = ["<30", "30-60", "60-90", "90-120", ">=120"]  # km/h


def _values(items: list[str], col: str, alias: str) -> str:
    rows = ", ".join(f"('{i}')" for i in items)
    return f"(VALUES {rows}) {alias}({col})"


_VEH = "(SELECT unnest([{}]) AS vehicle UNION SELECT DISTINCT coalesce(vehicle, 'unknown') FROM segments)".format(
    ", ".join(f"'{v}'" for v in sorted(set(DONGLE_VEHICLE.values())))
)

# One row per segment: vehicle, duration in hours, and the clock hour at which it
# starts. The hour is derived from the *route name* (start of the route) plus the
# segment offset; upstream does not document the time zone of route names.
_SEG = """
seg AS (
  SELECT dongle_id, route, segment, coalesce(vehicle, 'unknown') AS vehicle,
         duration_s / 3600.0 AS hours,
         CAST(floor((CAST(substr(route, 13, 2) AS INT) * 3600
                   + CAST(substr(route, 16, 2) AS INT) * 60
                   + CAST(substr(route, 19, 2) AS INT) + segment * 60) / 3600.0) AS INT) % 24 AS hr
  FROM segments),
seg_b AS (
  SELECT *, CASE WHEN hr < 6 THEN '00-06' WHEN hr < 12 THEN '06-12'
                 WHEN hr < 18 THEN '12-18' ELSE '18-24' END AS tod
  FROM seg)"""


def _tod_sql() -> str:
    return f"""
WITH {_SEG.strip()}
SELECT v.vehicle, b.tod AS route_clock_hours,
       count(s.segment) AS segments,
       round(coalesce(sum(s.hours), 0), 4) AS driving_hours
FROM {_VEH} v CROSS JOIN {_values(TOD_BUCKETS, 'tod', 'b')}
LEFT JOIN seg_b s ON s.vehicle = v.vehicle AND s.tod = b.tod
GROUP BY ALL ORDER BY 1, 2"""


def _speed_sql() -> str:
    return f"""
WITH {_SEG.strip()},
sp AS (
  SELECT {KEYS}, CAST(floor(t_rel / {BIN_S}) AS BIGINT) AS bin, avg(speed_mps) * 3.6 AS kmh
  FROM can_speed GROUP BY ALL),
spb AS (
  SELECT s.vehicle,
         CASE WHEN kmh < 30 THEN '<30' WHEN kmh < 60 THEN '30-60' WHEN kmh < 90 THEN '60-90'
              WHEN kmh < 120 THEN '90-120' ELSE '>=120' END AS band,
         count(*) * {BIN_S} / 3600.0 AS hours
  FROM sp JOIN seg s USING ({KEYS}) GROUP BY ALL)
SELECT v.vehicle, b.band AS speed_kmh, round(coalesce(spb.hours, 0), 4) AS driving_hours
FROM {_VEH} v CROSS JOIN {_values(SPEED_BANDS, 'band', 'b')}
LEFT JOIN spb ON spb.vehicle = v.vehicle AND spb.band = b.band
ORDER BY 1, (CASE b.band WHEN '<30' THEN 0 WHEN '30-60' THEN 1 WHEN '60-90' THEN 2
                         WHEN '90-120' THEN 3 ELSE 4 END)"""


def _event_rate_sql() -> str:
    return f"""
WITH {_SEG.strip()},
ev AS (SELECT s.vehicle, e.event_type, count(*) AS n
       FROM events e JOIN seg s USING ({KEYS}) GROUP BY ALL),
hrs AS (SELECT vehicle, sum(hours) AS h FROM seg GROUP BY 1)
SELECT v.vehicle, t.event_type, coalesce(ev.n, 0) AS events,
       round(coalesce(hrs.h, 0), 4) AS driving_hours,
       CASE WHEN coalesce(hrs.h, 0) > 0 THEN round(coalesce(ev.n, 0) / hrs.h, 2) END AS events_per_hour
FROM {_VEH} v CROSS JOIN {_values(EVENT_TYPES, 'event_type', 't')}
LEFT JOIN ev ON ev.vehicle = v.vehicle AND ev.event_type = t.event_type
LEFT JOIN hrs ON hrs.vehicle = v.vehicle
ORDER BY 1, array_position([{', '.join(repr(t) for t in EVENT_TYPES)}], t.event_type)"""


def _signal_health_sql() -> str:
    per_table = []
    for t in TABLE_NAMES:
        per_table.append(f"""
SELECT '{t}' AS signal, count(*) AS segments_with_data, coalesce(sum(n), 0) AS rows,
       round(median(hz), 2) AS median_hz, round(max(max_gap), 3) AS max_gap_s
FROM (
  SELECT {KEYS}, count(*) AS n,
         CASE WHEN max(t_rel) > min(t_rel) THEN (count(*) - 1) / (max(t_rel) - min(t_rel)) END AS hz,
         max(dt) AS max_gap
  FROM (SELECT *, t_rel - lag(t_rel) OVER (PARTITION BY {KEYS} ORDER BY t_rel) AS dt FROM {t})
  GROUP BY {KEYS})""")
    union = "\nUNION ALL\n".join(per_table)
    return f"""
WITH total AS (SELECT count(*) AS n FROM {MANIFEST}),
h AS ({union})
SELECT h.signal, total.n AS segments_total, h.segments_with_data,
       round(100.0 * h.segments_with_data / nullif(total.n, 0), 1) AS pct_segments_present,
       h.rows, h.median_hz, h.max_gap_s
FROM h CROSS JOIN total"""


def _missing_sql() -> str:
    parts = [
        f"SELECT {KEYS}, '{t}' AS missing_signal FROM {MANIFEST} WHERE n_{t} = 0"
        for t in TABLE_NAMES
    ]
    return "\nUNION ALL\n".join(parts) + f"\nORDER BY {KEYS}, missing_signal"


def _scope_sql() -> str:
    return f"""
SELECT count(*) AS segments, round(coalesce(sum(duration_s), 0) / 3600.0, 4) AS driving_hours,
       count(DISTINCT coalesce(vehicle, 'unknown')) AS vehicles,
       count(DISTINCT dongle_id || route) AS routes,
       min(substr(route, 1, 10)) AS first_route_date, max(substr(route, 1, 10)) AS last_route_date
FROM {MANIFEST}"""


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _md_table(headers: list[str], rows: list[tuple]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(_fmt(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _q(con, sql: str) -> tuple[list[str], list[tuple]]:
    rel = con.sql(sql)
    return list(rel.columns), rel.fetchall()


def run(parquet_root: Path, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    events_path = out_dir / "events.parquet"
    if not events_path.exists():
        raise FileNotFoundError(f"{events_path} not found - run the 'events' step first")
    con = connect(parquet_root)
    ep = str(events_path).replace("'", "''")
    con.execute(f"CREATE VIEW events AS SELECT * FROM read_parquet('{ep}')")

    outputs = {
        "coverage_vehicle_timeofday": _tod_sql(),
        "coverage_vehicle_speedband": _speed_sql(),
        "event_rates": _event_rate_sql(),
        "signal_health": _signal_health_sql(),
        "missing_signals": _missing_sql(),
    }
    results: dict[str, tuple[list[str], list[tuple]]] = {}
    for name, sql in outputs.items():
        results[name] = _q(con, sql)
        target = str(out_dir / f"{name}.csv").replace("'", "''")
        con.execute(f"COPY ({sql}) TO '{target}' (FORMAT csv, HEADER)")

    scope_h, scope_r = _q(con, _scope_sql())
    scope = dict(zip(scope_h, scope_r[0]))
    params_file = out_dir / "event_params.txt"
    params = params_file.read_text(encoding="utf-8").strip().splitlines() if params_file.exists() else []

    md = [
        "# comma2k19 coverage and gap report",
        "",
        f"**Scope:** {scope['segments']} segment(s), {scope['driving_hours']} driving hours, "
        f"{scope['routes']} route(s), {scope['vehicles']} vehicle(s), "
        f"route dates {scope['first_route_date']} to {scope['last_route_date']}.",
        "",
        "Cells with 0 are real gaps in *this* conversion, not necessarily in the full dataset.",
        "",
        "## Driving hours by vehicle and route clock hour",
        "",
        "Clock hour is derived from the route name (route start plus segment offset); "
        "upstream does not document its time zone.",
        "",
        _md_table(*results["coverage_vehicle_timeofday"]),
        "",
        "## Driving hours by vehicle and speed band",
        "",
        _md_table(*results["coverage_vehicle_speedband"]),
        "",
        "## Tagged events per driving hour",
        "",
        *(["> Warning: under one hour of driving in total, so per-hour rates below are not "
           "statistically meaningful. Use the raw event counts.", ""]
          if (scope["driving_hours"] or 0) < 1 else []),
        _md_table(*results["event_rates"]),
        "",
        "## Signal health",
        "",
        "`median_hz` is per segment; radar rows are interleaved across tracks, so its rate is the aggregate over tracks.",
        "",
        _md_table(*results["signal_health"]),
        "",
        "## Segments missing a signal",
        "",
    ]
    missing = results["missing_signals"]
    md.append(_md_table(*missing) if missing[1] else "None: every converted segment has every signal.")
    md += ["", "## Event thresholds used", "", "```", *params, "```", ""]
    (out_dir / "REPORT.md").write_text("\n".join(md), encoding="utf-8")
