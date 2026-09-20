"""Test the pairing handshake in WebOsClient._check_registration."""

from typing import Any

import pytest

from aiowebostv import WebOsTvPairError, handshake
from aiowebostv.webos_client import WebOsClient

from .fake_tv import FakeWebOsTV


async def _make_client() -> WebOsClient:
    return WebOsClient(host="127.0.0.1", client_key=None)


@pytest.mark.parametrize(
    ("first_frame", "second_frame", "expected_key", "expected_error"),
    [
        pytest.param(
            {"type": "error", "id": "register_0", "error": "403 rejected"},
            None,
            None,
            "403 rejected",
            id="direct-error-frame",
        ),
        pytest.param(
            {
                "type": "registered",
                "id": "register_0",
                "payload": {"client-key": "direct-key"},
            },
            None,
            "direct-key",
            None,
            id="direct-registered-frame",
        ),
        pytest.param(
            {
                "type": "response",
                "id": "register_0",
                "payload": {"pairingType": "PROMPT"},
            },
            {
                "type": "registered",
                "id": "register_0",
                "payload": {"client-key": "prompted-key"},
            },
            "prompted-key",
            None,
            id="prompt-then-registered",
        ),
        pytest.param(
            {
                "type": "response",
                "id": "register_0",
                "payload": {"pairingType": "PROMPT"},
            },
            {"type": "error", "id": "register_0", "error": "PIN invalid"},
            None,
            "PIN invalid",
            id="prompt-then-error",
        ),
        pytest.param(
            {"type": "response", "id": "register_0", "payload": {}},
            None,
            None,
            "Client key not set, pairing failed.",
            id="response-without-pairingtype",
        ),
    ],
)
async def test_check_registration(
    first_frame: dict[str, Any],
    second_frame: dict[str, Any] | None,
    expected_key: str | None,
    expected_error: str | None,
) -> None:
    """Test the registration handshake dispatches on the frame type."""
    client = await _make_client()
    tv = FakeWebOsTV([first_frame, *([second_frame] if second_frame else [])])

    if expected_error is not None:
        with pytest.raises(WebOsTvPairError, match=expected_error):
            await client._check_registration(tv)
    else:
        await client._check_registration(tv)

    assert client.client_key == expected_key


async def test_registration_message_carries_existing_key() -> None:
    """Test the registration message includes the stored client key."""
    client = WebOsClient(host="127.0.0.1", client_key="stored-key")
    message = client.registration_msg()

    assert message["type"] == handshake.REGISTRATION_MESSAGE["type"]
    assert message["payload"]["client-key"] == "stored-key"
