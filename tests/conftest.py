from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from bluetti_grid_bot.config import Settings
from bluetti_grid_bot.models import Sample
from bluetti_grid_bot.storage import Storage

REFERENCE = Path(__file__).resolve().parent.parent / "reference"
SN = "PR200V2XXXXXXXXXXXXXX"
T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def make_sample(
    *,
    grid: float | str | None = 0,
    ac: float | str | None = 0,
    dc: float | str | None = 0,
    soc: float | str | None = 100,
    ac_on: str | None = "1",
    online: bool = True,
    sn: str = SN,
    **extra: str,
) -> Sample:
    raw: dict[str, str] = {}
    for code, value in (
        ("GridAllTotalPower", grid),
        ("ACLoadAllTotalPower", ac),
        ("DCLoadAllTotalPower", dc),
        ("SOC", soc),
        ("SetCtrlAc", ac_on),
    ):
        if value is not None:
            raw[code] = str(value)
    raw.update(extra)
    return Sample.from_raw(sn, online, raw)


def fixture_json(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((REFERENCE / name).read_text())
    return data


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not see the developer's real environment."""
    import os

    for key in list(os.environ):
        if key.startswith(("BLUETTI_", "TELEGRAM_")) or key in {
            "TZ",
            "STATE_DIR",
            "DETECTION_MODE",
            "LOG_LEVEL",
            "LOG_FORMAT",
            "BOT_LANGUAGE",
        }:
            monkeypatch.delenv(key, raising=False)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "telegram_bot_token": "123456:TEST-TOKEN",
        "telegram_chat_ids": "111,-222",
        "state_dir": tmp_path,
        "bot_language": "en",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# PostgreSQL tests run when this points at a disposable database (its tables are dropped).
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set")


def reset_pg() -> None:
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS kv, device_state, outages")


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=needs_pg)])
def storage(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Storage]:
    if request.param == "postgres":
        reset_pg()
        st = Storage.postgres(PG_URL)
    else:
        st = Storage.sqlite(tmp_path / "state.sqlite3")
    yield st
    st.close()
