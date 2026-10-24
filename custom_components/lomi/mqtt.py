"""Minimal MQTT 3.1.1 over WebSocket client for AWS IoT (custom authorizer).

Only what the Lomi shadow needs: CONNECT / SUBSCRIBE / PUBLISH (QoS 0 and 1)
/ PINGREQ and the matching inbound packets. Runs entirely on Home Assistant's
shared ``aiohttp`` session, so the integration has no extra requirements.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import secrets
import struct

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Packet types (high nibble of the fixed header).
CONNECT = 0x10
CONNACK = 0x20
PUBLISH = 0x30
PUBACK = 0x40
SUBSCRIBE = 0x80
SUBACK = 0x90
UNSUBSCRIBE = 0xA0
UNSUBACK = 0xB0
PINGREQ = 0xC0
PINGRESP = 0xD0
DISCONNECT = 0xE0

CONNACK_CODES = {
    0: "accepted",
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorized",
}

MessageCallback = Callable[[str, bytes], Awaitable[None] | None]


class MqttError(Exception):
    """Transport or protocol error."""


class MqttConnectRefused(MqttError):
    """Broker answered CONNACK with a non-zero return code."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"CONNACK {code}: {CONNACK_CODES.get(code, 'unknown')}")


def _encode_string(value: str) -> bytes:
    data = value.encode("utf-8")
    return struct.pack("!H", len(data)) + data


def _encode_remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _packet(header: int, body: bytes) -> bytes:
    return bytes([header]) + _encode_remaining_length(len(body)) + body


def encode_connect(
    client_id: str,
    keepalive: int,
    clean_session: bool = True,
    username: str | None = None,
    password: str | None = None,
) -> bytes:
    """CONNECT, protocol level 4 (MQTT 3.1.1), optional user name / password."""
    flags = 0x02 if clean_session else 0x00
    if username is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    body = (
        _encode_string("MQTT")
        + bytes([4, flags])
        + struct.pack("!H", keepalive)
        + _encode_string(client_id)
    )
    if username is not None:
        body += _encode_string(username)
    if password is not None:
        body += _encode_string(password)
    return _packet(CONNECT, body)


def encode_subscribe(packet_id: int, topics: list[tuple[str, int]]) -> bytes:
    """SUBSCRIBE a list of (topic, qos)."""
    body = struct.pack("!H", packet_id)
    for topic, qos in topics:
        body += _encode_string(topic) + bytes([qos])
    return _packet(SUBSCRIBE | 0x02, body)


def encode_publish(topic: str, payload: bytes, qos: int = 0, packet_id: int = 0) -> bytes:
    """PUBLISH with QoS 0 or 1."""
    header = PUBLISH | (qos << 1)
    body = _encode_string(topic)
    if qos:
        body += struct.pack("!H", packet_id)
    return _packet(header, body + payload)


def encode_puback(packet_id: int) -> bytes:
    return _packet(PUBACK, struct.pack("!H", packet_id))


PINGREQ_PACKET = _packet(PINGREQ, b"")
DISCONNECT_PACKET = _packet(DISCONNECT, b"")


@dataclass(slots=True)
class Packet:
    """One decoded control packet."""

    kind: int
    flags: int
    body: bytes


