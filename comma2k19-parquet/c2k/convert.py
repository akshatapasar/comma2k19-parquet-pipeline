"""Convert comma2k19 segments (NumPy arrays) into hive-partitioned Parquet.

Input layout (see the comma2k19 README)::

    <root>/[Chunk_n/]<dongle_id>|<route_start>/<segment>/processed_log/...
                                                       /global_pose/...

Output layout (one file per segment per table)::

    <out>/<table>/dongle_id=<id>/route=<route_start>/segment=<n>/data.parquet

``dongle_id``, ``route`` and ``segment`` live only in the directory names
(standard hive partitioning); they are not repeated inside the files.
A segment is considered converted once its ``segments`` manifest file exists,
so the manifest doubles as a commit marker and re-runs skip finished work.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

# From the comma2k19 README: chunks 1-2 are the RAV4, the rest the Civic.
DONGLE_VEHICLE = {
    "b0c9d2329ad1606b": "RAV4",
    "99c94dc769b5d96e": "Civic",
}

ROUTE_DIR_RE = re.compile(
    r"^(?P<dongle>[0-9a-f]{16})[|_](?P<route>\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2})$"
)
CHUNK_RE = re.compile(r"[Cc]hunk_?(\d+)")


@dataclass(frozen=True)
class TableSpec:
    """How one ``processed_log`` signal maps to a Parquet table."""

    name: str
    rel: str  # path below processed_log/
    columns: tuple[str, ...]  # one name per column of the ``value`` array
    drop: tuple[str, ...] = ()
    ints: tuple[str, ...] = ()
    bools: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)

    @property
    def kept(self) -> list[str]:
        return [c for c in self.columns if c not in self.drop]

    def sql_type(self, col: str) -> str:
        if col in self.ints:
            return "BIGINT"
        if col in self.bools:
            return "BOOLEAN"
        return "DOUBLE"


_GNSS_COLS = (
    "lat_deg",
    "lon_deg",
    "speed_mps",
    "utc_timestamp_raw",
    "alt_m",
    "bearing_deg",
)

SPECS: tuple[TableSpec, ...] = (
    TableSpec("can_speed", "CAN/speed", ("speed_mps",)),
    TableSpec("can_steering", "CAN/steering_angle", ("steering_angle_deg",)),
    TableSpec(
        "can_wheel_speed",
        "CAN/wheel_speed",
        ("fl_mps", "fr_mps", "rl_mps", "rr_mps"),
    ),
    TableSpec(
        "can_radar",
        "CAN/radar",
        (
            "lead_dist_long_m",
            "lead_dist_lat_m",
            "rel_speed_mps",
            "_nan1",
            "_nan2",
            "address",
            "new_track",
        ),
        drop=("_nan1", "_nan2"),
        ints=("address",),
        bools=("new_track",),
    ),
    TableSpec("imu_accel", "IMU/accelerometer", ("ax", "ay", "az")),
    TableSpec("imu_gyro", "IMU/gyro", ("gx", "gy", "gz")),
    TableSpec("gnss_ublox", "GNSS/live_gnss_ublox", _GNSS_COLS),
    TableSpec("gnss_qcom", "GNSS/live_gnss_qcom", _GNSS_COLS),
)

POSE_COLUMNS = (
    ["gps_week", "gps_tow_s"]
    + ["ecef_x_m", "ecef_y_m", "ecef_z_m"]
    + ["vel_x_mps", "vel_y_mps", "vel_z_mps"]
    + ["q0", "q1", "q2", "q3"]  # component order is not documented upstream
)

TABLE_NAMES = [s.name for s in SPECS] + ["pose"]
MANIFEST = "segments"


def _load(path: Path) -> np.ndarray:
    return np.load(path, allow_pickle=False)


def parse_segment_path(seg: Path) -> tuple[str, str, int, int | None]:
    """Return (dongle_id, route, segment, chunk) for a segment directory."""
    m = ROUTE_DIR_RE.match(seg.parent.name)
    if not m or not seg.name.isdigit():
        raise ValueError(f"not a comma2k19 segment path: {seg}")
    chunk = None
    for part in reversed(seg.parts):
        c = CHUNK_RE.fullmatch(part)
        if c:
            chunk = int(c.group(1))
            break
    return m["dongle"], m["route"], int(seg.name), chunk


def find_segments(root: Path):
    """Yield segment directories (those containing ``processed_log``)."""
    root = root.resolve()
    for marker in sorted(root.rglob("processed_log")):
        seg = marker.parent
        try:
            seg.resolve().relative_to(root)
        except ValueError:
            continue  # symlink pointing outside the dataset root
        yield seg


def part_dir(out: Path, table: str, dongle: str, route: str, segment: int) -> Path:
    return (
        out
        / table
        / f"dongle_id={dongle}"
        / f"route={route}"
        / f"segment={segment}"
    )


def _arrow_signal(t: np.ndarray, t0: float, values: np.ndarray, spec: TableSpec):
    cols = {"t": pa.array(t, type=pa.float64()), "t_rel": pa.array(t - t0, type=pa.float64())}
    for i, name in enumerate(spec.columns):
        if name in spec.drop:
            continue
        col = values[:, i]
        nan = np.isnan(col)
        if name in spec.ints:
            cols[name] = pa.array(np.where(nan, 0, col).astype("int64"), mask=nan)
        elif name in spec.bools:
            cols[name] = pa.array(np.where(nan, 0, col).astype(bool), mask=nan)
        else:
            cols[name] = pa.array(col, type=pa.float64())
    return pa.table(cols)


def _write(table: pa.Table, path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    final = path / "data.parquet"
    tmp = path / ".data.parquet.tmp"
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, final)


def _pose_table(seg: Path, t0: float) -> pa.Table | None:
    gp = seg / "global_pose"
    try:
        times = _load(gp / "frame_times")
        gps = _load(gp / "frame_gps_times")
        pos = _load(gp / "frame_positions")
        vel = _load(gp / "frame_velocities")
        ori = _load(gp / "frame_orientations")
    except FileNotFoundError:
        return None
    n = len(times)
    if not (gps.shape == (n, 2) and pos.shape == (n, 3) and vel.shape == (n, 3) and ori.shape == (n, 4)):
        raise ValueError("global_pose arrays have inconsistent shapes")
    stacked = np.column_stack([gps, pos, vel, ori])
    cols = {
        "t": pa.array(times, type=pa.float64()),
        "t_rel": pa.array(times - t0, type=pa.float64()),
    }
    for i, name in enumerate(POSE_COLUMNS):
        cols[name] = pa.array(stacked[:, i], type=pa.float64())
    return pa.table(cols)


def _segment_t0(seg: Path) -> float | None:
    """Segment time origin: first camera-frame time, else first CAN speed time."""
    for rel in (Path("global_pose/frame_times"), Path("processed_log/CAN/speed/t")):
        try:
            arr = _load(seg / rel)
        except FileNotFoundError:
            continue
        if arr.size:
            return float(arr[0])
    return None


def convert_segment(seg: Path, out: Path, force: bool = False) -> dict:
    """Convert one segment. Returns a status dict (never raises)."""
    seg = Path(seg)
    try:
        dongle, route, segment, chunk = parse_segment_path(seg)
        manifest_dir = part_dir(out, MANIFEST, dongle, route, segment)
        if (manifest_dir / "data.parquet").exists() and not force:
            return {"seg": str(seg), "status": "skipped"}

        t0 = _segment_t0(seg)
        if t0 is None:
            raise ValueError("no usable time reference (no pose or CAN speed)")

        counts: dict[str, int] = {}
        t_last = t0
        for spec in SPECS:
            base = seg / "processed_log" / spec.rel
            try:
                t = _load(base / "t")
                v = _load(base / "value")
            except FileNotFoundError:
                counts[spec.name] = 0
                continue
            v = v.reshape(len(t), -1) if v.ndim == 1 else v
            if v.shape[0] != t.shape[0] or v.shape[1] != len(spec.columns):
                raise ValueError(f"{spec.name}: unexpected shape {v.shape} for {t.shape[0]} timestamps")
            counts[spec.name] = int(t.shape[0])
            if t.size:
                t_last = max(t_last, float(t[-1]))
                _write(_arrow_signal(t, t0, v.astype("float64"), spec),
                       part_dir(out, spec.name, dongle, route, segment))

        pose = _pose_table(seg, t0)
        counts["pose"] = pose.num_rows if pose is not None else 0
        if pose is not None and pose.num_rows:
            _write(pose, part_dir(out, "pose", dongle, route, segment))
            t_last = max(t_last, float(pose.column("t")[-1].as_py()))

        row = {
            "chunk": pa.array([chunk], type=pa.int32()),
            "vehicle": pa.array([DONGLE_VEHICLE.get(dongle)], type=pa.string()),
            "source": pa.array([_relative_source(seg)], type=pa.string()),
            "t0": pa.array([t0], type=pa.float64()),
            "duration_s": pa.array([t_last - t0], type=pa.float64()),
        }
        for name in TABLE_NAMES:
            row[f"n_{name}"] = pa.array([counts.get(name, 0)], type=pa.int64())
        _write(pa.table(row), manifest_dir)  # written last = commit marker
        return {"seg": str(seg), "status": "converted", "rows": sum(counts.values())}
    except Exception as exc:  # noqa: BLE001 - report per segment, keep going
        return {"seg": str(seg), "status": "error", "error": f"{type(exc).__name__}: {exc}"}


def _relative_source(seg: Path) -> str:
    # Keep the dongle|route/segment tail (plus chunk dir when present), not absolute paths.
    parts = seg.parts[-3:]
    if CHUNK_RE.fullmatch(seg.parts[-3]):
        return "/".join(parts)
    return "/".join(seg.parts[-2:])


def convert(src: Path, out: Path, workers: int = 1, force: bool = False,
            max_segments: int | None = None) -> dict:
    segs = list(find_segments(src))
    if max_segments is not None:
        segs = segs[:max_segments]
    results = []
    if workers <= 1:
        for s in segs:
            results.append(convert_segment(s, out, force))
            _progress(results[-1], len(results), len(segs))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(convert_segment, s, out, force) for s in segs]
            for f in as_completed(futs):
                results.append(f.result())
                _progress(results[-1], len(results), len(segs))
    summary = {k: sum(r["status"] == k for r in results) for k in ("converted", "skipped", "error")}
    summary["found"] = len(segs)
    summary["errors"] = [r for r in results if r["status"] == "error"]
    return summary


def _progress(res: dict, i: int, n: int) -> None:
    tail = res.get("error", "")
    print(f"[{i}/{n}] {res['status']:9s} {Path(res['seg']).parent.name}/{Path(res['seg']).name} {tail}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", required=True, type=Path, help="dataset root (contains segment dirs)")
    ap.add_argument("--out", required=True, type=Path, help="output Parquet root")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--force", action="store_true", help="re-convert finished segments")
    ap.add_argument("--max-segments", type=int, default=None)
    a = ap.parse_args(argv)
    s = convert(a.src, a.out, a.workers, a.force, a.max_segments)
    print(f"found={s['found']} converted={s['converted']} skipped={s['skipped']} errors={len(s['errors'])}")
    return 1 if s["errors"] or s["found"] == 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
