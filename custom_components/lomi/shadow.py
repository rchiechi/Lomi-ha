"""AWS IoT device-shadow client for Lomi: the realtime state channel.

The Lomi device reports its live state (lid, cycle phase, timers) to an AWS
IoT *classic* device shadow. The official app subscribes to that shadow over
MQTT-over-WebSocket, authenticating with an AWS IoT **custom authorizer**
(``lomi_iot_authorizer_production``) that validates the user's Cognito token.

Recovered from the iOS app (v3.3.0):

* broker ``wss://a3m8zk07454hpt-ats.iot.us-east-1.amazonaws.com/mqtt``
* ``connectUsingWebSocketWithClientId:cleanSession:customAuthorizerName:
  tokenKeyName:tokenValue:tokenSignature:`` with
  ``customAuthorizerName = lomi_iot_authorizer_production``,
  ``tokenKeyName = "authorization"``, ``tokenValue = <Cognito session token>``,
  ``tokenSignature = ""`` (token signing disabled on the authorizer)
* topics ``$aws/things/<thingName>/shadow/update/documents`` (stream) and
  ``$aws/things/<thingName>/shadow/get`` -> ``get/accepted`` (fetch on connect)
* ``state.reported`` keys: ``serNo fwVer progLoc currState prevState startDelay
  tRem runTime coverOpen filterState filterCounter finished paused recovery
  stage1 stage2 stage3 standby lastUpdated``

The defaults below reproduce the app's handshake exactly as aws-sdk-ios
(AWSIoTMQTTClient, v2.36.1) builds it: plain ``wss://..../mqtt`` URL with the
authorizer name/token/empty-signature in three HTTP upgrade headers, a single
``mqttv3.1`` WebSocket subprotocol, MQTT 3.1.1 CONNECT (``MQTT``/level 4,
keepalive 300) and no CONNECT user name. The alternatives (token kind, query
string, MQTT user name, client id) stay configurable through
:class:`ShadowAuthConfig` for ``tools/shadow_probe.py`` experiments.

This module deliberately has no Home Assistant imports.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import logging
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode

import aiohttp

from .api import LomiAuth, LomiAuthError, LomiError, parse_iso
from .mqtt import MqttConnectRefused, MqttError, MqttWebSocketClient

_LOGGER = logging.getLogger(__name__)

IOT_HOST = "a3m8zk07454hpt-ats.iot.us-east-1.amazonaws.com"
IOT_URL = f"wss://{IOT_HOST}/mqtt"
IOT_AUTHORIZER = "lomi_iot_authorizer_production"
IOT_TOKEN_HEADER = "authorization"
# aws-sdk-ios opens the WebSocket with exactly one subprotocol ("mqttv3.1");
# offering "mqtt" as well changes the handshake and AWS IoT refuses it.
IOT_SUBPROTOCOLS: tuple[str, ...] = ("mqttv3.1",)

# --- how to hand the token to the custom authorizer -------------------------
TOKEN_ID = "id_token"
TOKEN_ACCESS = "access_token"
TOKEN_BEARER_ID = "bearer_id_token"
TOKEN_BEARER_ACCESS = "bearer_access_token"
TOKEN_KINDS = (TOKEN_ID, TOKEN_ACCESS, TOKEN_BEARER_ID, TOKEN_BEARER_ACCESS)

TRANSPORT_HEADER = "header"  # HTTP upgrade headers (what the iOS SDK does)
TRANSPORT_QUERY = "query"  # ?x-amz-customauthorizer-name=..&authorization=..
TRANSPORT_USERNAME = "username"  # MQTT CONNECT user name / password
TRANSPORTS = (TRANSPORT_HEADER, TRANSPORT_QUERY, TRANSPORT_USERNAME)

# Placeholders: {uuid} (random 32-hex, the app's exact clientId shape), {user_id}
# (Lomi Mongo id), {sub} (Cognito sub), {email}, {random} (12 hex chars),
# {serial} / {thing} (first device).
DEFAULT_CLIENT_ID = "{uuid}"

# --- shadow vocabulary -------------------------------------------------------
STATE_CONNECT = "CONNECT_STATE"
STATE_OFF = "OFF_STATE"
STATE_STANDBY = "STANDBY_STATE"
STATE_GROW = "GROW_STATE"
STATE_ECO_EXPRESS = "ECO_EXPRESS_STATE"
STATE_LOMI_APPROVED = "LOMI_APPROVED_STATE"
STATE_MIXING = "MIXING_STATE"
STATE_DRYING = "DRYING_STATE"
STATE_COOLING = "COOLING_STATE"
STATE_CLEANING = "CLEANING_STATE"
STATE_RECOVERY = "RECOVERY_STATE"
STATE_FILTER = "FILTER_STATE"
STATE_ISSUE_FILTER = "ISSUE_FILTER_STATE"
STATE_UNKNOWN = "unknown"

KNOWN_STATES: tuple[str, ...] = (
    STATE_CONNECT,
    STATE_OFF,
    STATE_STANDBY,
    STATE_GROW,
    STATE_ECO_EXPRESS,
    STATE_LOMI_APPROVED,
    STATE_MIXING,
    STATE_DRYING,
    STATE_COOLING,
    STATE_CLEANING,
    STATE_RECOVERY,
    STATE_FILTER,
    STATE_ISSUE_FILTER,
)
# Lower-case enum values used for the HA sensor (translation keys).
STATE_OPTIONS: tuple[str, ...] = tuple(s.lower() for s in KNOWN_STATES) + (STATE_UNKNOWN,)

# States in which a cycle is in progress (unless ``paused``).
RUNNING_STATES = frozenset(
    {
        STATE_GROW,
        STATE_ECO_EXPRESS,
        STATE_LOMI_APPROVED,
        STATE_MIXING,
        STATE_DRYING,
        STATE_COOLING,
        STATE_CLEANING,
    }
)
IDLE_STATES = frozenset({STATE_CONNECT, STATE_OFF, STATE_STANDBY})


def _as_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "open")
    return None


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


@dataclass(slots=True)
class LomiShadowState:
    """Parsed ``state.reported`` of one Lomi shadow."""

    serial: str | None
    firmware: str | None
    program_location: str | None
    current_state: str | None
    previous_state: str | None
    start_delay: int | None
    time_remaining: int | None
    run_time: int | None
    cover_open: bool | None
    filter_state: str | None
    filter_counter: int | None
    finished: bool | None
    paused: bool | None
    recovery: bool | None
    standby: bool | None
    stage1: str | None
    stage2: str | None
    stage3: str | None
    last_updated: datetime | None
    version: int | None
    received_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_reported(
        cls,
        reported: dict[str, Any],
        *,
        version: int | None = None,
        received_at: datetime | None = None,
    ) -> LomiShadowState:
        """Build from a (merged) ``state.reported`` dict."""
        return cls(
            serial=_as_str(reported.get("serNo")),
            firmware=_as_str(reported.get("fwVer")),
            program_location=_as_str(reported.get("progLoc")),
            current_state=_as_str(reported.get("currState")),
            previous_state=_as_str(reported.get("prevState")),
            start_delay=_as_int(reported.get("startDelay")),
            time_remaining=_as_int(reported.get("tRem")),
            run_time=_as_int(reported.get("runTime")),
            cover_open=_as_bool(reported.get("coverOpen")),
            filter_state=_as_str(reported.get("filterState")),
            filter_counter=_as_int(reported.get("filterCounter")),
            finished=_as_bool(reported.get("finished")),
            paused=_as_bool(reported.get("paused")),
            recovery=_as_bool(reported.get("recovery")),
            standby=_as_bool(reported.get("standby")),
            stage1=_as_str(reported.get("stage1")),
            stage2=_as_str(reported.get("stage2")),
            stage3=_as_str(reported.get("stage3")),
            last_updated=parse_iso(reported.get("lastUpdated")),
            version=version,
            received_at=received_at or datetime.now(UTC),
            raw=dict(reported),
        )

    @property
    def state_option(self) -> str:
        """Lower-case enum value for the cycle-state sensor."""
        if self.current_state and self.current_state in KNOWN_STATES:
            return self.current_state.lower()
        return STATE_UNKNOWN

    @property
    def previous_state_option(self) -> str:
        if self.previous_state and self.previous_state in KNOWN_STATES:
            return self.previous_state.lower()
        return STATE_UNKNOWN

    @property
    def is_running(self) -> bool | None:
        """A cycle is in progress (not paused)."""
        if self.current_state is None:
            return None
        if self.current_state not in RUNNING_STATES:
            return False
        return not self.paused

    @property
    def is_idle(self) -> bool | None:
        if self.current_state is None:
            return None
        return self.current_state in IDLE_STATES

    @property
    def is_active(self) -> bool | None:
        """Cycle in progress *or* paused mid-cycle."""
        if self.current_state is None:
            return None
        return self.current_state in RUNNING_STATES

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly view for diagnostics / attributes."""
        return {
            "serial": self.serial,
            "firmware": self.firmware,
            "program_location": self.program_location,
            "current_state": self.current_state,
            "previous_state": self.previous_state,
            "start_delay": self.start_delay,
            "time_remaining": self.time_remaining,
            "run_time": self.run_time,
            "cover_open": self.cover_open,
            "filter_state": self.filter_state,
            "filter_counter": self.filter_counter,
            "finished": self.finished,
            "paused": self.paused,
            "recovery": self.recovery,
            "standby": self.standby,
            "stage1": self.stage1,
            "stage2": self.stage2,
            "stage3": self.stage3,
            "last_updated": self.last_updated.isoformat() if self.last_updated else None,
            "version": self.version,
            "received_at": self.received_at.isoformat(),
        }


