"""BLE client tests against a fake bleak client."""

import asyncio
from types import SimpleNamespace

import pytest

import improv_client.ble as improv_ble
from improv_client import (
    Capabilities,
    Error,
    ImprovConnectionError,
    ImprovDeviceError,
    ImprovNotSupportedError,
    State,
)
from improv_client.ble import ImprovBleClient, is_improv_advertisement, service_data_from_advertisement
from improv_client.protocol import (
    BLE_CAPABILITIES_UUID,
    BLE_CURRENT_STATE_UUID,
    BLE_ERROR_STATE_UUID,
    BLE_RPC_COMMAND_UUID,
    BLE_RPC_RESULT_UUID,
    BLE_SERVICE_DATA_UUID,
    BLE_SERVICE_UUID,
    Command,
    checksum,
    decode_strings,
    encode_strings,
)


class FakeService:
    def __init__(self, uuids):
        self.uuids = uuids

    def get_characteristic(self, uuid):
        if uuid not in self.uuids:
            return None
        properties = ["write"] if uuid == BLE_RPC_COMMAND_UUID else ["read", "notify"]
        return SimpleNamespace(uuid=uuid, properties=properties)


class FakeBleakClient:
    """Answers like an Improv BLE device."""

    def __init__(self, disconnected_callback, capabilities=0x0F, state=State.AUTHORIZED,
                 has_service=True):
        self.disconnected_callback = disconnected_callback
        self.is_connected = True
        self.notify = {}
        self.writes = []
        self.values = {
            BLE_CAPABILITIES_UUID: bytearray([capabilities]),
            BLE_CURRENT_STATE_UUID: bytearray([state]),
            BLE_ERROR_STATE_UUID: bytearray([0]),
            BLE_RPC_RESULT_UUID: bytearray(),
        }
        uuids = set(self.values) | {BLE_RPC_COMMAND_UUID}
        self.services = SimpleNamespace(
            get_service=lambda uuid: FakeService(uuids) if has_service and uuid == BLE_SERVICE_UUID
            else None
        )
        self.truncate_notifications = False
        self.disconnects = 0

    async def read_gatt_char(self, uuid):
        return self.values[uuid]

    async def start_notify(self, uuid, callback):
        self.notify[uuid] = callback

    async def disconnect(self):
        self.disconnects += 1
        if self.is_connected:
            self.is_connected = False
            self.disconnected_callback(self)

    async def write_gatt_char(self, uuid, data, response=None):
        assert uuid == BLE_RPC_COMMAND_UUID and response is True
        data = bytes(data)
        self.writes.append(data)
        assert data[-1] == checksum(data[:-1])
        command, length = data[0], data[1]
        asyncio.get_running_loop().call_soon(self.handle, command, data[2 : 2 + length])

    def set_value(self, uuid, value):
        self.values[uuid] = bytearray(value)
        if uuid in self.notify:
            notified = value[:20] if self.truncate_notifications else value
            self.notify[uuid](None, bytearray(notified))

    def result(self, command, strings):
        data = encode_strings(strings)
        body = bytes([command, len(data)]) + data
        self.set_value(BLE_RPC_RESULT_UUID, body + bytes([checksum(body)]))

    def handle(self, command, data):
        self.set_value(BLE_ERROR_STATE_UUID, b"\x00")
        if command == Command.DEVICE_INFO:
            self.result(command, ["ESPHome", "2021.11.0", "esp32", "Thing"])
        elif command == Command.SCAN_WIFI:
            self.result(command, ["Home", "-50", "WPA2", "Cafe", "-80", "NO", "Office", "-60",
                                  "WPA/WPA2"])
        elif command == Command.DEVICE_NAME:
            self.result(command, [data.decode() if data else "Thing"])
        elif command == Command.WIFI_SETTINGS:
            ssid, password = decode_strings(data)
            self.set_value(BLE_CURRENT_STATE_UUID, bytes([State.PROVISIONING]))
            if password == "right":
                self.set_value(BLE_CURRENT_STATE_UUID, bytes([State.PROVISIONED]))
                self.result(command, ["http://device.local"])
            else:
                self.set_value(BLE_ERROR_STATE_UUID, bytes([Error.UNABLE_TO_CONNECT]))
                self.set_value(BLE_CURRENT_STATE_UUID, bytes([State.AUTHORIZED]))
        elif command == Command.IDENTIFY:
            pass


@pytest.fixture
def fake_ble(monkeypatch):
    created = []
    options = {}

    async def establish_connection(client_class, device, name, disconnected_callback=None, **kw):
        client = FakeBleakClient(disconnected_callback, **options)
        created.append(client)
        return client

    monkeypatch.setattr(improv_ble, "establish_connection", establish_connection)
    return SimpleNamespace(created=created, options=options)


