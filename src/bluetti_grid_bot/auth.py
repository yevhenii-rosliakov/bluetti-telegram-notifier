"""BLUETTI OAuth2: authorization-code login, refresh, token persistence.

Token precedence: the token stored in ``STATE_DIR`` (it rotates on refresh, so it is the
newest) wins over ``BLUETTI_REFRESH_TOKEN``. The env token is only a seed; it is adopted
again only when its value changes (e.g. the user ran ``login`` elsewhere and pasted the
new token into ``.env``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import aiohttp

from .logs import register_secret
from .storage import Storage, TokenSet

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)
# Refresh this long before the access token expires.
EXPIRY_MARGIN_S = 60
_SEED_KEY = "seed_refresh_token_sha256"


class AuthError(Exception):
    """Login or token request failed."""


class AuthExpiredError(AuthError):
    """The refresh token is no longer accepted; the user has to run ``login`` again."""


class TransientAuthError(AuthError):
    """Network / server problem; retry later."""


@dataclass(frozen=True, slots=True)
class OAuthClient:
    sso_url: str
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def token_url(self) -> str:
        return self.sso_url.rstrip("/") + "/oauth2/token"

    def authorize_url(self, state: str) -> str:
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
            }
        )
        return f"{self.sso_url.rstrip('/')}/oauth2/grant?{query}"


def extract_code(pasted: str, expected_state: str | None) -> tuple[str, bool]:
    """Get the authorization code out of the pasted redirect URL.

    Returns ``(code, state_matches)``. The query string is decoded exactly once: the SSO
    double-encodes the code (``%252B``), and the token endpoint expects the once-decoded
    value (still containing ``%2B``).
    """
    text = pasted.strip().strip("'\"")
    query = urllib.parse.urlsplit(text).query if "://" in text else text.lstrip("?")
    params = urllib.parse.parse_qs(query)
    codes = params.get("code")
    if not codes or not codes[0]:
        raise AuthError(
            "The pasted text has no 'code=' parameter. Copy the full address of the "
            "my.home-assistant.io page you were redirected to."
        )
    state = params.get("state", [None])[0]
    return codes[0], expected_state is None or state == expected_state


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _tokens_from_response(data: dict[str, Any], now: float, previous: str | None) -> TokenSet:
    expires_in = data.get("expires_in")
    try:
        expires_at = now + float(expires_in) if expires_in not in (None, "") else None
    except (TypeError, ValueError):
        expires_at = None
    tokens = TokenSet(
        access_token=str(data["access_token"]),
        # The refresh token may rotate; keep the old one if the answer has none.
        refresh_token=str(data.get("refresh_token") or previous or "") or None,
        expires_at=expires_at,
        obtained_at=now,
    )
    register_secret(tokens.access_token)
    register_secret(tokens.refresh_token)
    return tokens


class TokenManager:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        oauth: OAuthClient,
        storage: Storage,
        *,
        seed_refresh_token: str | None = None,
        seed_access_token: str | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retries: int = 3,
    ) -> None:
        self._session = session
        self._oauth = oauth
        self._storage = storage
        self._seed_refresh = seed_refresh_token
        self._seed_access = seed_access_token
        self._clock = clock
        self._sleep = sleep
        self._retries = retries
        self._lock = asyncio.Lock()
        register_secret(seed_refresh_token)
        register_secret(seed_access_token)
        register_secret(oauth.client_secret)

    def current(self) -> TokenSet | None:
        """Tokens to use now, applying the seed rules. Re-read every time so that a
        ``login`` run from another process is picked up without a restart."""
        stored = self._storage.get_tokens()
        if self._seed_refresh:
            seed_sha = _sha(self._seed_refresh)
            if self._storage.get(_SEED_KEY) != seed_sha:
                if stored is None or stored.refresh_token != self._seed_refresh:
                    log.info("Using BLUETTI_REFRESH_TOKEN from the environment (new value)")
                    stored = TokenSet(
                        access_token=self._seed_access,
                        refresh_token=self._seed_refresh,
                        expires_at=None if self._seed_access else 0.0,
                        obtained_at=self._clock(),
                    )
                    self._storage.save_tokens(stored)
                self._storage.set(_SEED_KEY, seed_sha)
        if stored is None and self._seed_access:
            stored = TokenSet(self._seed_access, None, None, self._clock())
        if stored is not None:
            register_secret(stored.access_token)
            register_secret(stored.refresh_token)
        return stored

    def _valid(self, tokens: TokenSet) -> bool:
        if not tokens.access_token:
            return False
        return tokens.expires_at is None or self._clock() < tokens.expires_at - EXPIRY_MARGIN_S

    async def access_token(self) -> str:
        tokens = self.current()
        if tokens is None:
            raise AuthExpiredError("No BLUETTI token: run `bluetti-grid-bot login` first")
        if self._valid(tokens):
            assert tokens.access_token is not None  # noqa: S101 - narrowed by _valid
            return tokens.access_token
        return await self.refresh(stale=tokens.access_token)

    async def refresh(self, stale: str | None = None) -> str:
        """Get a new access token. ``stale`` is the token that just failed: if another
        caller (or process) has already replaced it, the new one is returned as-is."""
        async with self._lock:
            tokens = self.current()
            if tokens is None or not tokens.refresh_token:
                raise AuthExpiredError("No BLUETTI refresh token: run `bluetti-grid-bot login`")
            if tokens.access_token and tokens.access_token != stale and self._valid(tokens):
                return tokens.access_token
            log.info("Refreshing BLUETTI access token")
            data = await self._token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": tokens.refresh_token,
                    "client_id": self._oauth.client_id,
                    "client_secret": self._oauth.client_secret,
                },
                permanent_error=AuthExpiredError,
            )
            new = _tokens_from_response(data, self._clock(), tokens.refresh_token)
            self._storage.save_tokens(new)
            if new.refresh_token != tokens.refresh_token:
                log.info("BLUETTI refresh token rotated; stored the new one")
            assert new.access_token is not None  # noqa: S101 - always set from the answer
            return new.access_token

    async def exchange_code(self, code: str) -> TokenSet:
        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._oauth.redirect_uri,
                "client_id": self._oauth.client_id,
                "client_secret": self._oauth.client_secret,
            },
            permanent_error=AuthError,
        )
        tokens = _tokens_from_response(data, self._clock(), None)
        self._storage.save_tokens(tokens)
        if self._seed_refresh:
            # The env seed is older than this login; don't let it override the new token.
            self._storage.set(_SEED_KEY, _sha(self._seed_refresh))
        return tokens

    async def _token_request(
        self, form: dict[str, str], *, permanent_error: type[AuthError]
    ) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._retries):
            if attempt:
                await self._sleep(2.0 * 2 ** (attempt - 1))
            try:
                async with self._session.post(
                    self._oauth.token_url, data=form, timeout=TIMEOUT
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = TransientAuthError(f"token request failed: {exc!r}")
                continue
            if status >= 500 or status == 429:
                last = TransientAuthError(f"token endpoint answered HTTP {status}")
                continue
            try:
                data = json.loads(text)
            except ValueError:
                data = None
            if status < 400 and isinstance(data, dict) and data.get("access_token"):
                return data
            detail = _error_detail(data, text)
            raise permanent_error(f"token request rejected (HTTP {status}): {detail}")
        assert last is not None  # noqa: S101 - loop ran at least once
        raise last


def _error_detail(data: object, text: str) -> str:
    if isinstance(data, dict):
        for key in ("error_description", "error", "message", "msg"):
            if data.get(key):
                return str(data[key])[:300]
    return text[:300] or "empty answer"
