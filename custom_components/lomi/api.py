"""Async client for the Lomi cloud API.

The Lomi device has no local interface. The official app talks to
``api.lomi-app.net`` (the "LAAS" backend) using an AWS Cognito IdToken as a
Bearer credential. Telemetry is pulled by polling ``GET /userDevices``;
control is fire-and-forget ``POST /remoteCommands`` which the backend relays
to the device over AWS IoT.

Everything in here was derived from mitmproxy captures of the official iOS /
macOS app (v3.3.0) and verified against production.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

COGNITO_URL = "https://cognito-idp.us-east-1.amazonaws.com/"
COGNITO_CLIENT_ID = "5hss880dr2s87m2gbrd50ft7th"
COGNITO_USER_POOL_ID = "us-east-1_nwqtHcuYy"
COGNITO_TARGET_INITIATE_AUTH = "AWSCognitoIdentityProviderService.InitiateAuth"
COGNITO_TARGET_REFRESH = "AWSCognitoIdentityProviderService.GetTokensFromRefreshToken"

API_BASE = "https://api.lomi-app.net"
USER_AGENT = (
    "Lomi-Production/3.3.0 (earth.lomi.app.1; build:3; iOS 27.0.0) Alamofire/5.6.4"
)

TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Program modes; ``value`` in /remoteCommands and ``arguments.mode`` in replies.
MODE_GROW = "grow"
MODE_EXPRESS = "express"
MODE_LOMI_APPROVED = "lomi_approved"
MODE_CLEANING = "cleaning"
MODES: dict[str, int] = {
    MODE_GROW: 1,
    MODE_EXPRESS: 2,
    MODE_LOMI_APPROVED: 3,
    MODE_CLEANING: 4,
}
MODE_NAMES: dict[int, str] = {v: k for k, v in MODES.items()}

CMD_START = "start"
CMD_PAUSE = "pause"
CMD_RESUME = "resume"
CMD_STOP = "stop"
CMD_PROG = "prog"  # switch program without starting
COMMANDS = (CMD_START, CMD_PAUSE, CMD_RESUME, CMD_STOP, CMD_PROG)

# Cognito error types that mean "credentials are bad", not "network hiccup".
_COGNITO_AUTH_ERRORS = {
    "NotAuthorizedException",
    "UserNotFoundException",
    "UserNotConfirmedException",
    "PasswordResetRequiredException",
    "InvalidParameterException",
}


class LomiError(Exception):
    """Base error for the Lomi client."""


class LomiConnectionError(LomiError):
    """Network / transport error."""


class LomiAuthError(LomiError):
    """Credentials rejected (login, refresh, or 401 from the API)."""


class LomiApiError(LomiError):
    """The backend returned an error envelope."""

    def __init__(self, reason: str | None, message: str | None) -> None:
        self.reason = reason or "UNKNOWN"
        self.message = message or ""
        super().__init__(f"{self.reason}: {self.message}")


def _jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (we only need exp/sub)."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001 - anything odd just means "no claims"
        return {}


def parse_iso(value: Any) -> datetime | None:
    """Parse the backend's ISO-8601 timestamps (``...Z``) into aware datetimes."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_issue_at(when: datetime) -> str:
    """Format a datetime the way the app sends ``issueAt``."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(slots=True)
class LomiTokens:
    """Cognito token set."""

    id_token: str
    access_token: str | None
    refresh_token: str | None
    expires_at: datetime

    @classmethod
    def from_result(cls, result: dict[str, Any], refresh_token: str | None) -> LomiTokens:
        """Build from an ``AuthenticationResult`` dict."""
        id_token = result["IdToken"]
        claims = _jwt_payload(id_token)
        if isinstance(claims.get("exp"), (int, float)):
            expires_at = datetime.fromtimestamp(claims["exp"], tz=UTC)
        else:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=int(result.get("ExpiresIn", 3600))
            )
        return cls(
            id_token=id_token,
            access_token=result.get("AccessToken"),
            # Lomi's pool does not rotate refresh tokens today, but handle it if
            # it ever starts (GetTokensFromRefreshToken supports rotation).
            refresh_token=result.get("RefreshToken") or refresh_token,
            expires_at=expires_at,
        )

    @property
    def claims(self) -> dict[str, Any]:
        """Decoded IdToken claims (sub, email, ...)."""
        return _jwt_payload(self.id_token)


@dataclass(slots=True)
class LomiDevice:
    """One registered Lomi (merged ``userDevice`` + ``device`` records)."""

    user_device_id: str
    serial: str
    nickname: str
    device_type: str
    device_version: str | None
    thing_name: str | None
    manufacture_date: datetime | None
    refurbished: bool | None
    last_filter_counter: int | None
    last_filter_reset_date: datetime | None
    filter_counter_modified_at: datetime | None
    created_at: datetime | None
    last_modified: datetime | None
    removed: bool
    version: int | None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> LomiDevice:
        """Parse one entry of ``GET /userDevices``."""
        ud = record.get("userDevice") or {}
        dev = record.get("device") or {}
        serial = ud.get("deviceId") or dev.get("_id") or ""
        return cls(
            user_device_id=ud.get("_id", ""),
            serial=serial,
            nickname=ud.get("nickname") or serial or "Lomi",
            device_type=ud.get("deviceType") or dev.get("deviceType") or "",
            device_version=dev.get("deviceVersion"),
            thing_name=dev.get("thingName"),
            manufacture_date=parse_iso(dev.get("manufactureDate")),
            refurbished=dev.get("refurbished"),
            last_filter_counter=_as_int(ud.get("lastFilterCounter")),
            last_filter_reset_date=parse_iso(ud.get("lastFilterResetDate")),
            filter_counter_modified_at=parse_iso(ud.get("filterCounterModifiedAt")),
            created_at=parse_iso(ud.get("createdAt")),
            last_modified=parse_iso(ud.get("lastModified")),
            removed=bool(ud.get("removed", False)),
            version=_as_int(ud.get("version")),
            raw=record,
        )

    @property
    def model(self) -> str:
        """Human readable model from the ``deviceType`` family string."""
        family = self.device_type.removeprefix("registration_")
        names = {
            "classic": "Lomi Classic",
            "bloom": "Lomi Bloom",
            "sprout": "Lomi 3 (Sprout)",
        }
        return names.get(family, f"Lomi ({family or 'unknown'})")


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


class LomiAuth:
    """Manage Cognito tokens for one Lomi account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        refresh_token: str | None = None,
        email: str | None = None,
        password: str | None = None,
        on_tokens_updated: Callable[[LomiTokens], None] | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._email = email
        self._password = password
        self._on_tokens_updated = on_tokens_updated
        self._tokens: LomiTokens | None = None
        self._lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        """Current (long-lived) refresh token."""
        return self._refresh_token

    @property
    def tokens(self) -> LomiTokens | None:
        """Current token set, if any."""
        return self._tokens

    def set_credentials(self, email: str, password: str | None) -> None:
        """Update the fallback password credentials."""
        self._email = email
        self._password = password

    def invalidate(self) -> None:
        """Force a refresh on the next request."""
        if self._tokens is not None:
            self._tokens.expires_at = datetime.now(UTC) - timedelta(days=1)

    async def _cognito(self, target: str, body: dict[str, Any]) -> dict[str, Any]:
        body = {"ClientId": COGNITO_CLIENT_ID, "ClientMetadata": {}, **body}
        headers = {
            "content-type": "application/x-amz-json-1.1",
            "x-amz-target": target,
            "user-agent": USER_AGENT,
        }
        try:
            async with self._session.post(
                COGNITO_URL, json=body, headers=headers, timeout=REQUEST_TIMEOUT
            ) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LomiConnectionError(f"Cognito request failed: {err}") from err
        if not isinstance(data, dict):
            raise LomiError("Unexpected Cognito response")
        if "AuthenticationResult" not in data:
            err_type = str(data.get("__type", "")).split("#")[-1]
            message = data.get("message") or data.get("Message") or err_type
            if err_type in _COGNITO_AUTH_ERRORS:
                raise LomiAuthError(f"{err_type}: {message}")
            if data.get("ChallengeName"):
                raise LomiAuthError(
                    f"Unsupported Cognito challenge {data['ChallengeName']}"
                )
            raise LomiError(f"Cognito error {err_type or 'unknown'}: {message}")
        return data["AuthenticationResult"]

    def _apply(self, result: dict[str, Any]) -> LomiTokens:
        tokens = LomiTokens.from_result(result, self._refresh_token)
        self._tokens = tokens
        if tokens.refresh_token and tokens.refresh_token != self._refresh_token:
            self._refresh_token = tokens.refresh_token
        if self._on_tokens_updated is not None:
            self._on_tokens_updated(tokens)
        return tokens

    async def async_login(self, email: str, password: str) -> LomiTokens:
        """Password login (USER_PASSWORD_AUTH)."""
        result = await self._cognito(
            COGNITO_TARGET_INITIATE_AUTH,
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            },
        )
        self._email, self._password = email, password
        self._refresh_token = result.get("RefreshToken") or self._refresh_token
        return self._apply(result)

    async def _async_refresh(self) -> LomiTokens:
        if not self._refresh_token:
            raise LomiAuthError("No refresh token available")
        # The app uses GetTokensFromRefreshToken; the classic REFRESH_TOKEN_AUTH
        # flow is also accepted by this pool. Try the app's way first.
        try:
            result = await self._cognito(
                COGNITO_TARGET_REFRESH, {"RefreshToken": self._refresh_token}
            )
        except LomiError as first_err:
            if isinstance(first_err, LomiConnectionError):
                raise
            _LOGGER.debug("GetTokensFromRefreshToken failed (%s); trying InitiateAuth", first_err)
            result = await self._cognito(
                COGNITO_TARGET_INITIATE_AUTH,
                {
                    "AuthFlow": "REFRESH_TOKEN_AUTH",
                    "AuthParameters": {"REFRESH_TOKEN": self._refresh_token},
                },
            )
        return self._apply(result)

    async def async_get_id_token(self) -> str:
        """Return a valid IdToken, refreshing or re-logging in as needed."""
        async with self._lock:
            if (
                self._tokens is not None
                and datetime.now(UTC) < self._tokens.expires_at - TOKEN_REFRESH_MARGIN
            ):
                return self._tokens.id_token
            try:
                return (await self._async_refresh()).id_token
            except LomiAuthError as err:
                if self._email and self._password:
                    _LOGGER.debug("Refresh token rejected (%s); re-logging in", err)
                    return (await self.async_login(self._email, self._password)).id_token
                raise


