"""Tests for the webOS client."""

import asyncio
import base64
import gc
import json
import logging
import socket
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio
from aiohttp.test_utils import unused_port
from wakeonlan import create_magic_packet

from aiowebostv import (
    WebOsClient,
    WebOsTvCommandError,
    WebOsTvPairError,
    WebOsTvRegistrationError,
    WebOsTvServiceNotFoundError,
    WebOsTvState,
    WebOsTvWakeError,
    webos_client,
)
from aiowebostv import endpoints as ep
from aiowebostv.exceptions import (
    WebOsTvCommandTimeoutError,
    WebOsTvResponseTypeError,
)

from .conftest import (
    BOOT_ERROR,
    CLIENT_KEY,
    HOST,
    NEW_CLIENT_KEY,
    NOT_FOUND,
    WIFI_MAC,
    WIRED_MAC,
    FakeTv,
)

pytestmark = pytest.mark.asyncio

FULL_PAYLOAD = object()


async def push_and_wait(
    client: WebOsClient,
    fake_tv: FakeTv,
    uri: str,
    payload: dict[str, Any],
    predicate: Callable[[WebOsTvState], bool],
) -> None:
    """Push a subscription update and wait until the state matches."""
    event = asyncio.Event()

    async def on_state(state: WebOsTvState) -> None:
        if predicate(state):
            event.set()

    await client.register_state_update_callback(on_state)
    try:
        await fake_tv.push(uri, payload)
        await asyncio.wait_for(event.wait(), 1)
    finally:
        client.unregister_state_update_callback(on_state)


def mock_ws(frames: list[dict[str, Any]]) -> AsyncMock:
    """Return a websocket mock receiving the frames as text messages."""
    ws = AsyncMock()
    ws.receive.side_effect = [
        aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(frame), None)
        for frame in frames
    ]
    return ws


async def assert_disconnected(client: WebOsClient) -> None:
    """Wait for a failed connection to clean up and assert it is disconnected."""
    assert client.connect_task is not None
    await client.connect_task
    assert not client.is_connected()


# Connection


