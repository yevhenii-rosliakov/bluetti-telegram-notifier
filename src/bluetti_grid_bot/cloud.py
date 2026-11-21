"""Thin client for the (unofficial) BLUETTI cloud REST API used by the HA integration."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from .auth import AuthError, AuthExpiredError, TokenManager
from .config import GATEWAYS, Site
from .models import Sample, Station
from .storage import Storage

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)
MSG_OK = 0
MSG_TOKEN_EXPIRED = 805
DEVICES_PATH = "/api/bluiotdata/ha/v2/devices"
BIND_PATH = "/api/bluiotdata/ha/v1/bindDevices"
STATES_PATH = "/api/bluiotdata/ha/v1/deviceStates"
# Don't rebind more often than this when a station keeps answering with no data
# (an offline station also has an empty stateList).
REBIND_MIN_INTERVAL_S = 300.0
# The HA integration waits a moment after binding before the first poll.
BIND_SETTLE_S = 3.0


class CloudError(Exception):
    def __init__(self, message: str, msg_code: int | None = None) -> None:
        super().__init__(message)
        self.msg_code = msg_code


class TransientCloudError(CloudError):
    """Timeout, connection error, HTTP 5xx: retry later."""


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    sn: str
    model: str
    name: str
    online: bool
    bound_by_me: bool
    cloud: bool

    @classmethod
    def from_api(cls, item: dict[str, Any]) -> DeviceInfo:
        return cls(
            sn=str(item.get("sn") or ""),
            model=str(item.get("model") or ""),
            name=str(item.get("name") or ""),
            online=str(item.get("online")) == "1",
            bound_by_me=str(item.get("isBindByCurUser")) == "1",
            cloud=str(item.get("supportNetwork")) == "1",
        )


def gateway_name(url: str) -> str:
    """``eu``/``global``/``us`` for the known gateways, else the URL itself."""
    return next((k for k, v in GATEWAYS.items() if v == url), url)


class BluettiCloud:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenManager,
        storage: Storage,
        *,
        gateway: str = "auto",
        probe_order: Sequence[str] = tuple(GATEWAYS.values()),
        site: str = "",
        retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._storage = storage
        self._gateway_setting = gateway
        self._probe_order = list(probe_order)
        self._site = site
        self._retries = retries
        self._backoff = backoff_s
        self._sleep = sleep

    @classmethod
    def for_site(
        cls,
        session: aiohttp.ClientSession,
        tokens: TokenManager,
        storage: Storage,
        site: Site,
        probe_order: Sequence[str],
    ) -> BluettiCloud:
        return cls(
            session, tokens, storage, gateway=site.gateway, probe_order=probe_order, site=site.id
        )

    # gateway

    async def gateway(self) -> str:
        if self._gateway_setting != "auto":
            return self._gateway_setting
        if cached := self._storage.get_gateway(self._site):
            return cached
        return await self.probe_gateway()

    async def probe_gateway(self) -> str:
        """Find the data center that accepts this account's token and cache it."""
        failures = []
        for url in self._probe_order:
            try:
                await self._call("GET", DEVICES_PATH, gateway=url)
            except CloudError as exc:
                log.info("Gateway %s rejected the request: %s", gateway_name(url), exc)
                failures.append(f"{gateway_name(url)}: {exc}")
                continue
            log.info("Using BLUETTI gateway %s (%s)", gateway_name(url), url)
            self._storage.set_gateway(url, self._site)
            return url
        raise CloudError("No BLUETTI gateway accepted the token (" + "; ".join(failures) + ")")

    def _forget_gateway(self) -> None:
        if self._gateway_setting == "auto" and self._storage.get_gateway(self._site):
            log.warning("Forgetting the cached gateway; it will be probed again")
            self._storage.set_gateway(None, self._site)

    # calls

    async def list_devices(self) -> list[DeviceInfo]:
        try:
            data = await self._call("GET", DEVICES_PATH)
        except CloudError as exc:
            if not isinstance(exc, TransientCloudError):
                self._forget_gateway()
            raise
        return [DeviceInfo.from_api(d) for d in data or [] if isinstance(d, dict)]

    async def bind(self, sns: Sequence[str]) -> None:
        """Bind stations to this API client. Without it ``deviceStates`` answers
        ``online=0`` with an empty ``stateList``."""
        await self._call("POST", BIND_PATH, json_body={"bindSnList": list(sns)})
        log.debug("Bound stations %s", ",".join(sns))

    async def device_states(self, sns: Sequence[str]) -> list[Sample]:
        try:
            data = await self._call("GET", STATES_PATH, params={"sns": ",".join(sns)})
        except CloudError as exc:
            if not isinstance(exc, TransientCloudError):
                self._forget_gateway()
            raise
        samples = []
        for item in data or []:
            if not isinstance(item, dict) or not item.get("sn"):
                continue
            raw = {
                str(s["fnCode"]): "" if s.get("fnValue") is None else str(s["fnValue"])
                for s in item.get("stateList") or []
                if isinstance(s, dict) and s.get("fnCode")
            }
            samples.append(Sample.from_raw(str(item["sn"]), str(item.get("online")) == "1", raw))
        return samples

    async def _call(
        self,
        method: str,
        path: str,
        *,
        gateway: str | None = None,
        params: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> Any:
        url = (gateway or await self.gateway()) + path
        token = await self._tokens.access_token()
        refreshed = False
        attempt = 0
        while True:
            try:
                envelope = await self._request(method, url, token, params, json_body)
            except TransientCloudError:
                attempt += 1
                if attempt >= self._retries:
                    raise
                await self._sleep(self._backoff * 2 ** (attempt - 1))
                continue
            code = envelope.get("msgCode")
            if code == MSG_OK:
                return envelope.get("data")
            if code == MSG_TOKEN_EXPIRED and not refreshed:
                log.info("Access token expired (msgCode 805); refreshing")
                token = await self._tokens.refresh(stale=token)
                refreshed = True
                continue
            raise CloudError(
                f"{path}: msgCode={code} message={envelope.get('message')!r}",
                code if isinstance(code, int) else None,
            )

    async def _request(
        self,
        method: str,
        url: str,
        token: str,
        params: dict[str, str] | None,
        json_body: Any,
    ) -> dict[str, Any]:
        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": token},  # no "Bearer " prefix
                timeout=TIMEOUT,
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise TransientCloudError(f"{method} {url}: {exc!r}") from exc
        if status >= 500 or status == 429:
            raise TransientCloudError(f"{method} {url}: HTTP {status}")
        try:
            envelope = json.loads(text)
        except ValueError:
            envelope = None
        if not isinstance(envelope, dict):
            if status >= 400:
                raise CloudError(f"{method} {url}: HTTP {status}")
            raise TransientCloudError(f"{method} {url}: non-JSON answer")
        try:
            envelope["msgCode"] = int(envelope.get("msgCode"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise CloudError(f"{method} {url}: HTTP {status}, no msgCode") from None
        return envelope


class StateSource(Protocol):
    """Where samples come from. Polling today; a WebSocket push source can implement
    the same interface later."""

    @property
    def stations(self) -> list[Station]: ...

    @property
    def errors(self) -> Mapping[str, Exception]:
        """Site id -> error of the last ``start``/``fetch``, for sites that failed while
        others worked. When every site fails, the call raises instead."""
        ...

    async def start(self) -> list[Station]: ...

    async def fetch(self) -> dict[str, Sample | None]: ...


class PollingStateSource:
    def __init__(
        self,
        cloud: BluettiCloud,
        *,
        sns: Sequence[str] = (),
        names: dict[str, str] | None = None,
        site_name: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._cloud = cloud
        self._wanted = list(sns)
        self._names = names or {}
        self._site_name = site_name
        self._clock = clock
        self._sleep = sleep
        self._stations: list[Station] = []
        self._bound = False
        self._last_bind = -REBIND_MIN_INTERVAL_S

    @property
    def stations(self) -> list[Station]:
        return list(self._stations)

    @property
    def errors(self) -> Mapping[str, Exception]:
        return {}

    async def start(self) -> list[Station]:
        devices = {d.sn: d for d in await self._cloud.list_devices()}
        if self._wanted:
            sns = self._wanted
            for sn in sns:
                if sn not in devices:
                    log.warning(
                        "A configured station is not on this account (shared stations may be "
                        "missing); polling it anyway. LOG_LEVEL=DEBUG shows which one."
                    )
                    log.debug("Station not on the account: %s", sn)
                elif not devices[sn].cloud:
                    log.warning("A configured station does not report cloud support")
                    log.debug("Station without cloud support: %s", sn)
        else:
            sns = [sn for sn, d in devices.items() if d.cloud]
            if not sns:
                raise CloudError("No cloud-capable (Wi-Fi) stations on this BLUETTI account")
        self._stations = [self._station(sn, devices.get(sn), len(sns)) for sn in sns]
        await self._bind()
        return self.stations

    def _station(self, sn: str, info: DeviceInfo | None, count: int) -> Station:
        model = info.model if info else ""
        if sn in self._names:
            name = self._names[sn]
        elif self._site_name:
            name = self._site_name if count == 1 else f"{self._site_name} …{sn[-4:]}"
        elif info and info.name and info.name != sn:
            name = info.name
        else:
            name = f"{model.split('-')[0] or 'BLUETTI'} …{sn[-4:]}"
        return Station(sn=sn, model=model, name=name)

    async def _bind(self) -> None:
        self._last_bind = self._clock()
        await self._cloud.bind([s.sn for s in self._stations])
        self._bound = True
        await self._sleep(BIND_SETTLE_S)

    async def fetch(self) -> dict[str, Sample | None]:
        if not self._stations:
            await self.start()
        elif not self._bound:
            await self._bind()
        sns = [s.sn for s in self._stations]
        by_sn = {s.sn: s for s in await self._cloud.device_states(sns)}
        empty = [sn for sn in sns if sn not in by_sn or not by_sn[sn].raw]
        if empty and self._clock() - self._last_bind >= REBIND_MIN_INTERVAL_S:
            log.info("%d station(s) returned no data; binding again", len(empty))
            try:
                await self._bind()
            except CloudError as exc:
                log.warning("bindDevices failed: %s", exc)
            else:
                by_sn.update({s.sn: s for s in await self._cloud.device_states(sns)})
        return {sn: by_sn.get(sn) for sn in sns}


class MultiStateSource:
    """Stations of several sites (BLUETTI logins) polled together.

    A site that fails (expired login, its gateway down) does not stop the others: its
    stations get no sample and the error is kept in ``errors``. Only when every site fails
    is the error raised, an expired login first. A site that could not start is started
    again on the next ``fetch``; its stations then appear in ``stations``.
    """

    def __init__(self, sources: Mapping[str, PollingStateSource]) -> None:
        self._sources = dict(sources)
        self._errors: dict[str, Exception] = {}

    @property
    def stations(self) -> list[Station]:
        return [st for src in self._sources.values() for st in src.stations]

    @property
    def errors(self) -> Mapping[str, Exception]:
        return dict(self._errors)

    async def start(self) -> list[Station]:
        await self._each(lambda src: src.start())
        return self.stations

    async def fetch(self) -> dict[str, Sample | None]:
        results = await self._each(lambda src: src.fetch())
        samples: dict[str, Sample | None] = {st.sn: None for st in self.stations}
        for result in results.values():
            samples.update(result)
        return samples

    async def _each(self, call: Callable[[PollingStateSource], Awaitable[Any]]) -> dict[str, Any]:
        ids = list(self._sources)
        answers = await asyncio.gather(
            *(call(self._sources[i]) for i in ids), return_exceptions=True
        )
        results: dict[str, Any] = {}
        errors: dict[str, Exception] = {}
        for site, answer in zip(ids, answers, strict=True):
            if isinstance(answer, CloudError | AuthError):
                errors[site] = answer
            elif isinstance(answer, BaseException):
                raise answer
            else:
                results[site] = answer
        self._errors = errors
        if errors and not results:
            first = list(errors.values())
            raise next((e for e in first if isinstance(e, AuthExpiredError)), first[0])
        for site, exc in errors.items():
            log.warning("Station %s: %s", site, exc)
        return results
