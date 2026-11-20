# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-20

### Added
- Grid lost / restored notifications from a BLUETTI station via the BLUETTI cloud, with
  three detection modes (`ats`, `passthrough`, `grid_input`) and debouncing.
- Low battery (once per threshold per outage), AC output off, station offline / back online
  alerts; time windows when a change happened while the station was offline.
- `/status`, `/history [n]`, `/help`; Ukrainian and English messages.
- `login` (interactive OAuth), `check` (one poll with the detector's decision) and `run` commands.
- Refresh token rotation persisted in SQLite; gateway auto-detection and caching; automatic
  rebinding of stations that return no data.
- `/healthz`, `/readyz`, Prometheus `/metrics`.
- Multi-arch Docker image (amd64, arm64), Docker Compose file, Helm chart.