async def test_connect(connected_client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test connecting gets the TV info and initial state."""
    client = connected_client

    assert client.is_connected()
    assert client.is_registered()
    assert client.input_connection is not None
    register = next(msg for msg in fake_tv.received if msg["type"] == "register")
    assert register["payload"]["client-key"] == CLIENT_KEY

    assert client.tv_info.hello == {"x": 1}
    assert client.tv_info.system["modelName"] == "OLED55C1"
    assert client.tv_info.software["product_name"] == "webOSTV 6.0"
    assert client.mac_addresses == {
        "wired": WIRED_MAC.lower(),
        "wifi": WIFI_MAC.lower(),
    }

    state = client.tv_state
    assert state.is_on
    assert state.is_screen_on
    assert state.current_app_id == "com.webos.app.hdmi1"
    assert state.volume == 10
    assert state.volume_status == {"volume": 10}
    assert state.muted is False
    assert state.sound_output == "tv_speaker"
    assert list(state.apps) == ["netflix"]
    assert list(state.inputs) == ["com.webos.app.hdmi1"]
    assert state.channels == [{"channelId": "1", "channelName": "News"}]
    assert state.media_state == []


async def test_connect_when_connected(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test connecting again reuses the existing connection."""
    assert await connected_client.connect()

    assert [msg["type"] for msg in fake_tv.received].count("hello") == 1


async def test_concurrent_connect(client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test concurrent connects share a single connection attempt."""
    assert await asyncio.gather(client.connect(), client.connect()) == [True, True]

    assert [msg["type"] for msg in fake_tv.received].count("hello") == 1


async def test_cancelled_connect_caller(client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test cancelling a connect caller does not cancel the shared attempt."""
    fake_tv.responses.pop(ep.GET_POWER_STATE)
    cancelled = asyncio.create_task(client.connect())
    waiting = asyncio.create_task(client.connect())
    await fake_tv.wait_for_message("subscribe", ep.GET_POWER_STATE)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert client.connect_result is not None
    assert not client.connect_result.cancelled()
    assert not client.is_connected()

    await fake_tv.push(ep.GET_POWER_STATE, {"state": "Active"})
    assert await waiting
    assert client.is_connected()


async def test_cancelled_connect_caller_then_connect_fails(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a failed connect nobody waits for anymore does not log an error."""
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 0.05)
    fake_tv.responses.pop(ep.GET_POWER_STATE)

    unhandled_exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_exceptions.append(context)
    )
    try:
        connect = asyncio.create_task(client.connect())
        await fake_tv.wait_for_message("subscribe", ep.GET_POWER_STATE)
        connect.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect

        await assert_disconnected(client)
        assert client.connect_result is not None
        assert client.connect_result.done()

        # Drop the result like a new connect attempt does, a never retrieved
        # exception is logged when the future is garbage collected
        client.connect_result = None
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert unhandled_exceptions == []


@pytest.mark.usefixtures("fake_tv")
async def test_pairing() -> None:
    """Test pairing a client without a client key."""
    client = WebOsClient(HOST)
    assert not client.is_registered()

    try:
        assert await client.connect()
    finally:
        await client.disconnect()

    assert client.client_key == NEW_CLIENT_KEY


async def test_pairing_slower_than_receive_timeout(
    fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the user gets longer than the receive timeout to accept pairing."""
    monkeypatch.setattr(webos_client, "RECEIVE_TIMEOUT", 0.05)
    fake_tv.pairing_delay = 0.2
    client = WebOsClient(HOST)

    try:
        assert await client.connect()
    finally:
        await client.disconnect()

    assert client.client_key == NEW_CLIENT_KEY


async def test_pairing_timeout(
    fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the pairing prompt wait is bounded when the TV does not answer."""
    monkeypatch.setattr(webos_client, "PAIRING_TIMEOUT", 0.1)
    fake_tv.pairing_delay = 5
    client = WebOsClient(HOST)

    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(TimeoutError):
        await client.connect()

    # The pairing timeout, not the receive timeout or the TV's answer
    assert loop.time() - start < 1
    await assert_disconnected(client)
    assert client.client_key is None


@pytest.mark.parametrize("pairing", ["reject", "no_key"])
async def test_pairing_failed(fake_tv: FakeTv, pairing: str) -> None:
    """Test pairing failures raise a pair error."""
    fake_tv.pairing = pairing
    client = WebOsClient(HOST)

    with pytest.raises(WebOsTvPairError):
        await client.connect()

    await assert_disconnected(client)
    assert client.client_key is None


@pytest.mark.parametrize(
    ("frames", "client_key", "error"),
    [
        (
            [
                {
                    "type": "error",
                    "error": "403 Pairing rejected: blacklisted certificate detected",
                }
            ],
            None,
            "403 Pairing rejected",
        ),
        (
            [{"type": "registered", "payload": {"client-key": "direct-key"}}],
            "direct-key",
            None,
        ),
        (
            [{"type": "response", "payload": {}}],
            None,
            "Client key not set, pairing failed.",
        ),
        (
            [{"type": "error"}],
            None,
            "Pairing failed",
        ),
    ],
)
async def test_check_registration_first_frame(
    frames: list[dict[str, Any]], client_key: str | None, error: str | None
) -> None:
    """Test _check_registration handles the first frame by type."""
    ws = mock_ws(frames)
    client = WebOsClient(HOST)
    # Skip the signed-manifest retry: that path is covered by the
    # registration tests, this test is about the frame-type dispatch.
    client.signed_manifest = False

    if error is None:
        await client._check_registration(ws)
    else:
        with pytest.raises(WebOsTvPairError, match=error):
            await client._check_registration(ws)

    assert client.client_key == client_key


async def test_check_registration_registered_without_key_keeps_stored_key() -> None:
    """Test a registered frame without a key keeps the stored key."""
    ws = mock_ws([{"type": "registered", "payload": {}}])
    client = WebOsClient(HOST)
    client.client_key = "stored-key"
    client.signed_manifest = False

    await client._check_registration(ws)

    assert client.client_key == "stored-key"


async def test_check_registration_both_manifests_rejected() -> None:
    """Test the second rejection raises the TV's own message."""
    ws = mock_ws(
        [
            {"type": "error", "error": "403 Pairing rejected: signed manifest"},
            {"type": "error", "error": "403 Pairing rejected: unsigned manifest"},
        ]
    )
    client = WebOsClient(HOST)

    with pytest.raises(WebOsTvPairError, match="unsigned manifest"):
        await client._check_registration(ws)

    assert client.signed_manifest
    assert client.client_key is None


@pytest.mark.parametrize("signed_manifest", [True, False])
async def test_check_registration_error_with_stored_key(
    signed_manifest: bool,  # noqa: FBT001
) -> None:
    """Test a registration error on a paired TV is transient, not a pair error."""
    ws = mock_ws(
        [
            {"type": "error", "error": BOOT_ERROR},
            {"type": "error", "error": BOOT_ERROR},
        ]
    )
    client = WebOsClient(HOST, CLIENT_KEY)
    client.signed_manifest = signed_manifest

    with pytest.raises(WebOsTvRegistrationError, match=BOOT_ERROR):
        await client._check_registration(ws)

    assert client.signed_manifest == signed_manifest
    assert client.client_key == CLIENT_KEY


async def test_check_registration_prompt_error_with_stored_key() -> None:
    """Test an error in the prompt step is a pair error, even with a stored key."""
    ws = mock_ws(
        [
            {"type": "response", "payload": {"pairingType": "PROMPT"}},
            {"type": "error", "error": "403 User denied access"},
        ]
    )
    client = WebOsClient(HOST, "revoked-key")

    with pytest.raises(WebOsTvPairError, match="User denied access"):
        await client._check_registration(ws)

    assert client.signed_manifest


def registrations(fake_tv: FakeTv) -> list[dict[str, Any]]:
    """Return the manifests of all registration requests."""
    return [
        msg["payload"]["manifest"]
        for msg in fake_tv.received
        if msg["type"] == "register"
    ]


async def test_registration_signed_manifest(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a TV accepting the signed manifest registers with it."""
    assert connected_client.signed_manifest
    (manifest,) = registrations(fake_tv)
    assert "signatures" in manifest
    assert "READ_UPDATE_INFO" in manifest["signed"]["permissions"]


async def test_registration_signed_manifest_rejected(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a rejected signed manifest retries unsigned on the same socket."""
    fake_tv.reject_signed = True

    assert await client.connect()

    assert not client.signed_manifest
    assert [msg["type"] for msg in fake_tv.received].count("hello") == 1
    signed, unsigned = registrations(fake_tv)
    assert "signatures" in signed
    assert list(unsigned) == ["appVersion", "manifestVersion", "permissions"]
    assert len(unsigned["permissions"]) == 37
    assert unsigned["permissions"] == sorted(unsigned["permissions"])
    assert "READ_UPDATE_INFO" in unsigned["permissions"]


async def test_registration_unsigned_mac_addresses(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test the MAC addresses are available with the unsigned manifest.

    Real TVs reject software info without the signed READ_UPDATE_INFO
    permission, but still report the connection info.
    """
    fake_tv.reject_signed = True
    fake_tv.responses[ep.GET_SOFTWARE_INFO] = None

    assert await client.connect()

    assert not client.signed_manifest
    assert client.tv_info.software == {}
    assert client.mac_addresses == {
        "wired": WIRED_MAC.lower(),
        "wifi": WIFI_MAC.lower(),
    }


async def test_registration_unsigned_after_rejection(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test the next connect after a rejection sends the unsigned manifest."""
    fake_tv.reject_signed = True
    assert await client.connect()
    await client.disconnect()
    fake_tv.received.clear()

    assert await client.connect()

    (manifest,) = registrations(fake_tv)
    assert "signatures" not in manifest


async def test_registration_error_while_booting(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a booting TV fails the connect as transient and the retry succeeds."""
    fake_tv.booting = True

    with pytest.raises(WebOsTvRegistrationError, match=BOOT_ERROR):
        await client.connect()

    await assert_disconnected(client)
    assert isinstance(WebOsTvRegistrationError(), WebOsTvCommandError)
    assert client.signed_manifest
    assert client.client_key == CLIENT_KEY

    fake_tv.booting = False
    fake_tv.received.clear()
    assert await client.connect()

    (manifest,) = registrations(fake_tv)
    assert "signatures" in manifest
    assert client.signed_manifest


async def test_registration_error_while_booting_without_key(
    fake_tv: FakeTv,
) -> None:
    """Test a registration error without a stored key is a pair error."""
    fake_tv.booting = True
    client = WebOsClient(HOST)

    with pytest.raises(WebOsTvPairError, match=BOOT_ERROR):
        await client.connect()

    await assert_disconnected(client)
    assert client.signed_manifest
    assert client.client_key is None


async def test_invalid_hello_response(client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test an invalid hello response fails the connection."""
    fake_tv.hello_type = "error"

    with pytest.raises(WebOsTvCommandError, match="Invalid response type"):
        await client.connect()

    await assert_disconnected(client)


@pytest.mark.parametrize(
    ("hello_reply", "error"),
    [
        ("close", "Connection closed during handshake"),
        ("not json", "Invalid handshake message"),
        ("[1]", "Invalid handshake message"),
        ('{"payload": {}}', "Invalid handshake message"),
        ('{"type": "hello", "payload": [1]}', "Invalid handshake message payload"),
    ],
)
async def test_malformed_handshake_message(
    client: WebOsClient, fake_tv: FakeTv, hello_reply: str, error: str
) -> None:
    """Test a malformed handshake message or close raises a command error."""
    fake_tv.hello_reply = hello_reply

    with pytest.raises(WebOsTvCommandError, match=error):
        await client.connect()

    await assert_disconnected(client)


async def test_check_registration_registered_null_payload() -> None:
    """Test a registered frame with a null payload keeps the stored key."""
    ws = mock_ws([{"type": "registered", "payload": None}])
    client = WebOsClient(HOST, "stored-key")
    client.signed_manifest = False

    await client._check_registration(ws)

    assert client.client_key == "stored-key"


async def test_is_connected_during_failed_connect_closeout(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test is_connected does not raise while a failed connection closes out."""
    fake_tv.hello_type = "error"

    with pytest.raises(WebOsTvCommandError):
        await client.connect()

    assert client.connect_task_active()
    assert not client.is_connected()
    await assert_disconnected(client)


@pytest.mark.parametrize(
    ("msg_type", "uri"),
    [("request", ep.GET_SOFTWARE_INFO), ("subscribe", ep.GET_POWER_STATE)],
)
async def test_tv_closes_during_connect(
    client: WebOsClient, fake_tv: FakeTv, msg_type: str, uri: str
) -> None:
    """Test the TV closing while connecting fails connect without a timeout.

    Nothing waits for the receive task while connecting, the pending request
    must fail when it ends instead of after the request timeout.
    """
    del fake_tv.responses[uri]
    connect = asyncio.create_task(client.connect())
    await fake_tv.wait_for_message(msg_type, uri)

    assert fake_tv.main_ws is not None
    await fake_tv.main_ws.close()

    with pytest.raises(WebOsTvCommandError, match="Connection closed"):
        await asyncio.wait_for(connect, 1)
    await assert_disconnected(client)


async def test_connect_timeout(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test the TV not responding times out the connection."""
    monkeypatch.setattr(webos_client, "RECEIVE_TIMEOUT", 0.05)
    fake_tv.respond_to_hello = False

    with pytest.raises(TimeoutError):
        await client.connect()

    await assert_disconnected(client)


@pytest.mark.parametrize("external_session", [False, True])
async def test_connect_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    external_session: bool,  # noqa: FBT001
) -> None:
    """Test connecting to an unreachable TV fails."""
    monkeypatch.setattr(webos_client, "WS_PORT", unused_port())
    monkeypatch.setattr(webos_client, "WSS_PORT", unused_port())

    async with aiohttp.ClientSession() as session:
        client = WebOsClient(
            HOST,
            CLIENT_KEY,
            connect_timeout=0.5,
            client_session=session if external_session else None,
        )

        with pytest.raises((aiohttp.ClientConnectionError, TimeoutError)):
            await client.connect()

        await assert_disconnected(client)


MAIN_WS_ERRORS = [
    aiohttp.ClientConnectionError(),
    aiohttp.WSServerHandshakeError(None, ()),
    TimeoutError(),
]


def _uris(ws_connect: AsyncMock) -> list[str]:
    """Return the URIs passed to a patched _ws_connect."""
    return [call.args[0] for call in ws_connect.call_args_list]


async def test_main_ws_prefers_wss() -> None:
    """Test the main websocket tries wss first and keeps using it."""
    client = WebOsClient(HOST)
    wss_uri = f"wss://{HOST}:{webos_client.WSS_PORT}"

    with patch.object(client, "_ws_connect", return_value=object()) as ws_connect:
        await client._create_main_ws()
        await client._create_main_ws()

    assert _uris(ws_connect) == [wss_uri, wss_uri]


@pytest.mark.parametrize("error", MAIN_WS_ERRORS)
async def test_main_ws_falls_back_to_ws_and_remembers(error: Exception) -> None:
    """Test falling back to ws when wss fails, and preferring ws afterwards."""
    client = WebOsClient(HOST)
    wss_uri = f"wss://{HOST}:{webos_client.WSS_PORT}"
    ws_uri = f"ws://{HOST}:{webos_client.WS_PORT}"
    ws = object()

    with patch.object(client, "_ws_connect", side_effect=[error, ws, ws]) as ws_connect:
        assert await client._create_main_ws() is ws
        assert await client._create_main_ws() is ws

    assert _uris(ws_connect) == [wss_uri, ws_uri, ws_uri]


@pytest.mark.parametrize("error", MAIN_WS_ERRORS)
async def test_main_ws_falls_back_to_wss_after_ws_preference(error: Exception) -> None:
    """Test returning to wss when the remembered ws scheme fails."""
    client = WebOsClient(HOST)
    wss_uri = f"wss://{HOST}:{webos_client.WSS_PORT}"
    ws_uri = f"ws://{HOST}:{webos_client.WS_PORT}"
    ws = object()

    with patch.object(
        client,
        "_ws_connect",
        side_effect=[aiohttp.ClientConnectionError(), ws, error, ws, ws],
    ) as ws_connect:
        for _ in range(3):
            assert await client._create_main_ws() is ws

    assert _uris(ws_connect) == [wss_uri, ws_uri, ws_uri, wss_uri, wss_uri]


async def test_main_ws_both_schemes_fail_keeps_preference() -> None:
    """Test a failure on both schemes raises and keeps the preferred scheme."""
    client = WebOsClient(HOST)
    wss_uri = f"wss://{HOST}:{webos_client.WSS_PORT}"
    ws_uri = f"ws://{HOST}:{webos_client.WS_PORT}"

    with patch.object(
        client,
        "_ws_connect",
        side_effect=[TimeoutError(), TimeoutError(), object()],
    ) as ws_connect:
        with pytest.raises(TimeoutError):
            await client._create_main_ws()
        await client._create_main_ws()

    assert _uris(ws_connect) == [wss_uri, ws_uri, wss_uri]


async def test_connect_falls_back_to_ws(fake_tv: FakeTv) -> None:  # noqa: ARG001
    """Test connecting to a ws-only TV falls back and remembers ws."""
    client = WebOsClient(HOST, CLIENT_KEY)
    try:
        assert await client.connect()
        assert client._prefer_wss is False
    finally:
        await client.disconnect()


async def test_disconnect(connected_client: WebOsClient) -> None:
    """Test disconnecting clears the state and notifies callbacks."""
    client = connected_client
    states: list[bool] = []

    async def on_state(state: WebOsTvState) -> None:
        states.append(state.is_on)

    await client.register_state_update_callback(on_state)
    await client.disconnect()

    assert not client.is_connected()
    assert client.connection is None
    assert client.input_connection is None
    assert client.tv_state == WebOsTvState()
    assert states == [True, False]
    # The client created its own session, so it closes it on disconnect
    assert client.client_session is None


async def test_disconnect_closes_websockets_gracefully(
    connected_client: WebOsClient,
) -> None:
    """Test disconnect completes the closing handshake on both websockets."""
    main_ws = connected_client.connection
    input_ws = connected_client.input_connection
    assert main_ws is not None
    assert input_ws is not None

    await connected_client.disconnect()

    assert main_ws.close_code == aiohttp.WSCloseCode.OK
    assert input_ws.close_code == aiohttp.WSCloseCode.OK


async def test_disconnect_tv_does_not_answer_close(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test disconnect aborts websockets the TV does not close in time."""
    monkeypatch.setattr(webos_client, "CLOSE_TIMEOUT", 0.1)
    fake_tv.answer_close = False
    assert await client.connect()
    main_ws = client.connection
    input_ws = client.input_connection
    assert main_ws is not None
    assert input_ws is not None

    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.disconnect()

    # Bounded by CLOSE_TIMEOUT, not the 10s aiohttp close timeout
    assert loop.time() - start < 1
    assert main_ws.closed
    assert input_ws.closed
    assert main_ws.close_code == aiohttp.WSCloseCode.ABNORMAL_CLOSURE
    assert input_ws.close_code == aiohttp.WSCloseCode.ABNORMAL_CLOSURE
    assert not client.is_connected()


async def start_closing(
    client: WebOsClient, fake_tv: FakeTv, closed_by: str
) -> asyncio.Task[None] | None:
    """Close a connection the TV holds open, return the disconnect task if any.

    The TV does not answer the client's CLOSE, so the client stays closing
    until the close timeout.
    """
    assert fake_tv.main_ws is not None
    closing = asyncio.Event()
    close_websockets = client._close_websockets

    async def signal_and_close(*web_sockets: aiohttp.ClientWebSocketResponse) -> None:
        closing.set()
        await close_websockets(*web_sockets)

    disconnect = None
    with patch.object(client, "_close_websockets", signal_and_close):
        if closed_by == "client":
            disconnect = asyncio.create_task(client.disconnect())
        else:
            # The client answers the TV's CLOSE on the main websocket and
            # holds on the input websocket the TV does not close
            await asyncio.wait_for(fake_tv.main_ws.close(), 1)
        await asyncio.wait_for(closing.wait(), 1)

    assert client.connection is None
    assert client.connect_task_active()
    return disconnect


async def test_cancelled_disconnect_caller(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test cancelling the disconnect caller raises CancelledError in it.

    The connection keeps closing in the background.
    """
    monkeypatch.setattr(webos_client, "CLOSE_TIMEOUT", 0.2)
    fake_tv.answer_close = False
    assert await client.connect()

    disconnect = await start_closing(client, fake_tv, "client")
    assert disconnect is not None
    disconnect.cancel()

    with pytest.raises(asyncio.CancelledError):
        await disconnect
    assert disconnect.cancelled()

    assert client.connect_task is not None
    assert client.connect_task_active()
    await asyncio.wait({client.connect_task}, timeout=1)
    assert not client.connect_task_active()


async def test_disconnect_while_tv_closes(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test disconnect waits for a connection the TV is closing."""
    monkeypatch.setattr(webos_client, "CLOSE_TIMEOUT", 0.2)
    fake_tv.answer_close = False
    assert await client.connect()
    await start_closing(client, fake_tv, "tv")
    task = client.connect_task
    assert task is not None

    await asyncio.wait_for(client.disconnect(), 1)

    # The closing connection finishes instead of being cancelled
    assert task.done()
    assert not task.cancelled()


@pytest.mark.parametrize("closed_by", ["client", "tv"])
async def test_closing_connection_reports_not_connected(
    client: WebOsClient,
    fake_tv: FakeTv,
    monkeypatch: pytest.MonkeyPatch,
    closed_by: str,
) -> None:
    """Test a closing connection is not connected and rejects commands."""
    monkeypatch.setattr(webos_client, "CLOSE_TIMEOUT", 0.2)
    fake_tv.answer_close = False
    assert await client.connect()

    disconnect = await start_closing(client, fake_tv, closed_by)

    assert not client.is_connected()
    with pytest.raises(WebOsTvCommandError, match="Not connected"):
        await client.get_services()
    with pytest.raises(WebOsTvCommandError, match="Not connected"):
        await client.button("HOME")

    assert client.connect_task is not None
    # Cancelled when the client disconnects, wait for it without its result
    await asyncio.wait({client.connect_task}, timeout=1)
    assert client.connect_task.done()
    if disconnect is not None:
        await disconnect


@pytest.mark.parametrize("closed_by", ["client", "tv"])
async def test_connect_while_closing_reconnects(
    client: WebOsClient,
    fake_tv: FakeTv,
    monkeypatch: pytest.MonkeyPatch,
    closed_by: str,
) -> None:
    """Test connecting while the connection closes waits and connects again.

    Concurrent callers share the new connection attempt.
    """
    monkeypatch.setattr(webos_client, "CLOSE_TIMEOUT", 0.2)
    fake_tv.answer_close = False
    assert await client.connect()
    old_task = client.connect_task

    disconnect = await start_closing(client, fake_tv, closed_by)
    fake_tv.answer_close = True

    connects = asyncio.gather(client.connect(), client.connect())
    assert await asyncio.wait_for(connects, 2) == [True, True]
    if disconnect is not None:
        await disconnect

    assert client.connect_task is not old_task
    assert client.is_connected()
    assert [msg["type"] for msg in fake_tv.received].count("hello") == 2


@pytest.mark.parametrize(
    ("connection", "send", "method"),
    [
        ("connection", "send_json", "get_services"),
        ("input_connection", "send_str", "click"),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionResetError("Cannot write to closing transport"),
        ConnectionResetError("Connection lost"),
    ],
)
async def test_send_error_raises_command_error(
    connected_client: WebOsClient,
    connection: str,
    send: str,
    method: str,
    error: Exception,
) -> None:
    """Test a send failing on a connection the TV closed raises a command error."""
    with (
        patch.object(getattr(connected_client, connection), send, side_effect=error),
        pytest.raises(WebOsTvCommandError, match="Failed to send") as exc_info,
    ):
        await getattr(connected_client, method)()

    assert exc_info.value.__cause__ is error


async def test_disconnect_when_not_connected(client: WebOsClient) -> None:
    """Test disconnecting a client that is not connected."""
    await client.disconnect()

    assert not client.is_connected()


async def test_tv_closes_connection(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test the client cleans up when the TV closes the connection."""
    assert fake_tv.main_ws is not None
    assert connected_client.connect_task is not None

    await fake_tv.main_ws.close()
    await asyncio.wait_for(connected_client.connect_task, 1)

    assert not connected_client.is_connected()
    assert connected_client.tv_state == WebOsTvState()


@pytest.mark.parametrize("socket", ["main", "input"])
async def test_tv_sends_non_text_message(
    connected_client: WebOsClient, fake_tv: FakeTv, socket: str
) -> None:
    """Test a non-text message from the TV closes the connection."""
    ws = fake_tv.main_ws if socket == "main" else fake_tv.input_ws
    assert ws is not None
    assert connected_client.connect_task is not None

    # Messages for unknown ids are ignored
    await ws.send_str('{"id": "unknown"}')
    await ws.send_bytes(b"\x00")
    await asyncio.wait_for(connected_client.connect_task, 1)

    assert not connected_client.is_connected()


async def test_disconnect_while_connecting(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test disconnecting while a connection attempt is in progress."""
    fake_tv.respond_to_hello = False
    connect = asyncio.create_task(client.connect())
    await fake_tv.wait_for_message("hello")

    await client.disconnect()

    assert await connect is False
    assert not client.is_connected()


@pytest.mark.parametrize("closed_by", ["client", "tv"])
async def test_pending_request_fails_on_close(
    connected_client: WebOsClient, fake_tv: FakeTv, closed_by: str
) -> None:
    """Test a request waiting for a response fails when the connection closes.

    The request raises a command error, not CancelledError, the caller was
    not cancelled.
    """
    fake_tv.responses.pop(ep.GET_SERVICES, None)
    request = asyncio.create_task(connected_client.get_services())
    await fake_tv.wait_for_message("request", ep.GET_SERVICES)

    if closed_by == "client":
        await connected_client.disconnect()
    else:
        assert fake_tv.main_ws is not None
        assert connected_client.connect_task is not None
        await fake_tv.main_ws.close()
        await asyncio.wait_for(connected_client.connect_task, 1)

    with pytest.raises(WebOsTvCommandError, match="Connection closed"):
        await request
    assert not request.cancelled()


async def test_request_failing_to_send_on_close(
    connected_client: WebOsClient,
) -> None:
    """Test a request failing to send while closing logs no unhandled error."""
    assert connected_client.connection is not None
    sending = asyncio.Event()

    async def send_json(_message: dict[str, Any]) -> None:
        sending.set()
        # Fail after the connection closed and failed the pending future
        await asyncio.sleep(0.1)
        error = "Cannot write to closing transport"
        raise ConnectionResetError(error)

    unhandled_exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_exceptions.append(context)
    )
    try:
        with patch.object(connected_client.connection, "send_json", send_json):
            request = asyncio.create_task(connected_client.get_services())
            await sending.wait()
            await connected_client.disconnect()
            with pytest.raises(WebOsTvCommandError, match="Failed to send"):
                await request
        # The request traceback references the future, release it so a never
        # retrieved exception is logged when the future is garbage collected
        del request
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert unhandled_exceptions == []


@pytest.mark.usefixtures("fake_tv")
async def test_external_client_session() -> None:
    """Test a provided client session is used and not closed."""
    async with aiohttp.ClientSession() as session:
        client = WebOsClient(HOST, CLIENT_KEY, client_session=session)
        assert await client.connect()
        await client.disconnect()

        assert client.client_session is session
        assert not session.closed


async def test_optional_info_not_available(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test connecting when the TV does not provide optional info."""
    fake_tv.responses[ep.GET_SYSTEM_INFO] = None
    fake_tv.responses[ep.GET_SOFTWARE_INFO] = None
    fake_tv.responses[ep.GET_CONNECTION_INFO] = None
    fake_tv.responses[ep.GET_SOUND_OUTPUT] = None

    assert await client.connect()

    assert client.tv_info.system == {}
    assert client.tv_info.software == {}
    assert client.tv_info.connection == {}
    assert client.tv_state.sound_output is None


@pytest.mark.parametrize(
    ("message", "log"),
    [
        ("not json", "invalid JSON message"),
        ("[1, 2]", "non-object message"),
        ('{"id": [1], "type": "response"}', "message with id [1]"),
    ],
)
async def test_malformed_message_ignored(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    caplog: pytest.LogCaptureFixture,
    message: str,
    log: str,
) -> None:
    """Test a malformed message is logged and does not drop the connection."""
    assert fake_tv.main_ws is not None
    await fake_tv.main_ws.send_str(message)

    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_AUDIO_STATUS,
        {"mute": True},
        lambda state: state.muted is True,
    )

    assert connected_client.is_connected()
    assert log in caplog.text


async def test_message_processing_error_logged(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test an error processing a message is logged and keeps the connection."""
    process_text_message = connected_client._process_text_message
    calls = 0

    def fail_first(data: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "boom"
            raise RuntimeError(msg)
        process_text_message(data)

    with patch.object(connected_client, "_process_text_message", fail_first):
        await fake_tv.push(ep.GET_AUDIO_STATUS, {"mute": True})
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_AUDIO_STATUS,
            {"mute": False},
            lambda state: state.muted is False and calls == 2,
        )

    assert connected_client.is_connected()
    assert "error processing message" in caplog.text
    assert "RuntimeError: boom" in caplog.text


# State updates


async def test_failed_connect_does_not_call_callbacks(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a failed connect does not call the state update callbacks.

    It reported no state, a client retrying a TV that is off must not call
    the callbacks on every attempt.
    """
    callback = AsyncMock()
    await client.register_state_update_callback(callback)
    fake_tv.hello_type = "error"

    with pytest.raises(WebOsTvCommandError):
        await client.connect()
    await assert_disconnected(client)

    callback.assert_not_awaited()

    # A connection that reported state reports the cleared state on close
    fake_tv.hello_type = "hello"
    assert await client.connect()
    callback.assert_awaited_with(client.tv_state)
    callback.reset_mock()

    await client.disconnect()

    callback.assert_awaited_once()
    assert not client.tv_state.is_on


async def test_register_callback_when_connected(
    connected_client: WebOsClient,
) -> None:
    """Test a callback registered while connected is called immediately."""
    callback = AsyncMock()

    await connected_client.register_state_update_callback(callback)

    callback.assert_awaited_once_with(connected_client.tv_state)


async def test_register_failing_callback_when_connected(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test an error from the initial callback call is logged, not raised."""
    calls = 0

    async def failing_callback(_state: WebOsTvState) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "boom"
            raise RuntimeError(msg)

    await connected_client.register_state_update_callback(failing_callback)

    assert "state update callback" in caplog.text
    assert "RuntimeError: boom" in caplog.text

    # Still registered, it receives the next update
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_AUDIO_STATUS,
        {"mute": True},
        lambda state: state.muted is True,
    )
    assert calls == 2


async def test_unregister_and_clear_callbacks(client: WebOsClient) -> None:
    """Test unregistering and clearing state update callbacks."""
    callback1 = AsyncMock()
    callback2 = AsyncMock()
    await client.register_state_update_callback(callback1)
    await client.register_state_update_callback(callback2)

    client.unregister_state_update_callback(callback1)
    client.unregister_state_update_callback(callback1)
    assert client.state_update_callbacks == [callback2]

    client.clear_state_update_callbacks()
    assert client.state_update_callbacks == []


async def test_failing_callback_does_not_stop_state_updates(
    client: WebOsClient, fake_tv: FakeTv, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a failing state update callback is logged and updates continue."""
    failing_callback = AsyncMock(side_effect=RuntimeError("boom"))
    await client.register_state_update_callback(failing_callback)

    unhandled_exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_exceptions.append(context)
    )
    try:
        assert await client.connect()
        for volume in (30, 31):
            await push_and_wait(
                client,
                fake_tv,
                ep.GET_VOLUME,
                {"volumeStatus": {"volume": volume}},
                lambda state, volume=volume: state.volume == volume,
            )
        await client.disconnect()
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert unhandled_exceptions == []
    assert "state update callback" in caplog.text
    assert "RuntimeError: boom" in caplog.text


async def test_disconnect_with_hanging_callback(
    client: WebOsClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a state update callback that never finishes does not block disconnect."""
    monkeypatch.setattr(webos_client, "CLOSEOUT_TIMEOUT", 0.1)
    assert await client.connect()

    cancelled = asyncio.Event()

    async def hanging_callback(state: WebOsTvState) -> None:
        if state.is_on:
            return
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await client.register_state_update_callback(hanging_callback)

    await asyncio.wait_for(client.disconnect(), 1)

    assert not client.is_connected()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert "did not finish within 0.1s" in caplog.text
    assert "_call_state_update_callbacks" in caplog.text


async def test_callback_awaiting_disconnect(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a state update callback awaiting disconnect() does not deadlock.

    The teardown waits for the callback, which waits for the teardown, until
    the teardown gives up after the closeout timeout and cancels the callback.
    """
    monkeypatch.setattr(webos_client, "CLOSEOUT_TIMEOUT", 0.1)
    client = connected_client
    cancelled = asyncio.Event()

    async def disconnect_when_off(state: WebOsTvState) -> None:
        if not state.is_on:
            try:
                await client.disconnect()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    await client.register_state_update_callback(disconnect_when_off)
    assert fake_tv.main_ws is not None
    assert client.connect_task is not None

    await fake_tv.main_ws.close()
    # Cancelled by the callback's disconnect(), wait for it without its result
    await asyncio.wait({client.connect_task}, timeout=1)

    assert client.connect_task.done()
    assert not client.is_connected()
    await asyncio.wait_for(cancelled.wait(), 1)
    assert "did not finish within 0.1s" in caplog.text


@pytest.mark.parametrize(
    ("uri", "payload", "attribute", "expected"),
    [
        (ep.GET_VOLUME, {"volumeStatus": {"volume": 20}}, "volume", 20),
        (ep.GET_VOLUME, {"volume": 21, "supportvolume": False}, "volume", 21),
        (
            ep.GET_VOLUME,
            {
                "volumeStatus": {
                    "volume": 22,
                    "soundOutput": "external_arc",
                    "volumeSyncable": False,
                }
            },
            "volume_status",
            {"volume": 22, "soundOutput": "external_arc", "volumeSyncable": False},
        ),
        (
            ep.GET_VOLUME,
            {
                "volume": 23,
                "scenario": "mastervolume_tv_speaker",
                "supportvolume": True,
            },
            "volume_status",
            {
                "volume": 23,
                "scenario": "mastervolume_tv_speaker",
                "supportvolume": True,
                "returnValue": True,
            },
        ),
        (ep.GET_AUDIO_STATUS, {"mute": True}, "muted", True),
        (
            ep.GET_SOUND_OUTPUT,
            {"soundOutput": "external_arc"},
            "sound_output",
            "external_arc",
        ),
        (
            ep.GET_MEDIA_FOREGROUND_APP_INFO,
            {"foregroundAppInfo": [{"playState": "playing"}]},
            "media_state",
            [{"playState": "playing"}],
        ),
        (
            ep.GET_INPUTS,
            {"devices": [{"appId": "hdmi2"}]},
            "inputs",
            {"hdmi2": {"appId": "hdmi2"}},
        ),
        (ep.GET_CURRENT_APP_INFO, {"appId": "netflix"}, "current_app_id", "netflix"),
    ],
)
async def test_subscription_updates(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    uri: str,
    payload: dict[str, Any],
    attribute: str,
    expected: object,
) -> None:
    """Test subscription updates change the TV state."""
    await push_and_wait(
        connected_client,
        fake_tv,
        uri,
        payload,
        lambda state: getattr(state, attribute) == expected,
    )


async def test_subscribe_volume(connected_client: WebOsClient) -> None:
    """Test subscribing to volume level changes and setting the volume state."""
    callback = AsyncMock()
    await connected_client.subscribe_volume(callback)
    callback.assert_awaited_once_with(10)

    await connected_client.set_volume_state(30)
    assert connected_client.tv_state.volume == 30


@pytest.mark.parametrize(
    ("power_state", "is_on", "is_screen_on"),
    [
        ("Active", True, True),
        ("Screen Off", True, False),
        ("Active Standby", False, False),
        ("Suspend", False, False),
        ("Power Off", False, False),
    ],
)
async def test_power_state_updates(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    power_state: str,
    is_on: bool,  # noqa: FBT001
    is_screen_on: bool,  # noqa: FBT001
) -> None:
    """Test power state updates set the on and screen on state."""
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_POWER_STATE,
        {"state": power_state},
        lambda state: state.power_state.get("state") == power_state,
    )

    assert connected_client.tv_state.is_on is is_on
    assert connected_client.tv_state.is_screen_on is is_screen_on


@pytest.mark.parametrize(
    ("app_id", "is_on"), [(None, False), ("", False), ("netflix", True)]
)
async def test_is_on_without_power_state(
    client: WebOsClient,
    app_id: str | None,
    is_on: bool,  # noqa: FBT001
) -> None:
    """Test older TVs without a power state use the current app."""
    client.tv_state.current_app_id = app_id

    await client.set_power_state({})

    assert client.tv_state.is_on is is_on


async def test_apps_updates(connected_client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test app added and removed updates."""
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_APPS,
        {"change": "added", "id": "youtube", "title": "YouTube"},
        lambda state: "youtube" in state.apps,
    )
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_APPS,
        {"change": "removed", "id": "netflix"},
        lambda state: "netflix" not in state.apps,
    )

    assert list(connected_client.tv_state.apps) == ["youtube"]


@pytest.mark.parametrize(
    ("uri", "attribute", "expected"),
    [
        (ep.GET_POWER_STATE, "power_state", {}),
        (ep.GET_CURRENT_APP_INFO, "current_app_id", None),
        (ep.GET_AUDIO_STATUS, "muted", None),
        (ep.GET_VOLUME, "volume_status", {}),
        (ep.GET_APPS, "apps", {}),
        (ep.GET_INPUTS, "inputs", {}),
        (ep.GET_SOUND_OUTPUT, "sound_output", None),
        (ep.GET_MEDIA_FOREGROUND_APP_INFO, "media_state", []),
    ],
)
async def test_subscription_not_found_at_connect(
    client: WebOsClient,
    fake_tv: FakeTv,
    monkeypatch: pytest.MonkeyPatch,
    uri: str,
    attribute: str,
    expected: object,
) -> None:
    """Test a state subscription the TV does not support does not fail connect.

    The error is not passed to the callback, a subscription callback that
    failed on it would leave the subscribe request waiting for its timeout.
    """
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 1)
    fake_tv.responses[uri] = None

    async with asyncio.timeout(0.5):
        assert await client.connect()

    assert getattr(client.tv_state, attribute) == expected


async def test_subscription_error_update_ignored(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test an error message on a subscription does not change the state."""
    assert fake_tv.main_ws is not None
    await fake_tv.main_ws.send_json(
        {
            "type": "error",
            "id": fake_tv.subscriptions[ep.GET_INPUTS],
            "error": "500 Application error",
            "payload": {},
        }
    )
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_INPUTS,
        {"devices": [{"appId": "hdmi2"}]},
        lambda state: "hdmi2" in state.inputs,
    )

    assert list(connected_client.tv_state.inputs) == ["hdmi2"]


async def test_failing_subscription_callback_keeps_handling_updates(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a failing subscription callback is logged and updates continue."""
    uid = fake_tv.subscriptions[ep.GET_APPS]
    # Removing an unknown app fails in set_apps_state
    await fake_tv.push(ep.GET_APPS, {"change": "removed", "id": "unknown"})
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_APPS,
        {"change": "added", "id": "youtube", "title": "YouTube"},
        lambda state: "youtube" in state.apps,
    )

    assert not connected_client.callback_tasks[uid].done()
    assert "subscription callback" in caplog.text
    assert "KeyError: 'unknown'" in caplog.text


async def test_subscribe_slow_callback(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test a callback slower than the request timeout does not time out.

    The request timeout covers the TV's response only, subscribe still
    returns after the callback processed the first update.
    """
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 0.1)
    fake_tv.responses[ep.GET_SERVICES] = {"services": [], "returnValue": True}
    processed = asyncio.Event()

    async def slow_callback(_payload: dict[str, Any]) -> None:
        await asyncio.sleep(0.3)
        processed.set()

    response = await connected_client.subscribe(slow_callback, ep.GET_SERVICES)

    assert response == {"services": [], "returnValue": True}
    assert processed.is_set()


async def test_connect_slow_state_callback(
    client: WebOsClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a state callback slower than the request timeout does not fail connect.

    The current app callback can take long, it subscribes to the channels
    and the current channel when Live TV runs.
    """
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 0.1)
    set_current_app_state = client.set_current_app_state

    async def slow_set_current_app_state(app_id: str) -> None:
        await asyncio.sleep(0.3)
        await set_current_app_state(app_id)

    monkeypatch.setattr(client, "set_current_app_state", slow_set_current_app_state)

    assert await client.connect()

    assert client.tv_state.current_app_id == "com.webos.app.hdmi1"


async def test_subscribe_replaces_subscription(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test subscribing to a subscribed uri replaces the subscription."""
    client = connected_client
    fake_tv.responses[ep.GET_SERVICES] = {"services": [], "returnValue": True}
    first: list[dict[str, Any]] = []
    second: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def first_callback(payload: dict[str, Any]) -> None:
        first.append(payload)

    async def second_callback(payload: dict[str, Any]) -> None:
        second.put_nowait(payload)

    await client.subscribe(first_callback, ep.GET_SERVICES)
    first_uid = client._subscriptions[ep.GET_SERVICES]
    first_task = client.callback_tasks[first_uid]

    await client.subscribe(second_callback, ep.GET_SERVICES)

    assert first_task.done()
    assert first_uid not in client.callback_tasks
    assert first_uid not in client.callback_queues
    assert {"type": "unsubscribe", "id": first_uid} in [
        {"type": msg["type"], "id": msg["id"]}
        for msg in fake_tv.received
        if msg["type"] == "unsubscribe"
    ]

    await asyncio.wait_for(second.get(), 1)
    await fake_tv.push(ep.GET_SERVICES, {"services": ["new"]})
    assert (await asyncio.wait_for(second.get(), 1))["services"] == ["new"]
    assert len(first) == 1


async def test_resubscribe_deprecated(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test resubscribe warns and replaces the subscription like subscribe."""
    client = connected_client
    fake_tv.responses[ep.GET_SERVICES] = {"services": [], "returnValue": True}
    callback = AsyncMock()
    await client.subscribe(callback, ep.GET_SERVICES)
    first_uid = client._subscriptions[ep.GET_SERVICES]

    with pytest.warns(DeprecationWarning, match="use subscribe"):
        await client.resubscribe(callback, ep.GET_SERVICES)

    assert client._subscriptions[ep.GET_SERVICES] != first_uid
    assert first_uid not in client.callback_tasks


async def test_subscribe_closed_before_first_update_processed(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test subscribe fails when the connection closes during the first update."""
    fake_tv.responses[ep.GET_SERVICES] = {"services": [], "returnValue": True}
    processing = asyncio.Event()

    async def hanging_callback(_payload: dict[str, Any]) -> None:
        processing.set()
        await asyncio.Event().wait()

    subscribe = asyncio.create_task(
        connected_client.subscribe(hanging_callback, ep.GET_SERVICES)
    )
    await asyncio.wait_for(processing.wait(), 1)

    await connected_client.disconnect()

    with pytest.raises(WebOsTvCommandError, match="closed"):
        await asyncio.wait_for(subscribe, 1)
    assert ep.GET_SERVICES not in connected_client._subscriptions


async def test_live_tv_channel_subscriptions(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test switching to Live TV subscribes to the current channel and info."""
    current_channel = fake_tv.responses[ep.GET_CURRENT_CHANNEL]
    channel_info = fake_tv.responses[ep.GET_CHANNEL_INFO]

    def on_live_tv(state: WebOsTvState) -> bool:
        return (
            state.current_channel == current_channel
            and state.channel_info == channel_info
        )

    with patch("aiowebostv.webos_client.asyncio.sleep") as mock_sleep:
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            on_live_tv,
        )
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.hdmi1"},
            lambda state: state.current_channel is None and state.channel_info is None,
        )
        # Switching back resubscribes, unsubscribing the previous subscriptions
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            on_live_tv,
        )

    mock_sleep.assert_awaited_with(2)
    unsubscribed = [
        msg["uri"] for msg in fake_tv.received if msg["type"] == "unsubscribe"
    ]
    assert unsubscribed == [
        f"ssap://{ep.GET_CURRENT_CHANNEL}",
        f"ssap://{ep.GET_CHANNEL_INFO}",
    ]


async def test_leaving_live_tv_unsubscribes_channel(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test leaving Live TV unsubscribes the current channel and info.

    A channel update the TV sends after leaving Live TV must not set a stale
    channel.
    """
    with patch("aiowebostv.webos_client.asyncio.sleep"):
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            lambda state: state.channel_info is not None,
        )
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_CURRENT_APP_INFO,
        {"appId": "com.webos.app.hdmi1"},
        lambda state: state.current_channel is None and state.channel_info is None,
    )

    assert ep.GET_CURRENT_CHANNEL not in connected_client._subscriptions
    assert ep.GET_CHANNEL_INFO not in connected_client._subscriptions

    # A late update on the old subscription is ignored, the mute update after
    # it signals that both were handled
    await fake_tv.push(ep.GET_CURRENT_CHANNEL, {"channelId": "2"})
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_AUDIO_STATUS,
        {"mute": True},
        lambda state: state.muted is True,
    )

    assert connected_client.tv_state.current_channel is None
    assert connected_client.tv_state.channel_info is None


async def test_repeated_live_tv_update_keeps_channel(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a repeated Live TV app update keeps the channel subscriptions."""
    handled: asyncio.Queue[str] = asyncio.Queue()
    set_current_app_state = client.set_current_app_state

    async def signal_set_current_app_state(app_id: str) -> None:
        await set_current_app_state(app_id)
        handled.put_nowait(app_id)

    monkeypatch.setattr(client, "set_current_app_state", signal_set_current_app_state)
    assert await client.connect()
    assert await asyncio.wait_for(handled.get(), 1) == "com.webos.app.hdmi1"

    with patch("aiowebostv.webos_client.asyncio.sleep"):
        for _ in range(2):
            await fake_tv.push(
                ep.GET_CURRENT_APP_INFO, {"appId": "com.webos.app.livetv"}
            )
            assert await asyncio.wait_for(handled.get(), 1) == "com.webos.app.livetv"
            assert client.tv_state.current_channel is not None
            assert client.tv_state.channel_info is not None

    assert not any(msg["type"] == "unsubscribe" for msg in fake_tv.received)


async def test_unsubscribe_send_failure_deletes_handler(
    connected_client: WebOsClient,
) -> None:
    """Test unsubscribe deletes the handler when the command fails to send."""
    assert ep.GET_POWER_STATE in connected_client._subscriptions
    uid = connected_client._subscriptions[ep.GET_POWER_STATE]

    with (
        patch.object(
            connected_client,
            "command",
            side_effect=WebOsTvCommandError("Failed to send command"),
        ),
        pytest.raises(WebOsTvCommandError),
    ):
        await connected_client.unsubscribe(ep.GET_POWER_STATE)

    assert ep.GET_POWER_STATE not in connected_client._subscriptions
    assert uid not in connected_client.callback_tasks


CHANNEL_ERROR = {
    "returnValue": False,
    "errorText": "broadcastId = 00000047 is already closed",
}


async def test_current_channel_failed_subscribe(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a failed current channel subscribe does not store the error."""
    fake_tv.responses[ep.GET_CURRENT_CHANNEL] = CHANNEL_ERROR

    # Wait for the failed subscription to be cleaned up before teardown
    deleted = asyncio.Event()
    delete_subscription_handler = connected_client.delete_subscription_handler

    async def delete_and_signal(uri: str) -> None:
        await delete_subscription_handler(uri)
        if uri == ep.GET_CURRENT_CHANNEL:
            deleted.set()

    with (
        patch("aiowebostv.webos_client.asyncio.sleep"),
        patch.object(
            connected_client, "delete_subscription_handler", delete_and_signal
        ),
    ):
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            lambda state: (
                state.current_app_id == "com.webos.app.livetv"
                and state.current_channel is None
            ),
        )
        await asyncio.wait_for(deleted.wait(), 1)

    assert f"ssap://{ep.GET_CHANNEL_INFO}" not in fake_tv.received_uris()


async def test_current_channel_error_update(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test an error update on the current channel subscription clears it."""
    with patch("aiowebostv.webos_client.asyncio.sleep"):
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            lambda state: state.current_channel is not None,
        )

    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_CURRENT_CHANNEL,
        CHANNEL_ERROR,
        lambda state: state.current_channel is None,
    )

    # The next Live TV update replaces the failed subscription
    with patch("aiowebostv.webos_client.asyncio.sleep"):
        await push_and_wait(
            connected_client,
            fake_tv,
            ep.GET_CURRENT_APP_INFO,
            {"appId": "com.webos.app.livetv"},
            lambda state: state.current_channel is not None,
        )

    await fake_tv.wait_for_message("unsubscribe", ep.GET_CURRENT_CHANNEL)


# Requests


@pytest.mark.parametrize(
    ("method", "uri", "payload", "expected"),
    [
        ("get_power_state", ep.GET_POWER_STATE, {"state": "Active"}, FULL_PAYLOAD),
        ("get_apps", ep.GET_APPS, {"launchPoints": [1]}, [1]),
        ("get_apps_all", ep.GET_APPS_ALL, {"apps": [2]}, [2]),
        ("get_current_app", ep.GET_CURRENT_APP_INFO, {"appId": "netflix"}, "netflix"),
        ("get_input", ep.GET_CURRENT_APP_INFO, {"appId": "hdmi1"}, "hdmi1"),
        ("get_services", ep.GET_SERVICES, {"services": [3]}, [3]),
        ("get_software_info", ep.GET_SOFTWARE_INFO, {"a": 1}, FULL_PAYLOAD),
        ("get_system_info", ep.GET_SYSTEM_INFO, {"a": 1}, FULL_PAYLOAD),
        ("get_connection_info", ep.GET_CONNECTION_INFO, {"a": 1}, FULL_PAYLOAD),
        ("get_inputs", ep.GET_INPUTS, {"devices": [4]}, [4]),
        ("get_audio_status", ep.GET_AUDIO_STATUS, {"mute": True}, FULL_PAYLOAD),
        ("get_muted", ep.GET_AUDIO_STATUS, {"mute": True}, True),
        ("get_volume", ep.GET_VOLUME, {"volumeStatus": {"volume": 7}}, 7),
        ("get_volume", ep.GET_VOLUME, {"volume": 8}, 8),
        ("get_channels", ep.GET_TV_CHANNELS, {"channelList": [5]}, [5]),
        ("get_current_channel", ep.GET_CURRENT_CHANNEL, {"a": 1}, FULL_PAYLOAD),
        ("get_channel_info", ep.GET_CHANNEL_INFO, {"a": 1}, FULL_PAYLOAD),
        ("get_sound_output", ep.GET_SOUND_OUTPUT, {"soundOutput": "x"}, "x"),
        (
            "get_media_foreground_app",
            ep.GET_MEDIA_FOREGROUND_APP_INFO,
            {"foregroundAppInfo": [6]},
            [6],
        ),
    ],
)
async def test_get_requests(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    method: str,
    uri: str,
    payload: dict[str, Any],
    expected: object,
) -> None:
    """Test requests that return information from the TV."""
    response = {**payload, "returnValue": True}
    fake_tv.responses[uri] = response

    result = await getattr(connected_client, method)()

    assert result == (response if expected is FULL_PAYLOAD else expected)
    assert fake_tv.received_uris()[-1] == f"ssap://{uri}"


@pytest.mark.parametrize(
    ("method", "args", "uri", "payload"),
    [
        ("launch_app", ("netflix",), ep.LAUNCH, {"id": "netflix"}),
        (
            "launch_app_with_params",
            ("netflix", {"a": 1}),
            ep.LAUNCH,
            {"id": "netflix", "params": {"a": 1}},
        ),
        (
            "launch_app_with_content_id",
            ("netflix", "c1"),
            ep.LAUNCH,
            {"id": "netflix", "contentId": "c1"},
        ),
        ("close_app", ("netflix",), ep.LAUNCHER_CLOSE, {"id": "netflix"}),
        ("power_on", (), ep.POWER_ON, {}),
        ("turn_3d_on", (), ep.SET_3D_ON, {}),
        ("turn_3d_off", (), ep.SET_3D_OFF, {}),
        ("set_input", ("hdmi2",), ep.SET_INPUT, {"inputId": "hdmi2"}),
        ("set_mute", (True,), ep.SET_MUTE, {"mute": True}),
        ("set_volume", (15,), ep.SET_VOLUME, {"volume": 15}),
        ("set_volume", (-5,), ep.SET_VOLUME, {"volume": 0}),
        ("volume_up", (), ep.VOLUME_UP, {}),
        ("volume_down", (), ep.VOLUME_DOWN, {}),
        ("channel_up", (), ep.TV_CHANNEL_UP, {}),
        ("channel_down", (), ep.TV_CHANNEL_DOWN, {}),
        ("set_channel", ("2",), ep.SET_CHANNEL, {"channelId": "2"}),
        (
            "change_sound_output",
            ("external_arc",),
            ep.CHANGE_SOUND_OUTPUT,
            {"output": "external_arc"},
        ),
        ("play", (), ep.MEDIA_PLAY, {}),
        ("pause", (), ep.MEDIA_PAUSE, {}),
        ("stop", (), ep.MEDIA_STOP, {}),
        ("close", (), ep.MEDIA_CLOSE, {}),
        ("rewind", (), ep.MEDIA_REWIND, {}),
        ("fast_forward", (), ep.MEDIA_FAST_FORWARD, {}),
        (
            "send_message",
            ("Hello",),
            ep.SHOW_MESSAGE,
            {"message": "Hello", "iconData": "", "iconExtension": ""},
        ),
    ],
)
async def test_commands(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    method: str,
    args: tuple[Any, ...],
    uri: str,
    payload: dict[str, Any],
) -> None:
    """Test commands send the expected request to the TV."""
    fake_tv.responses[uri] = {"returnValue": True}

    assert await getattr(connected_client, method)(*args) == {"returnValue": True}

    request = fake_tv.received[-1]
    assert request["uri"] == f"ssap://{uri}"
    assert request["payload"] == payload


async def test_send_message_with_icon(
    connected_client: WebOsClient, fake_tv: FakeTv, tmp_path: Path
) -> None:
    """Test sending a message with an icon."""
    icon = tmp_path / "icon.png"
    icon.write_bytes(b"icon-data")
    fake_tv.responses[ep.SHOW_MESSAGE] = {"returnValue": True}

    await connected_client.send_message("Hello", icon_path=str(icon))

    assert fake_tv.received[-1]["payload"] == {
        "message": "Hello",
        "iconData": base64.b64encode(b"icon-data").decode("ascii"),
        "iconExtension": "png",
    }


@pytest.mark.parametrize(
    ("power_state", "on", "uri"),
    [
        ("Active", False, ep.TURN_OFF_SCREEN),
        ("Screen Off", True, ep.TURN_ON_SCREEN),
        ("Active", True, None),
        ("Screen Off", False, None),
    ],
)
async def test_set_screen_state(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    power_state: str,
    on: bool,  # noqa: FBT001
    uri: str | None,
) -> None:
    """Test turning the screen on and off only when needed."""
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_POWER_STATE,
        {"state": power_state},
        lambda state: state.power_state.get("state") == power_state,
    )
    if uri is not None:
        fake_tv.responses[uri] = {"returnValue": True}
    request_count = len(fake_tv.received_uris())

    await connected_client.set_screen_state(on)

    sent = fake_tv.received_uris()[request_count:]
    assert sent == ([f"ssap://{uri}"] if uri else [])


@pytest.mark.parametrize(
    ("power_state", "sent"), [("Active", True), ("Suspend", False)]
)
async def test_power_off(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    power_state: str,
    sent: bool,  # noqa: FBT001
) -> None:
    """Test powering off only sends the command when the TV is on."""
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_POWER_STATE,
        {"state": power_state},
        lambda state: state.power_state.get("state") == power_state,
    )

    await connected_client.power_off()
    # Power off does not wait for a response, a request makes sure it arrived
    await connected_client.get_power_state()

    assert (f"ssap://{ep.POWER_OFF}" in fake_tv.received_uris()) is sent


@pytest.mark.parametrize(
    ("mac", "expected"),
    [
        ("AA:BB:CC:00:00:01", "aa:bb:cc:00:00:01"),
        ("aa-bb-cc-00-00-01", "aa:bb:cc:00:00:01"),
        ("AABB.CC00.0001", "aa:bb:cc:00:00:01"),
        ("AABBCC000001", "aa:bb:cc:00:00:01"),
        ("", None),
        ("not-a-mac", None),
        ("GG:BB:CC:00:00:01", None),
        ("AA:BB:CC:00:00:01/AA:BB:CC:00:00:02", None),
    ],
)
async def test_normalize_mac(mac: str, expected: str | None) -> None:
    """Test normalizing MAC addresses."""
    assert webos_client.normalize_mac(mac) == expected


@pytest.mark.parametrize(
    ("connection", "expected"),
    [
        (
            {
                "wiredInfo": {"macAddress": "AA:BB:CC:00:00:01"},
                "wifiInfo": {"macAddress": "AA:BB:CC:00:00:02"},
                "p2pInfo": {"macAddress": "AA:BB:CC:00:00:03"},
            },
            {"wired": "aa:bb:cc:00:00:01", "wifi": "aa:bb:cc:00:00:02"},
        ),
        (
            {"wifiInfo": {"macAddress": "AA:BB:CC:00:00:02"}},
            {"wifi": "aa:bb:cc:00:00:02"},
        ),
        # p2pInfo is the Wi-Fi Direct MAC address and can't wake the TV
        ({"p2pInfo": {"macAddress": "AA:BB:CC:00:00:03"}}, {}),
        # webOS 2.0 has no connection info service
        ({}, {}),
        ({"wiredInfo": {}, "wifiInfo": {"macAddress": ""}}, {}),
        ({"wiredInfo": {"macAddress": "invalid"}, "wifiInfo": None}, {}),
        ({"wiredInfo": {"macAddress": None}, "wifiInfo": "invalid"}, {}),
    ],
)
async def test_parse_mac_addresses(
    connection: dict[str, Any], expected: dict[str, str]
) -> None:
    """Test MAC addresses are read from the connection info."""
    assert webos_client.parse_mac_addresses(connection) == expected


async def test_mac_addresses_copy(connected_client: WebOsClient) -> None:
    """Test changing the returned MAC addresses does not change the client."""
    connected_client.mac_addresses.clear()

    assert connected_client.mac_addresses


class WolReceiver(asyncio.DatagramProtocol):
    """Receive Wake-on-LAN packets."""

    def __init__(self) -> None:
        """Initialize the receiver."""
        self.port = 0
        self.packets: asyncio.Queue[bytes] = asyncio.Queue()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:  # noqa: ARG002
        """Store a received packet."""
        self.packets.put_nowait(data)

    async def received(self, count: int) -> list[bytes]:
        """Return the received packets, asserting the exact count."""
        async with asyncio.timeout(1):
            packets = [await self.packets.get() for _ in range(count)]
        await asyncio.sleep(0.05)
        assert self.packets.empty()
        return packets


@pytest_asyncio.fixture
async def wol(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[WolReceiver]:
    """Receive Wake-on-LAN packets sent to HOST without delay between rounds."""
    monkeypatch.setattr(webos_client, "WOL_PACKET_INTERVAL", 0)
    transport, receiver = await asyncio.get_running_loop().create_datagram_endpoint(
        WolReceiver, local_addr=(HOST, 0)
    )
    receiver.port = transport.get_extra_info("sockname")[1]
    yield receiver
    transport.close()


def packets_for(*macs: str) -> list[bytes]:
    """Return the expected magic packets for all rounds."""
    return [create_magic_packet(mac) for mac in macs] * webos_client.WOL_PACKET_COUNT


async def test_turn_on_when_on(
    connected_client: WebOsClient, fake_tv: FakeTv, wol: WolReceiver
) -> None:
    """Test turning on a TV that is already on does nothing."""
    await connected_client.turn_on(broadcast_address=HOST, port=wol.port)

    assert f"ssap://{ep.POWER_ON}" not in fake_tv.received_uris()
    assert await wol.received(0) == []


async def test_turn_on_standby(
    connected_client: WebOsClient, fake_tv: FakeTv, wol: WolReceiver
) -> None:
    """Test a connected TV in standby is turned on with Wake-on-LAN.

    Newer TVs don't support the turn on request (404).
    """
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_POWER_STATE,
        {"state": "Active Standby"},
        lambda state: not state.is_on,
    )

    await connected_client.turn_on(broadcast_address=HOST, port=wol.port)

    assert f"ssap://{ep.POWER_ON}" not in fake_tv.received_uris()
    assert await wol.received(6) == packets_for(WIRED_MAC, WIFI_MAC)


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_mac_addresses(wol: WolReceiver) -> None:
    """Test turning on a disconnected TV with given MAC addresses."""
    client = WebOsClient(HOST, CLIENT_KEY)

    await client.turn_on(
        # The same MAC address in another format is sent once
        [WIRED_MAC, WIRED_MAC.lower().replace(":", "-"), WIFI_MAC],
        broadcast_address=HOST,
        port=wol.port,
    )

    assert await wol.received(6) == packets_for(WIRED_MAC, WIFI_MAC)


async def test_turn_on_last_mac_addresses(
    client: WebOsClient, fake_tv: FakeTv, wol: WolReceiver
) -> None:
    """Test the MAC addresses are remembered when reconnecting fails."""
    assert await client.connect()
    await client.disconnect()
    fake_tv.booting = True

    with pytest.raises(WebOsTvRegistrationError):
        await client.connect()

    await assert_disconnected(client)
    assert client.tv_info.connection == {}
    assert client.mac_addresses == {
        "wired": WIRED_MAC.lower(),
        "wifi": WIFI_MAC.lower(),
    }

    await client.turn_on(broadcast_address=HOST, port=wol.port)

    # The p2p (Wi-Fi Direct) MAC address is not used
    assert await wol.received(6) == packets_for(WIRED_MAC, WIFI_MAC)


async def test_turn_on_given_mac_addresses_preferred(
    client: WebOsClient, wol: WolReceiver
) -> None:
    """Test given MAC addresses are used over the remembered ones."""
    assert await client.connect()
    await client.disconnect()

    await client.turn_on(["AA:BB:CC:00:00:09"], broadcast_address=HOST, port=wol.port)

    assert await wol.received(3) == packets_for("AA:BB:CC:00:00:09")


async def test_turn_on_no_mac_address(client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test turning on fails when the TV never reported a MAC address."""
    # webOS 2.0 has no connection info service
    fake_tv.responses[ep.GET_CONNECTION_INFO] = None
    assert await client.connect()
    assert client.mac_addresses == {}
    await push_and_wait(
        client,
        fake_tv,
        ep.GET_POWER_STATE,
        {"state": "Active Standby"},
        lambda state: not state.is_on,
    )

    # Connected in standby, no turn on request is sent
    with pytest.raises(WebOsTvWakeError):
        await client.turn_on()
    assert f"ssap://{ep.POWER_ON}" not in fake_tv.received_uris()

    await client.disconnect()
    with pytest.raises(WebOsTvWakeError):
        await client.turn_on()

    with pytest.raises(WebOsTvWakeError):
        await client.turn_on([])


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_invalid_mac_address() -> None:
    """Test turning on with an invalid MAC address."""
    client = WebOsClient(HOST, CLIENT_KEY)

    with pytest.raises(ValueError, match="Invalid MAC address: invalid"):
        await client.turn_on([WIRED_MAC, "invalid"])


async def test_create_wake_socket() -> None:
    """Test the Wake-on-LAN socket is non-blocking and can broadcast."""
    with webos_client._create_wake_socket() as sock:
        assert not sock.getblocking()
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST)


@pytest.mark.parametrize("failing", ["socket", "setsockopt"])
@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_socket_error(failing: str) -> None:
    """Test failing to create the socket raises a wake error."""
    client = WebOsClient(HOST, CLIENT_KEY)
    error = PermissionError("Operation not permitted")
    sock = MagicMock()
    socket_cls = MagicMock(return_value=sock)
    if failing == "socket":
        socket_cls.side_effect = error
    else:
        sock.setsockopt.side_effect = error

    with (
        patch.object(webos_client.socket, "socket", socket_cls),
        pytest.raises(WebOsTvWakeError, match="Operation not permitted") as exc_info,
    ):
        await client.turn_on([WIRED_MAC])

    assert exc_info.value.__cause__ is error
    if failing == "setsockopt":
        sock.close.assert_called_once()


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_all_packets_failed() -> None:
    """Test a wake error when every packet fails to send."""
    client = WebOsClient(HOST, CLIENT_KEY)
    sock = MagicMock()
    error = OSError("No route to host")
    sock.sendto.side_effect = error

    with (
        patch.object(webos_client, "_create_wake_socket", return_value=sock),
        patch.object(webos_client.asyncio, "sleep", AsyncMock()),
        pytest.raises(WebOsTvWakeError, match="No route to host") as exc_info,
    ):
        await client.turn_on([WIRED_MAC, WIFI_MAC])

    assert exc_info.value.__cause__ is error
    assert sock.sendto.call_count == 2 * webos_client.WOL_PACKET_COUNT
    sock.close.assert_called_once()


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_some_packets_failed(caplog: pytest.LogCaptureFixture) -> None:
    """Test no error when some packets were sent, they may wake the TV."""
    caplog.set_level(logging.DEBUG)
    client = WebOsClient(HOST, CLIENT_KEY)
    sock = MagicMock()
    sock.sendto.side_effect = [
        BlockingIOError("Resource temporarily unavailable"),
        *[None] * (webos_client.WOL_PACKET_COUNT - 1),
    ]

    with (
        patch.object(webos_client, "_create_wake_socket", return_value=sock),
        patch.object(webos_client.asyncio, "sleep", AsyncMock()),
    ):
        await client.turn_on([WIRED_MAC])

    assert sock.sendto.call_count == webos_client.WOL_PACKET_COUNT
    assert (
        f"1 of {webos_client.WOL_PACKET_COUNT} Wake-on-LAN packets failed: "
        "Resource temporarily unavailable"
    ) in caplog.text


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_packet_rounds() -> None:
    """Test the packet rounds are spaced and the socket is closed."""
    client = WebOsClient(HOST, CLIENT_KEY)
    sock = MagicMock()

    with (
        patch.object(webos_client, "_create_wake_socket", return_value=sock),
        patch.object(webos_client.asyncio, "sleep", AsyncMock()) as sleep,
    ):
        await client.turn_on([WIRED_MAC, WIFI_MAC])

    assert sleep.await_count == webos_client.WOL_PACKET_COUNT - 1
    sleep.assert_awaited_with(webos_client.WOL_PACKET_INTERVAL)
    assert [c.args for c in sock.sendto.call_args_list] == [
        (packet, ("255.255.255.255", 9)) for packet in packets_for(WIRED_MAC, WIFI_MAC)
    ]
    sock.close.assert_called_once()


@pytest.mark.usefixtures("fake_tv")
async def test_turn_on_cancelled_closes_socket() -> None:
    """Test cancelling between packet rounds closes the socket."""
    client = WebOsClient(HOST, CLIENT_KEY)
    sock = MagicMock()
    sleeping = asyncio.Event()

    async def sleep(_delay: float) -> None:
        sleeping.set()
        await asyncio.Event().wait()

    with (
        patch.object(webos_client, "_create_wake_socket", return_value=sock),
        patch.object(webos_client.asyncio, "sleep", sleep),
    ):
        task = asyncio.create_task(client.turn_on([WIRED_MAC]))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    sock.sendto.assert_called_once()
    sock.close.assert_called_once()


async def test_volume_step_delay(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test volume steps are delayed for sound outputs that need it."""
    await push_and_wait(
        connected_client,
        fake_tv,
        ep.GET_SOUND_OUTPUT,
        {"soundOutput": "external_arc"},
        lambda state: state.sound_output == "external_arc",
    )
    fake_tv.responses[ep.VOLUME_UP] = {"returnValue": True}
    loop = asyncio.get_running_loop()

    connected_client.set_volume_step_delay(100)
    start = loop.time()
    await connected_client.volume_up()
    assert loop.time() - start >= 0.08

    connected_client.set_volume_step_delay(None)
    with patch("aiowebostv.webos_client.asyncio.sleep") as mock_sleep:
        await connected_client.volume_up()
    mock_sleep.assert_not_called()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            {"type": "error", "error": NOT_FOUND, "payload": {}},
            WebOsTvServiceNotFoundError,
        ),
        ({"type": "error", "error": "500", "payload": {}}, WebOsTvResponseTypeError),
        ({"type": "response"}, WebOsTvCommandError),
        ({"type": "response", "payload": {}}, WebOsTvCommandError),
        ({"type": "response", "payload": {"returnValue": False}}, WebOsTvCommandError),
        ({"type": "response", "payload": {"subscription": False}}, WebOsTvCommandError),
    ],
)
async def test_parse_response_errors(
    client: WebOsClient, response: dict[str, Any], error: type[Exception]
) -> None:
    """Test invalid and error responses raise."""
    with pytest.raises(error):
        client._parse_response(response)


async def test_request_service_not_found(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a request for a service the TV does not have."""
    fake_tv.responses[ep.GET_SERVICES] = None

    with pytest.raises(WebOsTvServiceNotFoundError):
        await connected_client.get_services()


async def test_request_timeout(
    connected_client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test a request times out when the TV does not respond."""
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 0.05)
    fake_tv.responses.pop(ep.GET_SERVICES, None)

    with pytest.raises(WebOsTvCommandTimeoutError):
        await connected_client.get_services()

    assert connected_client.futures == {}


async def test_subscribe_failure_cleans_up(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a failed subscription removes its handler."""
    fake_tv.responses[ep.GET_SERVICES] = None

    with pytest.raises(WebOsTvServiceNotFoundError):
        await connected_client.subscribe(AsyncMock(), ep.GET_SERVICES)

    assert ep.GET_SERVICES not in connected_client._subscriptions


async def test_delete_subscription_handler_propagates_cancel(
    client: WebOsClient,
) -> None:
    """Test deleting a subscription handler does not swallow our cancellation.

    A failed subscribe deletes its handler from inside another callback task,
    which disconnect() may cancel. Swallowing that cancel leaves the callback
    task running and disconnect() waits for it forever.
    """
    in_callback = asyncio.Event()
    release = asyncio.Event()

    async def slow_callback(_payload: dict[str, Any]) -> None:
        in_callback.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Keep the handler busy after its cancel, so deleting it waits
            await release.wait()
            raise

    await client.create_subscription_handler(ep.GET_SERVICES, 1, slow_callback)
    client.callback_queues[1].put_nowait({"payload": {}})
    await in_callback.wait()

    delete_task = asyncio.create_task(
        client.delete_subscription_handler(ep.GET_SERVICES)
    )
    await asyncio.sleep(0)
    delete_task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await delete_task
    assert ep.GET_SERVICES not in client._subscriptions
    assert 1 not in client.callback_queues


@pytest.mark.parametrize(
    ("method", "args", "message"),
    [
        ("button", ("ENTER",), "type:button\nname:ENTER\n\n"),
        ("move", (1, 2), "type:move\ndx:1\ndy:2\ndown:0\n\n"),
        ("click", (), "type:click\n\n"),
        ("scroll", (3, 4), "type:scroll\ndx:3\ndy:4\n\n"),
    ],
)
async def test_input_commands(
    connected_client: WebOsClient,
    fake_tv: FakeTv,
    method: str,
    args: tuple[Any, ...],
    message: str,
) -> None:
    """Test input commands are sent on the input websocket."""
    await getattr(connected_client, method)(*args)

    assert await asyncio.wait_for(fake_tv.input_received.get(), 1) == message


async def test_not_connected(client: WebOsClient) -> None:
    """Test commands fail when not connected."""
    with pytest.raises(WebOsTvCommandError, match="Not connected"):
        await client.get_power_state()

    with pytest.raises(WebOsTvCommandError, match="Not connected"):
        await client.button("ENTER")

    assert client.futures == {}


async def test_multiple_subscribe_failures_retrieve_all_task_exceptions(
    client: WebOsClient, fake_tv: FakeTv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test multiple simultaneous subscription failures retrieve all task exceptions."""
    monkeypatch.setattr(webos_client, "REQUEST_TIMEOUT", 0.05)
    fake_tv.responses[ep.GET_SOUND_OUTPUT] = None
    fake_tv.responses.pop(ep.GET_VOLUME, None)
    fake_tv.responses.pop(ep.GET_APPS, None)

    unhandled_exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_exceptions.append(context)
    )
    try:
        with pytest.raises(WebOsTvCommandTimeoutError):
            await client.connect()
        await assert_disconnected(client)
        # connect_result holds the raised exception, whose traceback references
        # the subscribe tasks; drop it so unretrieved exceptions get collected.
        client.connect_result = None
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert unhandled_exceptions == []


async def test_disconnect_during_subscribe_retrieves_task_exceptions(
    client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test disconnect during initial subscribe retrieves failed task exceptions."""
    fake_tv.responses[ep.GET_SOUND_OUTPUT] = None
    fake_tv.responses.pop(ep.GET_VOLUME, None)

    unhandled_exceptions: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    orig_handler = loop.get_exception_handler()
    loop.set_exception_handler(
        lambda _loop, context: unhandled_exceptions.append(context)
    )
    try:
        connect_task = asyncio.create_task(client.connect())
        await fake_tv.wait_for_message("subscribe", ep.GET_SOUND_OUTPUT)
        await fake_tv.wait_for_message("subscribe", ep.GET_VOLUME)
        await asyncio.sleep(0.01)
        await client.disconnect()
        assert await connect_task is False
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(orig_handler)

    assert unhandled_exceptions == []
