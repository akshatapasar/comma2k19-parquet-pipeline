"""End-to-end tests on a synthetic segment with known, injected events.

Ground truth injected into segment A (60 s, 30 m/s cruise):
  * t=20-22 s : braking at 5 m/s^2                      -> hard_braking
  * t=40 s    : 40 deg steering ramp over 0.5 s          -> steering_rate_high
  * t=10-14 s : moving in-lane lead, 24 m ahead,
                closing at 15 m/s (TTC 1.6 s)            -> close_lead_critical + car_following
  * t=30-36 s : STATIC in-lane object, 40 m ahead
                (ego speed + relative speed = 0)         -> must NOT be tagged
Segment B has no radar files at all (missing-signal handling).
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pytest

from c2k import convert, coverage, events
from c2k.db import connect

T0 = 1000.0
DONGLE = "b0c9d2329ad1606b"
ROUTE = "2018-08-02--08-34-47"


def _save(path: Path, arr: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:  # no .npy suffix, like upstream
        np.save(f, arr)


def _ego_speed(t_rel: np.ndarray) -> np.ndarray:
    v = np.full_like(t_rel, 30.0)
    braking = (t_rel >= 20) & (t_rel < 22)
    v[braking] = 30.0 - 5.0 * (t_rel[braking] - 20)
    v[t_rel >= 22] = 20.0
    return v


def _make_segment(root: Path, seg_no: int, radar: bool = True, pose: bool = False) -> Path:
    seg = root / "Chunk_1" / f"{DONGLE}|{ROUTE}" / str(seg_no)
    pl = seg / "processed_log"
    t_rel = np.arange(0, 60, 1 / 83.0)
    t = T0 + t_rel
    _save(pl / "CAN/speed/t", t)
    _save(pl / "CAN/speed/value", _ego_speed(t_rel).reshape(-1, 1))

    angle = np.zeros_like(t_rel)
    ramp = (t_rel >= 40) & (t_rel < 40.5)
    angle[ramp] = 40.0 * (t_rel[ramp] - 40) / 0.5
    angle[t_rel >= 40.5] = 40.0
    _save(pl / "CAN/steering_angle/t", t)
    _save(pl / "CAN/steering_angle/value", angle)  # 1-D, as upstream

    if radar:
        rt = np.arange(0, 60, 0.01)
        rows_t, rows_v = [], []
        v_ego = _ego_speed(rt)
        for i, tr in enumerate(rt):
            if 10 <= tr < 14:  # moving lead
                rows_t.append(T0 + tr)
                rows_v.append([24.0, 0.0, -15.0, np.nan, np.nan, 528.0, 0.0])
            if 30 <= tr < 36:  # static object: closes at exactly ego speed
                rows_t.append(T0 + tr)
                rows_v.append([40.0, 0.0, -v_ego[i], np.nan, np.nan, 529.0, 0.0])
        _save(pl / "CAN/radar/t", np.array(rows_t))
        _save(pl / "CAN/radar/value", np.array(rows_v))

    if pose:
        n = 1200
        gp = seg / "global_pose"
        _save(gp / "frame_times", T0 + np.arange(n) / 20.0)
        _save(gp / "frame_gps_times", np.column_stack([np.full(n, 2012.0), np.arange(n) / 20.0]))
        _save(gp / "frame_positions", np.zeros((n, 3)))
        _save(gp / "frame_velocities", np.zeros((n, 3)))
        _save(gp / "frame_orientations", np.tile([1.0, 0, 0, 0], (n, 1)))
    return seg


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    base = tmp_path_factory.mktemp("c2k")
    src, pq_root, res = base / "src", base / "parquet", base / "results"
    _make_segment(src, 3, radar=True, pose=True)
    _make_segment(src, 4, radar=False)
    summary = convert.convert(src, pq_root)
    n_events = events.run(pq_root, res)
    coverage.run(pq_root, res)
    return {"src": src, "pq": pq_root, "res": res, "summary": summary, "n_events": n_events}


def _events(pipeline) -> list[dict]:
    rel = duckdb.sql(f"SELECT * FROM read_parquet('{pipeline['res'] / 'events.parquet'}') "
                     f"ORDER BY segment, t_start_s")
    cols = rel.columns
    return [dict(zip(cols, r)) for r in rel.fetchall()]


def test_conversion_summary(pipeline):
    s = pipeline["summary"]
    assert (s["found"], s["converted"], s["errors"]) == (2, 2, [])


def test_partition_layout_and_types(pipeline):
    f = pipeline["pq"] / "can_speed" / f"dongle_id={DONGLE}" / f"route={ROUTE}" / "segment=3" / "data.parquet"
    assert f.exists()
    con = connect(pipeline["pq"])
    row = con.sql("SELECT typeof(dongle_id), typeof(route), typeof(segment) FROM segments LIMIT 1").fetchall()[0]
    assert row == ("VARCHAR", "VARCHAR", "INTEGER")


def test_manifest_counts_and_vehicle(pipeline):
    con = connect(pipeline["pq"])
    got = {r[0]: r[1:] for r in con.sql(
        "SELECT segment, vehicle, chunk, n_can_speed, n_can_radar, n_pose FROM segments").fetchall()}
    assert got[3][0] == "RAV4" and got[3][1] == 1
    assert got[3][2] == len(np.arange(0, 60, 1 / 83.0))
    assert got[3][3] > 0 and got[3][4] == 1200
    assert got[4][3] == 0 and got[4][4] == 0  # no radar, no pose


def test_rerun_skips_and_force_reconverts(pipeline):
    pq2 = pipeline["pq"]
    again = convert.convert(pipeline["src"], pq2)
    assert again["skipped"] == 2 and again["converted"] == 0
    forced = convert.convert(pipeline["src"], pq2, force=True)
    assert forced["converted"] == 2


def test_hard_braking_detected(pipeline):
    ev = [e for e in _events(pipeline) if e["event_type"] == "hard_braking" and e["segment"] == 3]
    assert len(ev) == 1
    assert 19.0 < ev[0]["t_start_s"] < 22.0 and 21.0 < ev[0]["t_end_s"] < 23.5
    assert 4.0 <= ev[0]["peak_value"] <= 5.5


def test_steering_event_detected(pipeline):
    ev = [e for e in _events(pipeline) if e["event_type"] == "steering_rate_high" and e["segment"] == 3]
    assert len(ev) == 1 and 39.0 < ev[0]["t_start_s"] < 41.0
    assert ev[0]["peak_value"] >= 40.0


def test_moving_lead_tagged_and_static_object_not(pipeline):
    ev = _events(pipeline)
    crit = [e for e in ev if e["event_type"] == "close_lead_critical" and e["segment"] == 3]
    assert len(crit) == 1 and 9.5 < crit[0]["t_start_s"] < 10.5 and 13.5 < crit[0]["t_end_s"] < 14.5
    assert crit[0]["peak_value"] == pytest.approx(1.6, abs=0.05)
    follow = [e for e in ev if e["event_type"] == "car_following" and e["segment"] == 3]
    assert len(follow) == 1
    # The static object (t=30-36 s) closes at TTC 2 s but must be excluded.
    assert not any(e["event_type"].startswith(("close_lead", "car_following")) and e["t_start_s"] >= 29
                   for e in ev if e["segment"] == 3)


def test_segment_without_radar_has_no_lead_events(pipeline):
    types = {e["event_type"] for e in _events(pipeline) if e["segment"] == 4}
    # CAN-based events still fire (segment B carries the same speed/steering signals) ...
    assert {"hard_braking", "steering_rate_high"} <= types
    # ... but nothing radar-based can be produced without radar data.
    assert not any(t.startswith(("close_lead", "car_following")) for t in types)


def test_report_flags_missing_signal_and_zero_cells(pipeline):
    res = pipeline["res"]
    text = (res / "REPORT.md").read_text()
    assert "Segments missing a signal" in text and "not statistically meaningful" in text
    missing = duckdb.sql(f"SELECT segment, missing_signal FROM read_csv('{res / 'missing_signals.csv'}')").fetchall()
    assert (4, "can_radar") in missing and (3, "can_radar") not in missing
    tod = duckdb.sql(f"SELECT count(*) FROM read_csv('{res / 'coverage_vehicle_timeofday.csv'}') "
                     f"WHERE driving_hours = 0").fetchall()[0][0]
    assert tod == 7  # 2 vehicles x 4 buckets = 8 cells; only one holds data


def test_path_parsing():
    seg = Path("/data/Chunk_7") / f"{DONGLE}_{ROUTE}" / "12"  # '|' replaced by '_' on NTFS
    assert convert.parse_segment_path(seg) == (DONGLE, ROUTE, 12, 7)
    with pytest.raises(ValueError):
        convert.parse_segment_path(Path("/data/not-a-route/3"))


def test_malformed_segment_reported_not_raised(tmp_path):
    seg = _make_segment(tmp_path / "src", 9)
    _save(seg / "processed_log/CAN/speed/value", np.zeros((5, 1)))  # wrong length
    s = convert.convert(tmp_path / "src", tmp_path / "pq")
    assert s["converted"] == 0 and len(s["errors"]) == 1 and "unexpected shape" in s["errors"][0]["error"]
