"""Transport-independent parts of the Improv Wi-Fi protocol.

This module has no third-party dependencies. It contains the protocol constants and
enums, the RPC encoding and decoding shared by serial and BLE, the serial frame codec,
BLE service data parsing, and the dataclasses returned by the clients.

Specifications:

- Serial: https://www.improv-wifi.com/serial/
- BLE: https://www.improv-wifi.com/ble/

Example, encoding the Wi-Fi settings command from the specification:

>>> payload = encode_rpc_command(Command.WIFI_SETTINGS,
...                              encode_wifi_settings("MyWirelessAP", "mysecurepassword"))
>>> payload[:3].hex(" ")
'01 1e 0c'
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum, IntFlag

from .exceptions import ImprovProtocolError

__all__ = [
    "BLE_CAPABILITIES_UUID",
    "BLE_CURRENT_STATE_UUID",
    "BLE_ERROR_STATE_UUID",
    "BLE_RPC_COMMAND_UUID",
    "BLE_RPC_RESULT_UUID",
    "BLE_SERVICE_DATA_UUID",
    "BLE_SERVICE_UUID",
    "SERIAL_HEADER",
    "SERIAL_VERSION",
    "Capabilities",
    "Command",
    "DeviceInfo",
    "Error",
    "NetworkFlags",
    "NetworkState",
    "PacketType",
    "RpcResult",
    "SerialPacket",
    "SerialPacketDecoder",
    "ServiceData",
    "State",
    "WifiNetwork",
    "checksum",
    "decode_ble_rpc_result",
    "decode_rpc_result",
    "decode_strings",
    "encode_ble_rpc_command",
    "encode_rpc_command",
    "encode_serial_packet",
    "encode_string",
    "encode_strings",
    "encode_wifi_settings",
    "parse_service_data",
    "parse_state",
    "parse_wifi_networks",
]

#: Header that starts every serial frame.
SERIAL_HEADER = b"IMPROV"
#: Serial protocol version this library speaks.
SERIAL_VERSION = 1

#: Improv GATT service.
BLE_SERVICE_UUID = "00467768-6228-2272-4663-277478268000"
#: Current state characteristic (read, notify).
BLE_CURRENT_STATE_UUID = "00467768-6228-2272-4663-277478268001"
#: Error state characteristic (read, notify).
BLE_ERROR_STATE_UUID = "00467768-6228-2272-4663-277478268002"
#: RPC command characteristic (write).
BLE_RPC_COMMAND_UUID = "00467768-6228-2272-4663-277478268003"
#: RPC result characteristic (read, notify).
BLE_RPC_RESULT_UUID = "00467768-6228-2272-4663-277478268004"
#: Capabilities characteristic (read).
BLE_CAPABILITIES_UUID = "00467768-6228-2272-4663-277478268005"
#: UUID of the service data in the advertisement (16-bit ``4677``).
BLE_SERVICE_DATA_UUID = "00004677-0000-1000-8000-00805f9b34fb"

_MAX_FIELD_LENGTH = 255


class State(IntEnum):
    """Current provisioning state of the device."""

    #: Provisioning is unavailable, for example because Wi-Fi is turned off (serial only).
    STOPPED = 0x00
    #: Waiting for authorization through physical interaction (BLE only).
    AUTHORIZATION_REQUIRED = 0x01
    #: Ready to accept credentials.
    AUTHORIZED = 0x02
    #: Credentials received, trying to connect.
    PROVISIONING = 0x03
    #: Connected to the network.
    PROVISIONED = 0x04
    #: Alias of :attr:`AUTHORIZED`, the name the serial specification uses.
    READY = 0x02


class Error(IntEnum):
    """Error state reported by the device."""

    NONE = 0x00
    INVALID_RPC_PACKET = 0x01
    UNKNOWN_RPC_COMMAND = 0x02
    UNABLE_TO_CONNECT = 0x03
    #: Credentials were sent but the device isn't authorized (BLE only).
    NOT_AUTHORIZED = 0x04
    BAD_HOSTNAME = 0x05
    UNKNOWN = 0xFF

    @classmethod
    def parse(cls, value: int) -> Error:
        """Convert a raw error byte, mapping unknown values to :attr:`UNKNOWN`.

        >>> Error.parse(3).name
        'UNABLE_TO_CONNECT'
        >>> Error.parse(0x42).name
        'UNKNOWN'
        """
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class Command(IntEnum):
    """RPC command identifiers."""

    WIFI_SETTINGS = 0x01
    #: Ask the device to identify itself, e.g. by blinking a LED (BLE only).
    IDENTIFY = 0x02
    DEVICE_INFO = 0x03
    SCAN_WIFI = 0x04
    HOSTNAME = 0x05
    DEVICE_NAME = 0x06
    #: Request the network state (serial only).
    NETWORK_STATE = 0x07
    #: Alias of :attr:`IDENTIFY`. On serial, command 0x02 requests the current state.
    REQUEST_CURRENT_STATE = 0x02


class Capabilities(IntFlag):
    """Capabilities advertised by a BLE device."""

    IDENTIFY = 0x01
    DEVICE_INFO = 0x02
    SCAN_WIFI = 0x04
    HOSTNAME = 0x08


class NetworkFlags(IntFlag):
    """Flags returned by :attr:`Command.NETWORK_STATE`."""

    #: The device currently has network connectivity.
    ONLINE = 0x01
    #: The device has a Wi-Fi interface (it may be disabled).
    WIFI = 0x02
    ETHERNET = 0x04
    THREAD = 0x08
    CELLULAR = 0x10


class PacketType(IntEnum):
    """Serial packet types."""

    CURRENT_STATE = 0x01
    ERROR_STATE = 0x02
    RPC_COMMAND = 0x03
    RPC_RESULT = 0x04


def parse_state(value: int) -> State:
    """Convert a raw state byte to a :class:`State`.

    :raises ImprovProtocolError: If the value isn't a known state.
    """
    try:
        return State(value)
    except ValueError:
        raise ImprovProtocolError(f"Unknown state 0x{value:02x}") from None


def checksum(data: bytes) -> int:
    """Return the Improv checksum: the sum of all bytes, keeping only the low byte.

    >>> checksum(bytes([0x03, 0x00]))
    3
    >>> checksum(bytes([0xff, 0x02]))
    1
    """
    return sum(data) & 0xFF


def _check_length(data: bytes, what: str) -> None:
    if len(data) > _MAX_FIELD_LENGTH:
        raise ValueError(f"{what} is {len(data)} bytes, the maximum is {_MAX_FIELD_LENGTH}")


def encode_string(value: str | bytes) -> bytes:
    """Encode a string as UTF-8 with a one byte length prefix.

    >>> encode_string("abc")
    b'\\x03abc'

    :raises ValueError: If the encoded string is longer than 255 bytes.
    """
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    _check_length(data, "String")
    return bytes([len(data)]) + data


def encode_strings(values: Iterable[str | bytes]) -> bytes:
    """Encode a sequence of length-prefixed strings."""
    return b"".join(encode_string(value) for value in values)


def decode_strings(data: bytes) -> list[str]:
    """Decode a sequence of length-prefixed UTF-8 strings.

    Invalid UTF-8 is replaced rather than rejected, since device names come from
    firmware we don't control.

    >>> decode_strings(b"\\x03abc\\x00\\x01d")
    ['abc', '', 'd']

    :raises ImprovProtocolError: If a length prefix runs past the end of the data.
    """
    strings = []
    index = 0
    while index < len(data):
        length = data[index]
        start = index + 1
        end = start + length
        if end > len(data):
            raise ImprovProtocolError(
                f"String at offset {index} needs {length} bytes, only {len(data) - start} left"
            )
        strings.append(data[start:end].decode("utf-8", errors="replace"))
        index = end
    return strings


def encode_wifi_settings(ssid: str, password: str) -> bytes:
    """Encode the data part of :attr:`Command.WIFI_SETTINGS`.

    >>> encode_wifi_settings("ab", "c")
    b'\\x02ab\\x01c'
    """
    return encode_strings((ssid, password))


def encode_rpc_command(command: int, data: bytes = b"") -> bytes:
    """Encode an RPC command: command, data length, data.

    This is the payload used inside a serial frame. Use
    :func:`encode_ble_rpc_command` for BLE, which adds a checksum.

    >>> encode_rpc_command(Command.DEVICE_INFO).hex(" ")
    '03 00'
    """
    data = bytes(data)
    _check_length(data, "RPC data")
    return bytes([command, len(data)]) + data


def encode_ble_rpc_command(command: int, data: bytes = b"") -> bytes:
    """Encode an RPC command for BLE, with the checksum appended.

    >>> encode_ble_rpc_command(Command.DEVICE_INFO).hex(" ")
    '03 00 03'
    """
    payload = encode_rpc_command(command, data)
    return payload + bytes([checksum(payload)])


@dataclass(frozen=True)
class RpcResult:
    """A decoded RPC result.

    :ivar command: The command this is a result for. A :class:`Command` when known.
    :ivar strings: The strings in the result. May be empty.
    """

    command: int
    strings: tuple[str, ...]


def _command(value: int) -> int:
    try:
        return Command(value)
    except ValueError:
        return value


def decode_rpc_result(payload: bytes) -> RpcResult:
    """Decode an RPC result without a checksum, as carried in a serial frame.

    >>> decode_rpc_result(bytes([0x05, 0x04, 0x03]) + b"abc")
    RpcResult(command=<Command.HOSTNAME: 5>, strings=('abc',))

    :raises ImprovProtocolError: If the payload is truncated or malformed.
    """
    payload = bytes(payload)
    if len(payload) < 2:
        raise ImprovProtocolError(f"RPC result too short ({len(payload)} bytes)")
    length = payload[1]
    if len(payload) < 2 + length:
        raise ImprovProtocolError(
            f"RPC result says {length} data bytes, only {len(payload) - 2} received"
        )
    return RpcResult(_command(payload[0]), tuple(decode_strings(payload[2 : 2 + length])))


def decode_ble_rpc_result(payload: bytes) -> RpcResult:
    """Decode an RPC result read from the BLE RPC result characteristic.

    The last byte is a checksum over everything before it.

    :raises ImprovProtocolError: If the checksum doesn't match or the payload is truncated.
    """
    payload = bytes(payload)
    if len(payload) < 3:
        raise ImprovProtocolError(f"BLE RPC result too short ({len(payload)} bytes)")
    body, received = payload[:-1], payload[-1]
    if checksum(body) != received:
        raise ImprovProtocolError(
            f"BLE RPC result checksum mismatch (got 0x{received:02x}, "
            f"expected 0x{checksum(body):02x})"
        )
    return decode_rpc_result(body)


# --- Serial framing ---------------------------------------------------------------


@dataclass(frozen=True)
class SerialPacket:
    """A decoded serial frame.

    :ivar packet_type: A :class:`PacketType` when known, otherwise the raw byte.
    :ivar data: The frame data, without header, length and checksum.
    """

    packet_type: int
    data: bytes


def encode_serial_packet(packet_type: int, data: bytes) -> bytes:
    """Encode a serial frame: ``IMPROV``, version, type, length, data, checksum.

    The checksum covers every byte from the header through the data. Clients write a
    newline after the frame; that isn't part of the frame and isn't added here.

    >>> encode_serial_packet(PacketType.RPC_COMMAND, bytes([0x03, 0x00])).hex(" ")
    '49 4d 50 52 4f 56 01 03 02 03 00 e6'
    """
    data = bytes(data)
    _check_length(data, "Serial packet data")
    frame = SERIAL_HEADER + bytes([SERIAL_VERSION, packet_type, len(data)]) + data
    return frame + bytes([checksum(frame)])


class SerialPacketDecoder:
    """Incremental decoder that finds Improv frames in a serial byte stream.

    Devices usually log text on the same port, so anything that isn't a valid frame
    is skipped. Feed it bytes as they arrive; it returns the complete frames found
    so far and keeps any partial frame for the next call.

    >>> decoder = SerialPacketDecoder()
    >>> frame = encode_serial_packet(PacketType.CURRENT_STATE, b"\\x02")
    >>> decoder.feed(b"[I][log]: hello\\n" + frame[:5])
    []
    >>> decoder.feed(frame[5:] + b"\\n")
    [SerialPacket(packet_type=<PacketType.CURRENT_STATE: 1>, data=b'\\x02')]
    """

    _PREFIX_LENGTH = len(SERIAL_HEADER) + 3  # header, version, type, length

    def __init__(self) -> None:
        self._buffer = bytearray()

    def reset(self) -> None:
        """Drop any buffered data."""
        self._buffer.clear()

    def feed(self, data: bytes) -> list[SerialPacket]:
        """Add received bytes and return the complete frames found."""
        buffer = self._buffer
        buffer += data
        packets: list[SerialPacket] = []
        while True:
            start = buffer.find(SERIAL_HEADER)
            if start < 0:
                # Keep a tail that could be the start of a header split across reads.
                keep = len(SERIAL_HEADER) - 1
                if len(buffer) > keep:
                    del buffer[: len(buffer) - keep]
                break
            if start:
                del buffer[:start]
            if len(buffer) < self._PREFIX_LENGTH:
                break
            if buffer[len(SERIAL_HEADER)] != SERIAL_VERSION:
                del buffer[:1]
                continue
            length = buffer[self._PREFIX_LENGTH - 1]
            end = self._PREFIX_LENGTH + length
            if len(buffer) < end + 1:
                break
            if buffer[end] != checksum(buffer[:end]):
                # Not a real frame (or a corrupted one); resync after this header.
                del buffer[:1]
                continue
            packet_type = buffer[len(SERIAL_HEADER) + 1]
            try:
                packet_type = PacketType(packet_type)
            except ValueError:
                pass
            packets.append(SerialPacket(packet_type, bytes(buffer[self._PREFIX_LENGTH : end])))
            del buffer[: end + 1]
        return packets


# --- Results ----------------------------------------------------------------------


def _require(strings: Sequence[str], count: int, what: str) -> None:
    if len(strings) < count:
        raise ImprovProtocolError(f"{what} needs at least {count} strings, got {len(strings)}")


@dataclass(frozen=True)
class DeviceInfo:
    """Device information returned by :attr:`Command.DEVICE_INFO`.

    :ivar firmware_name: Firmware name, e.g. ``ESPHome``.
    :ivar firmware_version: Firmware version.
    :ivar hardware: Hardware chip or variant, e.g. ``esp32-s3-devkitc-1/esp32-s3``.
    :ivar device_name: User-facing device name.
    :ivar os_name: Operating system name, sent by newer devices (e.g. ESPHome 2025.12
        and later, where ``firmware_name`` is the project name instead).
    :ivar os_version: Operating system version, when sent.
    """

    firmware_name: str
    firmware_version: str
    hardware: str
    device_name: str
    os_name: str | None = None
    os_version: str | None = None

    @classmethod
    def from_strings(cls, strings: Sequence[str]) -> DeviceInfo:
        """Build from the result strings.

        >>> DeviceInfo.from_strings(["ESPHome", "2021.11.0", "esp32", "Temp"]).os_name is None
        True

        :raises ImprovProtocolError: If fewer than 4 strings were returned.
        """
        _require(strings, 4, "Device info")
        return cls(
            firmware_name=strings[0],
            firmware_version=strings[1],
            hardware=strings[2],
            device_name=strings[3],
            os_name=strings[4] if len(strings) > 4 else None,
            os_version=strings[5] if len(strings) > 5 else None,
        )


@dataclass(frozen=True)
class WifiNetwork:
    """A network found by :attr:`Command.SCAN_WIFI`.

    :ivar ssid: Network name.
    :ivar rssi: Signal strength in dBm.
    :ivar auth: Authentication as reported by the device. Serial devices send ``YES``
        or ``NO``; BLE devices send a type such as ``WPA2`` or ``WPA/WPA2``, or ``NO``.
    """

    ssid: str
    rssi: int
    auth: str

    @property
    def secured(self) -> bool:
        """``True`` unless the device reported the network as open (``NO``)."""
        return self.auth.strip().upper() != "NO"

    @classmethod
    def from_strings(cls, strings: Sequence[str]) -> WifiNetwork:
        """Build from SSID, RSSI and auth strings.

        >>> WifiNetwork.from_strings(["MyWirelessNetwork", "-60", "YES"])
        WifiNetwork(ssid='MyWirelessNetwork', rssi=-60, auth='YES')

        :raises ImprovProtocolError: If there are fewer than 3 strings or the RSSI isn't a number.
        """
        _require(strings, 3, "Wi-Fi network")
        try:
            rssi = int(strings[1])
        except ValueError:
            raise ImprovProtocolError(f"Invalid RSSI {strings[1]!r}") from None
        return cls(strings[0], rssi, strings[2])


def parse_wifi_networks(strings: Sequence[str]) -> list[WifiNetwork]:
    """Parse a BLE scan result: SSID, RSSI and auth repeated for each network.

    :raises ImprovProtocolError: If the number of strings isn't a multiple of 3.
    """
    if len(strings) % 3:
        raise ImprovProtocolError(f"Scan result has {len(strings)} strings, not a multiple of 3")
    return [WifiNetwork.from_strings(strings[i : i + 3]) for i in range(0, len(strings), 3)]


@dataclass(frozen=True)
class NetworkState:
    """Network state returned by :attr:`Command.NETWORK_STATE`.

    :ivar flags: Network interface and connectivity flags.
    :ivar urls: URLs for the device, only present while online.
    """

    flags: NetworkFlags
    urls: tuple[str, ...] = ()

    @property
    def online(self) -> bool:
        """``True`` if the device currently has network connectivity."""
        return NetworkFlags.ONLINE in self.flags

    @classmethod
    def from_strings(cls, strings: Sequence[str]) -> NetworkState:
        """Build from the result strings: flags as a decimal string, then URLs.

        >>> NetworkState.from_strings(["7", "http://192.168.1.10"]).online
        True

        :raises ImprovProtocolError: If the flags are missing or not a number.
        """
        _require(strings, 1, "Network state")
        try:
            flags = int(strings[0])
        except ValueError:
            raise ImprovProtocolError(f"Invalid network flags {strings[0]!r}") from None
        return cls(NetworkFlags(flags & 0xFF), tuple(url for url in strings[1:] if url))


@dataclass(frozen=True)
class ServiceData:
    """Improv service data from a BLE advertisement.

    :ivar state: Current provisioning state.
    :ivar capabilities: Supported optional commands.
    """

    state: State
    capabilities: Capabilities


def parse_service_data(data: bytes) -> ServiceData:
    """Parse the Improv service data (UUID ``4677``) from a BLE advertisement.

    >>> parse_service_data(bytes([0x02, 0x03, 0, 0, 0, 0])).state.name
    'AUTHORIZED'

    :raises ImprovProtocolError: If the data is too short or the state is unknown.
    """
    if len(data) < 2:
        raise ImprovProtocolError(f"Service data too short ({len(data)} bytes)")
    return ServiceData(parse_state(data[0]), Capabilities(data[1]))
