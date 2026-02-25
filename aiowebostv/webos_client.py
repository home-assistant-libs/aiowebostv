"""Provide a websockets client for controlling LG webOS based TVs."""

import asyncio
import base64
import copy
import json
import logging
from asyncio import Future, Task
from asyncio.queues import Queue
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import aiohttp
from aiohttp import ClientSession, ClientWebSocketResponse, WSMsgType

from . import endpoints as ep
from .exceptions import (
    WebOsTvCommandError,
    WebOsTvCommandTimeoutError,
    WebOsTvPairError,
    WebOsTvResponseTypeError,
    WebOsTvServiceNotFoundError,
)
from .handshake import REGISTRATION_MESSAGE
from .models import WebOsTvInfo, WebOsTvState

CONNECT_TIMEOUT = 2  # Timeout for connecting to the TV
RECEIVE_TIMEOUT = 10  # Timeout for receiving messages using ws.receive_json
REQUEST_TIMEOUT = 20  # Timeout for waiting for a response to a request

HEARTBEAT = 5

SOUND_OUTPUTS_TO_DELAY_CONSECUTIVE_VOLUME_STEPS = {"external_arc"}

MAIN_WS_MAX_MSG_SIZE = 8 * 1024 * 1024  # 8MB, based on channel list size
INPUT_WS_MAX_MSG_SIZE = 8 * 1024  # 8KB, not expecting any messages

WS_PORT = 3000
WSS_PORT = 3001

_LOGGER = logging.getLogger(__package__)


