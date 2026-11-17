"""Exceptions raised by improv-client.

Every exception derives from :class:`ImprovError`, so a single ``except ImprovError``
catches anything the library raises on purpose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .protocol import Error


class ImprovError(Exception):
    """Base class for all improv-client errors."""


class ImprovProtocolError(ImprovError):
    """Data received from the device could not be parsed."""


class ImprovConnectionError(ImprovError):
    """Connecting to the device failed, or the connection was lost."""


class ImprovTimeoutError(ImprovError, TimeoutError):
    """The device did not answer in time."""


class ImprovNotSupportedError(ImprovError):
    """The device does not support the requested command.

    Raised before sending when the BLE capabilities say the command isn't
    supported, and when the device answers with
    :attr:`~improv_client.protocol.Error.UNKNOWN_RPC_COMMAND`.
    """


class ImprovDeviceError(ImprovError):
    """The device reported an error state.

    :ivar error: The error code reported by the device.
    :vartype error: improv_client.protocol.Error
    """

    def __init__(self, error: Error, message: str | None = None) -> None:
        self.error = error
        super().__init__(message or f"Device reported error: {error.name}")
