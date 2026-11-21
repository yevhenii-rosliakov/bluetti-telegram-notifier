# Contributing

Thanks for helping! Bug reports with the output of `bluetti-grid-bot check` (serial numbers
and tokens removed) are especially useful for models other than the Premium 200 V2.

## Development

```bash
uv sync                      # Python 3.12+, installs dev tools too
pre-commit install           # gitleaks secret scan on every commit
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
uv run pytest --cov
helm lint charts/bluetti-grid-bot --strict
```

- Keep `detector.py` pure (no I/O, no clock) and cover every new transition with a test.
- All user-facing text goes into `src/bluetti_grid_bot/i18n/{uk,en}.json` (same keys in both).
- Never commit real serial numbers, tokens or chat IDs. Fixtures use `PR200V2XXXXXXXXXXXXXX`.
  Known-public values gitleaks flags go into the allowlist in `.gitleaks.toml`.
- New configuration: add it to `config.py`, `.env.example`, both READMEs and
  `charts/bluetti-grid-bot/values.yaml`.

## Releasing

1. Bump `version` in `pyproject.toml`, `version`/`appVersion` in `charts/bluetti-grid-bot/Chart.yaml`,
   turn `[Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`, and merge that into `main`.
2. On GitHub, go to Releases → Draft a new release, create the tag `vX.Y.Z` on `main` and publish.
   The Release workflow builds the multi-arch image from that tag and pushes it to GHCR as
   `X.Y.Z`, `X.Y` and `latest` (no `latest` for a pre-release such as `v1.0.0-rc.1`), then
   pushes the chart.
