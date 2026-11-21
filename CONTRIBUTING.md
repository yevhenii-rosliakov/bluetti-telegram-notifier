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

1. On a branch named `release/vX.Y.Z`, bump `version` in `pyproject.toml`,
   `version`/`appVersion` in `charts/bluetti-grid-bot/Chart.yaml`, and turn `[Unreleased]` in
   `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`.
2. Open a PR into `main` and merge it. The Release workflow checks that the versions match
   and the tag is new, builds the multi-arch image and pushes it to GHCR as `X.Y.Z`, `X.Y` and
   `latest`, pushes the chart, then tags `vX.Y.Z` and creates a GitHub release with the
   changelog section as notes. Only merged PRs from `release/*` branches trigger it.