def extract_reported(topic_suffix: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, int | None, bool]:
    """Pull ``state.reported`` out of a shadow message.

    Returns ``(reported, version, is_full_document)``. ``update/documents``
    carries ``{"previous": {...}, "current": {"state": {...}, "version": n}}``;
    ``get/accepted`` carries the full ``{"state": {...}, "version": n}``;
    ``update/accepted`` carries only the changed keys (partial).
    """
    doc: dict[str, Any] | None = None
    full = True
    if topic_suffix == "update/documents":
        doc = payload.get("current") if isinstance(payload.get("current"), dict) else None
    elif topic_suffix in ("get/accepted", "update/accepted", "update/delta"):
        doc = payload
        full = topic_suffix == "get/accepted"
    if doc is None:
        return None, None, full
    state = doc.get("state") if isinstance(doc.get("state"), dict) else {}
    reported = state.get("reported")
    if not isinstance(reported, dict):
        return None, _as_int(doc.get("version")), full
    return reported, _as_int(doc.get("version")), full


@dataclass(slots=True)
class ShadowAuthConfig:
    """How to authenticate against the custom authorizer."""

    token_kind: str = TOKEN_ID
    transport: str = TRANSPORT_HEADER
    client_id_template: str = DEFAULT_CLIENT_ID
    authorizer: str = IOT_AUTHORIZER
    token_header: str = IOT_TOKEN_HEADER
    subprotocols: tuple[str, ...] = IOT_SUBPROTOCOLS
    # AWSIoTMQTTClient adds the signature header unconditionally, even when
    # token signing is disabled and the value is the empty string.
    include_empty_signature: bool = True
    keepalive: int = 300  # AWSIoTSDK keepalive default


