"""Configuration from environment variables (12-factor), validated at startup."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from .detector import DetectorConfig
from .models import DetectionMode
from .schedule import PROVIDERS

GATEWAYS: dict[str, str] = {
    "eu": "https://gwde.bluettipower.com",
    "global": "https://gw.bluettipower.com",
    "us": "https://gwpry.bluettipower.com",
}


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ConfigError(Exception):
    """Invalid or missing configuration; the message is meant for the user."""


def _split(value: Any) -> Any:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


def _gateway_url(value: str) -> str:
    key = value.strip().lower()
    if key in GATEWAYS:
        return GATEWAYS[key]
    if key.startswith(("https://", "http://")):
        return value.strip().rstrip("/")
    raise ValueError(f"must be one of {', '.join(GATEWAYS)} or an http(s) URL, got {value!r}")


def _gateway_setting(value: str) -> str:
    if value.strip().lower() in ("", "auto"):
        return "auto"
    return _gateway_url(value)


CsvStr = Annotated[list[str], NoDecode]
CsvInt = Annotated[list[int], NoDecode]

# BLUETTI_<ID>_<FIELD>: per-station settings when BLUETTI_STATIONS lists several stations.
SITE_FIELDS = ("name", "refresh_token", "access_token", "gateway", "device_sns")
_SITE_VAR = re.compile(r"bluetti_([a-z0-9]+)_(" + "|".join(SITE_FIELDS) + ")")


@dataclass(frozen=True, slots=True)
class Site:
    """One BLUETTI account login (token + gateway) and the stations polled with it.

    ``id`` is empty for the single-account setup (``BLUETTI_REFRESH_TOKEN`` etc.).
    """

    id: str
    name: str = ""
    refresh_token: SecretStr | None = None
    access_token: SecretStr | None = None
    gateway: str = "auto"
    device_sns: tuple[str, ...] = ()

    def var(self, field: str) -> str:
        """Environment variable name of a setting of this site."""
        return f"BLUETTI_{self.id.upper()}_{field}" if self.id else f"BLUETTI_{field}"

    @property
    def label(self) -> str:
        return self.name or self.id


class _SiteVarsSource(PydanticBaseSettingsSource):
    """Collect ``BLUETTI_<ID>_<FIELD>`` variables (their names are not known in advance)
    from the environment and the .env file into ``bluetti_site_vars``."""

    def __init__(
        self, settings_cls: type[BaseSettings], *sources: Mapping[str, str | None]
    ) -> None:
        super().__init__(settings_cls)
        self._sources = sources

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # unused: __call__ is overridden

    def __call__(self) -> dict[str, Any]:
        found: dict[str, dict[str, str]] = {}
        for env in self._sources:  # later sources (the process environment) win
            for key, value in env.items():
                if value is not None and (m := _SITE_VAR.fullmatch(key.lower())):
                    found.setdefault(m[1], {})[m[2]] = value
        return {"bluetti_site_vars": found} if found else {}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # BLUETTI
    bluetti_refresh_token: SecretStr | None = None
    bluetti_access_token: SecretStr | None = None
    bluetti_gateway: str = "auto"
    bluetti_gateway_probe_order: CsvStr = Field(default_factory=lambda: list(GATEWAYS))
    bluetti_device_sns: CsvStr = Field(default_factory=list)
    bluetti_device_names: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict)
    # Several stations, each with its own login: ids here, settings in BLUETTI_<ID>_*.
    bluetti_stations: CsvStr = Field(default_factory=list)
    bluetti_site_vars: dict[str, dict[str, str]] = Field(
        default_factory=dict, exclude=True, repr=False
    )
    bluetti_sso_url: str = "https://sso.bluettipower.com"
    bluetti_client_id: str = "HomeAssistant"
    bluetti_client_secret: SecretStr = SecretStr("SG9tZUFzc2lzdGFudA==")
    bluetti_redirect_uri: str = "https://my.home-assistant.io/redirect/oauth"

    # Telegram (required for `run` only)
    telegram_bot_token: SecretStr | None = None
    telegram_chat_ids: CsvInt = Field(default_factory=list)
    telegram_allowed_chat_ids: CsvInt | None = None

    # Detection
    poll_interval_seconds: int = Field(default=60, ge=15)
    detection_mode: DetectionMode = DetectionMode.ATS
    load_threshold_w: float = Field(default=5.0, ge=0)
    grid_threshold_w: float = Field(default=5.0, ge=0)
    soc_drop_pct: float = Field(default=2.0, gt=0, le=100)
    debounce_polls: int = Field(default=2, ge=1, le=100)
    restore_debounce_polls: int = Field(default=2, ge=1, le=100)
    offline_grace_seconds: int = Field(default=600, ge=0)
    low_soc_thresholds: CsvInt = Field(default_factory=lambda: [30, 15])
    alert_ac_off: bool = True
    notify_startup: bool = True
    full_charge_soc: int = Field(default=100, ge=0, le=100)

    # Reports
    # "<weekday> HH:MM" in TZ, e.g. "mon 09:00"; "off" disables.
    weekly_summary: str = "mon 09:00"

    # Planned outage schedule
    schedule_provider: Literal["off", "yasno", "outage-data-ua"] = "off"
    schedule_region: str = ""
    schedule_group: str = ""
    schedule_warn_minutes: int = Field(default=30, ge=0, le=720)

    # Presentation / runtime
    bot_language: Literal["uk", "en"] = "uk"
    tz: str = "UTC"
    state_dir: Path = Path("/data")
    # postgresql://user:password@host:5432/db; empty = SQLite in STATE_DIR.
    database_url: SecretStr | None = None
    # Override the URL's credentials, e.g. to keep them in a separate Secret.
    database_username: str | None = None
    database_password: SecretStr | None = None
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["text", "json"] = "text"
    metrics_enabled: bool = False
    http_port: int = Field(default=8080, ge=0, le=65535)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        site_vars = _SiteVarsSource(
            settings_cls,
            getattr(dotenv_settings, "env_vars", {}),
            getattr(env_settings, "env_vars", {}),
        )
        return init_settings, env_settings, dotenv_settings, site_vars, file_secret_settings

    @field_validator(
        "bluetti_gateway_probe_order",
        "bluetti_device_sns",
        "bluetti_stations",
        "telegram_chat_ids",
        "telegram_allowed_chat_ids",
        "low_soc_thresholds",
        mode="before",
    )
    @classmethod
    def _csv(cls, value: Any) -> Any:
        return _split(value)

    @field_validator("bluetti_device_names", mode="before")
    @classmethod
    def _pairs(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        names: dict[str, str] = {}
        for pair in _split(value):
            sn, sep, name = pair.partition("=")
            if not sep or not sn.strip() or not name.strip():
                raise ValueError(f"expected SN=Name pairs separated by commas, got {pair!r}")
            names[sn.strip()] = name.strip()
        return names

    @field_validator("bluetti_gateway")
    @classmethod
    def _gateway(cls, value: str) -> str:
        return _gateway_setting(value)

    @field_validator("bluetti_stations")
    @classmethod
    def _station_ids(cls, value: list[str]) -> list[str]:
        ids = [v.lower() for v in value]
        for sid in ids:
            if not re.fullmatch(r"[a-z0-9]+", sid):
                raise ValueError(f"station ids may contain only letters and digits, got {sid!r}")
        if len(set(ids)) != len(ids):
            raise ValueError("station ids must be unique")
        return ids

    @field_validator("bluetti_gateway_probe_order")
    @classmethod
    def _probe_order(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("must list at least one gateway")
        return [_gateway_url(v) for v in value]

    @field_validator("bluetti_sso_url", "bluetti_redirect_uri")
    @classmethod
    def _url(cls, value: str) -> str:
        # The redirect URI is compared verbatim by the SSO, so it is not normalised.
        if not value.startswith(("https://", "http://")):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator("low_soc_thresholds")
    @classmethod
    def _thresholds(cls, value: list[int]) -> list[int]:
        if any(not 0 < v < 100 for v in value):
            raise ValueError("thresholds must be between 1 and 99")
        return sorted(set(value), reverse=True)

    @field_validator("weekly_summary")
    @classmethod
    def _weekly(cls, value: str) -> str:
        text = " ".join(value.lower().split())
        if text in ("", "off", "false", "no"):
            return "off"
        m = re.fullmatch(r"([a-z]{3})\w* (\d{1,2}):(\d{2})", text)
        if m is None or m[1] not in WEEKDAYS or int(m[2]) > 23 or int(m[3]) > 59:
            raise ValueError(f"expected e.g. 'mon 09:00' or 'off', got {value!r}")
        return f"{m[1]} {int(m[2]):02d}:{m[3]}"

    @field_validator("schedule_region", "schedule_group")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("tz")
    @classmethod
    def _tz(cls, value: str) -> str:
        name = value.strip().lstrip(":") or "UTC"
        try:
            ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {value!r} (use e.g. Europe/Kyiv)") from exc
        return name

    @field_validator("database_url")
    @classmethod
    def _database_url(cls, value: SecretStr | None) -> SecretStr | None:
        url = "" if value is None else value.get_secret_value().strip()
        if not url:
            return None
        if not url.startswith(("postgresql://", "postgres://")):
            raise ValueError("must be a postgresql:// URL (leave empty for SQLite)")
        return SecretStr(url)

    @field_validator("database_username")
    @classmethod
    def _empty_str(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator(
        "bluetti_refresh_token", "bluetti_access_token", "telegram_bot_token", "database_password"
    )
    @classmethod
    def _empty_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return SecretStr(value.get_secret_value().strip())

    @model_validator(mode="after")
    def _database_credentials(self) -> Settings:
        if self.database_url is None and (self.database_username or self.database_password):
            raise ValueError("DATABASE_USERNAME / DATABASE_PASSWORD need DATABASE_URL")
        return self

    @model_validator(mode="after")
    def _sites_valid(self) -> Settings:
        unknown = sorted(set(self.bluetti_site_vars) - set(self.bluetti_stations))
        if unknown:
            raise ValueError(
                f"BLUETTI_{unknown[0].upper()}_* is set but {unknown[0]!r} is not listed "
                "in BLUETTI_STATIONS"
            )
        if not self.bluetti_stations:
            return self
        single = [
            name
            for name, value in (
                ("BLUETTI_REFRESH_TOKEN", self.bluetti_refresh_token),
                ("BLUETTI_ACCESS_TOKEN", self.bluetti_access_token),
                ("BLUETTI_DEVICE_SNS", self.bluetti_device_sns),
            )
            if value
        ]
        if single:
            raise ValueError(
                f"{', '.join(single)} cannot be used with BLUETTI_STATIONS: set "
                "BLUETTI_<ID>_REFRESH_TOKEN / BLUETTI_<ID>_DEVICE_SNS for each station instead"
            )
        self.sites  # noqa: B018 - validates the per-station values
        return self

    @model_validator(mode="after")
    def _allowed_default(self) -> Settings:
        if self.telegram_allowed_chat_ids is None:
            self.telegram_allowed_chat_ids = list(self.telegram_chat_ids)
        return self

    @model_validator(mode="after")
    def _schedule(self) -> Settings:
        if self.schedule_provider == "off":
            return self
        regions = PROVIDERS[self.schedule_provider]
        if self.schedule_region not in regions:
            raise ValueError(
                f"SCHEDULE_REGION must be one of {', '.join(regions)} "
                f"for SCHEDULE_PROVIDER={self.schedule_provider}"
            )
        if not re.fullmatch(r"\d{1,2}(\.\d)?", self.schedule_group):
            raise ValueError("SCHEDULE_GROUP must be your queue, e.g. 3.1")
        return self

    # Derived values

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def allowed_chat_ids(self) -> frozenset[int]:
        return frozenset(self.telegram_allowed_chat_ids or ())

    @property
    def weekly_summary_at(self) -> tuple[int, time] | None:
        """(weekday, local time), Monday = 0; None when disabled."""
        if self.weekly_summary == "off":
            return None
        day, hhmm = self.weekly_summary.split()
        hour, minute = hhmm.split(":")
        return WEEKDAYS.index(day), time(int(hour), int(minute))

    @property
    def sites(self) -> list[Site]:
        """The logins to poll: one per BLUETTI_STATIONS entry, else the single account."""
        if not self.bluetti_stations:
            return [
                Site(
                    "",
                    refresh_token=self.bluetti_refresh_token,
                    access_token=self.bluetti_access_token,
                    gateway=self.bluetti_gateway,
                    device_sns=tuple(self.bluetti_device_sns),
                )
            ]
        return [self.site(sid) for sid in self.bluetti_stations]

    def site(self, sid: str) -> Site:
        """Settings of station ``sid`` (also one not listed yet, for ``login``)."""
        sid = sid.lower()
        if not re.fullmatch(r"[a-z0-9]+", sid):
            raise ConfigError(f"Station ids may contain only letters and digits, got {sid!r}")
        values = self.bluetti_site_vars.get(sid, {})
        site = Site(sid)
        try:
            gateway = _gateway_setting(values.get("gateway") or self.bluetti_gateway)
        except ValueError as exc:
            raise ValueError(f"{site.var('GATEWAY')}: {exc}") from None
        return Site(
            sid,
            name=values.get("name", "").strip(),
            refresh_token=_secret(values.get("refresh_token")),
            access_token=_secret(values.get("access_token")),
            gateway=gateway,
            device_sns=tuple(_split(values.get("device_sns", ""))),
        )

    @property
    def db_path(self) -> Path:
        return self.state_dir / "bluetti-grid-bot.sqlite3"

    def detector_config(self) -> DetectorConfig:
        return DetectorConfig(
            mode=self.detection_mode,
            load_threshold_w=self.load_threshold_w,
            grid_threshold_w=self.grid_threshold_w,
            soc_drop_pct=self.soc_drop_pct,
            debounce_polls=self.debounce_polls,
            restore_debounce_polls=self.restore_debounce_polls,
            offline_grace=timedelta(seconds=self.offline_grace_seconds),
            low_soc_thresholds=tuple(self.low_soc_thresholds),
            alert_ac_off=self.alert_ac_off,
            full_charge_soc=self.full_charge_soc,
        )

    def require_telegram(self) -> None:
        missing = []
        if self.telegram_bot_token is None:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_ids:
            missing.append("TELEGRAM_CHAT_IDS")
        if missing:
            raise ConfigError(f"Missing required configuration: {', '.join(missing)}")

    def masked(self) -> dict[str, Any]:
        """Settings for debug logs; secrets are shown as ``**********``."""
        values = {
            k: ("**********" if isinstance(v, SecretStr) else v)
            for k, v in self.model_dump(mode="python").items()
        }
        if self.bluetti_stations:
            values["sites"] = [
                {"id": st.id, "name": st.name, "gateway": st.gateway, "sns": st.device_sns}
                for st in self.sites
            ]
        return values


def _secret(value: str | None) -> SecretStr | None:
    value = (value or "").strip()
    return SecretStr(value) if value else None


def load_settings(**overrides: Any) -> Settings:
    """Build settings from the environment; turn validation errors into ``ConfigError``."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        lines = []
        for err in exc.errors():
            name = "_".join(str(p) for p in err["loc"]).upper() or "configuration"
            lines.append(f"  {name}: {err['msg']}")
        raise ConfigError("Invalid configuration:\n" + "\n".join(lines)) from None
