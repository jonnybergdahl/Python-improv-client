"""Improv Wi-Fi over Bluetooth Low Energy.

Requires the ``ble`` extra: ``pip install improv-client[ble]``. Built on
`bleak <https://github.com/hbldh/bleak>`_ and
`bleak-retry-connector <https://github.com/Bluetooth-Devices/bleak-retry-connector>`_.

The client connects on demand: every command connects first if needed, and the
connection closes after a period without commands (90 seconds by default). This
follows the design of
`py-improv-ble-client <https://github.com/home-assistant-libs/py-improv-ble-client>`_
(MIT), as do the retrying connector and matching results to commands by command id.

Example::

    import asyncio
    from improv_client.ble import ImprovBleClient, discover
    from improv_client.protocol import State

    async def main():
        devices = await discover()
        if not devices:
            return
        async with ImprovBleClient(devices[0].device) as client:
            if client.state is State.AUTHORIZATION_REQUIRED:
                print("Press the button on the device")
                await client.wait_for_state(State.AUTHORIZED, timeout=60)
            url = await client.provision("MyWirelessAP", "mysecurepassword")
            print("Provisioned, next step:", url)

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bleak import BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from .client import DEFAULT_TIMEOUT, ImprovClient
from .exceptions import (
    ImprovConnectionError,
    ImprovDeviceError,
    ImprovNotSupportedError,
    ImprovProtocolError,
)
from .protocol import (
    BLE_CAPABILITIES_UUID,
    BLE_CURRENT_STATE_UUID,
    BLE_ERROR_STATE_UUID,
    BLE_RPC_COMMAND_UUID,
    BLE_RPC_RESULT_UUID,
    BLE_SERVICE_DATA_UUID,
    BLE_SERVICE_UUID,
    Capabilities,
    Command,
    Error,
    ServiceData,
    State,
    decode_ble_rpc_result,
    encode_ble_rpc_command,
    parse_service_data,
    parse_state,
)

__all__ = [
    "DEFAULT_IDLE_DISCONNECT_DELAY",
    "DiscoveredDevice",
    "ImprovBleClient",
    "discover",
    "find_device",
    "is_improv_advertisement",
    "service_data_from_advertisement",
]

_LOGGER = logging.getLogger(__name__)

#: Seconds without commands before the client disconnects.
DEFAULT_IDLE_DISCONNECT_DELAY = 90.0

_REQUIRED_CAPABILITY = {
    Command.IDENTIFY: Capabilities.IDENTIFY,
    Command.DEVICE_INFO: Capabilities.DEVICE_INFO,
    Command.SCAN_WIFI: Capabilities.SCAN_WIFI,
    Command.HOSTNAME: Capabilities.HOSTNAME,
}


def _normalize(uuid: str) -> str:
    return uuid.lower()


def is_improv_advertisement(advertisement: AdvertisementData) -> bool:
    """``True`` if the advertisement carries the Improv service UUID or service data."""
    return BLE_SERVICE_UUID in map(_normalize, advertisement.service_uuids) or any(
        _normalize(uuid) == BLE_SERVICE_DATA_UUID for uuid in advertisement.service_data
    )


def service_data_from_advertisement(advertisement: AdvertisementData) -> ServiceData | None:
    """Return the parsed Improv service data, or ``None`` if it's missing or invalid."""
    for uuid, data in advertisement.service_data.items():
        if _normalize(uuid) == BLE_SERVICE_DATA_UUID:
            try:
                return parse_service_data(data)
            except ImprovProtocolError as err:
                _LOGGER.debug("Invalid Improv service data %s: %s", data.hex(), err)
    return None


@dataclass(frozen=True)
class DiscoveredDevice:
    """An Improv device found by :func:`discover`.

    :ivar device: The bleak device, pass this to :class:`ImprovBleClient`.
    :ivar name: Advertised name, if any.
    :ivar rssi: Signal strength in dBm.
    :ivar service_data: State and capabilities from the advertisement, if present.
    """

    device: BLEDevice
    name: str | None
    rssi: int | None
    service_data: ServiceData | None

    @property
    def address(self) -> str:
        """The Bluetooth address (a UUID on macOS)."""
        return self.device.address


