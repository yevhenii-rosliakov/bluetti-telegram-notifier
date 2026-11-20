from __future__ import annotations

import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from bluetti_grid_bot.auth import (
    AuthError,
    AuthExpiredError,
    OAuthClient,
    TokenManager,
    TransientAuthError,
    extract_code,
)
from bluetti_grid_bot.logs import redact
from bluetti_grid_bot.storage import Storage, TokenSet

OAUTH = OAuthClient(
    sso_url="https://sso.example.test",
    client_id="HomeAssistant",
    client_secret="SG9tZUFzc2lzdGFudA==",
    redirect_uri="https://my.home-assistant.io/redirect/oauth",
)
TOKEN_URL = "https://sso.example.test/oauth2/token"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    async with aiohttp.ClientSession() as s:
        yield s


def manager(
    session: aiohttp.ClientSession, storage: Storage, clock: Clock | None = None, **kw: Any
) -> TokenManager:
    return TokenManager(session, OAUTH, storage, clock=clock or Clock(), sleep=no_sleep, **kw)


def sent_form(m: aioresponses, index: int = 0) -> dict[str, str]:
    form: dict[str, str] = m.requests[("POST", URL(TOKEN_URL))][index].kwargs["data"]
    return form


# authorize URL / pasted redirect


def test_authorize_url() -> None:
    url = urllib.parse.urlsplit(OAUTH.authorize_url("st4te"))
    assert url.netloc == "sso.example.test"
    assert url.path == "/oauth2/grant"
    q = urllib.parse.parse_qs(url.query)
    assert q == {
        "response_type": ["code"],
        "client_id": ["HomeAssistant"],
        "redirect_uri": ["https://my.home-assistant.io/redirect/oauth"],
        "state": ["st4te"],
    }


def test_extract_double_encoded_code_is_decoded_once() -> None:
    pasted = "https://my.home-assistant.io/redirect/oauth/?code=ab%252Bcd%252Fef%253D&state=xyz"
    code, ok = extract_code(pasted, "xyz")
    assert code == "ab%2Bcd%2Fef%3D"
    assert ok


def test_extract_state_mismatch_is_only_a_warning() -> None:
    code, ok = extract_code("https://x/redirect/oauth?code=abc&state=old", "new")
    assert code == "abc"
    assert not ok


def test_extract_accepts_query_only_and_quotes() -> None:
    assert extract_code("'?code=abc&state=s'", "s") == ("abc", True)


def test_extract_without_code_fails() -> None:
    with pytest.raises(AuthError, match="code="):
        extract_code("https://my.home-assistant.io/redirect/oauth/?state=xyz", "xyz")


# code exchange


