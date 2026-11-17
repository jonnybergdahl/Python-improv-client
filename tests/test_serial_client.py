"""Serial client tests against a simulated device."""

import asyncio

import pytest

import improv_client.serial as improv_serial
from improv_client import (
    Error,
    ImprovConnectionError,
    ImprovDeviceError,
    ImprovNotSupportedError,
    ImprovTimeoutError,
    State,
)
from improv_client.protocol import (
    Command,
    PacketType,
    SerialPacketDecoder,
    decode_strings,
    encode_serial_packet,
    encode_strings,
)
from improv_client.serial import ImprovSerialClient


class FakeDevice(asyncio.Transport):
    """Transport that answers like an Improv serial device, with log noise in between."""

    def __init__(self, protocol):
        super().__init__()
        self.protocol = protocol
        self.decoder = SerialPacketDecoder()
        self.commands = []
        self.raw_writes = []
        self.state = State.READY
        self.networks = [("Home", "-50", "YES"), ("Guest", "-70", "NO")]
        self.hostname = "esp-device"
        self.answer = True
        self.closed = False

    # Transport API
    def write(self, data):
        self.raw_writes.append(bytes(data))
        for packet in self.decoder.feed(data):
            assert packet.packet_type is PacketType.RPC_COMMAND
            command, length = packet.data[0], packet.data[1]
            self.commands.append((command, packet.data[2 : 2 + length]))
            if self.answer:
                asyncio.get_running_loop().call_soon(self.handle, command, packet.data[2:])

    def close(self):
        if not self.closed:
            self.closed = True
            asyncio.get_running_loop().call_soon(self.protocol.connection_lost, None)

    # Device simulation
    def send(self, packet_type, data):
        self.protocol.data_received(b"[I][app]: log line\r\n")
        frame = encode_serial_packet(packet_type, data)
        # Split the frame to exercise reassembly.
        self.protocol.data_received(frame[:4])
        self.protocol.data_received(frame[4:] + b"\n")

    def send_state(self, state):
        self.send(PacketType.CURRENT_STATE, bytes([state]))

    def send_error(self, error):
        self.send(PacketType.ERROR_STATE, bytes([error]))

    def send_result(self, command, strings):
        data = encode_strings(strings)
        self.send(PacketType.RPC_RESULT, bytes([command, len(data)]) + data)

    def handle(self, command, data):
        self.send_error(Error.NONE)
        if command == Command.REQUEST_CURRENT_STATE:
            self.send_state(self.state)
            if self.state is State.PROVISIONED:
                self.send_result(Command.WIFI_SETTINGS, ["http://device.local"])
        elif command == Command.DEVICE_INFO:
            self.send_result(command, ["ESPHome", "2025.12.2", "esp32", "Thing"])
        elif command == Command.SCAN_WIFI:
            for network in self.networks:
                self.send_result(command, list(network))
            self.send_result(command, [])
        elif command == Command.HOSTNAME:
            if data:
                name = data.decode()
                if "_" in name:
                    self.send_error(Error.BAD_HOSTNAME)
                    return
                self.hostname = name
            self.send_result(command, [self.hostname])
        elif command == Command.WIFI_SETTINGS:
            ssid, password = decode_strings(data)
            self.send_state(State.PROVISIONING)
            if password == "right":
                self.state = State.PROVISIONED
                self.send_state(State.PROVISIONED)
                self.send_result(command, ["http://device.local"])
            else:
                self.state = State.READY
                self.send_error(Error.UNABLE_TO_CONNECT)
                self.send_state(State.READY)
        else:
            self.send_error(Error.UNKNOWN_RPC_COMMAND)


@pytest.fixture
def fake_serial(monkeypatch):
    devices = []

    async def create_serial_connection(loop, factory, port, **kwargs):
        if port == "/dev/missing":
            raise OSError("No such file or directory")
        protocol = factory()
        device = FakeDevice(protocol)
        devices.append(device)
        # Like serial_asyncio: connection_made() runs on the next loop iteration.
        loop.call_soon(protocol.connection_made, device)
        return device, protocol

    monkeypatch.setattr(
        improv_serial.serial_asyncio, "create_serial_connection", create_serial_connection
    )
    return devices


