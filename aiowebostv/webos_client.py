"""Provide a websockets client for controlling LG webOS based TVs."""

import asyncio
import base64
import os
from collections.abc import Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any, cast

from aiohttp import ClientSession

from aiowebostv.base_client import WebOsClientBase

from . import endpoints as ep
from .exceptions import (
    WebOsTvCommandError,
)

SOUND_OUTPUTS_TO_DELAY_CONSECUTIVE_VOLUME_STEPS = {"external_arc"}


class WebOsClient(WebOsClientBase):
    """webOS TV client class."""

    def __init__(
        self,
        host: str,
        client_key: str | None = None,
        connect_timeout: float = 2,
        heartbeat: float = 5,
        client_session: ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        super().__init__(host, client_key, connect_timeout, heartbeat, client_session)
        self.subscribe_state_updates = {
            self.subscribe_power_state(self.set_power_state),
            self.subscribe_current_app(self.set_current_app_state),
            self.subscribe_muted(self.set_muted_state),
            self.subscribe_volume(self.set_volume_state),
            self.subscribe_apps(self.set_apps_state),
            self.subscribe_inputs(self.set_inputs_state),
            self.subscribe_sound_output(self.set_sound_output_state),
            self.subscribe_media_foreground_app(self.set_media_state),
        }

        self._power_state: dict[str, Any] = {}
        self._current_app_id: str | None = None
        self._muted: bool | None = None
        self._volume: int | None = None
        self._current_channel: dict[str, Any] | None = None
        self._channel_info: dict[str, Any] | None = None
        self._channels: list[dict[str, Any]] | None = None
        self._apps: dict[str, Any] = {}
        self._extinputs: dict[str, Any] = {}
        self._sound_output: str | None = None
        self._volume_step_lock = asyncio.Lock()
        self._volume_step_delay: timedelta | None = None
        self._media_state: list[dict[str, Any]] = []

    def clean_properties(self) -> None:
        """Clean collected properties from webOS TV upon disconnect."""
        super().clean_properties()
        self._power_state = {}
        self._current_app_id = None
        self._muted = None
        self._volume = None
        self._current_channel = None
        self._channel_info = None
        self._channels = None
        self._apps = {}
        self._extinputs = {}
        self._sound_output = None
        self._media_state = []

    # manage state
    @property
    def power_state(self) -> dict[str, Any]:
        """Return TV power state."""
        return self._power_state

    @property
    def current_app_id(self) -> str | None:
        """Return current TV App Id."""
        return self._current_app_id

    @property
    def muted(self) -> bool | None:
        """Return true if TV is muted."""
        return self._muted

    @property
    def volume(self) -> int | None:
        """Return current TV volume level."""
        return self._volume

    @property
    def current_channel(self) -> dict[str, Any] | None:
        """Return current TV channel."""
        return self._current_channel

    @property
    def channel_info(self) -> dict[str, Any] | None:
        """Return current channel info."""
        return self._channel_info

    @property
    def channels(self) -> list[dict[str, Any]] | None:
        """Return channel list."""
        return self._channels

    @property
    def apps(self) -> dict[str, Any]:
        """Return apps list."""
        return self._apps

    @property
    def inputs(self) -> dict[str, Any]:
        """Return external inputs list."""
        return self._extinputs

    @property
    def sound_output(self) -> str | None:
        """Return TV sound output."""
        return self._sound_output

    @property
    def is_on(self) -> bool:
        """Return true if TV is powered on."""
        if state := self._power_state.get("state"):
            return state not in [None, "Power Off", "Suspend", "Active Standby"]

        # fallback to current app id for some older webos versions
        # which don't support explicit power state
        return self._current_app_id not in [None, ""]

    @property
    def is_screen_on(self) -> bool:
        """Return true if screen is on."""
        if self.is_on:
            return self._power_state.get("state") != "Screen Off"
        return False

    @property
    def media_state(self) -> list[dict[str, Any]]:
        """Return media player state."""
        return self._media_state

    def set_volume_step_delay(self, step_delay_ms: float | None) -> None:
        """Set volume step delay in ms or None to disable step delay."""
        if step_delay_ms is None:
            self._volume_step_delay = None
            return

        self._volume_step_delay = timedelta(milliseconds=step_delay_ms)

    async def set_power_state(self, payload: dict[str, bool | str]) -> None:
        """Set TV power state callback."""
        self._power_state = payload

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_current_app_state(self, app_id: str) -> None:
        """Set current app state variable.

        This function also handles subscriptions to current channel and
        channel list, since the current channel subscription can only
        succeed when Live TV is running and channel list subscription
        can only succeed after channels have been configured.
        """
        self._current_app_id = app_id

        if self._channels is None:
            with suppress(WebOsTvCommandError):
                await self.subscribe_channels(self.set_channels_state)

        if app_id == "com.webos.app.livetv" and self._current_channel is None:
            await asyncio.sleep(2)
            with suppress(WebOsTvCommandError):
                await self.subscribe_current_channel(self.set_current_channel_state)

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_muted_state(self, muted: bool) -> None:  # noqa: FBT001
        """Set TV mute state callback."""
        self._muted = muted

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_volume_state(self, volume: int) -> None:
        """Set TV volume level callback."""
        self._volume = volume

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_channels_state(self, channels: list[dict[str, Any]]) -> None:
        """Set TV channels callback."""
        self._channels = channels

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_current_channel_state(self, channel: dict[str, Any]) -> None:
        """Set current channel state variable.

        This function also handles the channel info subscription,
        since that call may fail if channel information is not
        available when it's called.
        """
        self._current_channel = channel

        if self._channel_info is None:
            with suppress(WebOsTvCommandError):
                await self.subscribe_channel_info(self.set_channel_info_state)

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_channel_info_state(self, channel_info: dict[str, Any]) -> None:
        """Set TV channel info state callback."""
        self._channel_info = channel_info

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_apps_state(self, payload: dict[str, Any]) -> None:
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

    async def set_inputs_state(self, extinputs: list[dict[str, Any]]) -> None:
        """Set TV inputs state callback."""
        self._extinputs = {}
        for extinput in extinputs:
            self._extinputs[extinput["appId"]] = extinput

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_sound_output_state(self, sound_output: str) -> None:
        """Set TV sound output state callback."""
        self._sound_output = sound_output

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

    async def set_media_state(self, foreground_app_info: list[dict[str, bool]]) -> None:
        """Set TV media player state callback."""
        self._media_state = foreground_app_info

        if self.state_update_callbacks and self.do_state_update:
            await self.do_state_update_callbacks()

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
        message_payload["iconExtension"] = os.path.splitext(icon_path)[1][1:]
        with open(icon_path, "rb") as icon_file:
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

    async def power_off(self) -> None:
        """Power off TV.

        Protect against turning tv back on if it is off.
        """
        if not self.is_on:
            return

        # if tv is shutting down and standby+ option is not enabled,
        # response is unreliable, so don't wait for one,
        await self.command("request", ep.POWER_OFF)

    async def power_on(self) -> dict[str, Any]:
        """Play media."""
        return await self.request(ep.POWER_ON)

    async def turn_screen_off(self, webos_ver: str = "") -> dict[str, Any]:
        """Turn TV Screen off."""
        ep_name = f"TURN_OFF_SCREEN_WO{webos_ver}" if webos_ver else "TURN_OFF_SCREEN"

        if not hasattr(ep, ep_name):
            raise ValueError(f"there's no {ep_name} endpoint")

        return await self.request(getattr(ep, ep_name), {"standbyMode": "active"})

    async def turn_screen_on(self, webos_ver: str = "") -> dict[str, Any]:
        """Turn TV Screen on."""
        ep_name = f"TURN_ON_SCREEN_WO{webos_ver}" if webos_ver else "TURN_ON_SCREEN"

        if not hasattr(ep, ep_name):
            raise ValueError(f"there's no {ep_name} endpoint")

        return await self.request(getattr(ep, ep_name), {"standbyMode": "active"})

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
        return await self.subscribe(callback, ep.GET_CURRENT_CHANNEL)

    async def get_channel_info(self) -> dict[str, Any]:
        """Get the current channel info."""
        return await self.request(ep.GET_CHANNEL_INFO)

    async def subscribe_channel_info(self, callback: Callable) -> dict[str, Any]:
        """Subscribe to current channel info."""
        return await self.subscribe(callback, ep.GET_CHANNEL_INFO)

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
