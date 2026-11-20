# Contributing

Thanks for helping! Bug reports with the output of `bluetti-grid-bot check` (serial numbers
and tokens removed) are especially useful for models other than the Premium 200 V2.

## Development

```bash
uv sync                      # Python 3.12+, installs dev tools too
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict
uv run pytest --cov
helm lint charts/bluetti-grid-bot --strict
```

- Keep `detector.py` pure (no I/O, no clock) and cover every new transition with a test.
- All user-facing text goes into `src/bluetti_grid_bot/i18n/{uk,en}.json` (same keys in both).
- Never commit real serial numbers, tokens or chat IDs. Fixtures use `PR200V2XXXXXXXXXXXXXX`.
- New configuration: add it to `config.py`, `.env.example`, both READMEs and
  `charts/bluetti-grid-bot/values.yaml`.

## Releasing

1. Bump `version` in `pyproject.toml`, `version`/`appVersion` in `charts/bluetti-grid-bot/Chart.yaml`,
   and update `CHANGELOG.md`.
2. Tag `vX.Y.Z` and push the tag. CI builds the multi-arch image, pushes it and the chart to
   GHCR, and creates a GitHub release.