class LomiClient:
    """REST client for api.lomi-app.net."""

    def __init__(self, session: aiohttp.ClientSession, auth: LomiAuth) -> None:
        self._session = session
        self.auth = auth

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        authenticated: bool = True,
        _retry: bool = True,
    ) -> Any:
        headers = {
            "x-lomi-client": "iOS",
            "x-lomi-app": "lomi",
            "accept": "application/json",
            "user-agent": USER_AGENT,
        }
        if authenticated:
            headers["authorization"] = f"Bearer {await self.auth.async_get_id_token()}"
        if json_body is not None:
            headers["content-type"] = "application/json"
        try:
            async with self._session.request(
                method,
                f"{API_BASE}{path}",
                params=params,
                json=json_body,
                headers=headers,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in (401, 403) and authenticated:
                    if _retry:
                        self.auth.invalidate()
                        return await self._request(
                            method,
                            path,
                            params=params,
                            json_body=json_body,
                            authenticated=authenticated,
                            _retry=False,
                        )
                    raise LomiAuthError(f"HTTP {resp.status} from {path}")
                try:
                    data = await resp.json(content_type=None)
                except (aiohttp.ContentTypeError, json.JSONDecodeError) as err:
                    text = await resp.text()
                    raise LomiApiError(
                        f"HTTP_{resp.status}", f"Non-JSON response: {text[:200]}"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise LomiConnectionError(f"{method} {path} failed: {err}") from err

        if not isinstance(data, dict):
            raise LomiApiError("BAD_ENVELOPE", f"Unexpected payload for {path}")
        if data.get("error"):
            error = data["error"]
            if isinstance(error, dict):
                raise LomiApiError(error.get("reason"), error.get("message"))
            raise LomiApiError(None, str(error))
        if "result" not in data:
            raise LomiApiError("BAD_ENVELOPE", f"No result in response for {path}")
        return data["result"]

    # ---- account -----------------------------------------------------------

    async def async_get_app_support(self) -> dict[str, Any]:
        """Unauthenticated min-version gate; handy as a reachability check."""
        return await self._request("GET", "/v2/appSupport", authenticated=False)

    async def async_get_user(self) -> dict[str, Any]:
        """Account profile (``_id``, email, name...)."""
        return await self._request("GET", "/v2/users")

    # ---- devices / telemetry ----------------------------------------------

    async def async_get_devices(self) -> list[LomiDevice]:
        """Primary telemetry poll: all registered devices with their records."""
        result = await self._request("GET", "/userDevices")
        devices = [LomiDevice.from_record(r) for r in (result or [])]
        return [d for d in devices if d.user_device_id and not d.removed]

    async def async_register_device(self, serial: str, nickname: str) -> dict[str, Any]:
        """Register a Lomi to this account by serial number."""
        return await self._request(
            "POST",
            "/userDevices",
            json_body={"serialNumber": serial, "nickname": nickname},
        )

    async def async_get_device_status(self, serial: str) -> dict[str, Any]:
        """Transient status. Observed ``{}`` when idle; shape otherwise unknown."""
        result = await self._request(
            "GET", "/devices/status", params={"serialNumber": serial}
        )
        return result if isinstance(result, dict) else {"value": result}

    # ---- control -------------------------------------------------------------

    async def async_send_command(
        self,
        user_device_id: str,
        command: str,
        *,
        mode: str | int | None = None,
        issue_at: datetime | None = None,
    ) -> dict[str, Any]:
        """POST /remoteCommands.

        ``mode`` may be a mode key (``express``) or its number (``2``). The app
        always sends a ``value`` with every command; it echoes the currently
        selected program.
        """
        if command not in COMMANDS:
            raise ValueError(f"Unknown command {command!r}")
        body: dict[str, Any] = {"command": command, "userDeviceId": user_device_id}
        if mode is not None:
            body["value"] = MODES[mode] if isinstance(mode, str) else int(mode)
        if issue_at is not None:
            body["issueAt"] = format_issue_at(issue_at)
        return await self._request("POST", "/remoteCommands", json_body=body)

    async def async_get_pending_commands(self, user_device_id: str) -> list[dict[str, Any]]:
        """Commands not yet acknowledged by the device (incl. scheduled starts)."""
        result = await self._request(
            "GET",
            "/remoteCommands",
            params={"userDeviceId": user_device_id, "status": "pending"},
        )
        return list(result or [])

    async def async_cancel_command(self, command_id: str) -> dict[str, Any]:
        """Cancel a pending / scheduled command."""
        result = await self._request(
            "DELETE", "/remoteCommands", params={"_id": command_id}
        )
        return result if isinstance(result, dict) else {}

    # ---- low value extras ----------------------------------------------------

    async def async_get_achievements(self) -> Any:
        """Gamification data."""
        return await self._request("GET", "/achievements")

    async def async_get_subscriptions(self) -> Any:
        """Shopify membership subscriptions."""
        return await self._request("GET", "/subscriptions")