StateCallback = Callable[[str, LomiShadowState, str], None]
ConnectionCallback = Callable[[bool, str | None], None]
TaskFactory = Callable[[Coroutine[Any, Any, Any], str], asyncio.Task[Any]]


class LomiShadowClient:
    """Maintain one MQTT session and track the shadows of all known things."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: LomiAuth,
        *,
        config: ShadowAuthConfig | None = None,
        identity: dict[str, str] | None = None,
        on_state: StateCallback,
        on_connection: ConnectionCallback | None = None,
        create_task: TaskFactory | None = None,
    ) -> None:
        self._session = session
        self._auth = auth
        self.config = config or ShadowAuthConfig()
        self._identity = dict(identity or {})
        self._on_state = on_state
        self._on_connection = on_connection
        self._create_task = create_task or (
            lambda coro, name: asyncio.get_running_loop().create_task(coro, name=name)
        )
        self._things: dict[str, str] = {}  # thing_name -> serial
        self._reported: dict[str, dict[str, Any]] = {}
        self._subscribed: set[str] = set()
        self._client: MqttWebSocketClient | None = None
        self._task: asyncio.Task[Any] | None = None
        self._stopped = asyncio.Event()
        self._wake = asyncio.Event()
        self.connected = False
        self.last_error: str | None = None
        self.last_connected_at: datetime | None = None
        self.last_message_at: datetime | None = None
        self.message_count = 0

    # ---- public API -------------------------------------------------------

    def set_identity(self, **identity: str) -> None:
        """Values available to the client-id template."""
        self._identity.update({k: v for k, v in identity.items() if v})

    def set_things(self, things: dict[str, str]) -> None:
        """Replace the set of thing_name -> serial to follow."""
        new = {t: s for t, s in things.items() if t}
        if new == self._things:
            return
        self._things = new
        self._wake.set()
        if self.connected and self._client is not None:
            missing = [t for t in new if t not in self._subscribed]
            if missing:
                self._create_task(self._async_subscribe(self._client, missing), "lomi_shadow_subscribe")

    def start(self) -> None:
        """Start the background connection loop."""
        if self._task is None or self._task.done():
            self._stopped.clear()
            self._task = self._create_task(self._run_forever(), "lomi_shadow")

    async def async_stop(self) -> None:
        """Stop and disconnect."""
        self._stopped.set()
        self._wake.set()
        client, self._client = self._client, None
        if client is not None:
            await client.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def async_refresh(self, thing_name: str | None = None) -> None:
        """Ask the broker for the current shadow (``shadow/get``)."""
        if not self.connected or self._client is None:
            return
        for thing in [thing_name] if thing_name else list(self._things):
            try:
                await self._client.publish(f"$aws/things/{thing}/shadow/get", b"")
            except MqttError as err:
                _LOGGER.debug("shadow get for %s failed: %s", thing, err)

    def get_state(self, thing_name: str) -> LomiShadowState | None:
        reported = self._reported.get(thing_name)
        return LomiShadowState.from_reported(reported) if reported else None

    # ---- connection parameters ------------------------------------------

    async def _async_token(self) -> str:
        kind = self.config.token_kind
        id_token = await self._auth.async_get_id_token()
        tokens = self._auth.tokens
        if kind in (TOKEN_ACCESS, TOKEN_BEARER_ACCESS):
            if tokens is None or not tokens.access_token:
                raise LomiAuthError("No Cognito access token available")
            token = tokens.access_token
        else:
            token = id_token
        if kind in (TOKEN_BEARER_ID, TOKEN_BEARER_ACCESS):
            token = f"Bearer {token}"
        return token

    def _client_id(self) -> str:
        values = {
            "uuid": uuid.uuid4().hex,  # UUID().uuidString minus dashes — app-exact
            "user_id": self._identity.get("user_id", ""),
            "sub": self._identity.get("sub", ""),
            "email": self._identity.get("email", ""),
            "random": secrets.token_hex(6),
            "serial": next(iter(self._things.values()), ""),
            "thing": next(iter(self._things), ""),
        }
        try:
            client_id = self.config.client_id_template.format(**values)
        except (KeyError, IndexError, ValueError):
            client_id = self.config.client_id_template
        return client_id or f"lomi-ha-{values['random']}"

    def _build(self, token: str) -> tuple[str, dict[str, str], str | None, str | None]:
        """Return (url, headers, username, password) for the chosen transport."""
        cfg = self.config
        transport = cfg.transport
        auth_params = {"x-amz-customauthorizer-name": cfg.authorizer, cfg.token_header: token}
        if cfg.include_empty_signature:
            auth_params["x-amz-customauthorizer-signature"] = ""
        if transport == TRANSPORT_QUERY:
            return f"{IOT_URL}?{urlencode(auth_params)}", {}, None, None
        if transport == TRANSPORT_USERNAME:
            username = f"?x-amz-customauthorizer-name={cfg.authorizer}"
            if cfg.include_empty_signature:
                username += "&x-amz-customauthorizer-signature="
            return IOT_URL, {}, username, token
        return IOT_URL, auth_params, None, None

    # ---- loop ----------------------------------------------------------------

    def _set_connected(self, value: bool, error: str | None = None) -> None:
        changed = value != self.connected or error != self.last_error
        self.connected = value
        self.last_error = error
        if value:
            self.last_connected_at = datetime.now(UTC)
        if changed and self._on_connection is not None:
            try:
                self._on_connection(value, error)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Shadow connection callback failed")

    async def _run_forever(self) -> None:
        backoff = 5.0
        warned = False
        while not self._stopped.is_set():
            if not self._things:
                # Nothing to follow yet; wait for set_things().
                self._wake.clear()
                await self._wake.wait()
                continue
            started = datetime.now(UTC)
            try:
                await self._run_once()
                error = "connection closed"
            except MqttConnectRefused as err:
                error = str(err)
            except MqttError as err:
                error = str(err)
            except LomiAuthError as err:
                error = f"auth: {err}"
            except LomiError as err:
                error = f"cloud: {err}"
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("Unexpected error in Lomi shadow loop")
                error = f"unexpected: {err}"
            self._set_connected(False, error)
            if self._stopped.is_set():
                break
            lived = (datetime.now(UTC) - started).total_seconds()
            if lived > 120:
                backoff = 5.0
                warned = False
            if not warned:
                _LOGGER.warning(
                    "Lomi shadow connection lost (%s); retrying in %.0f s", error, backoff
                )
                warned = True
            else:
                _LOGGER.debug("Lomi shadow reconnect in %.0f s (%s)", backoff, error)
            try:
                await asyncio.wait_for(self._stopped.wait(), backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 300.0)

    async def _run_once(self) -> None:
        token = await self._async_token()
        url, headers, username, password = self._build(token)
        client_id = self._client_id()
        _LOGGER.debug(
            "Shadow connect: transport=%s token=%s client_id=%s things=%s",
            self.config.transport,
            self.config.token_kind,
            client_id,
            list(self._things),
        )
        client = MqttWebSocketClient(
            self._session,
            url,
            headers=headers,
            protocols=self.config.subprotocols,
            client_id=client_id,
            keepalive=self.config.keepalive,
            username=username,
            password=password,
            on_message=self._on_message,
        )
        self._subscribed.clear()
        await client.connect()
        self._client = client
        self._set_connected(True, None)
        _LOGGER.info("Connected to the Lomi device shadow as %s", client_id)
        try:
            await self._async_subscribe(client, list(self._things))
            await client.run()
        finally:
            if self._client is client:
                self._client = None
            self._subscribed.clear()

    async def _async_subscribe(self, client: MqttWebSocketClient, things: list[str]) -> None:
        for thing in things:
            base = f"$aws/things/{thing}/shadow"
            topics = [
                (f"{base}/update/documents", 0),
                (f"{base}/update/accepted", 0),
                (f"{base}/get/accepted", 0),
                (f"{base}/get/rejected", 0),
            ]
            granted = await client.subscribe(topics)
            failed = [t for (t, _), g in zip(topics, granted, strict=False) if g == 0x80]
            if failed:
                _LOGGER.warning("Shadow subscribe refused for %s", failed)
            self._subscribed.add(thing)
            await client.publish(f"{base}/get", b"")
            _LOGGER.debug("Subscribed to shadow of %s (granted %s)", thing, granted)

    # ---- inbound ----------------------------------------------------------

    def _on_message(self, topic: str, payload: bytes) -> None:
        self.message_count += 1
        self.last_message_at = datetime.now(UTC)
        if not topic.startswith("$aws/things/"):
            return
        rest = topic[len("$aws/things/") :]
        thing, sep, suffix = rest.partition("/shadow/")
        if not sep:
            return
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            _LOGGER.debug("Non-JSON shadow payload on %s: %r", topic, payload[:120])
            return
        if suffix == "get/rejected":
            _LOGGER.warning("Shadow get rejected for %s: %s", thing, data)
            return
        if not isinstance(data, dict):
            return
        reported, version, full = extract_reported(suffix, data)
        if reported is None:
            _LOGGER.debug("No reported state in %s message: %s", suffix, str(data)[:200])
            return
        expected = self._things.get(thing)
        ser = reported.get("serNo")
        if expected and ser and str(ser) != expected:
            _LOGGER.warning(
                "Ignoring shadow for %s: serial %s does not match %s", thing, ser, expected
            )
            return
        merged = dict(reported) if full else {**self._reported.get(thing, {}), **reported}
        self._reported[thing] = merged
        received_at = datetime.now(UTC)
        ts = data.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 1_000_000_000:
            received_at = datetime.fromtimestamp(ts, tz=UTC)
        state = LomiShadowState.from_reported(merged, version=version, received_at=received_at)
        _LOGGER.debug("Shadow %s (%s): %s", thing, suffix, state.as_dict())
        try:
            self._on_state(thing, state, suffix)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Shadow state callback failed")
