"""Wiring for the three CLI commands: ``run``, ``login``, ``check``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError, TelegramUnauthorizedError

from . import __version__
from .auth import AuthError, OAuthClient, TokenManager, extract_code
from .cloud import BluettiCloud, CloudError, MultiStateSource, PollingStateSource, gateway_name
from .config import ConfigError, ScheduleSource, Settings, Site
from .detector import classify
from .health import HealthServer, Metrics
from .messages import Formatter, Translator
from .monitor import Monitor
from .notifier import TelegramNotifier, build_dispatcher, set_commands
from .schedule import ScheduleClient, ScheduleService
from .storage import Storage, StorageError

log = logging.getLogger(__name__)


def oauth_client(s: Settings) -> OAuthClient:
    return OAuthClient(
        sso_url=s.bluetti_sso_url,
        client_id=s.bluetti_client_id,
        client_secret=s.bluetti_client_secret.get_secret_value(),
        redirect_uri=s.bluetti_redirect_uri,
    )


def open_storage(s: Settings) -> Storage:
    if s.database_url is not None:
        return Storage.postgres(
            s.database_url.get_secret_value(),
            s.database_username,
            s.database_password.get_secret_value() if s.database_password else None,
        )
    try:
        return Storage.sqlite(s.db_path)
    except StorageError as exc:
        raise StorageError(f"Cannot use STATE_DIR={s.state_dir}: {exc.__cause__}") from exc


@dataclass
class _SiteClient:
    site: Site
    tokens: TokenManager
    cloud: BluettiCloud


@dataclass
class _Cloud:
    session: aiohttp.ClientSession
    storage: Storage
    sites: list[_SiteClient]


@asynccontextmanager
async def _cloud(s: Settings, sites: Sequence[Site] | None = None) -> AsyncIterator[_Cloud]:
    """Storage, HTTP session and one cloud client per site (default: all configured)."""
    try:
        storage = open_storage(s)
    except StorageError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        async with aiohttp.ClientSession() as session:
            clients = []
            for site in s.sites if sites is None else sites:
                tokens = TokenManager(
                    session,
                    oauth_client(s),
                    storage,
                    seed_refresh_token=(
                        site.refresh_token.get_secret_value() if site.refresh_token else None
                    ),
                    seed_access_token=(
                        site.access_token.get_secret_value() if site.access_token else None
                    ),
                    site=site.id,
                )
                cloud = BluettiCloud.for_site(
                    session, tokens, storage, site, s.bluetti_gateway_probe_order
                )
                clients.append(_SiteClient(site, tokens, cloud))
            yield _Cloud(session, storage, clients)
    finally:
        storage.close()


def _state_source(s: Settings, c: _Cloud) -> MultiStateSource:
    return MultiStateSource(
        {
            sc.site.id: PollingStateSource(
                sc.cloud,
                sns=sc.site.device_sns,
                names=s.bluetti_device_names,
                site=sc.site.id,
                site_name=sc.site.name,
            )
            for sc in c.sites
        }
    )


# run


async def _telegram_polling(dp: Dispatcher, bot: Bot, stop: asyncio.Event) -> None:
    """Answer commands. Telegram being unreachable (e.g. the ISP is down during an outage)
    must not stop the service, so network errors are retried; a bad token is fatal."""
    delay = 5.0
    while not stop.is_set():
        try:
            await dp.start_polling(
                bot, handle_signals=False, close_bot_session=False, allowed_updates=["message"]
            )
            return
        except TelegramUnauthorizedError:
            raise
        except (TelegramAPIError, aiohttp.ClientError, TimeoutError) as exc:
            log.warning("Telegram polling failed (%s); retrying in %.0f s", exc, delay)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            delay = min(delay * 2, 300.0)


async def run_service(s: Settings) -> int:
    s.require_telegram()
    assert s.telegram_bot_token is not None  # noqa: S101 - checked by require_telegram
    log.info("bluetti-grid-bot %s starting", __version__)
    log.debug("Settings: %s", s.masked())
    metrics = Metrics()
    t = Translator(s.bot_language)
    formatter = Formatter(t, s.zone, names={})

    async with _cloud(s) as c:
        source = _state_source(s, c)
        bot = Bot(token=s.telegram_bot_token.get_secret_value())
        notifier = TelegramNotifier(bot, s.telegram_chat_ids, metrics)
        # One service per distinct schedule: sites in the same queue share it.
        services: dict[ScheduleSource, ScheduleService] = {}
        schedules: dict[str, ScheduleService] = {}
        for site in s.sites:
            if (src := site.schedule) is not None:
                if src not in services:
                    client = ScheduleClient(c.session, src.provider, src.region, src.group)
                    services[src] = ScheduleService(client, c.storage)
                schedules[site.id] = services[src]
        monitor = Monitor(s, c.storage, source, notifier, formatter, metrics, schedules=schedules)
        dp = build_dispatcher(monitor, s.allowed_chat_ids)
        health = HealthServer(
            s.http_port,
            alive=monitor.alive,
            ready=monitor.ready,
            metrics=metrics if s.metrics_enabled else None,
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        if s.http_port:
            await health.start()
        await set_commands(bot, t)
        monitor_task = asyncio.create_task(monitor.run(stop), name="monitor")
        polling_task = asyncio.create_task(_telegram_polling(dp, bot, stop), name="telegram")
        stop_task = asyncio.create_task(stop.wait(), name="stop")
        # The schedule is optional: if its loop dies the bot keeps working without it.
        schedule_tasks = [
            asyncio.create_task(service.run(stop), name=f"schedule {service.key}")
            for service in services.values()
        ]
        exit_code = 0
        try:
            done, _ = await asyncio.wait(
                {monitor_task, polling_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is not stop_task and (exc := task.exception()) is not None:
                    log.error("Task %s failed: %r", task.get_name(), exc)
                    exit_code = 1
        finally:
            log.info("Shutting down")
            stop.set()
            with contextlib.suppress(RuntimeError):
                await dp.stop_polling()
            tasks = [monitor_task, polling_task, stop_task, *schedule_tasks]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await health.stop()
            await bot.session.close()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.remove_signal_handler(sig)
        return exit_code


# login


LOGIN_HELP = """
BLUETTI login{title}
=============
1. Open this link in a browser and sign in with the BLUETTI account that OWNS the station:

   {url}