async def discover(timeout: float = 5.0, **scanner_kwargs: Any) -> list[DiscoveredDevice]:
    """Scan for Improv devices.

    :param timeout: Seconds to scan.
    :param scanner_kwargs: Extra arguments for :class:`bleak.BleakScanner`.
    :return: The devices found, strongest signal first.
    """
    found = await BleakScanner.discover(timeout=timeout, return_adv=True, **scanner_kwargs)
    devices = [
        DiscoveredDevice(
            device=device,
            name=advertisement.local_name or device.name,
            rssi=advertisement.rssi,
            service_data=service_data_from_advertisement(advertisement),
        )
        for device, advertisement in found.values()
        if is_improv_advertisement(advertisement)
    ]
    devices.sort(key=lambda d: d.rssi if d.rssi is not None else -1000, reverse=True)
    return devices


async def find_device(address: str, timeout: float = 10.0) -> BLEDevice | None:
    """Find a device by address (a UUID on macOS), or ``None`` if not seen in time."""
    return await BleakScanner.find_device_by_address(address, timeout=timeout)


class ImprovBleClient(ImprovClient):
    """Client for an Improv Wi-Fi device over BLE.

    Commands connect automatically, so calling :meth:`connect` first is optional.

    :param device: The device, from :func:`discover`, :func:`find_device` or a bleak scanner.
    :param timeout: Default time in seconds to wait for an answer.
    :param idle_disconnect_delay: Seconds without commands before disconnecting, or
        ``None`` to stay connected until :meth:`disconnect`.
    :param disconnected_callback: Called with the client whenever the connection closes,
        including idle and explicit disconnects.
    """

    def __init__(
        self,
        device: BLEDevice,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        idle_disconnect_delay: float | None = DEFAULT_IDLE_DISCONNECT_DELAY,
        disconnected_callback: Callable[[ImprovBleClient], None] | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self._device = device
        self.idle_disconnect_delay = idle_disconnect_delay
        self._disconnected_callback = disconnected_callback
        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._capabilities: Capabilities | None = None
        self._write_with_response = True
        self._idle_handle: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task] = set()

    @property
    def address(self) -> str:
        """The Bluetooth address of the device."""
        return self._device.address

    @property
    def capabilities(self) -> Capabilities | None:
        """Capabilities read from the device, or ``None`` before the first connection."""
        return self._capabilities

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, device: BLEDevice) -> None:
        """Replace the device used for the next connection, e.g. from a newer advertisement."""
        self._device = device

    async def connect(self) -> None:
        """Connect, read capabilities, state and error, and subscribe to notifications.

        :raises ImprovConnectionError: If connecting fails or the device has no Improv service.
        """
        async with self._connect_lock:
            if self.connected:
                return
            self._cancel_idle_timer()
            name = self._device.name or self._device.address
            _LOGGER.debug("Connecting to %s", name)
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    self._device,
                    name,
                    disconnected_callback=self._on_disconnected,
                )
            except (BleakError, asyncio.TimeoutError) as err:
                raise ImprovConnectionError(f"Can't connect to {name}: {err}") from err
            try:
                state, error = await self._set_up(client)
            except BaseException:
                await self._safe_disconnect(client)
                raise
            self._client = client
            _LOGGER.debug("Connected to %s, capabilities %r", name, self._capabilities)
            self._handle_state(state)
            self._handle_error(error)
        self._schedule_idle_timer()

    async def _set_up(self, client: BleakClientWithServiceCache) -> tuple[State, Error]:
        service = client.services.get_service(BLE_SERVICE_UUID)
        if service is None:
            raise ImprovConnectionError(f"{self.address} has no Improv service")
        command_char = service.get_characteristic(BLE_RPC_COMMAND_UUID)
        if command_char is None:
            raise ImprovConnectionError(f"{self.address} has no RPC command characteristic")
        self._write_with_response = "write" in command_char.properties
        try:
            if service.get_characteristic(BLE_CAPABILITIES_UUID) is not None:
                raw = await client.read_gatt_char(BLE_CAPABILITIES_UUID)
                self._capabilities = Capabilities(raw[0] if raw else 0)
            else:
                self._capabilities = Capabilities(0)
            state = parse_state((await client.read_gatt_char(BLE_CURRENT_STATE_UUID))[0])
            raw_error = await client.read_gatt_char(BLE_ERROR_STATE_UUID)
            error = Error.parse(raw_error[0]) if raw_error else Error.NONE
            await client.start_notify(BLE_CURRENT_STATE_UUID, self._on_state_notification)
            await client.start_notify(BLE_ERROR_STATE_UUID, self._on_error_notification)
            await client.start_notify(BLE_RPC_RESULT_UUID, self._on_result_notification)
        except (BleakError, IndexError, ImprovProtocolError) as err:
            raise ImprovConnectionError(f"Setting up {self.address} failed: {err}") from err
        return state, error

    async def disconnect(self) -> None:
        """Disconnect from the device."""
        self._cancel_idle_timer()
        async with self._connect_lock:
            client = self._client
            if client is None:
                return
            await self._safe_disconnect(client)
            # The disconnected callback normally clears this; make sure it happens.
            if self._client is client:
                self._on_disconnected(client)

    async def identify(self) -> None:
        """Ask the device to identify itself, e.g. by blinking a LED.

        :raises ImprovNotSupportedError: If the device lacks the identify capability.
        """
        async with self._operation_lock:
            await self._before_operation()
            try:
                self._check_supported(Command.IDENTIFY)
                await self._send_rpc_command(Command.IDENTIFY, b"")
            finally:
                self._after_operation()

    # --- Transport hooks ----------------------------------------------------------

    async def _before_operation(self) -> None:
        self._cancel_idle_timer()
        await self.connect()
        self._cancel_idle_timer()

    def _after_operation(self) -> None:
        self._schedule_idle_timer()

    def _check_supported(self, command: int) -> None:
        required = _REQUIRED_CAPABILITY.get(command)
        if required is not None and self._capabilities is not None:
            if required not in self._capabilities:
                raise ImprovNotSupportedError(
                    f"The device doesn't advertise the {required.name} capability"
                )
        if command == Command.WIFI_SETTINGS and self._state is State.AUTHORIZATION_REQUIRED:
            raise ImprovDeviceError(
                Error.NOT_AUTHORIZED,
                "The device needs authorization first, wait_for_state(State.AUTHORIZED)",
            )

    async def _send_rpc_command(self, command: int, data: bytes) -> None:
        client = self._client
        if client is None:
            raise ImprovConnectionError("Not connected")
        payload = encode_ble_rpc_command(command, data)
        _LOGGER.debug("> %s", payload.hex(" "))
        try:
            await client.write_gatt_char(
                BLE_RPC_COMMAND_UUID, payload, response=self._write_with_response
            )
        except BleakError as err:
            raise ImprovConnectionError(f"Writing the command failed: {err}") from err

    # --- Idle disconnect ----------------------------------------------------------

    def _schedule_idle_timer(self) -> None:
        self._cancel_idle_timer()
        if self.idle_disconnect_delay is None or not self.connected:
            return
        self._idle_handle = asyncio.get_running_loop().call_later(
            self.idle_disconnect_delay, self._idle_timeout
        )

    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _idle_timeout(self) -> None:
        self._idle_handle = None
        if self._operation_lock.locked():
            return  # The running operation reschedules the timer when it's done.
        _LOGGER.debug("Disconnecting from %s after being idle", self.address)
        self._create_task(self.disconnect())

    def _create_task(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # --- Bleak callbacks ----------------------------------------------------------

    @staticmethod
    async def _safe_disconnect(client: BleakClientWithServiceCache) -> None:
        try:
            await client.disconnect()
        except BleakError as err:
            _LOGGER.debug("Error while disconnecting: %s", err)

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if self._client is not client:
            return
        _LOGGER.debug("Disconnected from %s", self.address)
        self._client = None
        self._cancel_idle_timer()
        self._handle_connection_lost()
        if self._disconnected_callback is not None:
            self._run_callback(self._disconnected_callback, self)

    def _on_state_notification(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if not data:
            return
        try:
            self._handle_state(parse_state(data[0]))
        except ImprovProtocolError as err:
            _LOGGER.warning("Invalid state from %s: %s", self.address, err)

    def _on_error_notification(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        if data:
            self._handle_error(Error.parse(data[0]))

    def _on_result_notification(self, _char: BleakGATTCharacteristic, data: bytearray) -> None:
        _LOGGER.debug("< %s", bytes(data).hex(" "))
        try:
            result = decode_ble_rpc_result(data)
        except ImprovProtocolError as err:
            # A notification is limited by the MTU; a long result (e.g. a scan) can
            # arrive truncated. The characteristic value itself is complete, so read it.
            _LOGGER.debug("Incomplete RPC result notification (%s), reading it", err)
            self._create_task(self._read_rpc_result())
            return
        self._handle_rpc_result(result)

    async def _read_rpc_result(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            data = await client.read_gatt_char(BLE_RPC_RESULT_UUID)
            result = decode_ble_rpc_result(data)
        except (BleakError, ImprovProtocolError) as err:
            _LOGGER.warning("Can't read the RPC result from %s: %s", self.address, err)
            return
        self._handle_rpc_result(result)
