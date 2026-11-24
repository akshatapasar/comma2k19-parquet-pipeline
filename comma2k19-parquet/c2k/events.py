"""Event tagging in SQL (DuckDB) on top of the partitioned Parquet tables.

Event types
-----------
moderate_braking / hard_braking
    Deceleration from CAN speed, measured over 0.5 s windows on a 10 Hz grid.
steering_rate_high
    Steering-wheel angle rate (deg/s) over 0.5 s windows.
close_lead_caution / close_lead_critical
    Time-to-collision (range / closing speed) to a *moving* in-lane radar target.
car_following
    A moving in-lane radar target within range for a sustained period.

Every threshold is a parameter (see ``DEFAULTS``). The defaults are generic
a-priori values for highway driving, not tuned to any single segment.

Known limitations (also in the README)
--------------------------------------
* Radar cannot separate a stopped vehicle from static infrastructure, so
  targets whose absolute speed (ego speed + relative speed) is below
  ``lead_min_speed_mps`` are excluded from lead-vehicle events.
* The IMU is not used: its axes are not guaranteed to align with the vehicle.
* Radar sign convention assumed: negative relative speed = closing.
"""
from __future__ import annotations

from pathlib import Path

from .db import connect

BIN_S = 0.1  # grid step (s)
WIN = 5  # derivative window in bins -> 0.5 s

DEFAULTS: dict[str, float | int] = {
    "brake_flag_mps2": 2.0,
    "brake_hard_mps2": 3.0,
    "brake_min_bins": 3,
    "brake_tol_bins": 2,
    "steer_rate_degps": 15.0,
    "steer_min_bins": 2,
    "steer_tol_bins": 2,
    "lead_max_range_m": 60.0,
    "lead_max_lat_m": 1.8,
    "lead_min_speed_mps": 2.0,
    "ttc_caution_s": 4.0,
    "ttc_critical_s": 2.0,
    "ttc_min_bins": 2,
    "ttc_tol_bins": 2,
    "follow_min_bins": 30,
    "follow_tol_bins": 3,
}

KEYS = "dongle_id, route, segment"


def _params(overrides: dict | None) -> dict:
    p = dict(DEFAULTS)
    for k, v in (overrides or {}).items():
        if k not in DEFAULTS:
            raise KeyError(f"unknown parameter {k!r}; valid: {', '.join(DEFAULTS)}")
        p[k] = int(v) if isinstance(DEFAULTS[k], int) else float(v)
    return p


def _islands(src: str, min_bins: int, tol: int, agg: str) -> str:
    """Gaps-and-islands: group flagged bins separated by <= ``tol`` missing bins.

    ``src`` must expose dongle_id, route, segment, bin and m (the metric).
    """
    return f"""
SELECT {KEYS}, min(bin) AS b0, max(bin) AS b1, {agg} AS peak
FROM (
  SELECT *, sum(brk) OVER (PARTITION BY {KEYS} ORDER BY bin
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS g
  FROM (
    SELECT *, CASE WHEN bin - lag(bin) OVER (PARTITION BY {KEYS} ORDER BY bin) > {tol + 1}
                   THEN 1 ELSE 0 END AS brk
    FROM {src}
  )
)
GROUP BY {KEYS}, g
HAVING count(*) >= {min_bins}"""


_S10 = f"""
s10_speed AS (
  SELECT {KEYS}, CAST(floor(t_rel / {BIN_S}) AS BIGINT) AS bin, avg(speed_mps) AS v
  FROM can_speed GROUP BY ALL)"""


def _windowed(cte: str) -> str:
    """Derivative over WIN bins of the 10 Hz series ``cte`` (columns bin, v)."""
    return f"""
SELECT a.dongle_id, a.route, a.segment, a.bin, (a.v - b.v) / {WIN * BIN_S} AS m
FROM {cte} a JOIN {cte} b
  ON a.dongle_id = b.dongle_id AND a.route = b.route AND a.segment = b.segment
 AND b.bin = a.bin - {WIN}"""


def braking_sql(p: dict) -> str:
    return f"""
WITH {_S10.strip()},
acc AS ({_windowed('s10_speed')}),
flagged AS (SELECT * FROM acc WHERE m <= -{p['brake_flag_mps2']}),
isl AS ({_islands('flagged', p['brake_min_bins'], p['brake_tol_bins'], 'min(m)')})
SELECT {KEYS},
  CASE WHEN peak <= -{p['brake_hard_mps2']} THEN 'hard_braking' ELSE 'moderate_braking' END AS event_type,
  round(greatest(0, (b0 - {WIN}) * {BIN_S}), 3) AS t_start_s,
  round((b1 + 1) * {BIN_S}, 3) AS t_end_s,
  -peak AS peak_value, 'm/s2 (decel)' AS peak_unit
FROM isl"""