2. You will land on a "My Home Assistant" page saying it is not configured.
   Do NOT enter anything there and do NOT click "Link account".
   Copy the FULL address from the browser's address bar (it contains ?code=...&state=...).
   No code= in the address bar? Look in the browser history, or use DevTools -> Network
   with "Preserve log" enabled and copy the request URL.

3. Paste it below within a few minutes (the code is single-use and short-lived).
"""


def login(s: Settings, station: str | None = None, read_line: Callable[[str], str] = input) -> int:
    if station:
        site = s.site(station)
    elif s.bluetti_stations:
        raise ConfigError(
            "BLUETTI_STATIONS is set: choose the station to sign in for with "
            f"`login --station <id>` (one of: {', '.join(s.bluetti_stations)})"
        )
    else:
        site = s.sites[0]
    oauth = oauth_client(s)
    state = secrets.token_urlsafe(16)
    title = f" for station {site.id!r}" if site.id else ""
    print(LOGIN_HELP.format(title=title, url=oauth.authorize_url(state)))
    try:
        pasted = read_line("Redirected URL: ")
    except (EOFError, KeyboardInterrupt):
        print("\nAborted. (With Docker, run it with -it: docker compose run --rm bot login)")
        return 130
    try:
        code, state_ok = extract_code(pasted, state)
    except AuthError as exc:
        print(f"\nError: {exc}")
        return 1
    if not state_ok:
        print(
            "\nWarning: 'state' does not match this login attempt (URL from an earlier try?). "
            "Continuing anyway."
        )
    return asyncio.run(_finish_login(s, site, code))


async def _finish_login(s: Settings, site: Site, code: str) -> int:
    async with _cloud(s, [site]) as c:
        (sc,) = c.sites
        try:
            tokens = await sc.tokens.exchange_code(code)
        except AuthError as exc:
            print(
                f"\nToken exchange failed: {exc}\n"
                "The code is single-use and expires within minutes: run `login` again."
            )
            return 1
        print(f"\nSigned in. Tokens saved to {s.db_path}")
        try:
            if site.gateway == "auto":
                print("\nProbing gateways:")
                gateway = await sc.cloud.probe_gateway()
            else:
                gateway = await sc.cloud.gateway()
            devices = await sc.cloud.list_devices()
        except (CloudError, AuthError) as exc:
            print(f"\nCould not list stations: {exc}")
            return 1

        print(f"\nGateway: {gateway_name(gateway)} ({gateway})\n\nStations on this account:")
        if not devices:
            print("  (none - sign in with the account the station is bound to, not a shared one)")
        for d in devices:
            print(
                f"  SN={d.sn}  model={d.model}  name={d.name}  online={int(d.online)}  "
                f"owner={int(d.bound_by_me)}  cloud={int(d.cloud)}"
            )
        cloud_sns = [d.sn for d in devices if d.cloud]
        print(
            "\nAdd these lines to .env (or your Kubernetes Secret / values). Keep them private:\n"
        )
        if site.id and site.id not in s.bluetti_stations:
            print(f"BLUETTI_STATIONS={','.join([*s.bluetti_stations, site.id])}")
        if site.id:
            name = site.name or (devices[0].name if len(devices) == 1 else "") or site.id
            print(f"{site.var('NAME')}={name}")
        print(f"{site.var('REFRESH_TOKEN')}={tokens.refresh_token or ''}")
        print(f"{site.var('GATEWAY')}={gateway_name(gateway)}")
        print(f"{site.var('DEVICE_SNS')}={','.join(cloud_sns)}")
        if site.id and not s.bluetti_stations and (s.bluetti_refresh_token or s.bluetti_device_sns):
            print("\nRemove BLUETTI_REFRESH_TOKEN and BLUETTI_DEVICE_SNS: with BLUETTI_STATIONS")
            print("every station has its own BLUETTI_<ID>_... settings.")
        return 0


# check


async def check(s: Settings) -> int:
    cfg = s.detector_config()
    async with _cloud(s) as c:
        ok = True
        for sc in c.sites:
            prefix = f"[{sc.site.id}] " if sc.site.id else ""
            try:
                gateway = await sc.cloud.gateway()
            except (CloudError, AuthError) as exc:
                print(f"{prefix}Error: {exc}")
                ok = False
            else:
                print(f"{prefix}Gateway: {gateway_name(gateway)} ({gateway})")
        source = _state_source(s, c)
        try:
            stations = await source.start()
            print(f"Bound {len(stations)} station(s); fetching state...")
            samples = await source.fetch()
        except (CloudError, AuthError) as exc:
            print(f"Error: {exc}")
            return 1
        for site, err in source.errors.items():
            print(f"[{site}] Error: {err}")
            ok = False

        print(
            f"\nDetection: mode={cfg.mode.value} load>={cfg.load_threshold_w:g} W "
            f"grid>={cfg.grid_threshold_w:g} W debounce={cfg.debounce_polls}/"
            f"{cfg.restore_debounce_polls} polls"
        )
        for st in source.stations:
            sample = samples.get(st.sn)
            print(f"\n{st.name}  (SN {st.sn}, model {st.model or '?'})")
            if sample is None or not sample.has_data:
                ok = False
                online = "?" if sample is None else int(sample.online)
                print(f"  online={online}, no data (empty stateList). See README: Troubleshooting.")
                continue
            r = sample.reading
            print(f"  online={int(sample.online)}")
            for label, value in (
                ("Grid input, W", r.grid_w),
                ("AC load, W", r.ac_load_w),
                ("DC load, W", r.dc_load_w),
                ("Solar input, W", r.pv_w),
                ("SOC, %", r.soc),
                ("AC output on", r.ac_on),
                ("DC output on", r.dc_on),
                ("AC ECO on", r.ac_eco),
                ("Minutes to empty", r.dsg_minutes),
                ("Minutes to full", r.chg_minutes),
                ("Work mode", r.work_mode),
            ):
                print(f"  {label:18} {'?' if value is None else value}")
            if sample.invalid_fields:
                print(f"  unparseable fields: {', '.join(sample.invalid_fields)}")
            stored, _, _ = c.storage.load_device(st.sn)
            verdict = classify(r, cfg, stored.soc_at_grid if stored else None)
            state = verdict.state.value.upper() if verdict.state else "UNDECIDED"
            print(f"  => detector: {state} ({verdict.reason})")
            if r.ac_eco:
                print("  ! AC ECO is on: the AC output may switch itself off. Disable it.")
            if r.ac_on is False:
                print("  ! AC output is off: nothing on the station's AC outlets is powered.")
            print(f"  raw: {dict(sample.raw)}")
        return 0 if ok else 1
