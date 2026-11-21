from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from bluetti_grid_bot.config import GATEWAYS, ConfigError, Settings, Site, load_settings
from bluetti_grid_bot.models import DetectionMode

from .conftest import make_settings


def test_defaults(tmp_path: Path) -> None:
    s = Settings(_env_file=None)
    assert s.bluetti_gateway == "auto"
    assert s.bluetti_gateway_probe_order == [GATEWAYS["eu"], GATEWAYS["global"], GATEWAYS["us"]]
    assert s.bluetti_client_id == "HomeAssistant"
    assert s.bluetti_client_secret.get_secret_value() == "SG9tZUFzc2lzdGFudA=="
    assert s.bluetti_redirect_uri == "https://my.home-assistant.io/redirect/oauth"
    assert s.poll_interval_seconds == 60
    assert s.detection_mode is DetectionMode.ATS
    assert s.low_soc_thresholds == [30, 15]
    assert s.bot_language == "uk"
    assert s.state_dir == Path("/data")
    assert s.tz == "UTC"
    cfg = s.detector_config()
    assert cfg.offline_grace == timedelta(minutes=10)
    assert cfg.debounce_polls == 2


def test_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env = {
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_IDS": "111, -1002223334445",
        "BLUETTI_DEVICE_SNS": "SN1,SN2",
        "BLUETTI_DEVICE_NAMES": "SN1=Mom's flat, SN2=Garage",
        "BLUETTI_GATEWAY": "EU",
        "LOW_SOC_THRESHOLDS": "15,30,30",
        "DETECTION_MODE": "passthrough",
        "TZ": "Europe/Kyiv",
        "STATE_DIR": str(tmp_path),
        "ALERT_AC_OFF": "false",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = load_settings(_env_file=None)
    assert s.telegram_chat_ids == [111, -1002223334445]
    assert s.allowed_chat_ids == frozenset({111, -1002223334445})
    assert s.bluetti_device_sns == ["SN1", "SN2"]
    assert s.bluetti_device_names == {"SN1": "Mom's flat", "SN2": "Garage"}
    assert s.bluetti_gateway == GATEWAYS["eu"]
    assert s.low_soc_thresholds == [30, 15]
    assert s.detection_mode is DetectionMode.PASSTHROUGH
    assert s.zone.key == "Europe/Kyiv"
    assert s.db_path.parent == tmp_path
    assert s.alert_ac_off is False
    s.require_telegram()


def test_allowed_chat_ids_override(tmp_path: Path) -> None:
    s = make_settings(tmp_path, telegram_allowed_chat_ids="111,333")
    assert s.allowed_chat_ids == frozenset({111, 333})


def test_empty_lists(tmp_path: Path) -> None:
    s = make_settings(tmp_path, low_soc_thresholds="", bluetti_device_sns="")
    assert s.low_soc_thresholds == []
    assert s.bluetti_device_sns == []


def test_gateway_url_and_custom_probe_order(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path, bluetti_gateway="https://gw.example.test/", bluetti_gateway_probe_order="us,eu"
    )
    assert s.bluetti_gateway == "https://gw.example.test"
    assert s.bluetti_gateway_probe_order == [GATEWAYS["us"], GATEWAYS["eu"]]


def test_tz_leading_colon(tmp_path: Path) -> None:
    assert make_settings(tmp_path, tz=":UTC").tz == "UTC"


def test_blank_secret_is_none(tmp_path: Path) -> None:
    assert make_settings(tmp_path, bluetti_refresh_token="  ").bluetti_refresh_token is None


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"POLL_INTERVAL_SECONDS": "5"}, "POLL_INTERVAL_SECONDS"),
        ({"DETECTION_MODE": "magic"}, "DETECTION_MODE"),
        ({"BLUETTI_GATEWAY": "mars"}, "BLUETTI_GATEWAY"),
        ({"BLUETTI_GATEWAY_PROBE_ORDER": ","}, "BLUETTI_GATEWAY_PROBE_ORDER"),
        ({"TELEGRAM_CHAT_IDS": "abc"}, "TELEGRAM_CHAT_IDS"),
        ({"LOW_SOC_THRESHOLDS": "30,150"}, "LOW_SOC_THRESHOLDS"),
        ({"TZ": "Mars/Olympus"}, "unknown time zone"),
        ({"BOT_LANGUAGE": "de"}, "BOT_LANGUAGE"),
        ({"BLUETTI_DEVICE_NAMES": "SN1"}, "SN=Name"),
        ({"DEBOUNCE_POLLS": "0"}, "DEBOUNCE_POLLS"),
        ({"BLUETTI_SSO_URL": "sso.bluettipower.com"}, "BLUETTI_SSO_URL"),
    ],
)
def test_validation_errors_name_the_variable(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match=message):
        load_settings(_env_file=None)


def test_require_telegram(tmp_path: Path) -> None:
    s = Settings(_env_file=None, state_dir=tmp_path)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS"):
        s.require_telegram()


