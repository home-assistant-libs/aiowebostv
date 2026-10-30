"""Fixtures for aiowebostv tests."""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from aiowebostv import WebOsClient, webos_client
from aiowebostv import endpoints as ep

HOST = "127.0.0.1"
CLIENT_KEY = "valid-client-key"
NEW_CLIENT_KEY = "new-client-key"

NOT_FOUND = "404 no such service or method"


def default_responses() -> dict[str, dict[str, Any] | None]:
    """Return default request/subscribe payloads keyed by endpoint uri."""
    return {
        ep.GET_SYSTEM_INFO: {"modelName": "OLED55C1", "returnValue": True},
        ep.GET_SOFTWARE_INFO: {"product_name": "webOSTV 6.0", "returnValue": True},
        ep.GET_CONNECTION_INFO: {"wifiInfo": {}, "returnValue": True},
        ep.GET_POWER_STATE: {"state": "Active", "returnValue": True},
        ep.GET_CURRENT_APP_INFO: {"appId": "com.webos.app.hdmi1", "returnValue": True},
        ep.GET_AUDIO_STATUS: {"mute": False, "returnValue": True},
        ep.GET_VOLUME: {"volumeStatus": {"volume": 10}, "returnValue": True},
        ep.GET_APPS: {
            "launchPoints": [{"id": "netflix", "title": "Netflix"}],
            "returnValue": True,
        },
        ep.GET_INPUTS: {
            "devices": [{"appId": "com.webos.app.hdmi1", "label": "HDMI 1"}],
            "returnValue": True,
        },
        ep.GET_SOUND_OUTPUT: {"soundOutput": "tv_speaker", "returnValue": True},
        ep.GET_MEDIA_FOREGROUND_APP_INFO: {
            "foregroundAppInfo": [],
            "returnValue": True,
        },
        ep.GET_TV_CHANNELS: {
            "channelList": [{"channelId": "1", "channelName": "News"}],
            "returnValue": True,
        },
        ep.GET_CURRENT_CHANNEL: {"channelId": "1", "returnValue": True},
        ep.GET_CHANNEL_INFO: {"channel": {"channelId": "1"}, "returnValue": True},
    }


class FakeTv:
    """Fake webOS TV, serving the main and input websockets."""

    def __init__(self) -> None:
        """Initialize the fake TV."""
        self.port = 0
        self.hello_type = "hello"
        self.respond_to_hello = True
        # "accept", "reject" or "no_key", used when the client key is not valid
        self.pairing = "accept"
        # uri -> payload; None responds with a 404 error, missing uri never responds
        self.responses = default_responses()
        self.received: list[dict[str, Any]] = []
        self.input_received: asyncio.Queue[str] = asyncio.Queue()
        self.subscriptions: dict[str, Any] = {}
        self.main_ws: web.WebSocketResponse | None = None
        self.input_ws: web.WebSocketResponse | None = None
        self._message_received = asyncio.Event()
        self.app = web.Application()
        self.app.router.add_get("/", self._main_handler)
        self.app.router.add_get("/input", self._input_handler)

    def received_uris(self) -> list[str]:
        """Return the uris of all requests received on the main websocket."""
        return [msg["uri"] for msg in self.received if "uri" in msg]

    async def wait_for_message(self, msg_type: str, uri: str | None = None) -> None:
        """Wait until a message was received on the main websocket."""

        def received() -> bool:
            return any(
                msg["type"] == msg_type
                and (uri is None or msg.get("uri") == f"ssap://{uri}")
                for msg in self.received
            )

        async with asyncio.timeout(1):
            while not received():
                self._message_received.clear()
                await self._message_received.wait()

    async def push(self, uri: str, payload: dict[str, Any]) -> None:
        """Push a subscription update to the client."""
        assert self.main_ws is not None
        await self.main_ws.send_json(
            {
                "type": "response",
                "id": self.subscriptions[uri],
                "payload": {**payload, "returnValue": True},
            }
        )

    async def _main_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle the main websocket."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.main_ws = ws
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            data = msg.json()
            self.received.append(data)
            self._message_received.set()
            await self._handle_message(ws, data)
        return ws

    async def _handle_message(
        self, ws: web.WebSocketResponse, data: dict[str, Any]
    ) -> None:
        """Respond to a message from the client."""
        if data["type"] == "hello":
            if self.respond_to_hello:
                await ws.send_json(
                    {"type": self.hello_type, "id": "hello", "payload": {"x": 1}}
                )
        elif data["type"] == "register":
            await self._handle_register(ws, data)
        elif data["type"] in ("request", "subscribe"):
            uri = data["uri"].removeprefix("ssap://")
            if data["type"] == "subscribe":
                self.subscriptions[uri] = data["id"]
            await self._respond(ws, data["id"], uri)

    async def _handle_register(
        self, ws: web.WebSocketResponse, data: dict[str, Any]
    ) -> None:
        """Respond to a registration request."""
        msg_id = data["id"]
        if data["payload"].get("client-key") == CLIENT_KEY:
            await ws.send_json(
                {
                    "type": "registered",
                    "id": msg_id,
                    "payload": {"client-key": CLIENT_KEY},
                }
            )
            return

        await ws.send_json(
            {"type": "response", "id": msg_id, "payload": {"pairingType": "PROMPT"}}
        )
        if self.pairing == "accept":
            await ws.send_json(
                {
                    "type": "registered",
                    "id": msg_id,
                    "payload": {"client-key": NEW_CLIENT_KEY},
                }
            )
        elif self.pairing == "reject":
            await ws.send_json(
                {
                    "type": "error",
                    "id": msg_id,
                    "error": "403 User denied access",
                    "payload": {},
                }
            )
        else:
            await ws.send_json({"type": "response", "id": msg_id, "payload": {}})

    async def _respond(
        self, ws: web.WebSocketResponse, msg_id: int | str, uri: str
    ) -> None:
        """Respond to a request or subscription."""
        if uri == ep.INPUT_SOCKET:
            await ws.send_json(
                {
                    "type": "response",
                    "id": msg_id,
                    "payload": {
                        "socketPath": f"ws://{HOST}:{self.port}/input",
                        "returnValue": True,
                    },
                }
            )
            return

        if uri not in self.responses:
            return

        if (payload := self.responses[uri]) is None:
            await ws.send_json(
                {"type": "error", "id": msg_id, "error": NOT_FOUND, "payload": {}}
            )
            return

        await ws.send_json({"type": "response", "id": msg_id, "payload": payload})

    async def _input_handler(self, request: web.Request) -> web.WebSocketResponse:
        """Handle the input websocket."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.input_ws = ws
        async for msg in ws:
            if msg.type is not WSMsgType.TEXT:
                break
            self.input_received.put_nowait(msg.data)
        return ws


@pytest_asyncio.fixture
async def fake_tv(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[FakeTv]:
    """Run a fake TV and point the client at it."""
    tv = FakeTv()
    server = TestServer(tv.app, host=HOST)
    await server.start_server()
    assert server.port is not None
    tv.port = server.port
    monkeypatch.setattr(webos_client, "WS_PORT", server.port)
    yield tv
    await server.close()


@pytest_asyncio.fixture
async def client(fake_tv: FakeTv) -> AsyncGenerator[WebOsClient]:  # noqa: ARG001
    """Return a client for the fake TV, disconnected on teardown."""
    tv_client = WebOsClient(HOST, CLIENT_KEY)
    yield tv_client
    await tv_client.disconnect()


@pytest_asyncio.fixture
async def connected_client(client: WebOsClient) -> WebOsClient:
    """Return a client connected to the fake TV."""
    assert await client.connect()
    return client
