"""Improv Wi-Fi over a serial port.

Requires the ``serial`` extra: ``pip install improv-client[serial]``.

Example::

    import asyncio
    from improv_client.serial import ImprovSerialClient

    async def main():
        async with ImprovSerialClient("/dev/ttyUSB0") as client:
            print(await client.request_current_state())
            print(await client.get_device_info())
            url = await client.provision("MyWirelessAP", "mysecurepassword")
            print("Provisioned, next step:", url)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import serial_asyncio

from .client import DEFAULT_TIMEOUT, ImprovClient
from .exceptions import ImprovConnectionError, ImprovProtocolError
from .protocol import (
    Command,
    Error,
    NetworkState,
    PacketType,
    SerialPacket,
    SerialPacketDecoder,
    State,
    decode_rpc_result,
    encode_rpc_command,
    encode_serial_packet,
    parse_state,
)

__all__ = ["DEFAULT_BAUDRATE", "ImprovSerialClient"]

_LOGGER = logging.getLogger(__name__)

#: Baud rate used by ESPHome and most other Improv serial implementations.
DEFAULT_BAUDRATE = 115200


class _ImprovSerialProtocol(asyncio.Protocol):
    def __init__(self, client: ImprovSerialClient) -> None:
        self._client = client
        self._decoder = SerialPacketDecoder()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._client._connection_made(transport)  # noqa: SLF001

    def data_received(self, data: bytes) -> None:
        for packet in self._decoder.feed(data):
            self._client._handle_packet(packet)  # noqa: SLF001

    def connection_lost(self, exc: Exception | None) -> None:
        self._client._connection_lost(exc)  # noqa: SLF001


class ImprovSerialClient(ImprovClient):
    """Client for an Improv Wi-Fi device on a serial port.

    :param port: Serial port, e.g. ``/dev/ttyUSB0`` or ``COM3``.
    :param baudrate: Baud rate.
    :param timeout: Default time in seconds to wait for an answer.
    :param disconnected_callback: Called with the client when the port closes,
        whether through :meth:`disconnect` or because the device went away.
    :param serial_kwargs: Extra arguments for :class:`serial.Serial`.
    """

    _scan_multiple_results = True

    def __init__(
        self,
        port: str,
        baudrate: int = DEFAULT_BAUDRATE,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        disconnected_callback: Callable[[ImprovSerialClient], None] | None = None,
        **serial_kwargs: Any,
    ) -> None:
        super().__init__(timeout=timeout)
        self.port = port
        self.baudrate = baudrate
        self._serial_kwargs = serial_kwargs
        self._disconnected_callback = disconnected_callback
        self._transport: asyncio.Transport | None = None
        self._closed: asyncio.Event | None = None

    @property
    def connected(self) -> bool:
        return self._transport is not None

    async def connect(self) -> None:
        """Open the serial port.

        Opening the port doesn't tell whether the device speaks Improv. Call
        :meth:`request_current_state` to find out.

        :raises ImprovConnectionError: If the port can't be opened.
        """
        if self.connected:
            return
        _LOGGER.debug("Opening %s at %d baud", self.port, self.baudrate)
        self._closed = asyncio.Event()
        try:
            transport, _protocol = await serial_asyncio.create_serial_connection(
                asyncio.get_running_loop(),
                lambda: _ImprovSerialProtocol(self),
                self.port,
                baudrate=self.baudrate,
                **self._serial_kwargs,
            )
        except (OSError, ValueError) as err:
            raise ImprovConnectionError(f"Can't open {self.port}: {err}") from err
        # serial_asyncio calls connection_made() on the next loop iteration, so set the
        # transport here to be usable as soon as connect() returns.
        self._connection_made(transport)

    async def disconnect(self) -> None:
        """Close the serial port."""
        transport = self._transport
        if transport is None:
            return
        transport.close()
        if self._closed is not None:
            try:
                await asyncio.wait_for(self._closed.wait(), 2)
            except asyncio.TimeoutError:
                _LOGGER.debug("Serial port didn't report closing in time")
                self._connection_lost(None)

    async def request_current_state(self, *, timeout: float | None = None) -> State:
        """Ask the device for its current state (command 0x02).

        A device that is already provisioned also sends the provisioning result
        again; that result is ignored here.

        :raises ImprovTimeoutError: If the device doesn't answer, which usually means
            it doesn't run Improv on this port.
        """
        return await self._request_next_state(Command.REQUEST_CURRENT_STATE, timeout)

    async def get_network_state(self) -> NetworkState:
        """Request the network flags and the device URLs (command 0x07).

        This command is optional.

        :raises ImprovNotSupportedError: If the device doesn't implement it.
        """
        results = await self._rpc(Command.NETWORK_STATE)
        return NetworkState.from_strings(results[0])

    # --- Transport ----------------------------------------------------------------

    async def _before_operation(self) -> None:
        if not self.connected:
            raise ImprovConnectionError("Not connected, call connect() first")

    async def _send_rpc_command(self, command: int, data: bytes) -> None:
        transport = self._transport
        if transport is None:
            raise ImprovConnectionError("Not connected")
        frame = encode_serial_packet(PacketType.RPC_COMMAND, encode_rpc_command(command, data))
        _LOGGER.debug("> %s", frame.hex(" "))
        transport.write(frame + b"\n")

    def _connection_made(self, transport: asyncio.BaseTransport) -> None:
        if self._transport is transport:
            return
        _LOGGER.debug("Opened %s", self.port)
        self._transport = transport  # type: ignore[assignment]

    def _connection_lost(self, exc: Exception | None) -> None:
        if self._transport is None:
            return
        _LOGGER.debug("Closed %s (%s)", self.port, exc or "no error")
        self._transport = None
        if self._closed is not None:
            self._closed.set()
        self._handle_connection_lost(exc)
        if self._disconnected_callback is not None:
            self._run_callback(self._disconnected_callback, self)

    def _handle_packet(self, packet: SerialPacket) -> None:
        _LOGGER.debug("< %s %s", packet.packet_type, packet.data.hex(" "))
        try:
            if packet.packet_type is PacketType.CURRENT_STATE:
                self._handle_state(parse_state(self._first_byte(packet)))
            elif packet.packet_type is PacketType.ERROR_STATE:
                self._handle_error(Error.parse(self._first_byte(packet)))
            elif packet.packet_type is PacketType.RPC_RESULT:
                self._handle_rpc_result(decode_rpc_result(packet.data))
            else:
                _LOGGER.debug("Ignoring packet type %s", packet.packet_type)
        except ImprovProtocolError as err:
            _LOGGER.warning("Invalid packet from %s: %s", self.port, err)

    @staticmethod
    def _first_byte(packet: SerialPacket) -> int:
        if not packet.data:
            raise ImprovProtocolError(f"Empty {packet.packet_type} packet")
        return packet.data[0]