class _Decoder:
    """Reassemble control packets from WebSocket binary frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Packet]:
        self._buf.extend(data)
        out: list[Packet] = []
        while True:
            packet = self._pop()
            if packet is None:
                return out
            out.append(packet)

    def _pop(self) -> Packet | None:
        buf = self._buf
        if len(buf) < 2:
            return None
        length = 0
        multiplier = 1
        pos = 1
        while True:
            if pos >= len(buf):
                return None
            byte = buf[pos]
            length += (byte & 0x7F) * multiplier
            multiplier *= 128
            pos += 1
            if not byte & 0x80:
                break
            if pos > 4:
                raise MqttError("Malformed remaining length")
        if len(buf) < pos + length:
            return None
        header = buf[0]
        body = bytes(buf[pos : pos + length])
        del buf[: pos + length]
        return Packet(kind=header & 0xF0, flags=header & 0x0F, body=body)


def decode_publish(packet: Packet) -> tuple[str, bytes, int, int]:
    """Return (topic, payload, qos, packet_id) for a PUBLISH packet."""
    qos = (packet.flags >> 1) & 0x03
    (tlen,) = struct.unpack_from("!H", packet.body, 0)
    topic = packet.body[2 : 2 + tlen].decode("utf-8", "replace")
    pos = 2 + tlen
    packet_id = 0
    if qos:
        (packet_id,) = struct.unpack_from("!H", packet.body, pos)
        pos += 2
    return topic, packet.body[pos:], qos, packet_id


class MqttWebSocketClient:
    """One MQTT session over a WebSocket.

    ``run`` drives the session until the socket closes or ``close`` is called.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        headers: dict[str, str],
        client_id: str | None = None,
        keepalive: int = 60,
        protocols: tuple[str, ...] = ("mqtt",),
        username: str | None = None,
        password: str | None = None,
        on_message: MessageCallback,
    ) -> None:
        self._session = session
        self._url = url
        self._headers = headers
        self._protocols = protocols
        self.client_id = client_id or f"lomi-ha-{secrets.token_hex(6)}"
        self._keepalive = keepalive
        self._username = username
        self._password = password
        self._on_message = on_message
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._decoder = _Decoder()
        self._packet_id = 0
        self._connack: asyncio.Future[int] | None = None
        self._acks: dict[int, asyncio.Future[bytes]] = {}
        self._closing = False

    # ---- lifecycle -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def _next_id(self) -> int:
        self._packet_id = self._packet_id % 65535 + 1
        return self._packet_id

    async def connect(self, timeout: float = 20) -> None:
        """Open the socket and complete the MQTT handshake."""
        loop = asyncio.get_running_loop()
        self._closing = False
        try:
            self._ws = await self._session.ws_connect(
                self._url,
                headers=self._headers,
                protocols=self._protocols,
                heartbeat=None,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
                max_msg_size=4 * 1024 * 1024,
            )
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            raise MqttError(f"WebSocket connect failed: {err}") from err
        self._connack = loop.create_future()
        await self._send(
            encode_connect(
                self.client_id,
                self._keepalive,
                username=self._username,
                password=self._password,
            )
        )
        reader = asyncio.ensure_future(self._read_loop())
        try:
            code = await asyncio.wait_for(asyncio.shield(self._connack), timeout)
        except TimeoutError as err:
            reader.cancel()
            await self.close()
            raise MqttError("Timed out waiting for CONNACK") from err
        except MqttError:
            reader.cancel()
            await self.close()
            raise
        if code != 0:
            reader.cancel()
            await self.close()
            raise MqttConnectRefused(code)
        self._reader = reader

    async def run(self) -> None:
        """Keep the session alive until the socket drops. Raises MqttError."""
        pinger = asyncio.ensure_future(self._ping_loop())
        try:
            await self._reader
        finally:
            pinger.cancel()
            await self.close()

    async def close(self) -> None:
        self._closing = True
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.send_bytes(DISCONNECT_PACKET)
            except Exception:  # noqa: BLE001 - best effort on the way out
                pass
            await ws.close()
        if self._connack is not None and not self._connack.done():
            self._connack.set_exception(MqttError("Closed before CONNACK"))
        for fut in self._acks.values():
            if not fut.done():
                fut.set_exception(MqttError("Closed before ACK"))
        self._acks.clear()

    # ---- operations ------------------------------------------------------

    async def subscribe(self, topics: list[tuple[str, int]], timeout: float = 10) -> list[int]:
        """SUBSCRIBE and return the granted QoS per topic (0x80 = failure)."""
        packet_id = self._next_id()
        fut: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._acks[packet_id] = fut
        await self._send(encode_subscribe(packet_id, topics))
        try:
            body = await asyncio.wait_for(fut, timeout)
        except TimeoutError as err:
            raise MqttError("Timed out waiting for SUBACK") from err
        finally:
            self._acks.pop(packet_id, None)
        return list(body)

    async def publish(self, topic: str, payload: bytes = b"", qos: int = 0) -> None:
        packet_id = self._next_id() if qos else 0
        await self._send(encode_publish(topic, payload, qos, packet_id))

    # ---- internals -------------------------------------------------------

    async def _send(self, data: bytes) -> None:
        ws = self._ws
        if ws is None or ws.closed:
            raise MqttError("Not connected")
        try:
            await ws.send_bytes(data)
        except (aiohttp.ClientError, ConnectionResetError) as err:
            raise MqttError(f"Send failed: {err}") from err

    async def _ping_loop(self) -> None:
        interval = max(self._keepalive // 2, 5)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._send(PINGREQ_PACKET)
            except MqttError:
                return

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    for packet in self._decoder.feed(msg.data):
                        await self._handle(packet)
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise MqttError(f"WebSocket error: {ws.exception()}")
        finally:
            if not self._closing:
                code = ws.close_code
                _LOGGER.debug("MQTT socket closed (code %s)", code)
            if self._connack is not None and not self._connack.done():
                self._connack.set_exception(
                    MqttError(f"Socket closed before CONNACK (close code {ws.close_code})")
                )
        if not self._closing:
            raise MqttError(f"Connection closed (close code {ws.close_code})")

    async def _handle(self, packet: Packet) -> None:
        if packet.kind == CONNACK:
            code = packet.body[1] if len(packet.body) >= 2 else 255
            if self._connack is not None and not self._connack.done():
                self._connack.set_result(code)
        elif packet.kind == PUBLISH:
            topic, payload, qos, packet_id = decode_publish(packet)
            if qos == 1:
                await self._send(encode_puback(packet_id))
            result = self._on_message(topic, payload)
            if result is not None:
                await result
        elif packet.kind in (SUBACK, UNSUBACK, PUBACK):
            (packet_id,) = struct.unpack_from("!H", packet.body, 0)
            fut = self._acks.get(packet_id)
            if fut is not None and not fut.done():
                fut.set_result(packet.body[2:])
        elif packet.kind == PINGRESP:
            pass
        else:
            _LOGGER.debug("Ignoring MQTT packet type 0x%02x", packet.kind)
