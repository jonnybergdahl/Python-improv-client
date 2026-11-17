"""Protocol layer tests, using the examples from the Improv specification."""

import pytest

from improv_client.exceptions import ImprovProtocolError
from improv_client.protocol import (
    SERIAL_HEADER,
    Capabilities,
    Command,
    DeviceInfo,
    Error,
    NetworkFlags,
    NetworkState,
    PacketType,
    SerialPacket,
    SerialPacketDecoder,
    State,
    WifiNetwork,
    checksum,
    decode_ble_rpc_result,
    decode_rpc_result,
    decode_strings,
    encode_ble_rpc_command,
    encode_rpc_command,
    encode_serial_packet,
    encode_string,
    encode_strings,
    encode_wifi_settings,
    parse_service_data,
    parse_state,
    parse_wifi_networks,
)

SSID = "MyWirelessAP"
PASSWORD = "mysecurepassword"
# Spec example: 01 1E 0C {MyWirelessAP} 10 {mysecurepassword}
WIFI_COMMAND = bytes([0x01, 0x1E, 0x0C]) + SSID.encode() + bytes([0x10]) + PASSWORD.encode()


def rpc_result(command: int, strings: list[str]) -> bytes:
    data = encode_strings(strings)
    return bytes([command, len(data)]) + data


class TestEnums:
    def test_state_values(self):
        assert State.STOPPED == 0
        assert State.AUTHORIZATION_REQUIRED == 1
        assert State.READY is State.AUTHORIZED
        assert State.PROVISIONED == 4

    def test_unknown_state(self):
        with pytest.raises(ImprovProtocolError):
            parse_state(0x09)

    def test_error_parse(self):
        assert Error.parse(0x05) is Error.BAD_HOSTNAME
        assert Error.parse(0xFF) is Error.UNKNOWN
        assert Error.parse(0x10) is Error.UNKNOWN

    def test_command_alias(self):
        assert Command.REQUEST_CURRENT_STATE is Command.IDENTIFY


class TestEncoding:
    def test_wifi_settings_matches_spec(self):
        payload = encode_rpc_command(Command.WIFI_SETTINGS, encode_wifi_settings(SSID, PASSWORD))
        assert payload == WIFI_COMMAND
        assert payload[1] == 30

    def test_ble_command_appends_checksum(self):
        payload = encode_ble_rpc_command(
            Command.WIFI_SETTINGS, encode_wifi_settings(SSID, PASSWORD)
        )
        assert payload[:-1] == WIFI_COMMAND
        assert payload[-1] == sum(WIFI_COMMAND) & 0xFF

    def test_empty_commands(self):
        for command in (Command.DEVICE_INFO, Command.SCAN_WIFI, Command.HOSTNAME):
            assert encode_rpc_command(command) == bytes([command, 0])
            assert encode_ble_rpc_command(command) == bytes([command, 0, command])

    def test_set_hostname_is_raw_bytes(self):
        assert encode_rpc_command(Command.HOSTNAME, b"my-host") == b"\x05\x07my-host"

    def test_utf8_strings(self):
        assert encode_string("Kök") == b"\x04K\xc3\xb6k"
        assert decode_strings(b"\x04K\xc3\xb6k") == ["Kök"]

    def test_too_long(self):
        with pytest.raises(ValueError):
            encode_string("x" * 256)
        with pytest.raises(ValueError):
            encode_rpc_command(Command.HOSTNAME, b"x" * 256)
        encode_string("x" * 255)

    def test_checksum_wraps(self):
        assert checksum(b"\xff\xff\x03") == 0x01


class TestRpcResult:
    def test_decode(self):
        result = decode_rpc_result(rpc_result(0x01, ["http://192.168.1.10"]))
        assert result.command is Command.WIFI_SETTINGS
        assert result.strings == ("http://192.168.1.10",)

    def test_empty_list_allowed(self):
        assert decode_rpc_result(b"\x04\x00").strings == ()

    def test_unknown_command_kept_as_int(self):
        assert decode_rpc_result(b"\x42\x00").command == 0x42

    def test_truncated(self):
        with pytest.raises(ImprovProtocolError):
            decode_rpc_result(b"\x03\x10\x03abc")
        with pytest.raises(ImprovProtocolError):
            decode_rpc_result(b"\x03\x04\x05abc")  # string overruns data
        with pytest.raises(ImprovProtocolError):
            decode_rpc_result(b"\x03")

    def test_ble_checksum(self):
        body = rpc_result(0x05, ["host"])
        assert decode_ble_rpc_result(body + bytes([checksum(body)])).strings == ("host",)
        with pytest.raises(ImprovProtocolError):
            decode_ble_rpc_result(body + bytes([(checksum(body) + 1) & 0xFF]))

    def test_ble_truncated_notification_fails(self):
        body = rpc_result(0x04, ["Net", "-60", "WPA2"] * 5)
        full = body + bytes([checksum(body)])
        with pytest.raises(ImprovProtocolError):
            decode_ble_rpc_result(full[:20])


