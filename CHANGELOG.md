# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `/status` shows outage statistics for the last 7 days: number of outages, total time
  without power (and the share of time with power), longest outage.
- `/stats [days]`: outage statistics (default 30 days) with a breakdown by day and by hour.
- Weekly summary every Monday at 09:00, compared with the previous week (`WEEKLY_SUMMARY`).
- "Battery charged" message after an outage (`FULL_CHARGE_SOC`, default 100 %).
- On battery, "will it last": the battery runtime compared with the typical outage length
  (median of the last 30 days), in the "power went out" message and in `/status`.
- Planned outage schedule from Yasno or outage-data-ua (`SCHEDULE_PROVIDER`, `SCHEDULE_REGION`,
  `SCHEDULE_GROUP`): `/schedule`, a warning before a planned outage, messages when a schedule
  is published or changed, "on the schedule" / "not on the schedule" when the power goes out.
- PostgreSQL as an alternative to SQLite (`DATABASE_URL`, optionally `DATABASE_USERNAME` /
  `DATABASE_PASSWORD`; `postgres` extra, included in the Docker image). The Helm chart accepts
  them as secrets and has its own README.

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
