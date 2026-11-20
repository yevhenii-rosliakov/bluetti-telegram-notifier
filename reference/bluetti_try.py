#!/usr/bin/env python3
"""
Quick test of the BLUETTI cloud API (the same one the Home Assistant integration uses).

1. Prints a sign-in link -> open it in a browser, sign in with your BLUETTI account.
2. The browser lands on my.home-assistant.io/redirect/oauth?code=...  (the page itself
   may show an error, that's fine) -> copy the WHOLE address from the address bar
   and paste it here.
3. The script exchanges the code for tokens, saves them to bluetti_token.json,
   finds the gateway (region), lists the stations and prints their current state.

Next runs reuse bluetti_token.json (refresh token), no browser needed.
Only the Python standard library is used.
"""

import json
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SSO = "https://sso.bluettipower.com"
CLIENT_ID = "HomeAssistant"
CLIENT_SECRET = "SG9tZUFzc2lzdGFudA=="  # built-in credential of the HA integration
REDIRECT_URI = "https://my.home-assistant.io/redirect/oauth"
GATEWAYS = {
    "eu": "https://gwde.bluettipower.com",
    "global": "https://gw.bluettipower.com",
    "us": "https://gwpry.bluettipower.com",
}
TOKEN_FILE = Path("bluetti_token.json")

# fnCodes worth watching for the "is there grid power?" question
INTERESTING = {
    "GridAllTotalPower": "Вхід з мережі, Вт",
    "ACLoadAllTotalPower": "Навантаження AC, Вт",
    "DCLoadAllTotalPower": "Навантаження DC, Вт",
    "PVAllTotalPower": "Сонце, Вт",
    "SetCtrlAc": "Розетка AC увімкнена",
}


def http(method, url, *, form=None, body=None, params=None, token=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = None
    if form:
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if form:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", token)  # no "Bearer " prefix, same as pybluetti
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {url}:\n{e.read().decode()[:1000]}")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        sys.exit(f"Non-JSON answer from {url}:\n{body[:1000]}")


def save(tok):
    tok["obtained_at"] = int(time.time())
    TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    TOKEN_FILE.chmod(0o600)


def login():
    state = secrets.token_urlsafe(16)
    url = (
        SSO
        + "/oauth2/grant?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "state": state,
            }
        )
    )
    print("\n1) Відкрийте в браузері й увійдіть в акаунт BLUETTI:\n\n" + url + "\n")
    pasted = input("2) Вставте сюди повну адресу, на яку вас перекинуло: ").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(pasted).query)
    if "code" not in qs:
        sys.exit("У вставленій адресі немає параметра code=")
    if qs.get("state", [state])[0] != state:
        print("! state не збігається - продовжую, але перевірте, що це ваше посилання")
    tok = http(
        "POST",
        SSO + "/oauth2/token",
        form={
            "grant_type": "authorization_code",
            "code": qs["code"][0],
            "redirect_uri": REDIRECT_URI,
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    if "access_token" not in tok:
        sys.exit(f"Токен не видали: {tok}")
    save(tok)
    return tok


def refresh(tok):
    new = http(
        "POST",
        SSO + "/oauth2/token",
        form={
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    )
    if "access_token" not in new:
        print(f"Refresh не вдався ({new}), потрібен новий вхід")
        return login()
    new.setdefault("refresh_token", tok["refresh_token"])
    save(new)
    return new


def get_token():
    if not TOKEN_FILE.exists():
        return login()
    tok = json.loads(TOKEN_FILE.read_text())
    if time.time() > tok.get("obtained_at", 0) + int(tok.get("expires_in", 0)) - 60:
        tok = refresh(tok)
    return tok


def find_gateway(access):
    """The account lives in one data center; other gateways reject its token."""
    for region, gw in GATEWAYS.items():
        resp = http("GET", gw + "/api/bluiotdata/ha/v2/devices", token=access)
        print(
            f"   {region:6} {gw}: msgCode={resp.get('msgCode')} "
            f"devices={len(resp.get('data') or [])}"
        )
        if resp.get("msgCode") == 0:
            return gw, resp.get("data") or []
    return None, []


def main():
    tok = get_token()
    access = tok["access_token"]

    print("\n3) Шукаю gateway та станції:")
    gw, devices = find_gateway(access)
    if not gw:
        sys.exit("Жоден gateway не прийняв токен. Подивіться msgCode вище.")
    if not devices:
        sys.exit(
            "Акаунт є, але станцій у списку немає. Спробуйте акаунт, "
            "до якого станцію прив'язано (не поширений доступ)."
        )

    for d in devices:
        print(
            f"\n   SN={d.get('sn')} model={d.get('model')} name={d.get('name')} "
            f"online={d.get('online')} own={d.get('isBindByCurUser')} "
            f"cloud={d.get('supportNetwork')}"
        )

    # The HA integration "binds" the chosen stations to the HA client before polling;
    # without it deviceStates answers online=0 with an empty stateList.
    sn_list = [d["sn"] for d in devices]
    bind = http(
        "POST", gw + "/api/bluiotdata/ha/v1/bindDevices", body={"bindSnList": sn_list}, token=access
    )
    print(f"\n   bindDevices: msgCode={bind.get('msgCode')} {bind.get('message', '')}")
    time.sleep(3)

    sns = ",".join(sn_list)
    states = http(
        "GET", gw + "/api/bluiotdata/ha/v1/deviceStates", params={"sns": sns}, token=access
    )
    print("\n4) Поточний стан:")
    for d in states.get("data") or []:
        values = {s["fnCode"]: s.get("fnValue") for s in d.get("stateList", [])}
        print(f"\n   {d.get('sn')}  online={d.get('online')}")
        for code, label in INTERESTING.items():
            if code in values:
                print(f"     {label:24} {values[code]}")
        grid = values.get("GridAllTotalPower")
        if grid not in (None, ""):
            verdict = (
                "СВІТЛО Є" if float(grid) > 0 else "СВІТЛА НЕМАЄ (або станція не бере з мережі)"
            )
            print(f"     => {verdict}")
        print("     усі поля:", json.dumps(values, ensure_ascii=False))

    print(f"\nGateway: {gw}. Токен збережено в {TOKEN_FILE} (тримайте його в секреті).")


if __name__ == "__main__":
    main()