class WebOsClient:
    """webOS TV client class."""

    def __init__(
        self,
        host: str,
        client_key: str | None = None,
        connect_timeout: float = CONNECT_TIMEOUT,
        heartbeat: float = HEARTBEAT,
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
        self.futures: dict[int, Future[dict[str, Any]]] = {}
        self.tv_state = WebOsTvState()
        self.tv_info = WebOsTvInfo()
        self.state_update_callbacks: list[Callable] = []
        self.do_state_update = False
        self._volume_step_lock = asyncio.Lock()
        self._volume_step_delay: timedelta | None = None
        self._loop = asyncio.get_running_loop()
        self.callback_queues: dict[int, Queue[dict[str, Any]]] = {}
        self.callback_tasks: dict[int, Task] = {}
        self._rx_tasks: set[Task] = set()
        self._subscriptions: dict[str, int] = {}

    async def connect(self) -> bool:
        """Connect to webOS TV device."""
        if self.is_connected():
            _LOGGER.debug("connect(%s): already connected", self.host)
            return True

        if self.connect_task_active() and self.connect_result is not None:
            _LOGGER.debug("connect(%s): connection already in progress", self.host)
            return await self.connect_result

        self.connect_result = self._loop.create_future()
        self.connect_task = asyncio.create_task(
            self.connect_handler(self.connect_result)
        )
        return await self.connect_result

    async def disconnect(self) -> None:
        """Disconnect from webOS TV device."""
        if self.connect_task is not None and not self.connect_task.done():
            _LOGGER.debug("disconnect(%s): disconnecting", self.host)
            self.connect_task.cancel()
            try:
                await self.connect_task
            except asyncio.CancelledError:
                _LOGGER.debug("disconnect(%s): connect task cancelled", self.host)
                if self.connect_result is not None and not self.connect_result.done():
                    self.connect_result.set_result(False)
            return

        _LOGGER.debug("disconnect(%s): already disconnected", self.host)

    def is_registered(self) -> bool:
        """Paired with the tv."""
        return self.client_key is not None

    def connect_task_active(self) -> bool:
        """Return true if connect task is active."""
        return self.connect_task is not None and not self.connect_task.done()

    def is_connected(self) -> bool:
        """Return true if connected to the tv."""
        return (
            self.connect_task_active()
            and self.connect_result is not None
            and self.connect_result.done()
            and self.connect_result.result()
        )

    def registration_msg(self) -> dict[str, Any]:
        """Create registration message."""
        handshake = copy.deepcopy(REGISTRATION_MESSAGE)
        handshake["payload"]["client-key"] = self.client_key  # type: ignore[index]
        return handshake

    async def _ws_connect(self, uri: str, max_msg_size: int) -> ClientWebSocketResponse:
        """Create websocket connection."""
        _LOGGER.debug(
            "connect(%s): uri: %s, max_msg_size: %s", self.host, uri, max_msg_size
        )

        if TYPE_CHECKING:
            assert self.client_session is not None

        # webOS uses self-signed certificates, disable SSL certificate validation
        async with asyncio.timeout(self.timeout_connect):
            return await self.client_session.ws_connect(
                uri,
                heartbeat=self.heartbeat,
                ssl=False,
                max_msg_size=max_msg_size,
            )

    async def close_client_session(self) -> None:
        """Close client session if we created it."""
        if TYPE_CHECKING:
            assert self.client_session is not None

        await self.client_session.close()
        self.created_client_session = False
        self.client_session = None

    async def _create_main_ws(self) -> ClientWebSocketResponse:
        """Create main websocket connection.

        Try using ws:// and fallback to wss:// if the TV rejects the connection.
        """
        try:
            uri = f"ws://{self.host}:{WS_PORT}"
            return await self._ws_connect(uri, MAIN_WS_MAX_MSG_SIZE)
        # ClientConnectionError is raised when firmware reject WS_PORT
        # WSServerHandshakeError is raised when firmware enforce using ssl
        except (aiohttp.ClientConnectionError, aiohttp.WSServerHandshakeError):
            uri = f"wss://{self.host}:{WSS_PORT}"
            return await self._ws_connect(uri, MAIN_WS_MAX_MSG_SIZE)

    def _ensure_client_session(self) -> None:
        """Create a new client session if no client session provided."""
        if self.client_session is None:
            self.client_session = ClientSession()
            self.created_client_session = True

    async def _get_hello_info(self, ws: ClientWebSocketResponse) -> None:
        """Get hello info."""
        _LOGGER.debug("send(%s): hello", self.host)
        await ws.send_json({"id": "hello", "type": "hello", "payload": {}})
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            response = await ws.receive_json()
        _LOGGER.debug("recv(%s): %s", self.host, response)

        if response["type"] == "hello":
            self.tv_info.hello = response["payload"]
        else:
            error = f"Invalid response type {response}"
            raise WebOsTvCommandError(error)

    async def _get_pre_reg_system_info(self, ws: ClientWebSocketResponse) -> None:
        """Get system info before registration.

        Newer webOS versions require system info to be retrieved before registration.
        """
        request = {
            "id": "get_sys_info",
            "type": "request",
            "uri": f"ssap://{ep.GET_SYSTEM_INFO}",
            "payload": {},
        }
        _LOGGER.debug("send(%s): %s", self.host, request)
        await ws.send_json(request)
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            response = await ws.receive_json()
        _LOGGER.debug("recv(%s): %s", self.host, response)

        with suppress(WebOsTvResponseTypeError):
            self.tv_info.system = self._parse_response(response)

    async def _check_registration(self, ws: ClientWebSocketResponse) -> None:
        """Check if the client is registered with the tv."""
        _LOGGER.debug("send(%s): registration", self.host)
        await ws.send_json(self.registration_msg())
        async with asyncio.timeout(RECEIVE_TIMEOUT):
            response = await ws.receive_json()
        _LOGGER.debug("recv(%s): registration", self.host)

        if (
            response["type"] == "response"
            and response["payload"]["pairingType"] == "PROMPT"
        ):
            response = await ws.receive_json(timeout=RECEIVE_TIMEOUT)
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
            error = "Client key not set, pairing failed."
            raise WebOsTvPairError(error)

    async def _create_input_ws(self) -> ClientWebSocketResponse:
        """Create input websocket connection.

        Open additional connection needed to send button commands
        The url is dynamically generated and returned from the ep.INPUT_SOCKET
        endpoint on the main connection.
        """
        sockres = await self.request(ep.INPUT_SOCKET)
        inputsockpath = sockres["socketPath"]
        return await self._ws_connect(inputsockpath, INPUT_WS_MAX_MSG_SIZE)

    async def _get_states_and_subscribe_state_updates(self) -> None:
        """Get initial states and subscribe to state updates.

        Avoid partial updates during initial subscription.
        """
        self.do_state_update = False

        # Try to get system info; on newer webOS versions,
        # this may fail because system info must be retrieved before registration.
        if not self.tv_info.system:
            with suppress(WebOsTvResponseTypeError):
                self.tv_info.system = await self.get_system_info()

        self.tv_info.software = await self.get_software_info()
        self.tv_info.connection_macs = await self.get_connection_macs()

        subscribe_state_updates = {
            self.subscribe_power_state(self.set_power_state),
            self.subscribe_current_app(self.set_current_app_state),
            self.subscribe_muted(self.set_muted_state),
            self.subscribe_volume(self.set_volume_state),
            self.subscribe_apps(self.set_apps_state),
            self.subscribe_inputs(self.set_inputs_state),
            self.subscribe_sound_output(self.set_sound_output_state),
            self.subscribe_media_foreground_app(self.set_media_state),
        }
        subscribe_tasks = set()
        for state_update in subscribe_state_updates:
            subscribe_tasks.add(asyncio.create_task(state_update))
        await asyncio.wait(subscribe_tasks)
        for task in subscribe_tasks:
            with suppress(WebOsTvServiceNotFoundError):
                task.result()
        self.do_state_update = True
        await self.do_state_update_callbacks()

    def _cancel_tasks(self) -> None:
        """Cancel all tasks."""
        for callback_task in self.callback_tasks.values():
            if not callback_task.done():
                callback_task.cancel()

        for task in self._rx_tasks:
            if not task.done():
                task.cancel()

        for future in self.futures.values():
            future.cancel()

    async def _closeout_tasks(
        self,
        main_ws: ClientWebSocketResponse | None,
        input_ws: ClientWebSocketResponse | None,
    ) -> None:
        """Cancel all tasks and close connections."""
        closeout = set()

        self._cancel_tasks()

        if callback_tasks := set(self.callback_tasks.values()):
            closeout.update(callback_tasks)

        closeout.update(self._rx_tasks)

        if main_ws is not None:
            closeout.add(asyncio.create_task(main_ws.close()))
        if input_ws is not None:
            closeout.add(asyncio.create_task(input_ws.close()))
        if self.created_client_session:
            closeout.add(asyncio.create_task(self.close_client_session()))

        self.connection = None
        self.input_connection = None
        self.do_state_update = False
        self.tv_state.clear()

        for callback in self.state_update_callbacks:
            closeout.add(asyncio.create_task(callback(self.tv_state)))

        if not closeout:
            return

        closeout_task = asyncio.create_task(asyncio.wait(closeout))
        while not closeout_task.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(closeout_task)

    async def connect_handler(self, res: Future) -> None:
        """Handle connection for webOS TV."""
        self._rx_tasks = set()
        self.callback_queues = {}
        self.callback_tasks = {}
        self.futures = {}
        self._subscriptions = {}
        self.command_count = 0
        self.tv_info.clear()
        main_ws: ClientWebSocketResponse | None = None
        input_ws: ClientWebSocketResponse | None = None
        self._ensure_client_session()
        try:
            main_ws = await self._create_main_ws()
            await self._get_hello_info(main_ws)
            await self._get_pre_reg_system_info(main_ws)
            await self._check_registration(main_ws)
            self._rx_tasks.add(asyncio.create_task(self._rx_msgs_main_ws(main_ws)))
            self.connection = main_ws
            input_ws = await self._create_input_ws()
            self._rx_tasks.add(asyncio.create_task(self._rx_msgs_input_ws(input_ws)))
            self.input_connection = input_ws
            await self._get_states_and_subscribe_state_updates()
            res.set_result(True)
            await asyncio.wait(self._rx_tasks, return_when=asyncio.FIRST_COMPLETED)
        except TimeoutError:
            _LOGGER.debug("timeout(%s): connection", self.host)
            if not res.done():
                res.set_exception(asyncio.TimeoutError)
        except Exception as ex:
            _LOGGER.debug("exception(%s): %r", self.host, ex, exc_info=True)
            if not res.done():
                res.set_exception(ex)
            else:
                raise
        finally:
            await self._closeout_tasks(main_ws, input_ws)

    @staticmethod
    async def callback_handler(
        queue: Queue[dict[str, Any]],
        callback: Callable,
        future: Future[dict[str, Any]],
    ) -> None:
        """Handle callbacks."""
        with suppress(asyncio.CancelledError):
            while True:
                msg = await queue.get()
                payload = msg.get("payload")
                await callback(payload)
                if not future.done():
                    future.set_result(msg)

    def _process_text_message(self, data: str) -> None:
        """Process text message."""
        msg = json.loads(data)
        uid = msg.get("id")
        # if we have a callback for this message, put it in the queue
        # let the callback handle the message and mark the future as done
        if queue := self.callback_queues.get(uid):
            queue.put_nowait(msg)
        elif future := self.futures.get(uid):
            future.set_result(msg)

    async def _rx_msgs_main_ws(self, web_socket: ClientWebSocketResponse) -> None:
        """Receive messages from main websocket connection."""
        async for raw_msg in web_socket:
            _LOGGER.debug("recv(%s): %s", self.host, raw_msg)
            if raw_msg.type is not WSMsgType.TEXT:
                break

            self._process_text_message(raw_msg.data)

    async def _rx_msgs_input_ws(self, web_socket: ClientWebSocketResponse) -> None:
        """Receive messages from input websocket connection.

        We are not expecting any messages from the input connection.
        This is just to keep the connection alive.
        """
        async for raw_msg in web_socket:
            _LOGGER.debug("input recv(%s): %s", self.host, raw_msg)
            if raw_msg.type is not WSMsgType.TEXT:
                break

    async def register_state_update_callback(
        self, callback: Callable[[WebOsTvState], Any]
    ) -> None:
        """Register user state update callback."""
        self.state_update_callbacks.append(callback)
        if self.do_state_update:
            await callback(self.tv_state)

    def set_volume_step_delay(self, step_delay_ms: float | None) -> None:
        """Set volume step delay in ms or None to disable step delay."""
        if step_delay_ms is None:
            self._volume_step_delay = None
            return

        self._volume_step_delay = timedelta(milliseconds=step_delay_ms)

    def unregister_state_update_callback(
        self, callback: Callable[[WebOsTvState], Any]
    ) -> None:
        """Unregister user state update callback."""
        if callback in self.state_update_callbacks:
            self.state_update_callbacks.remove(callback)

    def clear_state_update_callbacks(self) -> None:
        """Clear user state update callback."""
        self.state_update_callbacks = []

    async def do_state_update_callbacks(self) -> None:
        """Call user state update callback."""
        if not self.state_update_callbacks or not self.do_state_update:
            return

        callbacks = {cb(self.tv_state) for cb in self.state_update_callbacks}
        await asyncio.gather(*callbacks)

    def _is_tv_on(self) -> bool:
        """Return true if TV is powered on."""
        if not self.tv_state.power_state:
            # fallback to current app id for some older webos versions
            # which don't support explicit power state
            return self.tv_state.current_app_id not in [None, ""]

        return self.tv_state.power_state.get("state") not in [
            None,
            "Power Off",
            "Suspend",
            "Active Standby",
        ]

    def _is_screen_on(self) -> bool:
        """Return true if screen is on."""
        if self.tv_state.is_on:
            return self.tv_state.power_state.get("state") != "Screen Off"
        return False

    async def set_power_state(self, payload: dict[str, bool | str]) -> None:
        """Set TV power state callback."""
        self.tv_state.power_state = payload
        self.tv_state.is_on = self._is_tv_on()
        self.tv_state.is_screen_on = self._is_screen_on()
        await self.do_state_update_callbacks()

    async def set_current_app_state(self, app_id: str) -> None:
        """Set current app state variable.

        This function also handles subscriptions to current channel and
        channel list, since the current channel subscription can only
        succeed when Live TV is running and channel list subscription
        can only succeed after channels have been configured.
        """
        self.tv_state.current_app_id = app_id
        self.tv_state.is_on = self._is_tv_on()
        self.tv_state.is_screen_on = self._is_screen_on()

        if self.tv_state.channels is None:
            with suppress(WebOsTvCommandError):
                await self.subscribe_channels(self.set_channels_state)

        if app_id == "com.webos.app.livetv" and self.tv_state.current_channel is None:
            await asyncio.sleep(2)
            with suppress(WebOsTvCommandError):
                await self.subscribe_current_channel(self.set_current_channel_state)
        else:
            self.tv_state.current_channel = None
            self.tv_state.channel_info = None

        await self.do_state_update_callbacks()

    async def set_muted_state(self, muted: bool) -> None:  # noqa: FBT001
        """Set TV mute state callback."""
        self.tv_state.muted = muted
        await self.do_state_update_callbacks()

    async def set_volume_state(self, volume: int) -> None:
        """Set TV volume level callback."""
        self.tv_state.volume = volume
        await self.do_state_update_callbacks()

    async def set_channels_state(self, channels: list[dict[str, Any]]) -> None:
        """Set TV channels callback."""
        self.tv_state.channels = channels
        await self.do_state_update_callbacks()

    async def set_current_channel_state(self, channel: dict[str, Any]) -> None:
        """Set current channel state variable.

        This function also handles the channel info subscription,
        since that call may fail if channel information is not
        available when it's called.
        """
        self.tv_state.current_channel = channel

        if self.tv_state.channel_info is None:
            with suppress(WebOsTvCommandError):
                await self.subscribe_channel_info(self.set_channel_info_state)

        await self.do_state_update_callbacks()

    async def set_channel_info_state(self, channel_info: dict[str, Any]) -> None:
        """Set TV channel info state callback."""
        self.tv_state.channel_info = channel_info
        await self.do_state_update_callbacks()

    async def set_apps_state(self, payload: dict[str, Any]) -> None:
        """Set TV channel apps state callback."""
        apps = payload.get("launchPoints")
        if apps is not None:
            self.tv_state.apps = {}
            for app in apps:
                self.tv_state.apps[app["id"]] = app
        else:
            change = payload["change"]
            app_id = payload["id"]
            if change == "removed":
                del self.tv_state.apps[app_id]
            else:
                self.tv_state.apps[app_id] = payload

        await self.do_state_update_callbacks()

    async def set_inputs_state(self, extinputs: list[dict[str, Any]]) -> None:
        """Set TV inputs state callback."""
        self.tv_state.inputs = {}
        for extinput in extinputs:
            self.tv_state.inputs[extinput["appId"]] = extinput

        await self.do_state_update_callbacks()

    async def set_sound_output_state(self, sound_output: str) -> None:
        """Set TV sound output state callback."""
        self.tv_state.sound_output = sound_output
        await self.do_state_update_callbacks()

    async def set_media_state(self, foreground_app_info: list[dict[str, bool]]) -> None:
        """Set TV media player state callback."""
        self.tv_state.media_state = foreground_app_info
        await self.do_state_update_callbacks()

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
            error = "Not connected, can't execute command."
            raise WebOsTvCommandError(error)

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
        else:
            res = self.futures[uid]
        try:
            await self.command(cmd_type, uri, payload, uid)
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await res
        except TimeoutError as err:
            error = f"Response timed out for {uri} with uid {uid}"
            raise WebOsTvCommandTimeoutError(error) from err
        except (asyncio.CancelledError, WebOsTvCommandError):
            raise
        finally:
            del self.futures[uid]

        return self._parse_response(response)

    def _parse_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Parse response."""
        payload: dict[str, Any] | None = response.get("payload")
        if payload is None:
            error = f"Invalid request response {response}"
            raise WebOsTvCommandError(error)

        return_value = (
            payload.get("returnValue")
            or payload.get("subscribed")
            or payload.get("subscription")
        )

        if response.get("type") == "error":
            error = response.get("error", "")
            if error == "404 no such service or method":
                raise WebOsTvServiceNotFoundError(error)
            raise WebOsTvResponseTypeError(response)
        if return_value is None:
            error = f"Invalid request response {response}"
            raise WebOsTvCommandError(error)
        if not return_value:
            error = f"Request failed with response {response}"
            raise WebOsTvCommandError(error)

        return payload

    async def create_subscription_handler(
        self, uri: str, uid: int, callback: Callable
    ) -> None:
        """Create a subscription handler for a given uri & uid.

        Create a queue to store the messages, a task to handle the messages
        and a future to signal first subscription update processed.
        """
        self._subscriptions[uri] = uid
        self.futures[uid] = future = self._loop.create_future()
        queue: Queue[dict[str, Any]] = asyncio.Queue()
        self.callback_queues[uid] = queue
        self.callback_tasks[uid] = asyncio.create_task(
            self.callback_handler(queue, callback, future)
        )

    async def delete_subscription_handler(self, uri: str) -> None:
        """Delete a subscription handler for a given uri."""
        uid = self._subscriptions[uri]
        task = self.callback_tasks.pop(uid)
        if not task.done():
            task.cancel()
        while not task.done():
            with suppress(asyncio.CancelledError):
                await task
        del self.callback_queues[uid]
        del self._subscriptions[uri]

    async def subscribe(
        self, callback: Callable, uri: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Subscribe to updates.

        Subsciption use a fixed uid, pre-create a future and a handler.
        """
        uid = self.command_count
        self.command_count += 1
        await self.create_subscription_handler(uri, uid, callback)

        try:
            return await self.request(
                uri, payload=payload, cmd_type="subscribe", uid=uid
            )
        except Exception:
            await self.delete_subscription_handler(uri)
            raise

    async def unsubscribe(self, uri: str) -> None:
        """Unsubscribe from updates by uri.

        Send unsubscribe command and delete the subscription handler.
        uri is ignored by the TV, it is sent for debugging purposes.
        """
        uid = self._subscriptions[uri]
        await self.command("unsubscribe", uri, uid=uid)
        await self.delete_subscription_handler(uri)

    async def resubscribe(
        self, callback: Callable, uri: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Resubscribe to updates by first unsubscribing and then subscribing."""
        if uri in self._subscriptions:
            await self.unsubscribe(uri)
        return await self.subscribe(callback, uri, payload)

    async def input_command(self, message: str) -> None:
        """Execute TV input command."""
        if self.input_connection is None:
            error = "Not connected, can't execute input command."
            raise WebOsTvCommandError(error)

        _LOGGER.debug("send(%s): %s", self.host, message)
        await self.input_connection.send_str(message)

    # high level request handling

    async def button(self, name: str) -> None:
        """Send button press command."""
        message = f"type:button\nname:{name}\n\n"
        await self.input_command(message)

    async def move(self, d_x: int, d_y: int, down: int = 0) -> None:
        """Send cursor move command."""
        message = f"type:move\ndx:{d_x}\ndy:{d_y}\ndown:{down}\n\n"
        await self.input_command(message)

    async def click(self) -> None:
        """Send cursor click command."""
        message = "type:click\n\n"
        await self.input_command(message)

    async def scroll(self, d_x: int, d_y: int) -> None:
        """Send scroll command."""
        message = f"type:scroll\ndx:{d_x}\ndy:{d_y}\n\n"
        await self.input_command(message)

    def read_icon(self, icon_path: str, message_payload: dict[str, Any]) -> None:
        """Read icon & set data in message payload."""
        path = Path(icon_path)
        message_payload["iconExtension"] = path.suffix[1:]
        with path.open("rb") as icon_file:
            message_payload["iconData"] = base64.b64encode(icon_file.read()).decode(
                "ascii"
            )

    async def send_message(
        self, message: str, icon_path: str | None = None
    ) -> dict[str, Any]:
        """Show a floating message."""
        message_payload = {
            "message": message,
            "iconData": "",
            "iconExtension": "",
        }

        if icon_path is not None:
            await self._loop.run_in_executor(
                None, self.read_icon, icon_path, message_payload
            )

        return await self.request(ep.SHOW_MESSAGE, message_payload)

    async def get_power_state(self) -> dict[str, Any]:
        """Get current power state."""
        return await self.request(ep.GET_POWER_STATE)

    async def subscribe_power_state(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to current power state."""
        return await self.subscribe(callback, ep.GET_POWER_STATE)

    # Apps
    async def get_apps(self) -> dict[str, Any] | None:
        """Return all apps."""
        res = await self.request(ep.GET_APPS)
        return res.get("launchPoints")

    async def subscribe_apps(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in available apps."""
        return await self.subscribe(callback, ep.GET_APPS)

    async def get_apps_all(self) -> dict[str, Any] | None:
        """Return all apps, including hidden ones."""
        res = await self.request(ep.GET_APPS_ALL)
        return res.get("apps")

    async def get_current_app(self) -> dict[str, Any] | None:
        """Get the current app id."""
        res = await self.request(ep.GET_CURRENT_APP_INFO)
        return res.get("appId")

    async def subscribe_current_app(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in the current app id."""

        async def current_app(payload: dict[str, Any]) -> None:
            await callback(payload.get("appId"))

        return await self.subscribe(current_app, ep.GET_CURRENT_APP_INFO)

    async def launch_app(self, app: str) -> dict[str, Any]:
        """Launch an app."""
        return await self.request(ep.LAUNCH, {"id": app})

    async def launch_app_with_params(
        self, app: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Launch an app with parameters."""
        return await self.request(ep.LAUNCH, {"id": app, "params": params})

    async def launch_app_with_content_id(
        self, app: str, content_id: str
    ) -> dict[str, Any]:
        """Launch an app with contentId."""
        return await self.request(ep.LAUNCH, {"id": app, "contentId": content_id})

    async def close_app(self, app: str) -> dict[str, Any]:
        """Close the current app."""
        return await self.request(ep.LAUNCHER_CLOSE, {"id": app})

    # Services
    async def get_services(self) -> dict[str, Any] | None:
        """Get all services."""
        res = await self.request(ep.GET_SERVICES)
        return res.get("services")

    async def get_software_info(self) -> dict[str, Any]:
        """Return the current software status."""
        return await self.request(ep.GET_SOFTWARE_INFO)

    async def get_system_info(
        self,
    ) -> dict[str, Any]:
        """Return the system information."""
        return await self.request(ep.GET_SYSTEM_INFO)

    async def get_connection_macs(self) -> dict[str, Any]:
        """Return the MAC addresses of network interfaces."""
        return await self.request(ep.GET_CONNECTION_MACS)

    async def power_off(self) -> None:
        """Power off TV.

        Protect against turning tv back on if it is off.
        """
        if not self.tv_state.is_on:
            return

        # if tv is shutting down and standby+ option is not enabled,
        # response is unreliable, so don't wait for one,
        await self.command("request", ep.POWER_OFF)

    async def power_on(self) -> dict[str, Any]:
        """Play media."""
        return await self.request(ep.POWER_ON)

    # 3D Mode
    async def turn_3d_on(self) -> dict[str, Any]:
        """Turn 3D on."""
        return await self.request(ep.SET_3D_ON)

    async def turn_3d_off(self) -> dict[str, Any]:
        """Turn 3D off."""
        return await self.request(ep.SET_3D_OFF)

    # Inputs
    async def get_inputs(self) -> dict[str, Any] | None:
        """Get all inputs."""
        res = await self.request(ep.GET_INPUTS)
        return res.get("devices")

    async def subscribe_inputs(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in available inputs."""

        async def inputs(payload: dict[str, Any]) -> None:
            await callback(payload.get("devices"))

        return await self.subscribe(inputs, ep.GET_INPUTS)

    async def get_input(self) -> dict[str, Any] | None:
        """Get current input."""
        return await self.get_current_app()

    async def set_input(self, input_id: str) -> dict[str, Any]:
        """Set the current input."""
        return await self.request(ep.SET_INPUT, {"inputId": input_id})

    # Audio
    async def get_audio_status(self) -> dict[str, Any]:
        """Get the current audio status."""
        return await self.request(ep.GET_AUDIO_STATUS)

    async def get_muted(self) -> bool | None:
        """Get mute status."""
        status = await self.get_audio_status()
        return status.get("mute")

    async def subscribe_muted(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in the current mute status."""

        async def muted(payload: dict[str, Any]) -> None:
            await callback(payload.get("mute"))

        return await self.subscribe(muted, ep.GET_AUDIO_STATUS)

    async def set_mute(self, mute: bool) -> dict[str, Any]:  # noqa: FBT001
        """Set mute."""
        return await self.request(ep.SET_MUTE, {"mute": mute})

    async def get_volume(self) -> int | None:
        """Get the current volume."""
        res = await self.request(ep.GET_VOLUME)
        return res.get("volumeStatus", res).get("volume")  # type: ignore[no-any-return]

    async def subscribe_volume(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in the current volume."""

        async def volume(payload: dict[str, Any]) -> None:
            await callback(payload.get("volumeStatus", payload).get("volume"))

        return await self.subscribe(volume, ep.GET_VOLUME)

    async def set_volume(self, volume: int) -> dict[str, Any]:
        """Set volume."""
        volume = max(0, volume)
        return await self.request(ep.SET_VOLUME, {"volume": volume})

    async def volume_up(self) -> dict[str, Any]:
        """Volume up."""
        return await self._volume_step(ep.VOLUME_UP)

    async def volume_down(self) -> dict[str, Any]:
        """Volume down."""
        return await self._volume_step(ep.VOLUME_DOWN)

    async def _volume_step(self, endpoint: str) -> dict[str, Any]:
        """Set volume with step delay.

        Set volume and conditionally sleep if a consecutive volume step
        shouldn't be possible to perform immediately after.
        """
        if (
            self.tv_state.sound_output
            in SOUND_OUTPUTS_TO_DELAY_CONSECUTIVE_VOLUME_STEPS
            and self._volume_step_delay is not None
        ):
            async with self._volume_step_lock:
                response = await self.request(endpoint)
                await asyncio.sleep(self._volume_step_delay.total_seconds())
                return response
        else:
            return await self.request(endpoint)

    # TV Channel
    async def channel_up(self) -> dict[str, Any]:
        """Channel up."""
        return await self.request(ep.TV_CHANNEL_UP)

    async def channel_down(self) -> dict[str, Any]:
        """Channel down."""
        return await self.request(ep.TV_CHANNEL_DOWN)

    async def get_channels(self) -> list[dict[str, Any]] | None:
        """Get list of tv channels."""
        res = await self.request(ep.GET_TV_CHANNELS)
        return res.get("channelList")

    async def subscribe_channels(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to list of tv channels."""

        async def channels(payload: dict[str, Any]) -> None:
            await callback(payload.get("channelList", []))

        return await self.subscribe(channels, ep.GET_TV_CHANNELS)

    async def get_current_channel(self) -> dict[str, Any]:
        """Get the current tv channel."""
        return await self.request(ep.GET_CURRENT_CHANNEL)

    async def subscribe_current_channel(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in the current tv channel."""
        return await self.resubscribe(callback, ep.GET_CURRENT_CHANNEL)

    async def get_channel_info(self) -> dict[str, Any]:
        """Get the current channel info."""
        return await self.request(ep.GET_CHANNEL_INFO)

    async def subscribe_channel_info(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to current channel info."""
        return await self.resubscribe(callback, ep.GET_CHANNEL_INFO)

    async def set_channel(self, channel: str) -> dict[str, Any]:
        """Set the current channel."""
        return await self.request(ep.SET_CHANNEL, {"channelId": channel})

    async def get_sound_output(self) -> dict[str, Any] | None:
        """Get the current audio output."""
        res = await self.request(ep.GET_SOUND_OUTPUT)
        return res.get("soundOutput")

    async def subscribe_sound_output(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to changes in current audio output."""

        async def sound_output(payload: dict[str, Any]) -> None:
            await callback(payload.get("soundOutput"))

        return await self.subscribe(sound_output, ep.GET_SOUND_OUTPUT)

    async def change_sound_output(self, output: str) -> dict[str, Any]:
        """Change current audio output."""
        return await self.request(ep.CHANGE_SOUND_OUTPUT, {"output": output})

    # Media control
    async def play(self) -> dict[str, Any]:
        """Play media."""
        return await self.request(ep.MEDIA_PLAY)

    async def pause(self) -> dict[str, Any]:
        """Pause media."""
        return await self.request(ep.MEDIA_PAUSE)

    async def stop(self) -> dict[str, Any]:
        """Stop media."""
        return await self.request(ep.MEDIA_STOP)

    async def close(self) -> dict[str, Any]:
        """Close media."""
        return await self.request(ep.MEDIA_CLOSE)

    async def rewind(self) -> dict[str, Any]:
        """Rewind media."""
        return await self.request(ep.MEDIA_REWIND)

    async def fast_forward(self) -> dict[str, Any]:
        """Fast Forward media."""
        return await self.request(ep.MEDIA_FAST_FORWARD)

    async def get_media_foreground_app(self) -> list[dict[str, Any]]:
        """Get media player state."""
        res = await self.request(ep.GET_MEDIA_FOREGROUND_APP_INFO)
        return cast(list, res.get("foregroundAppInfo", []))

    async def subscribe_media_foreground_app(
        self, callback: Callable
    ) -> dict[str, bool]:
        """Subscribe to changes in media player state."""

        async def current_media(payload: dict[str, Any]) -> None:
            await callback(payload.get("foregroundAppInfo", []))

        return await self.subscribe(current_media, ep.GET_MEDIA_FOREGROUND_APP_INFO)
