"""SQLite state in ``STATE_DIR``: tokens, gateway, last device state, outage history.

The standard-library ``sqlite3`` module is used directly: the database is a small local
file and every statement finishes in microseconds, so it does not need a thread pool.
Each write is its own transaction (atomic). WAL mode lets ``login`` run in a second
process (e.g. ``kubectl exec``) while the service is running.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .detector import DeviceState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS device_state (
    sn         TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    raw        TEXT,
    polled_at  TEXT
);
CREATE TABLE IF NOT EXISTS outages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sn              TEXT NOT NULL,
    start_at        TEXT NOT NULL,
    start_earliest  TEXT,
    end_at          TEXT,
    end_earliest    TEXT
);
CREATE INDEX IF NOT EXISTS outages_sn_start ON outages (sn, start_at);
"""


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str | None
    refresh_token: str | None
    # Unix time; None = unknown (use until the API says it expired).
    expires_at: float | None
    obtained_at: float

    def to_json(self) -> str:
        return json.dumps(
            {
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at,
                "obtained_at": self.obtained_at,
            }
        )

    @classmethod
    def from_json(cls, text: str) -> TokenSet:
        data = json.loads(text)
        return cls(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            obtained_at=float(data.get("obtained_at") or 0),
        )


@dataclass(frozen=True, slots=True)
class Outage:
    id: int
    sn: str
    start: datetime
    start_earliest: datetime | None
    end: datetime | None
    end_earliest: datetime | None

    @property
    def approximate(self) -> bool:
        return self.start_earliest is not None or self.end_earliest is not None


def _ts(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


class Storage:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            # Create it private before SQLite opens it: it holds the refresh token.
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        self._conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    # key/value

    def get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set(self, key: str, value: str | None) -> None:
        with self._tx() as c:
            if value is None:
                c.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                c.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def get_tokens(self) -> TokenSet | None:
        text = self.get("tokens")
        return None if text is None else TokenSet.from_json(text)

    def save_tokens(self, tokens: TokenSet) -> None:
        self.set("tokens", tokens.to_json())

    def get_gateway(self) -> str | None:
        return self.get("gateway")

    def set_gateway(self, url: str | None) -> None:
        self.set("gateway", url)

    # device state

    def load_device(self, sn: str) -> tuple[DeviceState | None, dict[str, str], datetime | None]:
        row = self._conn.execute(
            "SELECT state, raw, polled_at FROM device_state WHERE sn = ?", (sn,)
        ).fetchone()
        if row is None:
            return None, {}, None
        state = DeviceState.from_dict(json.loads(row["state"]))
        raw: dict[str, str] = json.loads(row["raw"]) if row["raw"] else {}
        return state, raw, _dt(row["polled_at"])

    def save_device(
        self,
        state: DeviceState,
        raw: Mapping[str, str] | None = None,
        polled_at: datetime | None = None,
    ) -> None:
        """Save the detector state; ``raw``/``polled_at`` only replace the stored ones if given."""
        with self._tx() as c:
            c.execute(
                "INSERT INTO device_state (sn, state, raw, polled_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(sn) DO UPDATE SET state = excluded.state, "
                "raw = COALESCE(excluded.raw, device_state.raw), "
                "polled_at = COALESCE(excluded.polled_at, device_state.polled_at)",
                (
                    state.sn,
                    json.dumps(state.to_dict()),
                    None if raw is None else json.dumps(dict(raw)),
                    _ts(polled_at),
                ),
            )

    # outage history

    def _row_to_outage(self, row: sqlite3.Row) -> Outage:
        return Outage(
            id=int(row["id"]),
            sn=str(row["sn"]),
            start=datetime.fromisoformat(row["start_at"]),
            start_earliest=_dt(row["start_earliest"]),
            end=_dt(row["end_at"]),
            end_earliest=_dt(row["end_earliest"]),
        )

    def open_outage(self, sn: str) -> Outage | None:
        row = self._conn.execute(
            "SELECT * FROM outages WHERE sn = ? AND end_at IS NULL ORDER BY id DESC LIMIT 1",
            (sn,),
        ).fetchone()
        return None if row is None else self._row_to_outage(row)

    def start_outage(self, sn: str, start: datetime, start_earliest: datetime | None) -> None:
        """Record an outage start unless one is already open for this station."""
        with self._tx() as c:
            open_row = c.execute(
                "SELECT 1 FROM outages WHERE sn = ? AND end_at IS NULL", (sn,)
            ).fetchone()
            if open_row is None:
                c.execute(
                    "INSERT INTO outages (sn, start_at, start_earliest) VALUES (?, ?, ?)",
                    (sn, _ts(start), _ts(start_earliest)),
                )

    def end_outage(self, sn: str, end: datetime, end_earliest: datetime | None) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE outages SET end_at = ?, end_earliest = ? WHERE sn = ? AND end_at IS NULL",
                (_ts(end), _ts(end_earliest), sn),
            )

    def history(self, limit: int, sns: list[str] | None = None) -> list[Outage]:
        query = "SELECT * FROM outages"
        params: list[Any] = []
        if sns:
            query += f" WHERE sn IN ({','.join('?' * len(sns))})"
            params += sns
        query += " ORDER BY start_at DESC, id DESC LIMIT ?"
        params.append(limit)
        return [self._row_to_outage(r) for r in self._conn.execute(query, params).fetchall()]

    def outages_since(self, since: datetime, sns: list[str]) -> list[Outage]:
        """Outages of these stations that were still going on at or after ``since``."""
        if not sns:
            return []
        # Timestamps are stored as UTC ISO strings, so they compare correctly as text.
        query = "SELECT * FROM outages WHERE (end_at IS NULL OR end_at >= ?)"
        query += f" AND sn IN ({','.join('?' * len(sns))}) ORDER BY start_at, id"
        rows = self._conn.execute(query, [_ts(since.astimezone(UTC)), *sns]).fetchall()
        return [self._row_to_outage(r) for r in rows]