def test_masked_hides_secrets(tmp_path: Path) -> None:
    s = make_settings(tmp_path, bluetti_refresh_token="REALREFRESHTOKEN")
    dumped = repr(s.masked())
    assert "REALREFRESHTOKEN" not in dumped
    assert "TEST-TOKEN" not in dumped
    assert "SG9tZUFzc2lzdGFudA" not in dumped
    assert "REALREFRESHTOKEN" not in repr(s)


def test_database_url(tmp_path: Path) -> None:
    assert make_settings(tmp_path).database_url is None
    assert make_settings(tmp_path, database_url="  ").database_url is None
    s = make_settings(tmp_path, database_url="postgresql://u:hunter2@db/bot")
    assert s.database_url is not None
    assert s.database_url.get_secret_value() == "postgresql://u:hunter2@db/bot"
    assert "hunter2" not in str(s.masked())
    with pytest.raises(ValueError, match=r"postgresql://"):
        make_settings(tmp_path, database_url="mysql://db/bot")


def test_database_credentials(tmp_path: Path) -> None:
    s = make_settings(
        tmp_path,
        database_url="postgresql://db/bot",
        database_username=" bot ",
        database_password="hunter2",
    )
    assert s.database_username == "bot"
    assert s.database_password is not None
    assert s.database_password.get_secret_value() == "hunter2"
    assert "hunter2" not in str(s.masked())
    assert make_settings(tmp_path, database_password="").database_password is None
    with pytest.raises(ValueError, match="need DATABASE_URL"):
        make_settings(tmp_path, database_password="hunter2")


# several stations, each with its own login


def test_single_account_is_one_site(tmp_path: Path) -> None:
    s = make_settings(tmp_path, bluetti_refresh_token="R", bluetti_device_sns="SN1")
    (site,) = s.sites
    assert site.id == ""
    assert site.refresh_token is not None
    assert site.refresh_token.get_secret_value() == "R"
    assert site.device_sns == ("SN1",)
    assert site.var("REFRESH_TOKEN") == "BLUETTI_REFRESH_TOKEN"


def test_stations_from_environment_and_env_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BLUETTI_STATIONS=Home,cottage\n"
        "BLUETTI_HOME_NAME=Дім\n"
        "BLUETTI_HOME_REFRESH_TOKEN=from-file\n"
        "BLUETTI_HOME_DEVICE_SNS=SN1, SN2\n"
        "BLUETTI_COTTAGE_REFRESH_TOKEN=cottage-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BLUETTI_GATEWAY", "eu")
    monkeypatch.setenv("BLUETTI_COTTAGE_GATEWAY", "us")
    monkeypatch.setenv("BLUETTI_COTTAGE_REFRESH_TOKEN", "cottage-env")
    s = load_settings(_env_file=env_file)
    home, cottage = s.sites
    assert (home.id, home.name, home.gateway, home.device_sns) == (
        "home",
        "Дім",
        GATEWAYS["eu"],  # BLUETTI_GATEWAY is the default for every station
        ("SN1", "SN2"),
    )
    assert home.refresh_token is not None
    assert home.refresh_token.get_secret_value() == "from-file"
    assert (cottage.name, cottage.label, cottage.gateway) == ("", "cottage", GATEWAYS["us"])
    assert cottage.refresh_token is not None
    assert cottage.refresh_token.get_secret_value() == "cottage-env"  # environment wins
    assert cottage.var("DEVICE_SNS") == "BLUETTI_COTTAGE_DEVICE_SNS"
    assert "from-file" not in repr(s)
    assert "from-file" not in repr(s.masked())
    assert s.site("new") == Site("new", gateway=GATEWAYS["eu"])


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"BLUETTI_STATIONS": "home,home"}, "unique"),
        ({"BLUETTI_STATIONS": "my_home"}, "letters and digits"),
        ({"BLUETTI_STATIONS": "home", "BLUETTI_HOME_GATEWAY": "mars"}, "BLUETTI_HOME_GATEWAY"),
        ({"BLUETTI_STATIONS": "home", "BLUETTI_COTTAGE_NAME": "x"}, "BLUETTI_COTTAGE_"),
        ({"BLUETTI_STATIONS": "home", "BLUETTI_REFRESH_TOKEN": "x"}, "BLUETTI_REFRESH_TOKEN"),
        ({"BLUETTI_STATIONS": "home", "BLUETTI_DEVICE_SNS": "SN1"}, "BLUETTI_DEVICE_SNS"),
    ],
)
def test_station_validation_errors(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(ConfigError, match=message):
        load_settings(_env_file=None)


def test_station_vars_need_stations(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BLUETTI_HOME_REFRESH_TOKEN", "x")
    with pytest.raises(ConfigError, match="'home' is not listed in BLUETTI_STATIONS"):
        load_settings(_env_file=None)