async def test_exchange_code_sends_code_as_given_and_stores_tokens(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    tm = manager(session, storage, clock)
    with aioresponses() as m:
        m.post(
            TOKEN_URL, payload={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 7200}
        )
        tokens = await tm.exchange_code("ab%2Bcd")
        form = sent_form(m)
    assert form == {
        "grant_type": "authorization_code",
        "code": "ab%2Bcd",
        "redirect_uri": "https://my.home-assistant.io/redirect/oauth",
        "client_id": "HomeAssistant",
        "client_secret": "SG9tZUFzc2lzdGFudA==",
    }
    assert tokens.refresh_token == "RT1"
    stored = storage.get_tokens()
    assert stored is not None
    assert stored.access_token == "AT1"
    assert stored.expires_at == clock.now + 7200


async def test_exchange_code_rejected(session: aiohttp.ClientSession, storage: Storage) -> None:
    with aioresponses() as m:
        m.post(TOKEN_URL, status=400, payload={"error": "invalid_grant"})
        with pytest.raises(AuthError, match="invalid_grant"):
            await manager(session, storage).exchange_code("used-code")


# refresh


async def test_refresh_with_rotation_persists_newest_token(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    storage.save_tokens(TokenSet("OLD-ACCESS", "REFRESH-1", clock.now - 1, clock.now - 7200))
    tm = manager(session, storage, clock)
    with aioresponses() as m:
        m.post(
            TOKEN_URL,
            payload={
                "access_token": "NEW-ACCESS",
                "refresh_token": "REFRESH-2",
                "expires_in": 60 * 60,
            },
        )
        assert await tm.access_token() == "NEW-ACCESS"
        form = sent_form(m)
    assert form["grant_type"] == "refresh_token"
    assert form["refresh_token"] == "REFRESH-1"
    assert form["client_secret"] == "SG9tZUFzc2lzdGFudA=="
    stored = storage.get_tokens()
    assert stored is not None
    assert stored.refresh_token == "REFRESH-2"


async def test_refresh_without_rotation_keeps_old_refresh_token(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    storage.save_tokens(TokenSet(None, "REFRESH-1", 0.0, 0.0))
    tm = manager(session, storage)
    with aioresponses() as m:
        m.post(TOKEN_URL, payload={"access_token": "NEW-ACCESS", "expires_in": "3600"})
        await tm.refresh()
    stored = storage.get_tokens()
    assert stored is not None
    assert stored.refresh_token == "REFRESH-1"
    assert stored.access_token == "NEW-ACCESS"


async def test_valid_access_token_is_reused(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    storage.save_tokens(TokenSet("AT", "RT", clock.now + 3600, clock.now))
    with aioresponses():
        assert await manager(session, storage, clock).access_token() == "AT"


async def test_token_close_to_expiry_is_refreshed(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    storage.save_tokens(TokenSet("AT", "RT", clock.now + 30, clock.now))
    with aioresponses() as m:
        m.post(TOKEN_URL, payload={"access_token": "AT2", "expires_in": 3600})
        assert await manager(session, storage, clock).access_token() == "AT2"


async def test_refresh_skipped_if_token_already_replaced(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    clock = Clock()
    storage.save_tokens(TokenSet("FRESH", "RT", None, clock.now))
    with aioresponses():
        assert await manager(session, storage, clock).refresh(stale="STALE") == "FRESH"


async def test_invalid_grant_is_permanent(session: aiohttp.ClientSession, storage: Storage) -> None:
    storage.save_tokens(TokenSet(None, "DEAD", 0.0, 0.0))
    with aioresponses() as m:
        m.post(TOKEN_URL, status=400, payload={"error": "invalid_grant", "error_description": "x"})
        with pytest.raises(AuthExpiredError):
            await manager(session, storage).access_token()


async def test_200_without_access_token_is_permanent(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    storage.save_tokens(TokenSet(None, "DEAD", 0.0, 0.0))
    with aioresponses() as m:
        m.post(TOKEN_URL, payload={"msgCode": 401, "message": "refresh token invalid"})
        with pytest.raises(AuthExpiredError, match="refresh token invalid"):
            await manager(session, storage).refresh()


async def test_server_errors_are_retried_then_transient(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    storage.save_tokens(TokenSet(None, "RT", 0.0, 0.0))
    with aioresponses() as m:
        m.post(TOKEN_URL, status=502, repeat=True)
        with pytest.raises(TransientAuthError):
            await manager(session, storage).refresh()
        assert len(m.requests[("POST", URL(TOKEN_URL))]) == 3


async def test_transient_then_success(session: aiohttp.ClientSession, storage: Storage) -> None:
    storage.save_tokens(TokenSet(None, "RT", 0.0, 0.0))
    with aioresponses() as m:
        m.post(TOKEN_URL, exception=aiohttp.ClientConnectionError("boom"))
        m.post(TOKEN_URL, body="<html>oops</html>", status=503)
        m.post(TOKEN_URL, payload={"access_token": "AT"})
        assert await manager(session, storage).refresh() == "AT"
    stored = storage.get_tokens()
    assert stored is not None
    assert stored.expires_at is None  # no expires_in: valid until the API says otherwise


async def test_no_tokens_at_all(session: aiohttp.ClientSession, storage: Storage) -> None:
    with pytest.raises(AuthExpiredError, match="login"):
        await manager(session, storage).access_token()


# seeding from the environment


async def test_env_refresh_token_seeds_empty_state(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    tm = manager(session, storage, seed_refresh_token="ENV-REFRESH")
    with aioresponses() as m:
        m.post(TOKEN_URL, payload={"access_token": "AT", "refresh_token": "ROTATED"})
        assert await tm.access_token() == "AT"
        assert sent_form(m)["refresh_token"] == "ENV-REFRESH"
    # After rotation the DB token wins over the (unchanged) env seed.
    tm2 = manager(session, storage, seed_refresh_token="ENV-REFRESH")
    current = tm2.current()
    assert current is not None
    assert current.refresh_token == "ROTATED"


async def test_changed_env_token_replaces_stored_one(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    manager(session, storage, seed_refresh_token="SEED-1").current()
    storage.save_tokens(TokenSet("AT", "ROTATED", None, 0.0))
    current = manager(session, storage, seed_refresh_token="SEED-2").current()
    assert current is not None
    assert current.refresh_token == "SEED-2"


async def test_login_overrides_older_env_seed(
    session: aiohttp.ClientSession, storage: Storage
) -> None:
    tm = manager(session, storage, seed_refresh_token="OLD-SEED")
    with aioresponses() as m:
        m.post(TOKEN_URL, payload={"access_token": "AT", "refresh_token": "FROM-LOGIN"})
        await tm.exchange_code("code")
    current = manager(session, storage, seed_refresh_token="OLD-SEED").current()
    assert current is not None
    assert current.refresh_token == "FROM-LOGIN"


async def test_env_access_token_only(session: aiohttp.ClientSession, storage: Storage) -> None:
    tm = manager(session, storage, seed_access_token="DEBUG-ACCESS")
    assert await tm.access_token() == "DEBUG-ACCESS"
    with pytest.raises(AuthExpiredError):
        await tm.refresh(stale="DEBUG-ACCESS")


def test_tokens_are_redacted_from_logs(storage: Storage) -> None:
    from bluetti_grid_bot.logs import register_secret

    register_secret("SUPERSECRET-REFRESH")
    assert redact("refresh=SUPERSECRET-REFRESH ok") == "refresh=********** ok"