class TestSerialFraming:
    def test_frame_layout(self):
        frame = encode_serial_packet(PacketType.RPC_COMMAND, WIFI_COMMAND)
        assert frame[:6] == SERIAL_HEADER
        assert frame[6] == 1
        assert frame[7] == PacketType.RPC_COMMAND
        assert frame[8] == len(WIFI_COMMAND)
        assert frame[9:-1] == WIFI_COMMAND
        assert frame[-1] == sum(frame[:-1]) & 0xFF

    def test_round_trip(self):
        frame = encode_serial_packet(PacketType.RPC_RESULT, rpc_result(3, ["a", "b"]))
        assert SerialPacketDecoder().feed(frame) == [
            SerialPacket(PacketType.RPC_RESULT, rpc_result(3, ["a", "b"]))
        ]

    def test_byte_by_byte_with_log_noise(self):
        frames = [
            encode_serial_packet(PacketType.ERROR_STATE, b"\x00"),
            encode_serial_packet(PacketType.CURRENT_STATE, b"\x03"),
        ]
        stream = b"[D][wifi:123]: IMPRO noise\r\n" + frames[0] + b"\n[I] IMPROV?\n" + frames[1]
        decoder = SerialPacketDecoder()
        packets = []
        for byte in stream:
            packets += decoder.feed(bytes([byte]))
        assert [p.packet_type for p in packets] == [PacketType.ERROR_STATE, PacketType.CURRENT_STATE]
        assert packets[1].data == b"\x03"

    def test_bad_checksum_resyncs(self):
        good = encode_serial_packet(PacketType.CURRENT_STATE, b"\x04")
        bad = bytearray(encode_serial_packet(PacketType.CURRENT_STATE, b"\x02"))
        bad[-1] ^= 0xFF
        assert SerialPacketDecoder().feed(bytes(bad) + good) == [
            SerialPacket(PacketType.CURRENT_STATE, b"\x04")
        ]

    def test_wrong_version_skipped(self):
        bad = bytearray(encode_serial_packet(PacketType.CURRENT_STATE, b"\x02"))
        bad[6] = 2
        good = encode_serial_packet(PacketType.CURRENT_STATE, b"\x04")
        assert len(SerialPacketDecoder().feed(bytes(bad) + good)) == 1

    def test_partial_frame_waits(self):
        frame = encode_serial_packet(PacketType.RPC_RESULT, rpc_result(4, []))
        decoder = SerialPacketDecoder()
        assert decoder.feed(frame[:-1]) == []
        assert len(decoder.feed(frame[-1:])) == 1

    def test_unknown_packet_type_kept(self):
        frame = encode_serial_packet(0x09, b"")
        assert SerialPacketDecoder().feed(frame)[0].packet_type == 0x09


class TestResults:
    def test_device_info_four_strings(self):
        info = DeviceInfo.from_strings(
            ["ESPHome", "2021.11.0", "esp32-s3-devkitc-1/esp32-s3", "Temperature Monitor"]
        )
        assert info.device_name == "Temperature Monitor"
        assert info.os_name is None and info.os_version is None

    def test_device_info_six_strings(self):
        info = DeviceInfo.from_strings(
            ["Bluetooth Proxy", "v1.0.0", "denky_d4/esp32", "My Bluetooth Proxy",
             "ESPHome", "2025.12.2"]
        )
        assert info.firmware_name == "Bluetooth Proxy"
        assert (info.os_name, info.os_version) == ("ESPHome", "2025.12.2")

    def test_device_info_too_short(self):
        with pytest.raises(ImprovProtocolError):
            DeviceInfo.from_strings(["a", "b", "c"])

    def test_serial_network(self):
        network = WifiNetwork.from_strings(["MyWirelessNetwork", "-60", "YES"])
        assert network.rssi == -60 and network.secured

    def test_ble_networks(self):
        networks = parse_wifi_networks(
            ["MyWirelessNetwork", "-60", "WPA2", "MyOtherWirelessNetwork", "-52", "WPA/WPA2",
             "Open", "-70", "NO"]
        )
        assert [n.ssid for n in networks] == ["MyWirelessNetwork", "MyOtherWirelessNetwork", "Open"]
        assert networks[1].auth == "WPA/WPA2"
        assert [n.secured for n in networks] == [True, True, False]

    def test_ble_networks_empty_and_invalid(self):
        assert parse_wifi_networks([]) == []
        with pytest.raises(ImprovProtocolError):
            parse_wifi_networks(["a", "-1"])
        with pytest.raises(ImprovProtocolError):
            WifiNetwork.from_strings(["a", "strong", "NO"])

    def test_network_state(self):
        state = NetworkState.from_strings(["7", "http://192.168.1.10"])
        assert state.online
        assert state.flags == NetworkFlags.ONLINE | NetworkFlags.WIFI | NetworkFlags.ETHERNET
        assert state.urls == ("http://192.168.1.10",)

    def test_network_state_offline(self):
        state = NetworkState.from_strings(["2"])
        assert not state.online and state.urls == ()
        with pytest.raises(ImprovProtocolError):
            NetworkState.from_strings([])

    def test_service_data(self):
        data = parse_service_data(bytes([0x01, 0x0F, 0, 0, 0, 0]))
        assert data.state is State.AUTHORIZATION_REQUIRED
        assert Capabilities.SCAN_WIFI in data.capabilities
        assert Capabilities.HOSTNAME in data.capabilities
        with pytest.raises(ImprovProtocolError):
            parse_service_data(b"\x01")
