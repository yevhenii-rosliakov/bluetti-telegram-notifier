from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses

from bluetti_grid_bot.auth import AuthExpiredError, OAuthClient, TokenManager
from bluetti_grid_bot.cloud import (
    BIND_PATH,
    DEVICES_PATH,
    STATES_PATH,
    BluettiCloud,
    CloudError,
    MultiStateSource,
    PollingStateSource,
    TransientCloudError,
    gateway_name,
)
from bluetti_grid_bot.config import GATEWAYS
from bluetti_grid_bot.storage import Storage, TokenSet

from .conftest import SN, fixture_json

EU, GLOBAL, US = GATEWAYS["eu"], GATEWAYS["global"], GATEWAYS["us"]
TOKEN_URL = "https://sso.example.test/oauth2/token"
OAUTH = OAuthClient("https://sso.example.test", "HomeAssistant", "secret", "https://r.test/oauth")


async def no_sleep(_: float) -> None:
    return None


def ok(data: Any) -> dict[str, Any]:
    return {"msgId": "1", "msgCode": 0, "message": "OK", "data": data}


def states_url(gw: str) -> re.Pattern[str]:
    return re.compile(re.escape(gw + STATES_PATH) + r"\?sns=.*")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def make_cloud(
    session: aiohttp.ClientSession,
    storage: Storage,
    gateway: str = "auto",
    site: str = "",
    **kw: Any,
) -> BluettiCloud:
    storage.save_tokens(TokenSet(f"ACCESS{site}", "REFRESH", None, 0.0), site)
    tokens = TokenManager(session, OAUTH, storage, site=site, sleep=no_sleep)
    kw.setdefault("sleep", no_sleep)
    return BluettiCloud(session, tokens, storage, gateway=gateway, site=site, **kw)


def calls(m: aioresponses) -> list[tuple[str, str]]:
    return [(method, str(url)) for (method, url) in m.requests]


# gateway


async def test_probe_order_and_caching(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload={"msgCode": 401, "message": "token invalid"})
        m.get(GLOBAL + DEVICES_PATH, payload=ok([]))
        m.get(GLOBAL + DEVICES_PATH, payload=ok([]))
        assert await cloud.gateway() == GLOBAL
        order = [url for _, url in calls(m)]
        assert order == [EU + DEVICES_PATH, GLOBAL + DEVICES_PATH]
        assert storage.get_gateway() == GLOBAL
        # Cached: no more probing.
        assert await cloud.gateway() == GLOBAL
        await cloud.list_devices()
        per_url = {str(url): len(reqs) for (_, url), reqs in m.requests.items()}
    assert per_url == {EU + DEVICES_PATH: 1, GLOBAL + DEVICES_PATH: 2}


async def test_probe_order_is_configurable(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, probe_order=[US, EU])
    with aioresponses() as m:
        m.get(US + DEVICES_PATH, payload=ok([]))
        assert await cloud.gateway() == US


async def test_probe_skips_unreachable_gateway(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, retries=1)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, exception=aiohttp.ClientConnectionError())
        m.get(GLOBAL + DEVICES_PATH, status=500)
        m.get(US + DEVICES_PATH, payload=ok([]))
        assert await cloud.gateway() == US


async def test_no_gateway_accepts(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage)
    with aioresponses() as m:
        for gw in (EU, GLOBAL, US):
            m.get(gw + DEVICES_PATH, payload={"msgCode": 401, "message": "nope"})
        with pytest.raises(CloudError, match="No BLUETTI gateway"):
            await cloud.gateway()
    assert storage.get_gateway() is None


async def test_fixed_gateway_is_not_probed(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=US)
    with aioresponses() as m:
        m.get(US + DEVICES_PATH, payload=fixture_json("devices.sample.json"))
        devices = await cloud.list_devices()
    assert [(d.sn, d.cloud, d.online, d.bound_by_me) for d in devices] == [(SN, True, True, True)]