def run(coro):
    return asyncio.run(coro)


def test_full_session(fake_serial):
    async def session():
        states = []
        client = ImprovSerialClient("/dev/fake", timeout=1)
        client.register_state_callback(states.append)
        async with client:
            device = fake_serial[0]
            assert await client.request_current_state() is State.READY

            info = await client.get_device_info()
            assert info.device_name == "Thing"

            networks = await client.scan_wifi()
            assert [(n.ssid, n.rssi, n.secured) for n in networks] == [
                ("Home", -50, True),
                ("Guest", -70, False),
            ]

            assert await client.get_hostname() == "esp-device"
            assert await client.set_hostname("new-name") == "new-name"

            assert await client.provision("Home", "right") == "http://device.local"
            assert client.state is State.PROVISIONED

            # Frames end with a newline.
            assert all(write.endswith(b"\n") for write in device.raw_writes)
            assert device.commands[0] == (Command.REQUEST_CURRENT_STATE, b"")
        assert not client.connected
        assert states == [State.READY, State.PROVISIONING, State.PROVISIONED]

    run(session())


def test_device_info_fields(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            info = await client.get_device_info()
            assert (info.firmware_name, info.firmware_version, info.hardware) == (
                "ESPHome", "2025.12.2", "esp32"
            )
            assert info.device_name == "Thing"
            assert info.os_name is None

    run(session())


def test_provision_wrong_password(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            with pytest.raises(ImprovDeviceError) as excinfo:
                await client.provision("Home", "wrong")
            assert excinfo.value.error is Error.UNABLE_TO_CONNECT
            # The client still works afterwards.
            assert await client.request_current_state() is State.READY

    run(session())


def test_bad_hostname(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            with pytest.raises(ImprovDeviceError) as excinfo:
                await client.set_hostname("bad_name")
            assert excinfo.value.error is Error.BAD_HOSTNAME
            with pytest.raises(ValueError):
                await client.set_hostname("")

    run(session())


def test_unsupported_network_state(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            with pytest.raises(ImprovNotSupportedError):
                await client.get_network_state()

    run(session())


def test_ignores_unsolicited_result(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            fake_serial[0].state = State.PROVISIONED
            assert await client.request_current_state() is State.PROVISIONED
            assert (await client.get_device_info()).device_name == "Thing"

    run(session())


def test_timeout(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=0.05) as client:
            fake_serial[0].answer = False
            with pytest.raises(ImprovTimeoutError):
                await client.request_current_state()
            with pytest.raises(TimeoutError):
                await client.get_hostname()

    run(session())


def test_concurrent_commands_are_serialized(fake_serial):
    async def session():
        async with ImprovSerialClient("/dev/fake", timeout=1) as client:
            hostname, info = await asyncio.gather(client.get_hostname(), client.get_device_info())
            assert hostname == "esp-device" and info.device_name == "Thing"

    run(session())


def test_connection_lost_fails_pending(fake_serial):
    async def session():
        disconnected = []
        client = ImprovSerialClient("/dev/fake", timeout=1, disconnected_callback=disconnected.append)
        await client.connect()
        fake_serial[0].answer = False
        task = asyncio.ensure_future(client.get_hostname())
        await asyncio.sleep(0)
        fake_serial[0].close()
        with pytest.raises(ImprovConnectionError):
            await task
        assert disconnected == [client] and not client.connected

    run(session())


def test_not_connected_and_open_failure(fake_serial):
    async def session():
        with pytest.raises(ImprovConnectionError):
            await ImprovSerialClient("/dev/fake").get_hostname()
        with pytest.raises(ImprovConnectionError):
            await ImprovSerialClient("/dev/missing").connect()

    run(session())
