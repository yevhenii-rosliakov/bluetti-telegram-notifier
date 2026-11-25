# bluetti-grid-bot Helm chart

Deploys [bluetti-grid-bot](https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier),
a Telegram bot that reports grid power outages using a BLUETTI power station, as a
single-replica Deployment.

The chart is published to GHCR as an OCI artifact:
`oci://ghcr.io/yevhenii-rosliakov/charts/bluetti-grid-bot`.

## Contents

1. [Quick start](#quick-start)
2. [Where the state lives](#where-the-state-lives)
   - [Option A: PVC (SQLite, default)](#option-a-pvc-sqlite-default)
   - [Option B: PostgreSQL](#option-b-postgresql)
3. [Secrets](#secrets)
4. [Application settings (`config.*`)](#application-settings-config)
5. [Chart values](#chart-values)
6. [Metrics](#metrics)
7. [Several stations](#several-stations)
8. [Operations](#operations)

## Quick start

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

# Sign in to BLUETTI once; the tokens are stored in the state (PVC or PostgreSQL).
kubectl -n bluetti exec -it deploy/bluetti-grid-bot -- bluetti-grid-bot login
```

The pod stays NotReady until the first successful poll (i.e. until `login` is done);
`kubectl exec` works regardless. A `login` is picked up within ~5 minutes without a restart.

## Where the state lives

The bot keeps its state in a database: the BLUETTI tokens, the cached gateway, the last
state of each station and the outage history. **The BLUETTI refresh token rotates on every
use and the newest one exists only in that database.** If the state is lost, you have to run
`login` again and the outage history is gone.

Choose one of the two options below. Do not run two releases against the same state:
two pollers invalidate each other's refresh token, and Telegram allows only one poller per
bot token. The chart therefore always runs `replicas: 1` with `strategy: Recreate`.

### Option A: PVC (SQLite, default)

The state is a SQLite file at `/data/bluetti-grid-bot.sqlite3`. The chart mounts the volume
named `data` at **`/data`** in the container and sets `STATE_DIR=/data`; the mount path is
fixed, do not set `config.STATE_DIR`.

By default the chart creates a 100 Mi PVC named after the release:

```yaml
persistence:
  enabled: true
  size: 100Mi             # plenty: the database stays small
  storageClass: ""        # "" = cluster default; "-" = storageClassName: "" (no dynamic provisioning)
  accessModes: [ReadWriteOnce]
  annotations: {}
```

**Use an existing PVC** (e.g. one you created, restored from a backup, or kept from a
previous release) — the chart then does not create one:

```yaml
persistence:
  enabled: true
  existingClaim: bluetti-data
```

```yaml
# Example claim, created by you in the same namespace
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: bluetti-data
  namespace: bluetti
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 100Mi
```

Requirements for the volume:

- **Writable by UID/GID 10001.** The pod runs as `10001:10001` with `fsGroup: 10001`, which
  covers most dynamically provisioned volumes. For NFS or hostPath volumes, where `fsGroup`
  is not applied, `chown 10001:10001` the directory yourself.
- **ReadWriteOnce is enough** — one pod at a time, and `Recreate` stops the old pod before the
  new one starts.
- **Local filesystem preferred.** SQLite runs in WAL mode and needs working file locks; avoid
  network filesystems with unreliable locking (some NFS setups, SMB). If that is all you have,
  use PostgreSQL instead.
- Everything in `/data` is created with mode `0600` — it contains tokens.

With `persistence.enabled: false` an `emptyDir` is mounted at `/data` instead: everything is
lost on every pod restart. Use it only for testing, or together with PostgreSQL.

**Backup** (the bot can keep running; SQLite's WAL makes `.backup` consistent):

```bash
kubectl -n bluetti exec deploy/bluetti-grid-bot -- \
  python -c "import sqlite3; sqlite3.connect('/data/bluetti-grid-bot.sqlite3').backup(sqlite3.connect('/tmp/backup.sqlite3'))"
kubectl -n bluetti cp "$(kubectl -n bluetti get pod -l app.kubernetes.io/name=bluetti-grid-bot -o name | head -1 | cut -d/ -f2)":/tmp/backup.sqlite3 ./bluetti-backup.sqlite3
```

Keep the backup private: it contains the refresh token.

### Option B: PostgreSQL

Set `DATABASE_URL` and the bot stores the same tables in PostgreSQL instead; `/data` is then
not used and the PVC can be disabled.

```yaml
persistence:
  enabled: false          # not needed with DATABASE_URL
secrets:
  DATABASE_URL: postgresql://bluetti:password@postgres.db.svc:5432/bluetti
```

or, with your own Secret, add the key `DATABASE_URL` to it (all its keys become env vars):

```bash
kubectl -n bluetti create secret generic bluetti-grid-bot-secrets \
  --from-literal=TELEGRAM_BOT_TOKEN='123456:…' \
  --from-literal=DATABASE_URL='postgresql://bluetti:password@postgres.db.svc:5432/bluetti'
```

The URL format is
`postgresql://USER:PASSWORD@HOST:PORT/DATABASE?sslmode=…` (`postgres://` is accepted too).

**Credentials separately.** `DATABASE_USERNAME` and `DATABASE_PASSWORD` override the user
and password in the URL. The URL then holds no secret and can go into `config`, while the
credentials stay in the Secret — and the password needs no encoding:

```yaml
persistence:
  enabled: false
config:
  DATABASE_URL: postgresql://postgres.db.svc:5432/bluetti?sslmode=require
secrets:
  DATABASE_USERNAME: bluetti
  DATABASE_PASSWORD: "p@ss:w/rd"
```

- **Percent-encode special characters** if the password is inside the URL: `@` → `%40`,
  `:` → `%3A`, `/` → `%2F`, `#` → `%23`, `?` → `%3F`, `%` → `%25`.
- **TLS:** append `?sslmode=require` to insist on TLS (the default is `prefer`). `verify-full`
  needs a CA file in the pod, and the chart has no option for extra volumes.
- **Permissions:** the bot creates its tables (`kv`, `device_state`, `outages`) on start with
  `CREATE TABLE IF NOT EXISTS`. The user needs `CONNECT` on the database and `CREATE` +
  `USAGE` on the schema (on PostgreSQL 15+ `public` no longer grants `CREATE` to everyone):

  ```sql
  CREATE ROLE bluetti LOGIN PASSWORD 'change-me';
  CREATE DATABASE bluetti OWNER bluetti;   -- the owner has all it needs
  ```

  To use another schema, set it on the role: `ALTER ROLE bluetti SET search_path = bluetti;`.
- **Availability:** if PostgreSQL is unreachable at startup, the pod exits with
  `Cannot connect to DATABASE_URL: …` and Kubernetes restarts it. A dropped connection while
  running is reopened automatically. Keep in mind that the bot is supposed to work *during a
  power outage*: the database should be on the same backup power as the bot.

**Using a Secret created by a PostgreSQL operator.** Point environment variables at it with
`extraEnv`; they take precedence over the same keys from `config`/`existingSecret`/`secrets`.
For example, CloudNativePG creates `<cluster>-app` with a ready-made `uri` key:

```yaml
persistence:
  enabled: false
extraEnv:
  - name: DATABASE_URL
    valueFrom:
      secretKeyRef:
        name: pg-cluster-app
        key: uri
```

Operators that store `username` / `password` keys (e.g. Zalando, Bitnami-style secrets):

```yaml
persistence:
  enabled: false
config:
  DATABASE_URL: postgresql://pg-cluster.db.svc:5432/bluetti?sslmode=require
extraEnv:
  - name: DATABASE_USERNAME
    valueFrom: { secretKeyRef: { name: bluetti.pg-cluster.credentials, key: username } }
  - name: DATABASE_PASSWORD
    valueFrom: { secretKeyRef: { name: bluetti.pg-cluster.credentials, key: password } }
```

Migrating from the PVC to PostgreSQL is not automated: after switching, run `login` again;
the outage history starts empty.

## Secrets

Secret values are put into a Kubernetes Secret and exposed to the pod as environment
variables. Either let the chart create the Secret from `secrets.*`:

```yaml
secrets:
  TELEGRAM_BOT_TOKEN: "123456:…"
  BLUETTI_REFRESH_TOKEN: ""
  DATABASE_URL: ""
  DATABASE_USERNAME: ""
  DATABASE_PASSWORD: ""
```

or create it yourself (kubectl, External Secrets Operator, Sealed Secrets, Vault…) and set
`existingSecret: <name>` — `secrets.*` is then ignored and **every key of your Secret becomes
an environment variable**.

| Key | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | **yes** | Bot token from [@BotFather](https://t.me/BotFather). |
| `BLUETTI_REFRESH_TOKEN` | no | Printed by `login`. Only *seeds* an empty state; the rotated token in the state wins. Changing the value replaces the stored token on the next restart (useful after a `login` on another machine). Can stay empty if you run `login` in the pod. |
| `BLUETTI_ACCESS_TOKEN` | no | Debugging only. |
| `BLUETTI_<ID>_REFRESH_TOKEN` | no | Per station with several stations, instead of `BLUETTI_REFRESH_TOKEN` (`stations.<id>.refreshToken` puts it into the chart's Secret). See [Several stations](#several-stations). |
| `BLUETTI_CLIENT_SECRET` | no | OAuth client secret; the default (Home Assistant integration's public client) is correct. |
| `DATABASE_URL` | no | PostgreSQL URL, see [Option B](#option-b-postgresql). Empty = SQLite on the PVC. May go into `config` instead when it holds no password. |
| `DATABASE_USERNAME` | no | Overrides the user in `DATABASE_URL`. Requires `DATABASE_URL`. |
| `DATABASE_PASSWORD` | no | Overrides the password in `DATABASE_URL`; no URL-encoding needed. Requires `DATABASE_URL`. |
| `TELEGRAM_CHAT_IDS` | no | May be put here instead of `config` if you consider it private. |

Example with External Secrets Operator:

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
    - secretKey: DATABASE_URL
      remoteRef: { key: home/bluetti-grid-bot, property: database_url }
```

The pod restarts automatically when a chart-managed Secret changes (checksum annotation);
with `existingSecret`, restart it yourself (`kubectl rollout restart`) or use a tool such as
Reloader.

## Application settings (`config.*`)

`config` is rendered into a ConfigMap and exposed as environment variables. Empty values are
left out, so the application default applies. Any variable from the
[configuration reference](https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier#configuration-reference)
can be added here, except the ones the chart sets itself (`STATE_DIR`, `HTTP_PORT`,
`METRICS_ENABLED`). All values must be strings in YAML (quote numbers and booleans).

**Telegram**

| Key | Chart default | Description |
|---|---|---|
| `TELEGRAM_CHAT_IDS` | — | **Required.** Chats that receive notifications, comma-separated. Group IDs are negative (`-100…`). |
| `TELEGRAM_ALLOWED_CHAT_IDS` | = `TELEGRAM_CHAT_IDS` | Chats allowed to use `/status`, `/stats`, … Others are ignored silently. |

**BLUETTI**

| Key | Chart default | Description |
|---|---|---|
| `BLUETTI_GATEWAY` | `auto` | Data center: `auto` (probe and remember), `eu`, `global`, `us`, or a full `https://` URL. |
| `BLUETTI_GATEWAY_PROBE_ORDER` | `eu,global,us` | Order in which `auto` probes gateways. |
| `BLUETTI_DEVICE_SNS` | all | Serial numbers to monitor, comma-separated. Empty = all cloud-capable stations of the account. |
| `BLUETTI_DEVICE_NAMES` | — | Display names: `SN1=Mom's flat,SN2=Garage`; shown when several stations are monitored. |
| `BLUETTI_STATIONS` | ids of `stations` | Several stations with their own logins. Usually generated from the `stations` map, see [Several stations](#several-stations). |
| `BLUETTI_SSO_URL` | `https://sso.bluettipower.com` | Change only if you know why. |
| `BLUETTI_CLIENT_ID` | `HomeAssistant` | OAuth client of the Home Assistant integration. |
| `BLUETTI_REDIRECT_URI` | `https://my.home-assistant.io/redirect/oauth` | Registered for that client; compared verbatim. |

**Detection**

| Key | Chart default | Description |
|---|---|---|
| `DETECTION_MODE` | `ats` | `ats` (station is the backup input of an ATS), `passthrough` (loads always on the station), `grid_input` (station always draws from the grid). See [Choosing DETECTION_MODE](https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier#choosing-detection_mode). |
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between polls, min 15. |
| `LOAD_THRESHOLD_W` | `5` | Watts at or above which "there is load". |
| `GRID_THRESHOLD_W` | `5` | Watts at or above which "grid input is present". |
| `SOC_DROP_PCT` | `2` | `passthrough` only: SOC drop (%) since the grid was last seen that counts as "on battery". |
| `DEBOUNCE_POLLS` | `2` | Consecutive polls before an outage (and an AC-off alert) is reported. |
| `RESTORE_DEBOUNCE_POLLS` | `2` | Consecutive polls before a restoration is reported. |
| `OFFLINE_GRACE_SECONDS` | `600` | No data this long → "station offline". |
| `LOW_SOC_THRESHOLDS` | `50,25` | Low battery alerts (%) while on battery, each once per outage. Empty disables. |
| `HIGH_LOAD_W` | `1000` | On battery, alert when the load stays at or above this many watts (e.g. a heater left on), once per outage; `0` disables. |
| `HIGH_LOAD_MINUTES` | `0` | Minutes the load must stay that high before the alert, e.g. `4` to ignore a kettle; `0`: after `DEBOUNCE_POLLS` polls. |
| `ALERT_AC_OFF` | `true` | Alert when the station's AC output is switched off. |
| `NOTIFY_STARTUP` | `true` | "Bot started …" on startup; `false` = silent unless it finds an outage not reported yet. |
| `FULL_CHARGE_SOC` | `100` | "Battery charged" after an outage at this %; `0` disables. |

**Reports and planned outage schedule**

| Key | Chart default | Description |
|---|---|---|
| `WEEKLY_SUMMARY` | `mon 09:00` | Weekday and time (in `TZ`) of the weekly summary; `off` disables. |
| `SCHEDULE_PROVIDER` | `off` | `off`, `yasno` or `outage-data-ua`. |
| `SCHEDULE_REGION` | — | `yasno`: `kyiv`, `dnipro-dnem`, `dnipro-cek`; `outage-data-ua`: `kyiv`, `kyiv-region`, `dnipro`, `odesa`. |
| `SCHEDULE_GROUP` | — | Your queue, e.g. `3.1`. Per station: `stations.<id>.schedule`. |
| `SCHEDULE_WARN_MINUTES` | `30` | Minutes before a planned outage to warn at, e.g. `60,15`; `0` disables. Per station: `stations.<id>.schedule.warnMinutes`. |

**Runtime**

| Key | Chart default | Description |
|---|---|---|
| `BOT_LANGUAGE` | `uk` | `uk` or `en`. |
| `TZ` | `UTC` | Time zone for times in messages, e.g. `Europe/Kyiv`. Set `Europe/Kyiv` when using the schedule. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. `DEBUG` also logs serial numbers. |
| `LOG_FORMAT` | `json` | `json` or `text`. |

## Chart values

| Value | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/yevhenii-rosliakov/bluetti-grid-bot` | Image (amd64 and arm64). |
| `image.tag` | `""` | Defaults to the chart's `appVersion`. |
| `image.pullPolicy` | `IfNotPresent` | |
| `imagePullSecrets` | `[]` | |
| `nameOverride` / `fullnameOverride` | `""` | |
| `config` | see above | Non-secret environment variables (ConfigMap). |
| `secrets` | see above | Secret environment variables; ignored when `existingSecret` is set. |
| `existingSecret` | `""` | Name of your own Secret; all its keys become env vars. |
| `persistence.enabled` | `true` | PVC for `/data`; `false` = `emptyDir`. |
| `persistence.size` | `100Mi` | |
| `persistence.storageClass` | `""` | `""` = cluster default, `"-"` = `storageClassName: ""`. |
| `persistence.accessModes` | `[ReadWriteOnce]` | |
| `persistence.existingClaim` | `""` | Use this PVC instead of creating one. |
| `persistence.annotations` | `{}` | Annotations on the created PVC (e.g. `helm.sh/resource-policy: keep`). |
| `httpPort` | `8080` | Port of `/healthz`, `/readyz`, `/metrics`. |
| `metrics.enabled` | `false` | Expose Prometheus metrics on `/metrics`. |
| `metrics.serviceMonitor.enabled` | `false` | Create a `ServiceMonitor` (needs `metrics.enabled`; also creates the Service). |
| `metrics.serviceMonitor.interval` | `60s` | |
| `metrics.serviceMonitor.scrapeTimeout` | `10s` | |
| `metrics.serviceMonitor.labels` | `{}` | E.g. `release: prometheus` for kube-prometheus-stack. |
| `metrics.dashboard.enabled` | `false` | Create a ConfigMap with the Grafana dashboard (needs `metrics.enabled`). |
| `metrics.dashboard.namespace` | `""` | Namespace the Grafana sidecar watches; `""` = release namespace. |
| `metrics.dashboard.labels` | `grafana_dashboard: "1"` | Label the Grafana sidecar looks for. |
| `metrics.dashboard.annotations` | `{}` | E.g. `grafana_folder: Home`. |
| `service.enabled` | `false` | ClusterIP Service for the HTTP port. |
| `service.type` / `service.port` | `ClusterIP` / `8080` | |
| `serviceAccount.create` | `true` | |
| `serviceAccount.name` | `""` | |
| `serviceAccount.annotations` | `{}` | |
| `serviceAccount.automountServiceAccountToken` | `false` | The bot does not talk to the Kubernetes API. |
| `podAnnotations` / `podLabels` | `{}` | |
| `podSecurityContext` | UID/GID/fsGroup 10001, `RuntimeDefault` seccomp | |
| `securityContext` | read-only root FS, no privilege escalation, all capabilities dropped | |
| `resources` | requests 10m CPU / 64Mi, limit 192Mi memory | |
| `livenessProbe` / `readinessProbe` | `/healthz` / `/readyz` | |
| `extraEnv` | `[]` | Extra `env` entries (e.g. `valueFrom.secretKeyRef`); they override `config`/`secrets`. |
| `nodeSelector` / `tolerations` / `affinity` | | |

## Metrics

```yaml
metrics:
  enabled: true
  serviceMonitor:
    enabled: true
    labels:
      release: prometheus
  dashboard:
    enabled: true
```

Without the sidecar, import [the dashboard](dashboards/bluetti-grid-bot.json) into Grafana by hand.
The metrics are listed in the
[main README](https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier#health-and-metrics).

## Several stations

To monitor stations on different BLUETTI accounts (e.g. home and cottage), describe them in
the `stations` map, keyed by station id (letters and digits):

```yaml
stations:
  home:
    name: Дім
    gateway: eu                       # default: config.BLUETTI_GATEWAY
    deviceSns: PR200V2XXXXXXXXXXXXXX  # string or list; empty = all stations of the account
    refreshToken: ""                  # may stay empty until `login --station home`
  cottage:
    name: Дача
    deviceSns: [AC180XXXXXXXXXXXXXXX]
    refreshToken: ""
```

| Key | Becomes | Where |
|---|---|---|
| map keys | `BLUETTI_STATIONS=cottage,home` (alphabetical; set `config.BLUETTI_STATIONS` to choose the order) | ConfigMap |
| `name` | `BLUETTI_<ID>_NAME` | ConfigMap |
| `gateway` | `BLUETTI_<ID>_GATEWAY` | ConfigMap |
| `deviceSns` | `BLUETTI_<ID>_DEVICE_SNS` | ConfigMap |
| `schedule.provider`, `.region`, `.group`, `.warnMinutes` | `BLUETTI_<ID>_SCHEDULE_PROVIDER`, `_REGION`, `_GROUP`, `_WARN_MINUTES` | ConfigMap |
| `refreshToken` | `BLUETTI_<ID>_REFRESH_TOKEN` | the chart's Secret |

- Leave `config.BLUETTI_DEVICE_SNS` and `secrets.BLUETTI_REFRESH_TOKEN` empty; the chart
  refuses to render otherwise.
- With `existingSecret`, keep `refreshToken` out of the map and put
  `BLUETTI_HOME_REFRESH_TOKEN`, `BLUETTI_COTTAGE_REFRESH_TOKEN` into your Secret instead.
- Without the map, the same variables can be set by hand in `config` / `secrets`.

Then sign in once per station, each time with the account that owns it (use a private
browser window for each, so the second login does not reuse the first account):

```bash
kubectl -n bluetti exec -it deploy/bluetti-grid-bot -- bluetti-grid-bot login --station home
kubectl -n bluetti exec -it deploy/bluetti-grid-bot -- bluetti-grid-bot login --station cottage
```

`login --station <id>` prints that station's `BLUETTI_<ID>_*` lines; the tokens are stored in
the state per station. See the
[main README](https://github.com/yevhenii-rosliakov/bluetti-telegram-notifier#several-stations-in-different-places)
for details.

## Operations

```bash
# Sign in again (e.g. after "BLUETTI login expired"); add --station <id> with several stations
kubectl -n bluetti exec -it deploy/bluetti-grid-bot -- bluetti-grid-bot login

# See what the bot sees and decides for one poll
kubectl -n bluetti exec deploy/bluetti-grid-bot -- bluetti-grid-bot check

# Logs
kubectl -n bluetti logs deploy/bluetti-grid-bot -f
```

To keep the PVC when the release is uninstalled, set
`persistence.annotations: { helm.sh/resource-policy: keep }`, or create the claim yourself and
use `persistence.existingClaim`.
