"""Command line tool: ``improv-client`` or ``python -m improv_client``.

Examples::

    improv-client discover
    improv-client serial /dev/ttyUSB0 info
    improv-client serial /dev/ttyUSB0 provision MyWirelessAP
    improv-client ble AA:BB:CC:DD:EE:FF scan
    improv-client ble AA:BB:CC:DD:EE:FF hostname new-hostname
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from .client import ImprovClient
from .exceptions import ImprovError
from .protocol import State

_COMMON_ACTIONS = ("state", "info", "scan", "hostname", "device-name", "provision")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="improv-client", description="Talk to Improv Wi-Fi devices over serial or BLE."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log protocol traffic")
    parser.add_argument("-t", "--timeout", type=float, default=10.0, help="answer timeout, s")
    transports = parser.add_subparsers(dest="transport", required=True)

    discover = transports.add_parser("discover", help="scan for Improv BLE devices")
    discover.add_argument("--scan-time", type=float, default=5.0, help="seconds to scan")

    serial = transports.add_parser("serial", help="use a serial port")
    serial.add_argument("port", help="serial port, e.g. /dev/ttyUSB0")
    serial.add_argument("-b", "--baudrate", type=int, default=115200)
    _add_actions(serial, (*_COMMON_ACTIONS, "network"))

    ble = transports.add_parser("ble", help="use Bluetooth Low Energy")
    ble.add_argument("address", help="device address (a UUID on macOS), see 'discover'")
    _add_actions(ble, (*_COMMON_ACTIONS, "identify"))
    return parser


def _add_actions(parser: argparse.ArgumentParser, actions: tuple[str, ...]) -> None:
    sub = parser.add_subparsers(dest="action", required=True)
    help_text = {
        "state": "show the current state",
        "info": "show device information",
        "scan": "list Wi-Fi networks the device can see",
        "hostname": "show or set the hostname",
        "device-name": "show or set the device name",
        "provision": "send Wi-Fi credentials",
        "network": "show the network state",
        "identify": "make the device identify itself",
    }
    for action in actions:
        action_parser = sub.add_parser(action, help=help_text[action])
        if action in ("hostname", "device-name"):
            action_parser.add_argument("value", nargs="?", help="new value to set")
        elif action == "provision":
            action_parser.add_argument("ssid")
            action_parser.add_argument(
                "password", nargs="?", help="prompted for if left out; use '' for open networks"
            )


async def _discover(args: argparse.Namespace) -> int:
    from .ble import discover

    devices = await discover(args.scan_time)
    if not devices:
        print("No Improv devices found")
        return 1
    for found in devices:
        details = ""
        if found.service_data is not None:
            details = (
                f"  {found.service_data.state.name}"
                f"  capabilities={found.service_data.capabilities!r}"
            )
        print(f"{found.address}  {found.rssi} dBm  {found.name or '(no name)'}{details}")
    return 0


async def _open(args: argparse.Namespace) -> ImprovClient:
    if args.transport == "serial":
        from .serial import ImprovSerialClient

        return ImprovSerialClient(args.port, args.baudrate, timeout=args.timeout)
    from .ble import ImprovBleClient, find_device

    device = await find_device(args.address)
    if device is None:
        raise ImprovError(f"Device {args.address} not found")
    return ImprovBleClient(device, timeout=args.timeout, idle_disconnect_delay=None)


async def _run(args: argparse.Namespace) -> int:
    if args.transport == "discover":
        return await _discover(args)
    client = await _open(args)
    async with client:
        if args.transport == "serial":
            await client.request_current_state()  # type: ignore[attr-defined]
        action = args.action
        if action == "state":
            print(f"State: {client.state.name if client.state else 'unknown'}")
            capabilities = getattr(client, "capabilities", None)
            if capabilities is not None:
                print(f"Capabilities: {capabilities!r}")
        elif action == "info":
            info = await client.get_device_info()
            for label, value in (
                ("Firmware", f"{info.firmware_name} {info.firmware_version}"),
                ("Hardware", info.hardware),
                ("Device name", info.device_name),
                ("OS", f"{info.os_name} {info.os_version or ''}" if info.os_name else None),
            ):
                if value:
                    print(f"{label + ':':13}{value.strip()}")
        elif action == "scan":
            for network in await client.scan_wifi():
                print(f"{network.rssi:5} dBm  {network.auth:10}  {network.ssid}")
        elif action == "hostname":
            if args.value:
                print(await client.set_hostname(args.value))
            else:
                print(await client.get_hostname())
        elif action == "device-name":
            if args.value:
                print(await client.set_device_name(args.value))
            else:
                print(await client.get_device_name())
        elif action == "network":
            network_state = await client.get_network_state()  # type: ignore[attr-defined]
            print(f"Flags: {network_state.flags!r}")
            for url in network_state.urls:
                print(f"URL: {url}")
        elif action == "identify":
            await client.identify()  # type: ignore[attr-defined]
        elif action == "provision":
            password = args.password
            if password is None:
                password = getpass.getpass(f"Password for {args.ssid}: ")
            if client.state is State.AUTHORIZATION_REQUIRED:
                print("Authorize the device, e.g. by pressing its button...")
                await client.wait_for_state(State.AUTHORIZED, timeout=120)
            url = await client.provision(args.ssid, password)
            print(f"Provisioned{', open ' + url if url else ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``improv-client`` command."""
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except ImprovError as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    except ImportError as err:
        print(f"Error: {err}. Install the extra: pip install improv-client[all]", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
