![CI](https://github.com/akshatapasar/comma2k19-parquet-pipeline/actions/workflows/ci.yml/badge.svg)
## Project Scope & Core Questions

This repository implements a reproducible data lakehouse and scenario curation pipeline over the **comma2k19** highway driving dataset. It is designed to answer three core questions:

1. **Scenario Coverage:** Which driving regimes and kinematic events (e.g., hard braking, close lead-vehicle approaches, sharp steering, dense radar traffic, stop-and-go) are well represented across segments, and which safety-critical edge cases are rare or missing?
2. **Dataset Representativeness:** How heavily is this comma2k19 slice skewed toward steady highway cruising versus urban/low-speed maneuvering, and how evenly are recordings distributed across vehicles (`dongle_id`) and times of day?
3. **Curated Snapshot Effectiveness:** Does a stratified snapshot policy (retaining 100% of rare safety-critical scenarios while downsampling routine highway cruising) measurably close coverage gaps compared to the raw ingested dataset?


# comma2k19 → partitioned Parquet → SQL event tagging

A small, reproducible data-platform and data-curation pipeline for the open
[comma2k19](https://github.com/commaai/comma2k19) driving dataset
(33 h of highway commuting, 2019 one-minute segments, MIT-licensed):

1. **Convert** per-signal NumPy logs into hive-partitioned, zstd-compressed Parquet (one file per segment per table).
2. **Query** them with SQL (DuckDB), including the event tagging.
3. **Tag events** (braking, steering, lead-vehicle approach, car following) with tunable thresholds.
4. **Report coverage and gaps** by vehicle, time of day, speed band and signal availability, with empty cells shown as explicit zeros.

Code, sample data and results are all in this repo and publishable under the licences below.
## Pipeline Architecture

1. **Object Storage Lakehouse (MinIO + Parquet):** Raw per-signal NumPy logs are converted into Hive-partitioned, `zstd`-compressed Parquet tables stored in S3-compatible object storage (`s3://comma2k19-lake/`).
2. **SQL Querying & Data Quality (DuckDB + pytest):** DuckDB queries Parquet directly from object storage via `httpfs`, validated by automated data quality checks (nulls, duplicate timestamps, sampling gaps, schema drift).
3. **Feature Extraction & Versioned Taxonomy (YAML):** Segment-level kinematic and radar features are tagged against a version-controlled YAML scenario taxonomy (`taxonomy/v1.0.0.yaml`).
4. **Immutable Snapshots & Coverage Analysis:** Stratified sampling policies generate reproducible JSON snapshot manifests (with SHA-256 content hashes) alongside coverage and gap reports.
## Quickstart

```bash
pip install -r requirements.txt

# Example segment bundled with the upstream repo (1 minute, ~50 MB raw)
git clone --depth 1 --no-recurse-submodules https://github.com/commaai/comma2k19.git raw/comma2k19

python -m c2k convert --src raw/comma2k19/Example_1 --out data/parquet
python -m c2k all --parquet data/parquet --out results      # events + report
python -m pytest -q                                          # synthetic-data tests
```

Change a threshold without touching code: `python -m c2k events --parquet data/parquet --out results --param brake_hard_mps2=4.0`.

## Scaling to the full dataset

The full dataset is about 100 GB in 10 chunks of roughly 10 GB
([torrent link in the upstream README](https://github.com/commaai/comma2k19#downloads)).
Unzip a chunk and point `--src` at its folder:

```bash
python -m c2k convert --src /data/comma2k19 --out data/parquet --workers 8
```

* Idempotent: a segment is skipped once its `segments` manifest file exists (`--force` redoes it), so chunks can be added over time or an interrupted run resumed.
* A malformed segment is reported and skipped; it does not abort the run.
* On NTFS/exFAT, upstream's `utils/unzip_msft_fs.py` replaces `|` with `_` in folder names; both spellings are parsed.
* The output is plain hive layout, so it can be synced to object storage as-is (S3, GCS, R2) and read back by DuckDB with `httpfs`. I have **not** tested object storage; everything here ran on local disk.

## Layout

```
data/parquet/<table>/dongle_id=<id>/route=<start>/segment=<n>/data.parquet
```

`dongle_id`, `route` and `segment` exist only in the path (read them with `hive_partitioning=true`).
Every signal table has `t` (device boot-time seconds) and `t_rel` (seconds since the segment's first camera frame).

| table | content (units) |
|---|---|
| `can_speed`, `can_steering`, `can_wheel_speed` | speed (m/s), steering-wheel angle (deg), four wheel speeds (m/s); ~83 Hz |
| `can_radar` | in-lane geometry per radar track row: longitudinal/lateral distance (m), relative speed (m/s), track address, `new_track`; ~168 Hz aggregate |
| `imu_accel`, `imu_gyro` | 3-axis accelerometer / gyro; ~104 Hz |
| `gnss_ublox`, `gnss_qcom` | lat/lon, speed, altitude, bearing, raw UTC timestamp |
| `pose` | camera pose per frame (20 Hz): GPS week/time, ECEF position and velocity, quaternion `q0..q3` |
| `segments` | one manifest row per segment: vehicle, chunk, duration, row counts per table |

Not converted: raw CAN frames, raw GNSS measurements, magnetometer and gyro bias (they dominate size and are not needed here), video and `raw_log.bz2`.

## Events

| event | definition (defaults, all parameters in `c2k/events.py`) |
|---|---|
| `moderate_braking` / `hard_braking` | deceleration over 0.5 s windows of the 10 Hz CAN speed ≥ 2.0 / ≥ 3.0 m/s², sustained ≥ 0.3 s |
| `steering_rate_high` | steering-wheel angle rate ≥ 15 deg/s over 0.5 s windows |
| `close_lead_caution` / `close_lead_critical` | time-to-collision (range ÷ closing speed) to a moving in-lane target < 4 s / < 2 s |
| `car_following` | a moving in-lane target within 60 m for ≥ 3 s |

"In-lane" means |lateral distance| ≤ 1.8 m. Event times sit on a 0.1 s grid and are window-based, so expect about ±0.5 s on braking and steering boundaries.
Thresholds are generic a-priori values for highway driving, not tuned to this data.

**Limitations**
* Radar cannot tell a stopped vehicle from static infrastructure (signs, bridges), so targets whose absolute speed (ego + relative) is below 2 m/s are excluded from lead events. Stopped leads are therefore never tagged.
* Sign convention assumed: negative radar relative speed means closing.
* The IMU is not used for events; in the example segment its x-axis swings to about 9 m/s² with almost no speed change, so it is not aligned with the vehicle.
* The dataset is a single 20 km Bay Area highway corridor and two cars. The report can show gaps by vehicle, clock hour, speed band and signal, not geographic diversity.

## Verification

* `tests/test_pipeline.py` injects known events into a synthetic segment (hard braking, steering ramp, a moving lead, and a **static** in-lane object that must not be tagged) and asserts they are found at the right times, plus layout, idempotency, missing-signal and malformed-input handling.
* On the real example segment, the SQL results were cross-checked against an independent NumPy recomputation from the raw arrays: identical braking window and car-following spans.

## Data notes (from the example segment)

* `utc_timestamp_raw` in the GNSS tables is in **milliseconds** since the Unix epoch (upstream's README says seconds).
* In this segment the GNSS UTC time matches the route name plus segment offset if the route name is US Pacific daylight time (UTC−7). Upstream does not document the time zone, so the report calls the grouping "route clock hour".
* Quaternion component order in `pose` is not documented upstream, hence `q0..q3`.

## Results snapshot

`results/` holds the output for the single example segment (1 minute, RAV4):
3 events (one `moderate_braking` of 2.34 m/s² at the very end of the segment, two `car_following` spans) and the coverage tables in `REPORT.md`.
One minute of data says nothing about rates; run the full chunks for real numbers.

## Licences and attribution

* Code: MIT, see `LICENSE`.
* `data/` is converted from comma2k19 (© 2018 comma.ai, MIT), see `DATA_LICENSE_comma2k19.txt`.
  Please cite: Schafer, Santana, Haden, Biasini, *A Commute in Data: The comma2k19 Dataset*, [arXiv:1812.05752](https://arxiv.org/abs/1812.05752).
* The upstream `LICENSE` file says "Software" and does not mention the data explicitly; the dataset card on Hugging Face also lists MIT. If you need certainty for a specific use, ask comma.ai (contact is in the upstream README).
