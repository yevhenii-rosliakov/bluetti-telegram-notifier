from __future__ import annotations

import re
from pathlib import Path

import pytest
from aioresponses import aioresponses

from bluetti_grid_bot import app
from bluetti_grid_bot.__main__ import main
from bluetti_grid_bot.cloud import BIND_PATH, DEVICES_PATH, STATES_PATH
from bluetti_grid_bot.config import GATEWAYS
from bluetti_grid_bot.storage import Storage, TokenSet

from .conftest import SN, fixture_json, make_settings

EU, GLOBAL = GATEWAYS["eu"], GATEWAYS["global"]
TOKEN_URL = "https://sso.bluettipower.com/oauth2/token"


@pytest.fixture(autouse=True)
def _fast_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bluetti_grid_bot.cloud.BIND_SETTLE_S", 0.0)


def test_login_flow(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = make_settings(tmp_path)
    prompts: list[str] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        url = capsys.readouterr().out
        state = re.search(r"state=([\w-]+)", url)
        assert state is not None
        return f"https://my.home-assistant.io/redirect/oauth/?code=x%252By&state={state.group(1)}"

    with aioresponses() as m:
        m.post(
            TOKEN_URL,
            payload={"access_token": "ACCESS-1", "refresh_token": "REFRESH-1", "expires_in": 3600},
        )
        m.get(EU + DEVICES_PATH, payload={"msgCode": 401, "message": "wrong region"})
        m.get(GLOBAL + DEVICES_PATH, payload={"msgCode": 0, "data": []})
        m.get(GLOBAL + DEVICES_PATH, payload=fixture_json("devices.sample.json"))
        assert app.login(s, read_line=read_line) == 0
        form = next(v for k, v in m.requests.items() if k[0] == "POST")[0].kwargs["data"]
    assert form["code"] == "x%2By"
    out = capsys.readouterr().out
    assert "BLUETTI_REFRESH_TOKEN=REFRESH-1" in out
    assert "BLUETTI_GATEWAY=global" in out
    assert f"BLUETTI_DEVICE_SNS={SN}" in out
    st = Storage(s.db_path)
    tokens = st.get_tokens()
    st.close()
    assert tokens is not None
    assert tokens.refresh_token == "REFRESH-1"


def test_login_without_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = make_settings(tmp_path)
    assert app.login(s, read_line=lambda _: "https://my.home-assistant.io/") == 1
    assert "no 'code='" in capsys.readouterr().out


def test_login_eof(tmp_path: Path) -> None:
    def eof(_: str) -> str:
        raise EOFError

    assert app.login(make_settings(tmp_path), read_line=eof) == 130


def test_login_exchange_rejected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = make_settings(tmp_path)
    with aioresponses() as m:
        m.post(TOKEN_URL, status=400, payload={"error": "invalid_grant"})
        assert app.login(s, read_line=lambda _: "?code=used") == 1
    assert "single-use" in capsys.readouterr().out


def _seed(tmp_path: Path) -> None:
    st = Storage(tmp_path / "bluetti-grid-bot.sqlite3")
    st.save_tokens(TokenSet("ACCESS", "REFRESH", None, 0.0))
    st.close()


async def test_check_prints_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path)
    s = make_settings(tmp_path, bluetti_gateway="eu")
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=fixture_json("devices.sample.json"))
        m.post(EU + BIND_PATH, payload={"msgCode": 0})
        m.get(
            re.compile(re.escape(EU + STATES_PATH) + ".*"),
            payload=fixture_json("deviceStates.sample.json"),
        )
        assert await app.check(s) == 0
    out = capsys.readouterr().out
    assert "=> detector: GRID" in out
    assert "SOC, %" in out


async def test_check_fails_without_data(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path)
    s = make_settings(tmp_path, bluetti_gateway="eu")
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=fixture_json("devices.sample.json"))
        m.post(EU + BIND_PATH, payload={"msgCode": 0}, repeat=True)
        m.get(
            re.compile(re.escape(EU + STATES_PATH) + ".*"),
            payload={"msgCode": 0, "data": [{"sn": SN, "online": "0", "stateList": []}]},
            repeat=True,
        )
        assert await app.check(s) == 1
    assert "no data" in capsys.readouterr().out


async def test_check_without_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert await app.check(make_settings(tmp_path, bluetti_gateway="eu")) == 1
    assert "login" in capsys.readouterr().out


def test_main_reports_config_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DETECTION_MODE", "nope")
    monkeypatch.chdir(Path(__file__).parent)  # no .env here
    assert main(["check"]) == 2
    assert "DETECTION_MODE" in capsys.readouterr().err


def test_run_requires_telegram(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.chdir(Path(__file__).parent)
    assert main(["run"]) == 2
    assert "TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_unwritable_state_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("STATE_DIR", str(blocker / "sub"))
    monkeypatch.chdir(Path(__file__).parent)
    assert main(["check"]) == 2
    assert "STATE_DIR" in capsys.readouterr().err


async def test_run_service_shuts_down_on_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import os
    import signal

    from aiogram import Bot, Dispatcher

    from bluetti_grid_bot.notifier import TelegramNotifier

    sent: list[str] = []

    async def fake_broadcast(self: TelegramNotifier, text: str) -> None:
        sent.append(text)

    async def fake_polling(dp: Dispatcher, bot: Bot, stop: asyncio.Event) -> None:
        await stop.wait()

    async def no_commands(*_: object) -> None:
        return None

    monkeypatch.setattr(TelegramNotifier, "broadcast", fake_broadcast)
    monkeypatch.setattr(app, "_telegram_polling", fake_polling)
    monkeypatch.setattr(app, "set_commands", no_commands)
    s = make_settings(tmp_path, http_port=0)

    async def terminate() -> None:
        await asyncio.sleep(0.2)
        os.kill(os.getpid(), signal.SIGTERM)

    killer = asyncio.create_task(terminate())
    assert await asyncio.wait_for(app.run_service(s), 5) == 0
    await killer
    assert any("login" in t for t in sent)  # no token yet: told to log in


async def test_telegram_polling_retries_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
    from aiogram.methods import GetMe

    calls: list[int] = []
    stop = asyncio.Event()

    class FakeDp:
        async def start_polling(self, *_: object, **__: object) -> None:
            calls.append(1)
            if len(calls) == 1:
                raise TelegramNetworkError(method=GetMe(), message="down")
            raise TelegramUnauthorizedError(method=GetMe(), message="bad token")

    monkeypatch.setattr(asyncio, "wait_for", _instant_wait_for)
    with pytest.raises(TelegramUnauthorizedError):
        await app._telegram_polling(FakeDp(), None, stop)  # type: ignore[arg-type]
    assert len(calls) == 2


async def _instant_wait_for(aw: object, timeout: float | None = None) -> None:  # noqa: ASYNC109
    import inspect

    if inspect.iscoroutine(aw):
        aw.close()
    raise TimeoutError