async def test_cached_gateway_forgotten_on_rejection(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage)
    storage.set_gateway(EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload={"msgCode": 401, "message": "wrong region"})
        with pytest.raises(CloudError):
            await cloud.list_devices()
    assert storage.get_gateway() is None


def test_gateway_name() -> None:
    assert gateway_name(EU) == "eu"
    assert gateway_name("https://custom.test") == "https://custom.test"


# envelope handling


async def test_authorization_header_has_no_bearer_prefix(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=ok([]))
        await cloud.list_devices()
        request = next(iter(m.requests.values()))[0]
    assert request.kwargs["headers"]["Authorization"] == "ACCESS"


async def test_805_refreshes_and_retries_once(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload={"msgCode": 805, "message": "token expired"})
        m.post(TOKEN_URL, payload={"access_token": "NEW", "refresh_token": "R2", "expires_in": 60})
        m.get(EU + DEVICES_PATH, payload=ok([]))
        assert await cloud.list_devices() == []
        get_requests = [r for (method, _), rs in m.requests.items() if method == "GET" for r in rs]
    assert [r.kwargs["headers"]["Authorization"] for r in get_requests] == ["ACCESS", "NEW"]
    tokens = storage.get_tokens()
    assert tokens is not None
    assert tokens.refresh_token == "R2"


async def test_805_twice_is_an_error(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload={"msgCode": 805}, repeat=True)
        m.post(TOKEN_URL, payload={"access_token": "NEW"})
        with pytest.raises(CloudError) as info:
            await cloud.list_devices()
    assert info.value.msg_code == 805


async def test_http_200_with_error_msgcode_fails(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.post(EU + BIND_PATH, payload={"msgCode": "1001", "message": "bad sn"})
        with pytest.raises(CloudError) as info:
            await cloud.bind([SN])
    assert info.value.msg_code == 1001


async def test_transient_errors_are_retried_with_backoff(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    delays: list[float] = []

    async def sleep(d: float) -> None:
        delays.append(d)

    cloud = make_cloud(session, storage, gateway=EU, sleep=sleep, backoff_s=1.0)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, status=502)
        m.get(EU + DEVICES_PATH, exception=TimeoutError())
        m.get(EU + DEVICES_PATH, payload=ok([]))
        assert await cloud.list_devices() == []
    assert delays == [1.0, 2.0]


async def test_transient_errors_give_up(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    storage.set_gateway(EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, status=503, repeat=True)
        with pytest.raises(TransientCloudError):
            await cloud.list_devices()


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (200, "<html>maintenance</html>", TransientCloudError),
        (404, "not found", CloudError),
        (200, '{"data": []}', CloudError),
    ],
)
async def test_bad_envelopes(
    session: aiohttp.ClientSession,
    storage: Storage,
    status: int,
    body: str,
    error: type[Exception],
) -> None:
    cloud = make_cloud(session, storage, gateway=EU, retries=1)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, status=status, body=body)
        with pytest.raises(error):
            await cloud.list_devices()


