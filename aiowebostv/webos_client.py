"""Provide a websockets client for controlling LG webOS based TVs."""
import asyncio
import base64
import copy
import json
import os
from datetime import timedelta

import websockets

from . import endpoints as ep
from .exceptions import (
    WebOsTvCommandError,
    WebOsTvPairError,
    WebOsTvResponseTypeError,
    WebOsTvServiceNotFoundError,
)
from .handshake import REGISTRATION_MESSAGE

SOUND_OUTPUTS_TO_DELAY_CONSECUTIVE_VOLUME_STEPS = {"external_arc"}


class WebOsClient:
    """webOS TV client class."""

    def __init__(
        self, host, client_key=None, timeout_connect=2, ping_interval=1, ping_timeout=20
    ):
        """Initialize the client."""
        self.host = host
        self.port = 3000
        self.client_key = client_key
        self.command_count = 0
        self.timeout_connect = timeout_connect
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.connect_task = None
        self.connect_result = None
        self.connection = None
        self.input_connection = None
        self.callbacks = {}
        self.futures = {}
        self._power_state = {}
        self._current_app_id = None
        self._muted = None
        self._volume = None
        self._current_channel = None
        self._channel_info = None
        self._channels = None
        self._apps = {}
        self._extinputs = {}
        self._system_info = None
        self._software_info = None
        self._hello_info = None
        self._sound_output = None
        self.state_update_callbacks = []
        self.do_state_update = False
        self._volume_step_lock = asyncio.Lock()
        self._volume_step_delay = None
        self._loop = asyncio.get_running_loop()

    async def connect(self):
        """Connect to webOS TV device."""
        if not self.is_connected():
            self.connect_result = self._loop.create_future()
            self.connect_task = asyncio.create_task(
                self.connect_handler(self.connect_result)
            )
        return await self.connect_result

    async def disconnect(self):
        """Disconnect from webOS TV device."""
        if self.is_connected():
            self.connect_task.cancel()
            try:
                await self.connect_task
            except asyncio.CancelledError:
                pass

    def is_registered(self):
        """Paired with the tv."""
        return self.client_key is not None

    def is_connected(self):
        """Return true if connected to the tv."""
        return self.connect_task is not None and not self.connect_task.done()

    def registration_msg(self):
        """Create registration message."""
        handshake = copy.deepcopy(REGISTRATION_MESSAGE)
        handshake["payload"]["client-key"] = self.client_key
        return handshake

    async def connect_handler(self, res):
        """Handle connection for webOS TV."""
        # pylint: disable=too-many-locals,too-many-statements
        handler_tasks = set()
        main_ws = None
        input_ws = None
        try:
            main_ws = await asyncio.wait_for(
                websockets.client.connect(
                    f"ws://{self.host}:{self.port}",
                    ping_interval=None,
                    close_timeout=self.timeout_connect,
                    max_size=None,
                ),
                timeout=self.timeout_connect,
            )

            # send hello
            await main_ws.send(json.dumps({"id": "hello", "type": "hello"}))
            raw_response = await main_ws.recv()
            response = json.loads(raw_response)

            if response["type"] == "hello":
                self._hello_info = response["payload"]
            else:
                raise WebOsTvCommandError(f"Invalid request type {response}")

            # send registration
            await main_ws.send(json.dumps(self.registration_msg()))
            raw_response = await main_ws.recv()
            response = json.loads(raw_response)

            if (
                response["type"] == "response"
                and response["payload"]["pairingType"] == "PROMPT"
            ):
                raw_response = await main_ws.recv()
                response = json.loads(raw_response)
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
            if self.ping_interval is not None:
                handler_tasks.add(
                    asyncio.create_task(
                        self.ping_handler(
                            main_ws, self.ping_interval, self.ping_timeout
                        )
                    )
                )
            self.connection = main_ws

            # open additional connection needed to send button commands
            # the url is dynamically generated and returned from the ep.INPUT_SOCKET
            # endpoint on the main connection
            sockres = await self.request(ep.INPUT_SOCKET)
            inputsockpath = sockres.get("socketPath")
            input_ws = await asyncio.wait_for(
                websockets.client.connect(
                    inputsockpath,
                    ping_interval=None,
                    close_timeout=self.timeout_connect,
                ),
                timeout=self.timeout_connect,
            )

            handler_tasks.add(asyncio.create_task(input_ws.wait_closed()))
            if self.ping_interval is not None:
                handler_tasks.add(
                    asyncio.create_task(
                        self.ping_handler(
                            input_ws, self.ping_interval, self.ping_timeout
                        )
                    )
                )
            self.input_connection = input_ws

            # set static state and subscribe to state updates
            # avoid partial updates during initial subscription

            self.do_state_update = False
            self._system_info, self._software_info = await asyncio.gather(
                self.get_system_info(), self.get_software_info()
            )
            subscribe_state_updates = {
                self.subscribe_power_state(self.set_power_state),
                self.subscribe_current_app(self.set_current_app_state),
                self.subscribe_muted(self.set_muted_state),
                self.subscribe_volume(self.set_volume_state),
                self.subscribe_apps(self.set_apps_state),
                self.subscribe_inputs(self.set_inputs_state),
                self.subscribe_sound_output(self.set_sound_output_state),
            }
            subscribe_tasks = set()
            for state_update in subscribe_state_updates:
                subscribe_tasks.add(asyncio.create_task(state_update))
            await asyncio.wait(subscribe_tasks)
            for task in subscribe_tasks:
                try:
                    task.result()
                except WebOsTvServiceNotFoundError:
                    pass
            # set placeholder power state if not available
            if not self._power_state:
                self._power_state = {"state": "Unknown"}
            self.do_state_update = True
            if self.state_update_callbacks:
                await self.do_state_update_callbacks()

            res.set_result(True)

            await asyncio.wait(handler_tasks, return_when=asyncio.FIRST_COMPLETED)

        except Exception as ex:  # pylint: disable=broad-except
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

            self.connection = None
            self.input_connection = None

            self.do_state_update = False

            self._power_state = {}
            self._current_app_id = None
            self._muted = None
            self._volume = None
            self._current_channel = None
            self._channel_info = None
            self._channels = None
            self._apps = {}
            self._extinputs = {}
            self._system_info = None
            self._software_info = None
            self._hello_info = None
            self._sound_output = None

            for callback in self.state_update_callbacks:
                closeout.add(callback(self))

            if closeout:
                closeout_task = asyncio.create_task(asyncio.wait(closeout))

                while not closeout_task.done():
                    try:
                        await asyncio.shield(closeout_task)
                    except asyncio.CancelledError:
                        pass

    async def ping_handler(self, web_socket, interval, timeout):
        """Ping Handler loop."""
        try:
            while True:
                await asyncio.sleep(interval)
                # In the "Suspend" state the tv can keep a connection alive,
                # but will not respond to pings
                if self._power_state.get("state") != "Suspend":
                    ping_waiter = await web_socket.ping()
                    if timeout is not None:
                        await asyncio.wait_for(ping_waiter, timeout=timeout)
        except (
            asyncio.TimeoutError,
            asyncio.CancelledError,
            websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
        ):
            pass

    @staticmethod
    async def callback_handler(queue, callback, future):
        """Handle callbacks."""
        try:
            while True:
                msg = await queue.get()
                payload = msg.get("payload")
                await callback(payload)
                if future is not None and not future.done():
                    future.set_result(msg)
        except asyncio.CancelledError:
            pass

    async def consumer_handler(self, web_socket, callbacks, futures):
        """Callbacks consumer handler."""
        callback_queues = {}
        callback_tasks = {}

        try:
            async for raw_msg in web_socket:
                if callbacks or futures:
                    msg = json.loads(raw_msg)
                    uid = msg.get("id")
                    callback = self.callbacks.get(uid)
                    future = self.futures.get(uid)
                    if callback is not None:
                        if uid not in callback_tasks:
                            queue = asyncio.Queue()
                            callback_queues[uid] = queue
                            callback_tasks[uid] = asyncio.create_task(
                                self.callback_handler(queue, callback, future)
                            )
                        callback_queues[uid].put_nowait(msg)
                    elif future is not None and not future.done():
                        self.futures[uid].set_result(msg)

        except (
            asyncio.CancelledError,
            websockets.exceptions.ConnectionClosedError,
            websockets.exceptions.ConnectionClosedOK,
        ):
            pass
        finally:
            for task in callback_tasks.values():
                if not task.done():
                    task.cancel()

            tasks = set()
            tasks.update(callback_tasks.values())

            if tasks:
                closeout_task = asyncio.create_task(asyncio.wait(tasks))

                while not closeout_task.done():
                    try:
                        await asyncio.shield(closeout_task)
                    except asyncio.CancelledError:
                        pass

    # manage state
    @property
    def power_state(self):
        """Return TV power state."""
        return self._power_state

    @property
    def current_app_id(self):
        """Return current TV App Id."""
        return self._current_app_id

    @property
    def muted(self):
        """Return true if TV is muted."""
        return self._muted

    @property
    def volume(self):
        """Return current TV volume level."""
        return self._volume

    @property
    def current_channel(self):
        """Return current TV channel."""
        return self._current_channel

    @property
    def channel_info(self):
        """Return current channel info."""
        return self._channel_info

    @property
    def channels(self):
        """Return channel list."""
        return self._channels

    @property
    def apps(self):
        """Return apps list."""
        return self._apps

    @property
    def inputs(self):
        """Return external inputs list."""
        return self._extinputs

    @property
    def system_info(self):
        """Return TV system info."""
        return self._system_info

    @property
    def software_info(self):
        """Return TV software info."""
        return self._software_info

    @property
    def hello_info(self):
        """Return TV hello info."""
        return self._hello_info

    @property
    def sound_output(self):
        """Return TV sound output."""
        return self._sound_output

    @property
    def is_on(self):
        """Return true if TV is powered on."""
        state = self._power_state.get("state")
        if state == "Unknown":
            # fallback to current app id for some older webos versions
            # which don't support explicit power state
            if self._current_app_id in [None, ""]:
                return False
            return True
        if state in [None, "Power Off", "Suspend", "Active Standby"]:
            return False
        return True

    @property
    def is_screen_on(self):
        """Return true if screen is on."""
        if self.is_on:
            return self._power_state.get("state") != "Screen Off"
        return False

    async def register_state_update_callback(self, callback):
        """Register user state update callback."""
        self.state_update_callbacks.append(callback)
        if self.do_state_update:
            await callback(self)

    def set_volume_step_delay(self, step_delay_ms):
        """Set volume step delay in ms or None to disable step delay."""
        if step_delay_ms is None:
            self._volume_step_delay = None
            return

        self._volume_step_delay = timedelta(milliseconds=step_delay_ms)

    def unregister_state_update_callback(self, callback):
        """Unregister user state update callback."""
        if callback in self.state_update_callbacks:
            self.state_update_callbacks.remove(callback)

    def clear_state_update_callbacks(self):
        """Clear user state update callback."""
        self.state_update_callbacks = []

    async def do_state_update_callbacks(self):
        """Call user state update callback."""
        callbacks = set()
        for callback in self.state_update_callbacks:
            callbacks.add(callback(self))

        if callbacks:
            await asyncio.gather(*callbacks)

    async def set_power_state(self, payload):
        """Set TV power state callback."""
        self._power_state = payload

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_current_app_state(self, app_id):
        """Set current app state variable.

        This function also handles subscriptions to current channel and
        channel list, since the current channel subscription can only
        succeed when Live TV is running and channel list subscription
        can only succeed after channels have been configured.
        """
        self._current_app_id = app_id

        if self._channels is None:
            try:
                await self.subscribe_channels(self.set_channels_state)
            except WebOsTvCommandError:
                pass

        if app_id == "com.webos.app.livetv" and self._current_channel is None:
            try:
                await self.subscribe_current_channel(self.set_current_channel_state)
            except WebOsTvCommandError:
                pass

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_muted_state(self, muted):
        """Set TV mute state callback."""
        self._muted = muted

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_volume_state(self, volume):
        """Set TV volume level callback."""
        self._volume = volume

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_channels_state(self, channels):
        """Set TV channels callback."""
        self._channels = channels

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_current_channel_state(self, channel):
        """Set current channel state variable.

        This function also handles the channel info subscription,
        since that call may fail if channel information is not
        available when it's called.
        """
        self._current_channel = channel

        if self._channel_info is None:
            try:
                await self.subscribe_channel_info(self.set_channel_info_state)
            except WebOsTvCommandError:
                pass

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_channel_info_state(self, channel_info):
        """Set TV channel info state callback."""
        self._channel_info = channel_info

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_apps_state(self, payload):
        """Set TV channel apps state callback."""
        apps = payload.get("launchPoints")
        if apps is not None:
            self._apps = {}
            for app in apps:
                self._apps[app["id"]] = app
        else:
            change = payload["change"]
            app_id = payload["id"]
            if change == "removed":
                del self._apps[app_id]
            else:
                self._apps[app_id] = payload

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_inputs_state(self, extinputs):
        """Set TV inputs state callback."""
        self._extinputs = {}
        for extinput in extinputs:
            self._extinputs[extinput["appId"]] = extinput

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_sound_output_state(self, sound_output):
        """Set TV sound output state callback."""
        self._sound_output = sound_output

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    # low level request handling

    async def command(self, request_type, uri, payload=None, uid=None):
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

        await self.connection.send(json.dumps(message))

    async def request(self, uri, payload=None, cmd_type="request", uid=None):
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

        return_value = payload.get("returnValue") or payload.get("subscribed")

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

    async def subscribe(self, callback, uri, payload=None):
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

    async def input_command(self, message):
        """Execute TV input command."""
        if self.input_connection is None:
            raise WebOsTvCommandError("Couldn't execute input command.")

        await self.input_connection.send(message)

    # high level request handling

    async def button(self, name):
        """Send button press command."""
        message = f"type:button\nname:{name}\n\n"
        await self.input_command(message)

    async def move(self, d_x, d_y, down=0):
        """Send cursor move command."""
        message = f"type:move\ndx:{d_x}\ndy:{d_y}\ndown:{down}\n\n"
        await self.input_command(message)

    async def click(self):
        """Send cursor click command."""
        message = "type:click\n\n"
        await self.input_command(message)

    async def scroll(self, d_x, d_y):
        """Send scroll command."""
        message = f"type:scroll\ndx:{d_x}\ndy:{d_y}\n\n"
        await self.input_command(message)

    async def send_message(self, message, icon_path=None):
        """Show a floating message."""
        icon_encoded_string = ""
        icon_extension = ""

        if icon_path is not None:
            icon_extension = os.path.splitext(icon_path)[1][1:]
            with open(icon_path, "rb") as icon_file:
                icon_encoded_string = base64.b64encode(icon_file.read()).decode("ascii")

        return await self.request(
            ep.SHOW_MESSAGE,
            {
                "message": message,
                "iconData": icon_encoded_string,
                "iconExtension": icon_extension,
            },
        )

    async def get_power_state(self):
        """Get current power state."""
        return await self.request(ep.GET_POWER_STATE)

    async def subscribe_power_state(self, callback):
        """Subscribe to current power state."""
        return await self.subscribe(callback, ep.GET_POWER_STATE)

    # Apps
    async def get_apps(self):
        """Return all apps."""
        res = await self.request(ep.GET_APPS)
        return res.get("launchPoints")

    async def subscribe_apps(self, callback):
        """Subscribe to changes in available apps."""
        return await self.subscribe(callback, ep.GET_APPS)

    async def get_apps_all(self):
        """Return all apps, including hidden ones."""
        res = await self.request(ep.GET_APPS_ALL)
        return res.get("apps")

    async def get_current_app(self):
        """Get the current app id."""
        res = await self.request(ep.GET_CURRENT_APP_INFO)
        return res.get("appId")

    async def subscribe_current_app(self, callback):
        """Subscribe to changes in the current app id."""

        async def current_app(payload):
            await callback(payload.get("appId"))

        return await self.subscribe(current_app, ep.GET_CURRENT_APP_INFO)

    async def launch_app(self, app):
        """Launch an app."""
        return await self.request(ep.LAUNCH, {"id": app})

    async def launch_app_with_params(self, app, params):
        """Launch an app with parameters."""
        return await self.request(ep.LAUNCH, {"id": app, "params": params})

    async def launch_app_with_content_id(self, app, content_id):
        """Launch an app with contentId."""
        return await self.request(ep.LAUNCH, {"id": app, "contentId": content_id})

    async def close_app(self, app):
        """Close the current app."""
        return await self.request(ep.LAUNCHER_CLOSE, {"id": app})

    # Services
    async def get_services(self):
        """Get all services."""
        res = await self.request(ep.GET_SERVICES)
        return res.get("services")

    async def get_software_info(self):
        """Return the current software status."""
        return await self.request(ep.GET_SOFTWARE_INFO)

    async def get_system_info(self):
        """Return the system information."""
        return await self.request(ep.GET_SYSTEM_INFO)

    async def power_off(self):
        """Power off TV.

        Protect against turning tv back on if it is off.
        """
        if not self.is_on:
            return

        # if tv is shutting down and standby+ option is not enabled,
        # response is unreliable, so don't wait for one,
        await self.command("request", ep.POWER_OFF)

    async def power_on(self):
        """Play media."""
        return await self.request(ep.POWER_ON)

    async def turn_screen_off(self, webos_ver=""):
        """Turn TV Screen off."""
        ep_name = f"TURN_OFF_SCREEN_WO{webos_ver}" if webos_ver else "TURN_OFF_SCREEN"

        if not hasattr(ep, ep_name):
            raise ValueError(f"there's no {ep_name} endpoint")

        return await self.request(getattr(ep, ep_name), {"standbyMode": "active"})

    async def turn_screen_on(self, webos_ver=""):
        """Turn TV Screen on."""
        ep_name = f"TURN_ON_SCREEN_WO{webos_ver}" if webos_ver else "TURN_ON_SCREEN"

        if not hasattr(ep, ep_name):
            raise ValueError(f"there's no {ep_name} endpoint")

        return await self.request(getattr(ep, ep_name), {"standbyMode": "active"})

    # 3D Mode
    async def turn_3d_on(self):
        """Turn 3D on."""
        return await self.request(ep.SET_3D_ON)

    async def turn_3d_off(self):
        """Turn 3D off."""
        return await self.request(ep.SET_3D_OFF)

    # Inputs
    async def get_inputs(self):
        """Get all inputs."""
        res = await self.request(ep.GET_INPUTS)
        return res.get("devices")

    async def subscribe_inputs(self, callback):
        """Subscribe to changes in available inputs."""

        async def inputs(payload):
            await callback(payload.get("devices"))

        return await self.subscribe(inputs, ep.GET_INPUTS)

    async def get_input(self):
        """Get current input."""
        return await self.get_current_app()

    async def set_input(self, input_id):
        """Set the current input."""
        return await self.request(ep.SET_INPUT, {"inputId": input_id})

    # Audio
    async def get_audio_status(self):
        """Get the current audio status."""
        return await self.request(ep.GET_AUDIO_STATUS)

    async def get_muted(self):
        """Get mute status."""
        status = await self.get_audio_status()
        return status.get("mute")

    async def subscribe_muted(self, callback):
        """Subscribe to changes in the current mute status."""

        async def muted(payload):
            await callback(payload.get("mute"))

        return await self.subscribe(muted, ep.GET_AUDIO_STATUS)

    async def set_mute(self, mute):
        """Set mute."""
        return await self.request(ep.SET_MUTE, {"mute": mute})

    async def get_volume(self):
        """Get the current volume."""
        res = await self.request(ep.GET_VOLUME)
        return res.get("volumeStatus", res).get("volume")

    async def subscribe_volume(self, callback):
        """Subscribe to changes in the current volume."""

        async def volume(payload):
            await callback(payload.get("volumeStatus", payload).get("volume"))

        return await self.subscribe(volume, ep.GET_VOLUME)

    async def set_volume(self, volume):
        """Set volume."""
        volume = max(0, volume)
        return await self.request(ep.SET_VOLUME, {"volume": volume})

    async def volume_up(self):
        """Volume up."""
        return await self._volume_step(ep.VOLUME_UP)

    async def volume_down(self):
        """Volume down."""
        return await self._volume_step(ep.VOLUME_DOWN)

    async def _volume_step(self, endpoint):
        """Set volume with step delay.

        Set volume and conditionally sleep if a consecutive volume step
        shouldn't be possible to perform immediately after.
        """
        if (
            self.sound_output in SOUND_OUTPUTS_TO_DELAY_CONSECUTIVE_VOLUME_STEPS
            and self._volume_step_delay is not None
        ):
            async with self._volume_step_lock:
                response = await self.request(endpoint)
                await asyncio.sleep(self._volume_step_delay.total_seconds())
                return response
        else:
            return await self.request(endpoint)

    # TV Channel
    async def channel_up(self):
        """Channel up."""
        return await self.request(ep.TV_CHANNEL_UP)

    async def channel_down(self):
        """Channel down."""
        return await self.request(ep.TV_CHANNEL_DOWN)

    async def get_channels(self):
        """Get list of tv channels."""
        res = await self.request(ep.GET_TV_CHANNELS)
        return res.get("channelList")

    async def subscribe_channels(self, callback):
        """Subscribe to list of tv channels."""

        async def channels(payload):
            await callback(payload.get("channelList"))

        return await self.subscribe(channels, ep.GET_TV_CHANNELS)

    async def get_current_channel(self):
        """Get the current tv channel."""
        return await self.request(ep.GET_CURRENT_CHANNEL)

    async def subscribe_current_channel(self, callback):
        """Subscribe to changes in the current tv channel."""
        return await self.subscribe(callback, ep.GET_CURRENT_CHANNEL)

    async def get_channel_info(self):
        """Get the current channel info."""
        return await self.request(ep.GET_CHANNEL_INFO)

    async def subscribe_channel_info(self, callback):
        """Subscribe to current channel info."""
        return await self.subscribe(callback, ep.GET_CHANNEL_INFO)

    async def set_channel(self, channel):
        """Set the current channel."""
        return await self.request(ep.SET_CHANNEL, {"channelId": channel})

    async def get_sound_output(self):
        """Get the current audio output."""
        res = await self.request(ep.GET_SOUND_OUTPUT)
        return res.get("soundOutput")

    async def subscribe_sound_output(self, callback):
        """Subscribe to changes in current audio output."""

        async def sound_output(payload):
            await callback(payload.get("soundOutput"))

        return await self.subscribe(sound_output, ep.GET_SOUND_OUTPUT)

    async def change_sound_output(self, output):
        """Change current audio output."""
        return await self.request(ep.CHANGE_SOUND_OUTPUT, {"output": output})

    # Media control
    async def play(self):
        """Play media."""
        return await self.request(ep.MEDIA_PLAY)

    async def pause(self):
        """Pause media."""
        return await self.request(ep.MEDIA_PAUSE)

    async def stop(self):
        """Stop media."""
        return await self.request(ep.MEDIA_STOP)

    async def close(self):
        """Close media."""
        return await self.request(ep.MEDIA_CLOSE)

    async def rewind(self):
        """Rewind media."""
        return await self.request(ep.MEDIA_REWIND)

    async def fast_forward(self):
        """Fast Forward media."""
        return await self.request(ep.MEDIA_FAST_FORWARD)
