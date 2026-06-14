# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-06-14

### Added

- Initial scaffold of the Alpha EMS Manager integration.
- Config flow to select all source entities (house load, PV, Frank prices,
  battery), including optional east/west PV and battery SoC sensors.
- `DataUpdateCoordinator` that reads source sensors on a 1-minute interval.
- Self-learning household load profile per 15-minute interval, bucketed by
  season and weekday/weekend, using an exponential moving average.
- Persistent storage of the learned profile across restarts.
- Scaffold reserve calculation combining learned load and PV forecast.
- Sensors: predicted daily load, predicted remaining load, required reserve,
  PV forecast today/tomorrow, battery current energy, recommendation.
- Binary sensor: reserve satisfied.
- Diagnostics support.
- HACS compatibility.

### Notes

- AlphaESS write commands are intentionally **not** implemented in this release.