async def test_device_states_parses_fixture(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.get(states_url(EU), payload=fixture_json("deviceStates.sample.json"))
        (sample,) = await cloud.device_states([SN])
        url = next(iter(m.requests))[1]
    assert url.query["sns"] == SN
    assert sample.online
    assert sample.raw["SOC"] == "100"
    r = sample.reading
    assert (r.grid_w, r.ac_load_w, r.soc, r.ac_on, r.dc_on, r.ac_eco) == (
        0,
        0,
        100,
        True,
        False,
        False,
    )
    assert r.dsg_minutes == 5994
    assert r.work_mode == "workmode_1"


# polling source: bind before poll, rebind on empty stateList


def _devices(*items: dict[str, str]) -> dict[str, Any]:
    return ok(list(items))


def _device(sn: str, cloud: str = "1", name: str | None = None) -> dict[str, str]:
    return {
        "sn": sn,
        "model": "PR200V2-PR200V2",
        "name": name or sn,
        "online": "1",
        "isBindByCurUser": "1",
        "supportNetwork": cloud,
    }


def _states(*items: tuple[str, str, list[dict[str, str]]]) -> dict[str, Any]:
    return ok([{"sn": sn, "online": online, "stateList": sl} for sn, online, sl in items])


FULL = [{"fnCode": "SOC", "fnValue": "80"}, {"fnCode": "GridAllTotalPower", "fnValue": "0"}]


async def test_bind_is_called_before_first_poll(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    source = PollingStateSource(cloud, sleep=no_sleep, clock=Clock())
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN), _device("OTHERSN0001", cloud="0")))
        m.post(EU + BIND_PATH, payload=ok(None))
        m.get(states_url(EU), payload=_states((SN, "1", FULL)))
        samples = await source.fetch()
        order = [(method, url.path) for method, url in m.requests]
        bind_req = m.requests[next(k for k in m.requests if k[0] == "POST")][0]
    assert order == [("GET", DEVICES_PATH), ("POST", BIND_PATH), ("GET", STATES_PATH)]
    assert bind_req.kwargs["json"] == {"bindSnList": [SN]}  # only cloud-capable stations
    sample = samples[SN]
    assert sample is not None
    assert sample.reading.soc == 80


async def test_configured_sns_and_names(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    source = PollingStateSource(
        cloud, sns=[SN, "SHAREDSN0002"], names={SN: "Mom's flat"}, sleep=no_sleep
    )
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN), _device("X", name="Garage")))
        m.post(EU + BIND_PATH, payload=ok(None))
        stations = await source.start()
    assert [(s.sn, s.name) for s in stations] == [
        (SN, "Mom's flat"),
        ("SHAREDSN0002", "BLUETTI …0002"),
    ]


async def test_station_name_from_cloud(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    source = PollingStateSource(cloud, sleep=no_sleep)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN, name="Kitchen")))
        m.post(EU + BIND_PATH, payload=ok(None))
        (station,) = await source.start()
    assert station.name == "Kitchen"
    assert station.model == "PR200V2-PR200V2"


async def test_no_cloud_stations(session: aiohttp.ClientSession, storage: Storage) -> None:
    cloud = make_cloud(session, storage, gateway=EU)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN, cloud="0")))
        with pytest.raises(CloudError, match="cloud-capable"):
            await PollingStateSource(cloud, sleep=no_sleep).start()


async def test_empty_state_list_triggers_rebind_rate_limited(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    cloud = make_cloud(session, storage, gateway=EU)
    source = PollingStateSource(cloud, sleep=no_sleep, clock=clock)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN)))
        m.post(EU + BIND_PATH, payload=ok(None), repeat=True)
        await source.start()

        # Right after binding: an empty answer does not rebind again immediately.
        m.get(states_url(EU), payload=_states((SN, "0", [])))
        samples = await source.fetch()
        sample = samples[SN]
        assert sample is not None
        assert not sample.has_data

        def binds() -> int:
            return sum(len(v) for k, v in m.requests.items() if k[0] == "POST")

        assert binds() == 1

        # Later: empty again -> bind -> poll again, which now has data.
        clock.now += 301
        m.get(states_url(EU), payload=_states((SN, "0", [])))
        m.get(states_url(EU), payload=_states((SN, "1", FULL)))
        samples = await source.fetch()
        assert binds() == 2
        sample = samples[SN]
        assert sample is not None
        assert sample.has_data


async def test_rebind_failure_is_not_fatal(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    cloud = make_cloud(session, storage, gateway=EU)
    source = PollingStateSource(cloud, sleep=no_sleep, clock=clock)
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN)))
        m.post(EU + BIND_PATH, payload=ok(None))
        await source.start()
        clock.now += 1000
        m.get(states_url(EU), payload=_states())  # station missing from the answer
        m.post(EU + BIND_PATH, payload={"msgCode": 500, "message": "err"})
        samples = await source.fetch()
    assert samples == {SN: None}


