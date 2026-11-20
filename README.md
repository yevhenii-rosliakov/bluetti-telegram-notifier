# bluetti-grid-bot

[Українською](README.uk.md)

A Telegram bot that tells you when **grid power goes out and comes back**, using a BLUETTI
power station as the sensor. It polls the station through the BLUETTI cloud and sends:

- ⚡ **Power went out** — `Power went out at 14:32` (+ battery %, load)
- ✅ **Power is back** — `Power is back at 16:05, it was out for 1 h 33 min` (+ battery %)
- 🔋 **Low battery** while on battery — configurable thresholds (default 30 % and 15 %), each once per outage
- ⚠️ **AC output turned off** while it should be on (ECO mode, or someone switched it off)
- 📡 **Station offline** / **back online** — reported as "no connection, state unknown", never as an outage or a restoration

Commands: `/status`, `/history [n]`, `/help`. Several stations per account are supported.

> **Unofficial API.** This project uses the same *unofficial* BLUETTI cloud API as the
> community [Home Assistant integration](https://github.com/bluetti-community/bluetti-home-assistant)
> (MIT) and `pybluetti`, including the integration's built-in public OAuth client. It is not
> endorsed by BLUETTI and may break if BLUETTI changes its cloud.

**Supported stations:** anything that shows up in the BLUETTI app with Wi-Fi and cloud support.
Tested on a **Premium 200 V2 (PR200V2)**. Other models may name or omit fields differently;
unknown values are treated as "unknown", never as a crash. Please report what `check` prints
for your model.

---

## Contents

1. [Prerequisites](#prerequisites)
2. [Quick start (Docker)](#quick-start-docker)
3. [Getting the BLUETTI token](#getting-the-bluetti-token)
4. [Choosing `DETECTION_MODE`](#choosing-detection_mode)
5. [Recommended station settings](#recommended-station-settings)
6. [Configuration reference](#configuration-reference)
7. [Running without Docker](#running-without-docker)
8. [Kubernetes (Helm)](#kubernetes-helm) · [ArgoCD](#argocd)
9. [Moving to another host](#moving-to-another-host)
10. [Health and metrics](#health-and-metrics)
11. [Troubleshooting](#troubleshooting)
12. [Security notes](#security-notes)
13. [Design notes](#design-notes)

---

## Prerequisites

- A BLUETTI station **bound to your BLUETTI account** in the BLUETTI app and connected to
  **2.4 GHz Wi-Fi** (most stations don't support 5 GHz).
- A Telegram bot: talk to [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
- Your chat ID:
  - Private chat: send any message to [@userinfobot](https://t.me/userinfobot) — it answers with your ID.
  - Group: add your bot to the group, send a message there, open
    `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `"chat":{"id":-100…}`.
    Group IDs are negative.
  - Send `/start` to your bot once, otherwise Telegram does not let it write to you.
- Docker (with Compose), or Python 3.12+, or a Kubernetes cluster.

## Quick start (Docker)

```bash
git clone https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier.git
cd bluetti-telegram-notifier
cp .env.example .env        # set TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS, TZ, BOT_LANGUAGE

docker compose run --rm bot login   # sign in to BLUETTI (see next section)
# paste the printed BLUETTI_* lines into .env

docker compose run --rm bot check   # optional: see what the bot sees and decides
docker compose up -d
docker compose logs -f
```

The bot sends "Bot started …" and answers `/status`. Images are published for
`linux/amd64` and `linux/arm64` (Raspberry Pi 4/5 with a 64-bit OS) at
`ghcr.io/yevhenii-rosliakov/bluetti-grid-bot`.

Tokens, the chosen gateway, the last station state and the outage history live in a SQLite
file in the `bot-data` volume (`/data`). Keep that volume: the refresh token rotates and
the newest one is only stored there.

## Getting the BLUETTI token

The bot signs in with OAuth2 like the Home Assistant integration. You do this once; afterwards
the bot refreshes its token by itself.

1. Run `login`:
   - Docker: `docker compose run --rm bot login`
   - Python: `bluetti-grid-bot login`
   - Kubernetes: `kubectl exec -it deploy/<release> -- bluetti-grid-bot login`
2. Open the printed `https://sso.bluettipower.com/oauth2/grant?…` link and sign in with the
   BLUETTI account **that owns the station** (stations shared with you may not be listed).
3. You land on a **My Home Assistant** page that says it is not configured and shows a
   "Home Assistant URL" field and a **Link account** button. **Do not enter anything and do
   not click Link account.** Copy the full URL from the address bar — it contains
   `?code=…&state=…`.
4. No `code=` in the address bar? Find the URL in the browser history, or open DevTools →
   Network, tick **Preserve log**, sign in again and copy the request URL of the redirect.
5. Paste it into the terminal **within a few minutes** — the code is single-use and
   short-lived. If it fails, just run `login` again.
6. `login` stores the tokens in `STATE_DIR`, finds your data center, lists your stations
   (SN, model, online, cloud support) and prints lines like:
   ```
   BLUETTI_REFRESH_TOKEN=…
   BLUETTI_GATEWAY=eu
   BLUETTI_DEVICE_SNS=PR200V2XXXXXXXXXXXXXX
   ```
   Copy them into `.env` (or your Kubernetes Secret / values).

Notes:
- The code in the URL is double-encoded (`%252B`); the bot handles it — paste the URL as is.
- A `state` mismatch (URL from an earlier attempt) is only a warning.
- **Token precedence:** the token stored in `STATE_DIR` is the newest and wins. The
  `BLUETTI_REFRESH_TOKEN` variable only seeds an empty state, or replaces the stored token when
  you change its value (e.g. after running `login` on another machine).
- If the refresh token stops working, the bot sends "BLUETTI login expired, run `login`
  again" once and retries every 5 minutes. A `login` run against the same `STATE_DIR` is
  picked up without a restart.

## Choosing `DETECTION_MODE`

**`GridAllTotalPower` alone is not reliable.** With a full battery and nothing drawn from the
station, it reads `0` even while the grid is present — the station neither charges nor passes
power through. How to tell an outage apart therefore depends on how the station is wired.

### `ats` (default) — station is the backup input of an automatic transfer switch

```
             ┌─────────────┐
 grid ──────►│ voltage     ├──────────────────────► ATS input 1 (main)
             │ relay       │                                      │
             └──────┬──────┘                                      ▼
                    │                                    ┌────────────────┐
                    └──► socket ──► BLUETTI ──AC out───► │ ATS input 2    ├──► apartment
                         (charging)                      └────────────────┘
```

While the grid is present the apartment runs from the grid directly and the station has **no AC
load**. During an outage the ATS switches to the station and the apartment (router, fridge, …)
draws from it, so AC load is never zero.

- on battery: `ACLoadAllTotalPower >= LOAD_THRESHOLD_W` **and** `GridAllTotalPower < GRID_THRESHOLD_W`
- grid present: `GridAllTotalPower >= GRID_THRESHOLD_W` **or** `ACLoadAllTotalPower < LOAD_THRESHOLD_W`

Also fits any setup where nothing is plugged into the station while the grid is present.

### `passthrough` — loads are always plugged into the station

```
 grid ──► socket ──► BLUETTI (charges / passes through) ──AC/DC out──► router, fridge, …
```

- on battery: `GridAllTotalPower < GRID_THRESHOLD_W` **and** AC + DC load `>= LOAD_THRESHOLD_W`
- also on battery: SOC dropped by `SOC_DROP_PCT` or more since grid input was last seen
- grid present: `GridAllTotalPower >= GRID_THRESHOLD_W`
- no grid input and no load: undecided, the last state is kept

### `grid_input` — the station always draws something from the grid

```
 grid ──► socket ──► BLUETTI (always charging or passing through a constant load)
```

- on battery: `GridAllTotalPower < GRID_THRESHOLD_W`, otherwise grid present.

### Common rules

- A new state must be seen in `DEBOUNCE_POLLS` consecutive polls (restoration:
  `RESTORE_DEBOUNCE_POLLS`). The reported time is the **first** poll that saw it.
- The first poll after startup sets a baseline. If the station is on battery the bot says so
  ("Bot started: the station is currently on battery…"); otherwise "Bot started: grid power
  is present" (set `NOTIFY_STARTUP=false` to stay silent).
- If the station goes offline and comes back in a different state, the message says
  "between 14:30 and 15:10 (exact time unknown)".

### Verify your setup

Run `check` once during a real or **simulated outage**: switch off the main input breaker
(not only the station's own charging socket — in the `ats` topology that would just stop
charging), wait a minute and run:

```bash
docker compose run --rm bot check
```

Make sure small loads (e.g. a 10 W router) show up as `AC load` and the line
`=> detector: BATTERY` appears. If your load is below 5 W, lower `LOAD_THRESHOLD_W`.

## Recommended station settings

- **Turn AC ECO off.** With ECO on, the AC output can switch itself off at low load and does
  not switch back on by itself — in the `ats` topology the backup would then be dead.
  `/status` warns when AC ECO is on, and the bot alerts when the AC output is off.
- Keep the **AC output on**.
- Use **UPS mode** (if your model has it) for the fastest switchover.
- In the `ats` topology, an AC output that is off reads as 0 W load and therefore as "grid
  present"; the ⚠️ AC-off alert (`ALERT_AC_OFF=true`) covers that case.

## Configuration reference

All configuration comes from environment variables (`.env` with Docker). Invalid values stop
the bot at startup with a message naming the variable. See [.env.example](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **required** for `run` |
| `TELEGRAM_CHAT_IDS` | — | **required** for `run`; comma-separated, groups are negative |
| `TELEGRAM_ALLOWED_CHAT_IDS` | = `TELEGRAM_CHAT_IDS` | who may use commands; others are ignored silently |
| `BLUETTI_REFRESH_TOKEN` | — | from `login`; seeds the state DB (the rotated token in the DB wins) |
| `BLUETTI_ACCESS_TOKEN` | — | debugging only |
| `BLUETTI_GATEWAY` | `auto` | `auto` (probe, remember) / `eu` / `global` / `us` / full URL |
| `BLUETTI_GATEWAY_PROBE_ORDER` | `eu,global,us` | order for `auto` |
| `BLUETTI_DEVICE_SNS` | empty = all | comma-separated serial numbers |
| `BLUETTI_DEVICE_NAMES` | — | `SN1=Mom's flat,SN2=Garage`; shown when several stations are monitored |
| `BLUETTI_SSO_URL` | `https://sso.bluettipower.com` | |
| `BLUETTI_CLIENT_ID` | `HomeAssistant` | HA integration's public client |
| `BLUETTI_CLIENT_SECRET` | `SG9tZUFzc2lzdGFudA==` | sent as-is |
| `BLUETTI_REDIRECT_URI` | `https://my.home-assistant.io/redirect/oauth` | registered for that client |
| `POLL_INTERVAL_SECONDS` | `60` | min 15 |
| `DETECTION_MODE` | `ats` | `ats` / `passthrough` / `grid_input` |
| `LOAD_THRESHOLD_W` | `5` | |
| `GRID_THRESHOLD_W` | `5` | |
| `SOC_DROP_PCT` | `2` | `passthrough` only |
| `DEBOUNCE_POLLS` | `2` | also used for the AC-off alert |
| `RESTORE_DEBOUNCE_POLLS` | `2` | |
| `OFFLINE_GRACE_SECONDS` | `600` | no data this long → "station offline" |
| `LOW_SOC_THRESHOLDS` | `30,15` | empty disables |
| `ALERT_AC_OFF` | `true` | |
| `NOTIFY_STARTUP` | `true` | "Bot started" message when the grid is present |
| `BOT_LANGUAGE` | `uk` | `uk` / `en` |
| `TZ` | `UTC` | time zone for times in messages, e.g. `Europe/Kyiv` |
| `STATE_DIR` | `/data` | tokens, state, history (SQLite) |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs serial numbers |
| `LOG_FORMAT` | `text` | `json` for JSON lines |
| `METRICS_ENABLED` | `false` | Prometheus `/metrics` |
| `HTTP_PORT` | `8080` | `/healthz`, `/readyz`, `/metrics`; `0` disables |

## Running without Docker

```bash
uv sync                     # or: python -m venv .venv && .venv/bin/pip install .
export STATE_DIR=./data     # or put it into .env (read from the current directory)
uv run bluetti-grid-bot login
uv run bluetti-grid-bot check
uv run bluetti-grid-bot run   # `run` is also the default
```

## Kubernetes (Helm)

The chart is published to GHCR as an OCI artifact.

```bash
kubectl create namespace bluetti
kubectl -n bluetti create secret generic bluetti-grid-bot-secrets \
  --from-literal=TELEGRAM_BOT_TOKEN='123456:…' \
  --from-literal=BLUETTI_REFRESH_TOKEN=''          # may stay empty until `login`

helm install bluetti-grid-bot oci://ghcr.io/yevhenii-rosliakov/charts/bluetti-grid-bot \
  -n bluetti \
  --set existingSecret=bluetti-grid-bot-secrets \
  --set config.TELEGRAM_CHAT_IDS=123456789 \
  --set config.TZ=Europe/Kyiv

kubectl -n bluetti exec -it deploy/bluetti-grid-bot -- bluetti-grid-bot login
```

- One replica with `strategy: Recreate` — a single poller with SQLite on a PVC
  (`persistence.size: 100Mi`, `storageClass`, `existingClaim`). With
  `persistence.enabled=false` an `emptyDir` is used and the rotated token is lost on restart.
- `config.*` holds every non-secret variable (ConfigMap); `secrets.*` creates a Secret, or
  point `existingSecret` at your own Secret with the keys `TELEGRAM_BOT_TOKEN` and
  `BLUETTI_REFRESH_TOKEN` (all its keys become env vars).
- Runs as UID 10001 with a read-only root filesystem, all capabilities dropped, probes on
  `/healthz` and `/readyz`. `metrics.enabled` + `metrics.serviceMonitor.enabled` add a
  Service and a `ServiceMonitor`.
- The pod reports NotReady until the first successful poll (e.g. before `login`); `kubectl
  exec` still works.

**External Secrets Operator** (e.g. from Vault):

```yaml
apiVersion: external-secrets.io/v1   # v1beta1 on older ESO releases
kind: ExternalSecret
metadata:
  name: bluetti-grid-bot-secrets
  namespace: bluetti
spec:
  refreshInterval: 1h
  secretStoreRef: { kind: ClusterSecretStore, name: vault }
  target: { name: bluetti-grid-bot-secrets }
  data:
    - secretKey: TELEGRAM_BOT_TOKEN
      remoteRef: { key: home/bluetti-grid-bot, property: telegram_bot_token }
    - secretKey: BLUETTI_REFRESH_TOKEN
      remoteRef: { key: home/bluetti-grid-bot, property: bluetti_refresh_token }
```

The secret is only a seed: the bot keeps the rotated refresh token on its PVC. Updating
`BLUETTI_REFRESH_TOKEN` in Vault (to a new value) makes the bot adopt it on the next restart.

### ArgoCD

No hooks are used, so the chart works with plain ArgoCD:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bluetti-grid-bot
  namespace: argocd
spec:
  project: default
  destination: { server: https://kubernetes.default.svc, namespace: bluetti }
  source:
    repoURL: ghcr.io/yevhenii-rosliakov/charts
    chart: bluetti-grid-bot
    targetRevision: 0.1.0
    helm:
      valuesObject:
        existingSecret: bluetti-grid-bot-secrets
        config:
          TELEGRAM_CHAT_IDS: "123456789"
          TZ: Europe/Kyiv
          DETECTION_MODE: ats
  syncPolicy:
    automated: { prune: true, selfHeal: true }
    syncOptions: [CreateNamespace=true]
```

(For an OCI Helm repo, add it in ArgoCD as a Helm repository with "Enable OCI".)

## Moving to another host

All state lives in one SQLite database on the `/data` volume: the rotating BLUETTI tokens, the
cached gateway, the per-station detector state and the outage history. To move the bot, copy that
volume together with `.env`.

Run **one instance at a time**. BLUETTI rotates the refresh token on every use, so two copies
invalidate each other's login, and Telegram allows only one poller per bot token.

On the old host:

```bash
docker compose down                       # stop the bot; the volume is kept
docker volume ls | grep bot-data          # e.g. bluetti-telegram-notifier_bot-data
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar czf /backup/bot-data.tgz -C /data .
scp bot-data.tgz .env docker-compose.yml newhost:~/bluetti/
```

On the new host, in `~/bluetti`:

```bash
docker compose up --no-start              # create the volume without starting the bot
docker volume ls | grep bot-data          # the name is derived from the directory name
docker run --rm -v <volume>:/data -v "$PWD":/backup alpine \
  tar xzf /backup/bot-data.tgz -C /data   # runs as root, keeps the files owned by UID 10001
docker compose up -d
docker compose logs -f                    # check that it signs in and polls
rm bot-data.tgz                           # the archive contains tokens
```

Stop the bot before copying so the database (and its `-wal` file) is consistent, and don't start
the old copy again.

Starting fresh instead also works: keep only `.env` and run `docker compose run --rm bot login`
again, because the `BLUETTI_REFRESH_TOKEN` in `.env` has most likely been rotated already. The
outage history is lost and "power present since" restarts from the first poll.

With Helm: scale the Deployment to 0, copy the PVC contents to the new cluster's PVC (keeping UID
10001 ownership), then scale back to 1.

## Health and metrics

- `GET /healthz` — 200 while the polling loop is alive.
- `GET /readyz` — 200 if a poll succeeded within the last `max(3 × POLL_INTERVAL_SECONDS, 300)` s.
- `GET /metrics` (with `METRICS_ENABLED=true`) — `bluetti_polls_total`, `bluetti_grid_state`,
  `bluetti_battery_soc_percent`, `bluetti_power_watts{channel=grid|ac_load|dc_load|pv}`,
  `bluetti_station_online`, `bluetti_events_total`, `bluetti_notifications_total`,
  `bluetti_last_successful_poll_timestamp_seconds`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `online=0`, empty `stateList` | The station must be **bound** to the API client (`bindDevices`). The bot does this at startup and again (at most every 5 min) when a station returns no data. If it persists, the station really is offline: check its Wi-Fi (2.4 GHz) in the BLUETTI app. |
| `No BLUETTI gateway accepted the token` | Token invalid, or the account is in a data center not in `BLUETTI_GATEWAY_PROBE_ORDER`. Run `login` again; try `BLUETTI_GATEWAY=eu`/`global`/`us`. |
| Wrong gateway after moving the account | With `auto` the gateway is cached in `STATE_DIR`; it is forgotten and re-probed automatically when it rejects calls. |
| "BLUETTI login expired" | The refresh token was revoked (e.g. password change, too long offline). Run `login` again. |
| No stations listed in `login` | Sign in with the account that **owns** the station, not one it was shared with. Only stations with `cloud=1` (Wi-Fi) are polled. |
| Outage not detected | Run `check` during an outage; adjust `DETECTION_MODE` / `LOAD_THRESHOLD_W`. Check that AC ECO is off and AC output on. |
| "No connection" during every outage | Your router or ISP has no backup power, so the station can't reach the cloud. Power the router (and ONT) from the station. |
| Bot does not answer commands | The chat is not in `TELEGRAM_ALLOWED_CHAT_IDS`, or you never sent `/start` to the bot. |

`msgCode` values seen so far: `0` = OK; `805` = access token expired (the bot refreshes and
retries once). Any other non-zero code is logged with the server's message; on a cached
gateway it triggers a re-probe.

**About "no connection":** the station talks to the cloud through your Wi-Fi. If the router or
the ISP loses power during an outage, the station goes offline, and the bot can only say "no
connection, state unknown". In practice that often *is* an outage — the message says so — but
the bot never claims it as one.

## Security notes

- Keep `.env` and `STATE_DIR` private. The **refresh token gives full control of the station
  through the cloud** (including switching outputs). The state DB is created with mode `0600`.
- Tokens, codes and the Telegram token are never logged; serial numbers only at `DEBUG`.
- The OAuth client credentials are the public ones built into the Home Assistant integration.
- Only chats in `TELEGRAM_ALLOWED_CHAT_IDS` can query the bot; it never controls the station.

## Design notes

- **Telegram library: aiogram 3.** asyncio-native, fully typed, and built on aiohttp — the
  same HTTP stack as the BLUETTI client, so the service is one event loop with one HTTP
  library and no threads.
- **No `pybluetti` dependency.** The bot needs five calls (token, devices, bind, states, and
  the gateway probe); a thin client keeps control over `msgCode 805` handling, refresh-token
  rotation, gateway caching and rebinding.
- `detector.py` is a pure state machine (`previous state + sample + config + now → new state +
  events`) and is tested exhaustively. Samples come from a `StateSource` interface; the
  polling implementation can be swapped for BLUETTI's WebSocket push channel later.
- SQLite via the standard library: tiny local writes, each in its own transaction, WAL mode so
  `login` can run next to the service.

## License

[MIT](LICENSE). Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