DEVICE = SimpleNamespace(address="AA:BB:CC:DD:EE:FF", name="improv-test")


def run(coro):
    return asyncio.run(coro)


def test_session(fake_ble):
    async def session():
        async with ImprovBleClient(DEVICE, timeout=1) as client:
            assert client.capabilities == Capabilities(0x0F)
            assert client.state is State.AUTHORIZED
            assert (await client.get_device_info()).device_name == "Thing"
            networks = await client.scan_wifi()
            assert [(n.ssid, n.secured) for n in networks] == [
                ("Home", True), ("Cafe", False), ("Office", True)
            ]
            assert await client.set_device_name("Kök") == "Kök"
            await client.identify()
            assert fake_ble.created[0].writes[-1] == bytes([0x02, 0x00, 0x02])
            assert await client.provision("Home", "right") == "http://device.local"
            assert client.state is State.PROVISIONED
        assert not client.connected

    run(session())


def test_truncated_notification_falls_back_to_read(fake_ble):
    async def session():
        async with ImprovBleClient(DEVICE, timeout=1) as client:
            fake_ble.created[0].truncate_notifications = True
            assert len(await client.scan_wifi()) == 3

    run(session())


def test_provision_failure(fake_ble):
    async def session():
        async with ImprovBleClient(DEVICE, timeout=1) as client:
            with pytest.raises(ImprovDeviceError) as excinfo:
                await client.provision("Home", "wrong")
            assert excinfo.value.error is Error.UNABLE_TO_CONNECT

    run(session())


def test_capabilities_gate_commands(fake_ble):
    fake_ble.options["capabilities"] = 0

    async def session():
        async with ImprovBleClient(DEVICE, timeout=1) as client:
            for call in (client.identify, client.get_device_info, client.scan_wifi,
                         client.get_hostname):
                with pytest.raises(ImprovNotSupportedError):
                    await call()
            assert fake_ble.created[0].writes == []

    run(session())


def test_authorization(fake_ble):
    fake_ble.options["state"] = State.AUTHORIZATION_REQUIRED

    async def session():
        async with ImprovBleClient(DEVICE, timeout=1) as client:
            with pytest.raises(ImprovDeviceError) as excinfo:
                await client.provision("Home", "right")
            assert excinfo.value.error is Error.NOT_AUTHORIZED
            device = fake_ble.created[0]
            asyncio.get_running_loop().call_later(
                0.01, device.set_value, BLE_CURRENT_STATE_UUID, bytes([State.AUTHORIZED])
            )
            assert await client.wait_for_state(State.AUTHORIZED, timeout=1) is State.AUTHORIZED
            assert await client.provision("Home", "right") == "http://device.local"

    run(session())


def test_auto_connect_and_idle_disconnect(fake_ble):
    async def session():
        disconnected = []
        client = ImprovBleClient(DEVICE, timeout=1, idle_disconnect_delay=0.05,
                                 disconnected_callback=disconnected.append)
        assert (await client.get_device_info()).device_name == "Thing"
        assert client.connected
        await asyncio.sleep(0.15)
        assert not client.connected and disconnected == [client]
        # Reconnects on the next command.
        assert (await client.get_device_name()) == "Thing"
        assert len(fake_ble.created) == 2
        await client.disconnect()

    run(session())


def test_unexpected_disconnect_fails_pending(fake_ble):
    async def session():
        client = ImprovBleClient(DEVICE, timeout=1)
        await client.connect()
        device = fake_ble.created[0]
        device.handle = lambda command, data: None
        task = asyncio.ensure_future(client.get_device_info())
        await asyncio.sleep(0.01)
        await device.disconnect()
        with pytest.raises(ImprovConnectionError):
            await task

    run(session())


def test_not_an_improv_device(fake_ble):
    fake_ble.options["has_service"] = False

    async def session():
        with pytest.raises(ImprovConnectionError):
            await ImprovBleClient(DEVICE).connect()
        assert fake_ble.created[0].disconnects == 1

    run(session())


def test_advertisement_parsing():
    adv = SimpleNamespace(
        service_uuids=[BLE_SERVICE_UUID.upper()],
        service_data={BLE_SERVICE_DATA_UUID: bytes([0x02, 0x01, 0, 0, 0, 0])},
    )
    assert is_improv_advertisement(adv)
    data = service_data_from_advertisement(adv)
    assert data.state is State.AUTHORIZED and data.capabilities == Capabilities.IDENTIFY
    other = SimpleNamespace(service_uuids=["0000180f-0000-1000-8000-00805f9b34fb"], service_data={})
    assert not is_improv_advertisement(other)
    assert service_data_from_advertisement(other) is None