# several sites


async def test_gateway_and_tokens_are_kept_per_site(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    home = make_cloud(session, storage, site="home")
    cottage = make_cloud(session, storage, site="cottage", probe_order=[US])
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=ok([]))
        m.get(US + DEVICES_PATH, payload=ok([]))
        assert await home.gateway() == EU
        assert await cottage.gateway() == US
        sent = [r.kwargs["headers"]["Authorization"] for rs in m.requests.values() for r in rs]
    assert sorted(sent) == ["ACCESScottage", "ACCESShome"]
    assert (storage.get_gateway("home"), storage.get_gateway("cottage")) == (EU, US)
    assert storage.get_gateway() is None
    assert storage.get_tokens() is None


def _site_source(
    session: aiohttp.ClientSession, storage: Storage, site: str, gw: str, name: str = ""
) -> PollingStateSource:
    cloud = make_cloud(session, storage, gateway=gw, site=site)
    return PollingStateSource(cloud, site_name=name, sleep=no_sleep, clock=Clock())


async def test_multi_source_merges_sites_and_names_them(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    source = MultiStateSource(
        {
            "home": _site_source(session, storage, "home", EU, "Дім"),
            "cottage": _site_source(session, storage, "cottage", US, "Дача"),
        }
    )
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN)))
        m.get(US + DEVICES_PATH, payload=_devices(_device("COTTAGE0001"), _device("COTTAGE0002")))
        m.post(EU + BIND_PATH, payload=ok(None))
        m.post(US + BIND_PATH, payload=ok(None))
        m.get(states_url(EU), payload=_states((SN, "1", FULL)))
        m.get(
            states_url(US),
            payload=_states(("COTTAGE0001", "1", FULL), ("COTTAGE0002", "1", FULL)),
        )
        stations = await source.start()
        samples = await source.fetch()
    assert [(s.sn, s.name) for s in stations] == [
        (SN, "Дім"),
        ("COTTAGE0001", "Дача …0001"),
        ("COTTAGE0002", "Дача …0002"),
    ]
    assert set(samples) == {SN, "COTTAGE0001", "COTTAGE0002"}
    assert source.errors == {}


async def test_multi_source_keeps_working_sites(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    source = MultiStateSource(
        {
            "home": _site_source(session, storage, "home", EU),
            "cottage": _site_source(session, storage, "cottage", US),
        }
    )
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload=_devices(_device(SN)), repeat=True)
        m.get(US + DEVICES_PATH, payload={"msgCode": 401, "message": "no"})
        m.post(EU + BIND_PATH, payload=ok(None))
        m.post(US + BIND_PATH, payload=ok(None))
        m.get(states_url(EU), payload=_states((SN, "1", FULL)), repeat=True)
        stations = await source.start()
        assert [s.sn for s in stations] == [SN]
        assert set(source.errors) == {"cottage"}
        # The cottage starts on the next fetch and its station shows up.
        m.get(US + DEVICES_PATH, payload=_devices(_device("COTTAGE0001")))
        m.get(states_url(US), payload=_states(("COTTAGE0001", "1", FULL)))
        samples = await source.fetch()
    assert set(samples) == {SN, "COTTAGE0001"}
    assert [s.sn for s in source.stations] == [SN, "COTTAGE0001"]
    assert source.errors == {}


async def test_multi_source_raises_when_every_site_fails(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    home = _site_source(session, storage, "home", EU)
    cottage = _site_source(session, storage, "cottage", US)
    storage.save_tokens(TokenSet(None, None, None, 0.0), "cottage")  # no login
    source = MultiStateSource({"home": home, "cottage": cottage})
    with aioresponses() as m:
        m.get(EU + DEVICES_PATH, payload={"msgCode": 401, "message": "no"})
        with pytest.raises(AuthExpiredError, match="login --station cottage"):
            await source.start()
    assert set(source.errors) == {"home", "cottage"}
