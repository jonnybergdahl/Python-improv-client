"""Client for provisioning Improv Wi-Fi devices over serial and BLE.

The transports live in their own modules so that each only needs its own extra:

- :mod:`improv_client.serial` (``pip install improv-client[serial]``)
- :mod:`improv_client.ble` (``pip install improv-client[ble]``)

The protocol layer, results and exceptions are importable from here without any
extra installed.
"""

from .client import ImprovClient
from .exceptions import (
    ImprovConnectionError,
    ImprovDeviceError,
    ImprovError,
    ImprovNotSupportedError,
    ImprovProtocolError,
    ImprovTimeoutError,
)
from .protocol import (
    Capabilities,
    Command,
    DeviceInfo,
    Error,
    NetworkFlags,
    NetworkState,
    ServiceData,
    State,
    WifiNetwork,
)

__version__ = "1.0.0"

__all__ = [
    "Capabilities",
    "Command",
    "DeviceInfo",
    "Error",
    "ImprovClient",
    "ImprovConnectionError",
    "ImprovDeviceError",
    "ImprovError",
    "ImprovNotSupportedError",
    "ImprovProtocolError",
    "ImprovTimeoutError",
    "NetworkFlags",
    "NetworkState",
    "ServiceData",
    "State",
    "WifiNetwork",
    "__version__",
]
