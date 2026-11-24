# comma2k19 coverage and gap report

**Scope:** 1 segment(s), 0.0167 driving hours, 1 route(s), 1 vehicle(s), route dates 2018-08-02 to 2018-08-02.

Cells with 0 are real gaps in *this* conversion, not necessarily in the full dataset.

## Driving hours by vehicle and route clock hour

Clock hour is derived from the route name (route start plus segment offset); upstream does not document its time zone.

| vehicle | route_clock_hours | segments | driving_hours |
|---|---|---|---|
| Civic | 00-06 | 0 | 0 |
| Civic | 06-12 | 0 | 0 |
| Civic | 12-18 | 0 | 0 |
| Civic | 18-24 | 0 | 0 |
| RAV4 | 00-06 | 0 | 0 |
| RAV4 | 06-12 | 1 | 0.0167 |
| RAV4 | 12-18 | 0 | 0 |
| RAV4 | 18-24 | 0 | 0 |

## Driving hours by vehicle and speed band

| vehicle | speed_kmh | driving_hours |
|---|---|---|
| Civic | <30 | 0 |
| Civic | 30-60 | 0 |
| Civic | 60-90 | 0 |
| Civic | 90-120 | 0 |
| Civic | >=120 | 0 |
| RAV4 | <30 | 0.0001 |
| RAV4 | 30-60 | 0.0054 |
| RAV4 | 60-90 | 0.0112 |
| RAV4 | 90-120 | 0 |
| RAV4 | >=120 | 0 |

## Tagged events per driving hour

> Warning: under one hour of driving in total, so per-hour rates below are not statistically meaningful. Use the raw event counts.

| vehicle | event_type | events | driving_hours | events_per_hour |
|---|---|---|---|---|
| Civic | moderate_braking | 0 | 0 | – |
| Civic | hard_braking | 0 | 0 | – |
| Civic | steering_rate_high | 0 | 0 | – |
| Civic | close_lead_caution | 0 | 0 | – |
| Civic | close_lead_critical | 0 | 0 | – |
| Civic | car_following | 0 | 0 | – |
| RAV4 | moderate_braking | 1 | 0.0167 | 59.97 |
| RAV4 | hard_braking | 0 | 0.0167 | 0 |
| RAV4 | steering_rate_high | 0 | 0.0167 | 0 |
| RAV4 | close_lead_caution | 0 | 0.0167 | 0 |
| RAV4 | close_lead_critical | 0 | 0.0167 | 0 |
| RAV4 | car_following | 2 | 0.0167 | 119.94 |

## Signal health

`median_hz` is per segment; radar rows are interleaved across tracks, so its rate is the aggregate over tracks.

| signal | segments_total | segments_with_data | pct_segments_present | rows | median_hz | max_gap_s |
|---|---|---|---|---|---|---|
| can_speed | 1 | 1 | 100 | 4974 | 82.9 | 0.026 |
| can_steering | 1 | 1 | 100 | 4974 | 82.9 | 0.029 |
| can_wheel_speed | 1 | 1 | 100 | 4974 | 82.9 | 0.026 |
| can_radar | 1 | 1 | 100 | 10100 | 168.45 | 0.051 |
| imu_accel | 1 | 1 | 100 | 6256 | 104.26 | 0.01 |
| imu_gyro | 1 | 1 | 100 | 6256 | 104.26 | 0.01 |
| gnss_ublox | 1 | 1 | 100 | 579 | 9.68 | 0.197 |
| gnss_qcom | 1 | 1 | 100 | 30 | 0.5 | 2.029 |
| pose | 1 | 1 | 100 | 1200 | 20 | 0.051 |

## Segments missing a signal

None: every converted segment has every signal.

## Event thresholds used

```
brake_flag_mps2=2.0
brake_hard_mps2=3.0
brake_min_bins=3
brake_tol_bins=2
steer_rate_degps=15.0
steer_min_bins=2
steer_tol_bins=2
lead_max_range_m=60.0
lead_max_lat_m=1.8
lead_min_speed_mps=2.0
ttc_caution_s=4.0
ttc_critical_s=2.0
ttc_min_bins=2
ttc_tol_bins=2
follow_min_bins=30
follow_tol_bins=3
```
