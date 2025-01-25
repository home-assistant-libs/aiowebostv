"""Base websockets client for controlling LG webOS based TVs."""

import asyncio
import copy
import json
import logging
from asyncio import Future, Task
from asyncio.queues import Queue
from collections.abc import Callable, Coroutine
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from . import endpoints as ep
from .exceptions import (
    WebOsTvCommandError,
    WebOsTvPairError,
    WebOsTvResponseTypeError,
    WebOsTvServiceNotFoundError,
)
from .handshake import REGISTRATION_MESSAGE

WS_PORT = 3000
WSS_PORT = 3001

_LOGGER = logging.getLogger(__package__)


class WebOsClientBase:
    """Base webOS TV client class."""

    def __init__(
        self,
        host: str,
        client_key: str | None = None,
        connect_timeout: float = 2,
        heartbeat: float = 5,
        client_session: ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        self.host = host
        self.client_key = client_key
        self.command_count: int = 0
        self.timeout_connect = connect_timeout
        self.heartbeat = heartbeat
        self.client_session = client_session
        self.created_client_session = False
        self.connect_task: Task | None = None
        self.connect_result: Future[bool] | None = None
        self.connection: ClientWebSocketResponse | None = None
        self.input_connection: ClientWebSocketResponse | None = None
        self.callbacks: dict[int, Callable] = {}
        self.futures: dict[int, Future[dict[str, Any]]] = {}
        self._system_info: dict[str, Any] = {}
        self._software_info: dict[str, Any] = {}
        self._hello_info: dict[str, Any] = {}
        self.subscribe_state_updates: set[Coroutine[Any, Any, dict[str, Any]]] = set()
        self.state_update_callbacks: list[Callable] = []
        self.do_state_update = False
        self._loop = asyncio.get_running_loop()

    async def connect(self) -> bool:
        """Connect to webOS TV device."""
        if self.is_connected():
            return True

        self.connect_result = self._loop.create_future()
        self.connect_task = asyncio.create_task(
            self.connect_handler(self.connect_result)
        )
        return await self.connect_result

    async def disconnect(self) -> None:
        """Disconnect from webOS TV device."""
        if self.connect_task is not None and not self.connect_task.done():
            self.connect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.connect_task
                self.connect_task = None

    def is_registered(self) -> bool:
        """Paired with the tv."""
        return self.client_key is not None

    def is_connected(self) -> bool:
        """Return true if connected to the tv."""
        return self.connect_task is not None and not self.connect_task.done()

    def registration_msg(self) -> dict[str, Any]:
        """Create registration message."""
        handshake = copy.deepcopy(REGISTRATION_MESSAGE)
        handshake["payload"]["client-key"] = self.client_key  # type: ignore[index]
        return handshake

    async def _ws_connect(self, uri: str) -> ClientWebSocketResponse:
        """Create websocket connection."""
        _LOGGER.debug("connect(%s): uri: %s", self.host, uri)

        if TYPE_CHECKING:
            assert self.client_session is not None

        # webOS uses self-signed certificates, disable SSL certificate validation
        async with asyncio.timeout(self.timeout_connect):
            return await self.client_session.ws_connect(
                uri, heartbeat=self.heartbeat, ssl=False
            )

    async def close_client_session(self) -> None:
        """Close client session if we created it."""
        if TYPE_CHECKING:
            assert self.client_session is not None

        await self.client_session.close()
        self.created_client_session = False
        self.client_session = None

    def clean_properties(self) -> None:
        """Clean collected properties from webOS TV upon disconnect."""
        self._system_info = {}
        self._software_info = {}
        self._hello_info = {}

    async def connect_handler(self, res: Future) -> None:
        """Handle connection for webOS TV."""
        handler_tasks: set[Task] = set()
        main_ws: ClientWebSocketResponse | None = None
        input_ws: ClientWebSocketResponse | None = None

        try:
            # Create a new client session if not provided
            if self.client_session is None:
                self.client_session = ClientSession()
                self.created_client_session = True

            try:
                uri = f"ws://{self.host}:{WS_PORT}"
                main_ws = await self._ws_connect(uri)
            # ClientConnectionError is raised when firmware reject WS_PORT
            # WSServerHandshakeError is raised when firmware enforce using ssl
            except (aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError):
                uri = f"wss://{self.host}:{WSS_PORT}"
                main_ws = await self._ws_connect(uri)

            # send hello
            _LOGGER.debug("send(%s): hello", self.host)
            await main_ws.send_json({"id": "hello", "type": "hello"})
            response = await main_ws.receive_json()
            _LOGGER.debug("recv(%s): %s", self.host, response)

            if response["type"] == "hello":
                self._hello_info = response["payload"]
            else:
                raise WebOsTvCommandError(f"Invalid response type {response}")

            # send registration
            _LOGGER.debug("send(%s): registration", self.host)
            await main_ws.send_json(self.registration_msg())
            response = await main_ws.receive_json()
            _LOGGER.debug("recv(%s): registration", self.host)

            if (
                response["type"] == "response"
                and response["payload"]["pairingType"] == "PROMPT"
            ):
                response = await main_ws.receive_json()
                _LOGGER.debug("recv(%s): pairing", self.host)
                _LOGGER.debug(
                    "pairing(%s): type: %s, error: %s",
                    self.host,
                    response["type"],
                    response.get("error"),
                )
                if response["type"] == "error":
                    raise WebOsTvPairError(response["error"])
                if response["type"] == "registered":
                    self.client_key = response["payload"]["client-key"]

            if not self.client_key:
                raise WebOsTvPairError("Unable to pair")

            self.callbacks = {}
            self.futures = {}

            handler_tasks.add(
                asyncio.create_task(
                    self.consumer_handler(main_ws, self.callbacks, self.futures)
                )
            )
            self.connection = main_ws

            # open additional connection needed to send button commands
            # the url is dynamically generated and returned from the ep.INPUT_SOCKET
            # endpoint on the main connection
            # create an empty consumer handler to keep ping/pong alive
            sockres = await self.request(ep.INPUT_SOCKET)
            inputsockpath = sockres["socketPath"]
            input_ws = await self._ws_connect(inputsockpath)
            handler_tasks.add(
                asyncio.create_task(self.consumer_handler(input_ws, None, None))
            )
            self.input_connection = input_ws

            # set static state and subscribe to state updates
            # avoid partial updates during initial subscription

            self.do_state_update = False
            self._system_info, self._software_info = await asyncio.gather(
                self.get_system_info(), self.get_software_info()
            )

            subscribe_tasks = set()
            for state_update in self.subscribe_state_updates:
                subscribe_tasks.add(asyncio.create_task(state_update))
            await asyncio.wait(subscribe_tasks)
            for task in subscribe_tasks:
                with suppress(WebOsTvServiceNotFoundError):
                    task.result()

            self.do_state_update = True
            if self.state_update_callbacks:
                await self.do_state_update_callbacks()

            res.set_result(True)

            await asyncio.wait(handler_tasks, return_when=asyncio.FIRST_COMPLETED)

        except Exception as ex:  # pylint: disable=broad-except
            if isinstance(ex, TimeoutError):
                _LOGGER.debug("timeout(%s): connection", self.host)
            else:
                _LOGGER.debug("exception(%s): %r", self.host, ex, exc_info=True)
            if not res.done():
                res.set_exception(ex)
        finally:
            for task in handler_tasks:
                if not task.done():
                    task.cancel()

            for future in self.futures.values():
                future.cancel()

            closeout = set()
            closeout.update(handler_tasks)

            if main_ws is not None:
                closeout.add(asyncio.create_task(main_ws.close()))
            if input_ws is not None:
                closeout.add(asyncio.create_task(input_ws.close()))
            if self.created_client_session:
                closeout.add(asyncio.create_task(self.close_client_session()))

            self.connection = None
            self.input_connection = None

            self.do_state_update = False

            self.clean_properties()

            for callback in self.state_update_callbacks:
                closeout.add(asyncio.create_task(callback(self)))

            if closeout:
                closeout_task = asyncio.create_task(asyncio.wait(closeout))

                while not closeout_task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(closeout_task)

    @staticmethod
    async def callback_handler(
        queue: Queue[dict[str, Any]],
        callback: Callable,
        future: Future[dict[str, Any]] | None,
    ) -> None:
        """Handle callbacks."""
        with suppress(asyncio.CancelledError):
            while True:
                msg = await queue.get()
                payload = msg.get("payload")
                await callback(payload)
                if future is not None and not future.done():
                    future.set_result(msg)

    async def consumer_handler(
        self,
        web_socket: ClientWebSocketResponse,
        callbacks: dict[int, Callable] | None,
        futures: dict[int, Future] | None,
    ) -> None:
        """Callbacks consumer handler."""
        callback_queues: dict[int, Queue[dict[str, Any]]] = {}
        callback_tasks: dict[int, Task] = {}

        try:
            async for raw_msg in web_socket:
                _LOGGER.debug("recv(%s): %s", self.host, raw_msg)
                if raw_msg.type is not WSMsgType.TEXT:
                    break

                if callbacks or futures:
                    msg = json.loads(raw_msg.data)
                    uid = msg.get("id")
                    callback = self.callbacks.get(uid)
                    future = self.futures.get(uid)
                    if callback is not None:
                        if uid not in callback_tasks:
                            queue: Queue[dict[str, Any]] = asyncio.Queue()
                            callback_queues[uid] = queue
                            callback_tasks[uid] = asyncio.create_task(
                                self.callback_handler(queue, callback, future)
                            )
                        callback_queues[uid].put_nowait(msg)
                    elif future is not None and not future.done():
                        self.futures[uid].set_result(msg)

        finally:
            for task in callback_tasks.values():
                if not task.done():
                    task.cancel()

            tasks = set(callback_tasks.values())

            if tasks:
                closeout_task = asyncio.create_task(asyncio.wait(tasks))

                while not closeout_task.done():
                    with suppress(asyncio.CancelledError):
                        await asyncio.shield(closeout_task)

    @property
    def system_info(self) -> dict[str, Any]:
        """Return TV system info."""
        return self._system_info

    @property
    def software_info(self) -> dict[str, Any]:
        """Return TV software info."""
        return self._software_info

    @property
    def hello_info(self) -> dict[str, Any]:
        """Return TV hello info."""
        return self._hello_info

    async def register_state_update_callback(self, callback: Callable) -> None:
        """Register user state update callback."""
        self.state_update_callbacks.append(callback)
        if self.do_state_update:
            await callback(self)

    def unregister_state_update_callback(self, callback: Callable) -> None:
        """Unregister user state update callback."""
        if callback in self.state_update_callbacks:
            self.state_update_callbacks.remove(callback)

    def clear_state_update_callbacks(self) -> None:
        """Clear user state update callback."""
        self.state_update_callbacks = []

    async def do_state_update_callbacks(self) -> None:
        """Call user state update callback."""
        callbacks = set()
        for callback in self.state_update_callbacks:
            callbacks.add(callback(self))

        if callbacks:
            await asyncio.gather(*callbacks)

    # low level request handling

    async def command(
        self,
        request_type: str,
        uri: str,
        payload: dict[str, Any] | None = None,
        uid: int | None = None,
    ) -> None:
        """Build and send a command."""
        if uid is None:
            uid = self.command_count
            self.command_count += 1

        if payload is None:
            payload = {}

        message = {
            "id": uid,
            "type": request_type,
            "uri": f"ssap://{uri}",
            "payload": payload,
        }

        if self.connection is None:
            raise WebOsTvCommandError("Not connected, can't execute command.")

        _LOGGER.debug("send(%s): %s", self.host, message)
        await self.connection.send_json(message)

    async def request(
        self,
        uri: str,
        payload: dict[str, Any] | None = None,
        cmd_type: str = "request",
        uid: int | None = None,
    ) -> dict[str, Any]:
        """Send a request and wait for response."""
        if uid is None:
            uid = self.command_count
            self.command_count += 1
        res = self._loop.create_future()
        self.futures[uid] = res
        try:
            await self.command(cmd_type, uri, payload, uid)
        except (asyncio.CancelledError, WebOsTvCommandError):
            del self.futures[uid]
            raise
        try:
            response = await res
        except asyncio.CancelledError:
            if uid in self.futures:
                del self.futures[uid]
            raise
        del self.futures[uid]

        payload = response.get("payload")
        if payload is None:
            raise WebOsTvCommandError(f"Invalid request response {response}")

        return_value = (
            payload.get("returnValue")
            or payload.get("subscribed")
            or payload.get("subscription")
        )

        if response.get("type") == "error":
            error = response.get("error")
            if error == "404 no such service or method":
                raise WebOsTvServiceNotFoundError(error)
            raise WebOsTvResponseTypeError(response)
        if return_value is None:
            raise WebOsTvCommandError(f"Invalid request response {response}")
        if not return_value:
            raise WebOsTvCommandError(f"Request failed with response {response}")

        return payload

    async def subscribe(
        self, callback: Callable, uri: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Subscribe to updates."""
        uid = self.command_count
        self.command_count += 1
        self.callbacks[uid] = callback
        try:
            return await self.request(
                uri, payload=payload, cmd_type="subscribe", uid=uid
            )
        except Exception:
            del self.callbacks[uid]
            raise

    async def input_command(self, message: str) -> None:
        """Execute TV input command."""
        if self.input_connection is None:
            raise WebOsTvCommandError("Couldn't execute input command.")

        _LOGGER.debug("send(%s): %s", self.host, message)
        await self.input_connection.send_str(message)

    async def get_software_info(self) -> dict[str, Any]:
        """Return the current software status."""
        return await self.request(ep.GET_SOFTWARE_INFO)

    async def get_system_info(
        self,
    ) -> dict[str, Any]:
        """Return the system information."""
        return await self.request(ep.GET_SYSTEM_INFO)
