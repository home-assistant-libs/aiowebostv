"""A fake TV websocket for testing the registration handshake."""

from typing import Any
from unittest.mock import MagicMock


class FakeWebOsTV(MagicMock):
    """Stand-in for an aiohttp ClientWebSocketResponse.

    Feeds a scripted list of frames to ``receive_json`` so the pairing
    handshake can be exercised without a real TV.
    """

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        """Store the frames the TV will send."""
        super().__init__(spec=["receive_json", "send_json", "close"])
        self._frames = list(frames)
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        """Record a message sent to the TV."""
        self.sent.append(message)

    async def receive_json(self, timeout: float | None = None) -> dict[str, Any]:  # noqa: ASYNC109
        """Return the next scripted frame."""
        if not self._frames:
            msg = "FakeWebOsTV ran out of frames"
            raise AssertionError(msg)
        return self._frames.pop(0)
