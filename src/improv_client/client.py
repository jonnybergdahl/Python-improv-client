"""Shared client logic for the serial and BLE transports.

:class:`ImprovClient` holds the API both transports have in common. Use
:class:`~improv_client.serial.ImprovSerialClient` or
:class:`~improv_client.ble.ImprovBleClient` to talk to a device.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import TracebackType

from .exceptions import (
    ImprovConnectionError,
    ImprovDeviceError,
    ImprovError,
    ImprovNotSupportedError,
    ImprovProtocolError,
    ImprovTimeoutError,
)
from .protocol import (
    Command,
    DeviceInfo,
    Error,
    RpcResult,
    State,
    WifiNetwork,
    encode_wifi_settings,
    parse_wifi_networks,
)

_LOGGER = logging.getLogger(__name__)

#: Default time to wait for an answer, in seconds.
DEFAULT_TIMEOUT = 10.0
#: Default time to wait for provisioning to finish, in seconds.
DEFAULT_PROVISION_TIMEOUT = 60.0
#: Default time to wait for a Wi-Fi scan, in seconds.
DEFAULT_SCAN_TIMEOUT = 30.0

StateCallback = Callable[[State], None]
ErrorCallback = Callable[[Error], None]


@dataclass
class _PendingRpc:
    command: int
    future: asyncio.Future[list[tuple[str, ...]]]
    multiple: bool
    results: list[tuple[str, ...]] = field(default_factory=list)


class ImprovClient(ABC):
    """Base class with the API shared by all transports.

    Only one command runs at a time; concurrent calls wait for each other.
    The client can be used as an async context manager, which connects on entry and
    disconnects on exit.

    :param timeout: Default time in seconds to wait for an answer.
    """

    #: Serial sends one RPC result per network and ends with an empty one.
    #: BLE sends all networks in a single result.
    _scan_multiple_results = False

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._state: State | None = None
        self._error = Error.NONE
        self._operation_lock = asyncio.Lock()
        self._pending: _PendingRpc | None = None
        self._state_request: asyncio.Future[State] | None = None
        self._state_waiters: list[tuple[frozenset[State], asyncio.Future[State]]] = []
        self._state_callbacks: list[StateCallback] = []
        self._error_callbacks: list[ErrorCallback] = []

    # --- Connection ---------------------------------------------------------------

    @property
    @abstractmethod
    def connected(self) -> bool:
        """``True`` while connected to the device."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the device.

        :raises ImprovConnectionError: If the connection fails.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the device. Does nothing if not connected."""

    async def __aenter__(self) -> ImprovClient:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    # --- State --------------------------------------------------------------------

    @property
    def state(self) -> State | None:
        """The last state reported by the device, or ``None`` if none has been seen."""
        return self._state

    @property
    def error(self) -> Error:
        """The last error state reported by the device."""
        return self._error

    def register_state_callback(self, callback: StateCallback) -> Callable[[], None]:
        """Call ``callback`` whenever the device state changes.

        :return: A function that removes the callback again.
        """
        self._state_callbacks.append(callback)
        return lambda: self._remove(self._state_callbacks, callback)

    def register_error_callback(self, callback: ErrorCallback) -> Callable[[], None]:
        """Call ``callback`` whenever the device error state changes.

        :return: A function that removes the callback again.
        """
        self._error_callbacks.append(callback)
        return lambda: self._remove(self._error_callbacks, callback)

    @staticmethod
    def _remove(callbacks: list, callback: Callable) -> None:
        if callback in callbacks:
            callbacks.remove(callback)

    async def wait_for_state(self, *states: State, timeout: float | None = None) -> State:
        """Wait until the device reports one of ``states``.

        Returns at once if the current state already matches. On BLE, use this to wait
        for the user to authorize the device:
        ``await client.wait_for_state(State.AUTHORIZED, timeout=60)``.

        :param timeout: Seconds to wait, or ``None`` to wait forever.
        :return: The matching state.
        :raises ImprovTimeoutError: If no matching state arrives in time.
        :raises ImprovConnectionError: If the connection is lost while waiting.
        """
        if not states:
            raise ValueError("Pass at least one state")
        await self._before_operation()
        try:
            if self._state in states:
                return self._state
            future: asyncio.Future[State] = asyncio.get_running_loop().create_future()
            entry = (frozenset(states), future)
            self._state_waiters.append(entry)
            try:
                return await self._wait(future, timeout, "state " + "/".join(s.name for s in states))
            finally:
                if entry in self._state_waiters:
                    self._state_waiters.remove(entry)
        finally:
            self._after_operation()

    # --- Commands -----------------------------------------------------------------

    async def provision(
        self, ssid: str, password: str, *, timeout: float = DEFAULT_PROVISION_TIMEOUT
    ) -> str | None:
        """Send Wi-Fi credentials and wait until the device has connected.

        :param ssid: Network name.
        :param password: Network password, empty for an open network.
        :param timeout: Seconds to wait for the device to connect.
        :return: The URL the device wants the user to open next, or ``None``.
        :raises ImprovDeviceError: With :attr:`~improv_client.protocol.Error.UNABLE_TO_CONNECT`
            if the credentials didn't work, or
            :attr:`~improv_client.protocol.Error.NOT_AUTHORIZED` on an unauthorized BLE device.
        """
        results = await self._rpc(
            Command.WIFI_SETTINGS, encode_wifi_settings(ssid, password), timeout=timeout
        )
        strings = results[0]
        return strings[0] if strings and strings[0] else None

    async def get_device_info(self) -> DeviceInfo:
        """Request firmware, hardware and device name information."""
        results = await self._rpc(Command.DEVICE_INFO)
        return DeviceInfo.from_strings(results[0])

    async def scan_wifi(self, *, timeout: float = DEFAULT_SCAN_TIMEOUT) -> list[WifiNetwork]:
        """Ask the device to scan for Wi-Fi networks.

        :param timeout: Seconds to wait for the scan to finish.
        :return: The networks found, in the order the device sent them.
        """
        results = await self._rpc(
            Command.SCAN_WIFI, timeout=timeout, multiple=self._scan_multiple_results
        )
        if self._scan_multiple_results:
            return [WifiNetwork.from_strings(strings) for strings in results]
        return parse_wifi_networks(results[0])

    async def get_hostname(self) -> str:
        """Request the device hostname."""
        return self._first_string(await self._rpc(Command.HOSTNAME), "Hostname")

    async def set_hostname(self, hostname: str) -> str:
        """Set the device hostname.

        The device must be authorized. Hostnames may contain letters, digits and
        hyphens (RFC 1123).

        :return: The hostname reported back by the device.
        :raises ImprovDeviceError: With :attr:`~improv_client.protocol.Error.BAD_HOSTNAME`
            if the device rejects the hostname.
        """
        data = self._encode_value(hostname, "Hostname")
        return self._first_string(await self._rpc(Command.HOSTNAME, data), "Hostname")

    async def get_device_name(self) -> str:
        """Request the user-facing device name."""
        return self._first_string(await self._rpc(Command.DEVICE_NAME), "Device name")

    async def set_device_name(self, name: str) -> str:
        """Set the user-facing device name.

        The device must be authorized. If you set both, set the device name before
        the hostname.

        :return: The device name reported back by the device.
        """
        data = self._encode_value(name, "Device name")
        return self._first_string(await self._rpc(Command.DEVICE_NAME, data), "Device name")

    @staticmethod
    def _encode_value(value: str, what: str) -> bytes:
        # An empty value would turn the set into a get.
        data = value.encode("utf-8")
        if not data:
            raise ValueError(f"{what} can't be empty")
        if len(data) > 255:
            raise ValueError(f"{what} is {len(data)} bytes, the maximum is 255")
        return data

    @staticmethod
    def _first_string(results: Sequence[tuple[str, ...]], what: str) -> str:
        strings = results[0]
        if not strings:
            raise ImprovProtocolError(f"{what} result is empty")
        return strings[0]

    # --- Transport hooks ----------------------------------------------------------

    @abstractmethod
    async def _send_rpc_command(self, command: int, data: bytes) -> None:
        """Write an RPC command to the device."""

    async def _before_operation(self) -> None:
        """Called before each operation, e.g. to connect or check the connection."""

    def _after_operation(self) -> None:
        """Called after each operation, e.g. to start an idle timer."""

    def _check_supported(self, command: int) -> None:
        """Raise if ``command`` can't be sent right now. Called after connecting."""

    # --- RPC machinery ------------------------------------------------------------

    async def _rpc(
        self,
        command: int,
        data: bytes = b"",
        *,
        timeout: float | None = None,
        multiple: bool = False,
    ) -> list[tuple[str, ...]]:
        """Send a command and wait for its result.

        :param multiple: Collect results until an empty one arrives (serial scan).
        :return: The strings of each result received.
        """
        async with self._operation_lock:
            await self._before_operation()
            try:
                self._check_supported(command)
                future: asyncio.Future[list[tuple[str, ...]]]
                future = asyncio.get_running_loop().create_future()
                self._pending = _PendingRpc(command, future, multiple)
                try:
                    await self._send_rpc_command(command, data)
                    return await self._wait(
                        future, self._resolve_timeout(timeout), f"result of {_name(command)}"
                    )
                finally:
                    self._pending = None
            finally:
                self._after_operation()

    async def _request_next_state(self, command: int, timeout: float | None = None) -> State:
        """Send a command and wait for the next state report (serial 0x02)."""
        async with self._operation_lock:
            await self._before_operation()
            try:
                future: asyncio.Future[State] = asyncio.get_running_loop().create_future()
                self._state_request = future
                try:
                    await self._send_rpc_command(command, b"")
                    return await self._wait(future, self._resolve_timeout(timeout), "current state")
                finally:
                    self._state_request = None
            finally:
                self._after_operation()

    @staticmethod
    async def _wait(future: asyncio.Future, timeout: float | None, what: str):
        """Wait for ``future``; ``timeout=None`` waits forever."""
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise ImprovTimeoutError(f"Timed out waiting for {what}") from None

    def _resolve_timeout(self, timeout: float | None) -> float:
        return self.timeout if timeout is None else timeout

    # --- Called by the transports when data arrives ------------------------------

    def _handle_state(self, state: State) -> None:
        changed = state != self._state
        self._state = state
        _LOGGER.debug("State: %s", state.name)
        request = self._state_request
        if request is not None and not request.done():
            request.set_result(state)
        for states, future in list(self._state_waiters):
            if state in states and not future.done():
                future.set_result(state)
        if changed:
            for callback in list(self._state_callbacks):
                self._run_callback(callback, state)

    def _handle_error(self, error: Error) -> None:
        changed = error != self._error
        self._error = error
        if error is not Error.NONE:
            _LOGGER.debug("Error state: %s", error.name)
            self._fail_pending(self._exception_for(error))
        if changed:
            for callback in list(self._error_callbacks):
                self._run_callback(callback, error)

    def _handle_rpc_result(self, result: RpcResult) -> None:
        _LOGGER.debug("RPC result for %s: %s", _name(result.command), result.strings)
        pending = self._pending
        if pending is None or pending.future.done() or result.command != pending.command:
            _LOGGER.debug("Ignoring RPC result nobody is waiting for")
            return
        if not pending.multiple:
            pending.future.set_result([result.strings])
        elif result.strings:
            pending.results.append(result.strings)
        else:
            pending.future.set_result(pending.results)

    def _handle_connection_lost(self, exc: BaseException | None = None) -> None:
        message = "Connection to the device was lost"
        if exc is not None:
            message += f": {exc}"
        error = ImprovConnectionError(message)
        self._fail_pending(error)
        for _states, future in list(self._state_waiters):
            if not future.done():
                future.set_exception(error)

    def _fail_pending(self, exc: ImprovError) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(exc)
        request = self._state_request
        if request is not None and not request.done():
            request.set_exception(exc)

    @staticmethod
    def _exception_for(error: Error) -> ImprovError:
        if error is Error.UNKNOWN_RPC_COMMAND:
            return ImprovNotSupportedError("The device doesn't support this command")
        return ImprovDeviceError(error)

    @staticmethod
    def _run_callback(callback: Callable, value: object) -> None:
        try:
            callback(value)
        except Exception:  # noqa: BLE001 - a broken callback mustn't break the client
            _LOGGER.exception("Error in callback %r", callback)


def _name(command: int) -> str:
    return command.name if isinstance(command, Command) else f"command 0x{command:02x}"