def steering_sql(p: dict) -> str:
    return f"""
WITH s10_steer AS (
  SELECT {KEYS}, CAST(floor(t_rel / {BIN_S}) AS BIGINT) AS bin, avg(steering_angle_deg) AS v
  FROM can_steering GROUP BY ALL),
rate AS (SELECT dongle_id, route, segment, bin, abs(m) AS m FROM ({_windowed('s10_steer')})),
flagged AS (SELECT * FROM rate WHERE m >= {p['steer_rate_degps']}),
isl AS ({_islands('flagged', p['steer_min_bins'], p['steer_tol_bins'], 'max(m)')})
SELECT {KEYS}, 'steering_rate_high' AS event_type,
  round(greatest(0, (b0 - {WIN}) * {BIN_S}), 3) AS t_start_s,
  round((b1 + 1) * {BIN_S}, 3) AS t_end_s,
  peak AS peak_value, 'deg/s' AS peak_unit
FROM isl"""


def _lead_cte(p: dict) -> str:
    """In-lane, moving radar targets with per-bin range and TTC."""
    return f"""
{_S10.strip()},
radar_binned AS (
  SELECT *, CAST(floor(t_rel / {BIN_S}) AS BIGINT) AS bin FROM can_radar),
lead AS (
  SELECT r.dongle_id, r.route, r.segment, r.bin,
         r.lead_dist_long_m AS d, r.rel_speed_mps AS rv
  FROM radar_binned r
  JOIN s10_speed e ON e.dongle_id = r.dongle_id AND e.route = r.route
                  AND e.segment = r.segment AND e.bin = r.bin
  WHERE r.lead_dist_long_m > 0 AND r.lead_dist_long_m <= {p['lead_max_range_m']}
    AND abs(r.lead_dist_lat_m) <= {p['lead_max_lat_m']}
    AND e.v + r.rel_speed_mps >= {p['lead_min_speed_mps']})"""


def close_lead_sql(p: dict) -> str:
    return f"""
WITH {_lead_cte(p).strip()},
ttc AS (
  SELECT {KEYS}, bin, min(d / -rv) AS m FROM lead WHERE rv < 0 GROUP BY ALL),
flagged AS (SELECT * FROM ttc WHERE m < {p['ttc_caution_s']}),
isl AS ({_islands('flagged', p['ttc_min_bins'], p['ttc_tol_bins'], 'min(m)')})
SELECT {KEYS},
  CASE WHEN peak < {p['ttc_critical_s']} THEN 'close_lead_critical' ELSE 'close_lead_caution' END AS event_type,
  round(b0 * {BIN_S}, 3) AS t_start_s, round((b1 + 1) * {BIN_S}, 3) AS t_end_s,
  peak AS peak_value, 's (min TTC)' AS peak_unit
FROM isl"""


def car_following_sql(p: dict) -> str:
    return f"""
WITH {_lead_cte(p).strip()},
rng AS (SELECT {KEYS}, bin, min(d) AS m FROM lead GROUP BY ALL),
isl AS ({_islands('rng', p['follow_min_bins'], p['follow_tol_bins'], 'min(m)')})
SELECT {KEYS}, 'car_following' AS event_type,
  round(b0 * {BIN_S}, 3) AS t_start_s, round((b1 + 1) * {BIN_S}, 3) AS t_end_s,
  peak AS peak_value, 'm (min range)' AS peak_unit
FROM isl"""


def events_sql(p: dict) -> str:
    parts = [braking_sql(p), steering_sql(p), close_lead_sql(p), car_following_sql(p)]
    body = "\nUNION ALL\n".join(f"SELECT * FROM ({q})" for q in parts)
    return f"""
SELECT {KEYS}, event_type, t_start_s, t_end_s,
       round(t_end_s - t_start_s, 3) AS duration_s, round(peak_value, 3) AS peak_value, peak_unit
FROM ({body})
ORDER BY {KEYS}, t_start_s, event_type"""


def run(parquet_root: Path, out_dir: Path, overrides: dict | None = None) -> int:
    """Tag events; write ``events.parquet`` and ``events.csv`` into ``out_dir``."""
    p = _params(overrides)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    con = connect(parquet_root)
    con.execute(f"CREATE TABLE events AS {events_sql(p)}")
    for ext, opts in (("parquet", "FORMAT parquet, COMPRESSION zstd"), ("csv", "FORMAT csv, HEADER")):
        target = str(out_dir / f"events.{ext}").replace("'", "''")
        con.execute(f"COPY events TO '{target}' ({opts})")
    (out_dir / "event_params.txt").write_text(
        "".join(f"{k}={v}\n" for k, v in p.items()), encoding="utf-8"
    )
    return con.sql("SELECT count(*) FROM events").fetchall()[0][0]
