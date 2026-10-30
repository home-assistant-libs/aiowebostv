"""Tests for the webOS client."""

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from aiohttp.test_utils import unused_port

from aiowebostv import (
    WebOsClient,
    WebOsTvCommandError,
    WebOsTvPairError,
    WebOsTvServiceNotFoundError,
    WebOsTvState,
    webos_client,
)
from aiowebostv import endpoints as ep
from aiowebostv.exceptions import (
    WebOsTvCommandTimeoutError,
    WebOsTvResponseTypeError,
)

from .conftest import CLIENT_KEY, HOST, NEW_CLIENT_KEY, NOT_FOUND, FakeTv

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
    assert "wifiInfo" in client.tv_info.connection

    state = client.tv_state
    assert state.is_on
    assert state.is_screen_on
    assert state.current_app_id == "com.webos.app.hdmi1"
    assert state.volume == 10
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


@pytest.mark.parametrize("pairing", ["reject", "no_key"])
async def test_pairing_failed(fake_tv: FakeTv, pairing: str) -> None:
    """Test pairing failures raise a pair error."""
    fake_tv.pairing = pairing
    client = WebOsClient(HOST)

    with pytest.raises(WebOsTvPairError):
        await client.connect()

    await assert_disconnected(client)
    assert client.client_key is None


async def test_invalid_hello_response(client: WebOsClient, fake_tv: FakeTv) -> None:
    """Test an invalid hello response fails the connection."""
    fake_tv.hello_type = "error"

    with pytest.raises(WebOsTvCommandError, match="Invalid response type"):
        await client.connect()

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


@pytest.mark.parametrize(
    "error", [aiohttp.ClientConnectionError(), aiohttp.WSServerHandshakeError(None, ())]
)
async def test_main_ws_falls_back_to_wss(error: Exception) -> None:
    """Test the main websocket falls back to wss when ws is rejected."""
    client = WebOsClient(HOST)
    ws = object()

    with patch.object(client, "_ws_connect", side_effect=[error, ws]) as ws_connect:
        assert await client._create_main_ws() is ws

    assert [call.args[0] for call in ws_connect.call_args_list] == [
        f"ws://{HOST}:{webos_client.WS_PORT}",
        f"wss://{HOST}:{webos_client.WSS_PORT}",
    ]


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


async def test_pending_request_cancelled_on_disconnect(
    connected_client: WebOsClient, fake_tv: FakeTv
) -> None:
    """Test a request waiting for a response is cancelled when disconnected."""
    fake_tv.responses.pop(ep.GET_SERVICES, None)
    request = asyncio.create_task(connected_client.get_services())
    await fake_tv.wait_for_message("request", ep.GET_SERVICES)

    await connected_client.disconnect()

    with pytest.raises(asyncio.CancelledError):
        await request


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


# State updates


async def test_register_callback_when_connected(
    connected_client: WebOsClient,
) -> None:
    """Test a callback registered while connected is called immediately."""
    callback = AsyncMock()

    await connected_client.register_state_update_callback(callback)

    callback.assert_awaited_once_with(connected_client.tv_state)


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


@pytest.mark.parametrize(
    ("uri", "payload", "attribute", "expected"),
    [
        (ep.GET_VOLUME, {"volumeStatus": {"volume": 20}}, "volume", 20),
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
