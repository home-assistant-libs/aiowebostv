"""Check the client against a real TV.

Run it with the TV off: it checks the connection error, wakes the TV with
Wake-on-LAN, runs requests, switches to Live TV and back to the app the TV
started on, then turns the TV off again. If the TV is on, the off and wake
steps are skipped.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from aiowebostv import (
    WebOsClient,
    WebOsTvConnectionError,
    WebOsTvServiceNotFoundError,
    WebOsTvState,
)
from aiowebostv import endpoints as ep

HOST = "192.168.1.39"
CLIENT_KEY = "140cce792ae045920e14da4daa414582"
# The TV's MAC addresses, empty to use the ones it reports on connect
MAC_ADDRESSES: list[str] = ["AA:BB:CC:DD:EE:FF"]
# A directed broadcast (e.g. 192.168.1.255) is more reliable on hosts with
# several network adapters
BROADCAST_ADDRESS = "255.255.255.255"

LIVE_TV_APP_ID = "com.webos.app.livetv"
WAKE_TIMEOUT = 60
STATE_TIMEOUT = 20


class Checks:
    """Collect and print check results."""

    def __init__(self) -> None:
        """Initialize the results."""
        self.start = time.monotonic()
        self.failed: list[str] = []

    def result(self, name: str, *, passed: bool | None, detail: str = "") -> None:
        """Print a result, passed None means skipped."""
        status = {True: "PASS", False: "FAIL", None: "SKIP"}[passed]
        elapsed = time.monotonic() - self.start
        print(f"[{elapsed:6.1f}s] {status} {name}{f': {detail}' if detail else ''}")
        if passed is False:
            self.failed.append(name)


async def wait_for_state(
    client: WebOsClient, predicate: Callable[[WebOsTvState], bool]
) -> bool:
    """Wait until the state matches, return False after STATE_TIMEOUT."""
    matched = asyncio.Event()

    async def on_state(state: WebOsTvState) -> None:
        if predicate(state):
            matched.set()

    await client.register_state_update_callback(on_state)
    try:
        async with asyncio.timeout(STATE_TIMEOUT):
            await matched.wait()
    except TimeoutError:
        return False
    finally:
        client.unregister_state_update_callback(on_state)
    return True


async def connect_error(client: WebOsClient) -> WebOsTvConnectionError | None:
    """Connect, return the connection error or None if connected."""
    try:
        await client.connect()
    except WebOsTvConnectionError as err:
        await client.disconnect()
        return err
    return None


async def check_off_and_wake(client: WebOsClient, checks: Checks) -> None:
    """Check the connection error while off and wake the TV."""
    err = await connect_error(client)
    if err is None:
        checks.result("connection error while off", passed=None, detail="TV is on")
        checks.result("turn on", passed=None, detail="TV is on")
        return

    checks.result(
        "connection error while off",
        passed=bool(str(err)) and err.__cause__ is not None,
        detail=f"{err} (cause: {err.__cause__!r})",
    )

    await client.turn_on(MAC_ADDRESSES or None, broadcast_address=BROADCAST_ADDRESS)
    start = time.monotonic()
    while time.monotonic() - start < WAKE_TIMEOUT:
        if await connect_error(client) is None and client.tv_state.is_on:
            checks.result(
                "turn on",
                passed=True,
                detail=f"on after {time.monotonic() - start:.0f}s",
            )
            return
        await client.disconnect()
        await asyncio.sleep(2)
    checks.result("turn on", passed=False, detail=f"not on within {WAKE_TIMEOUT}s")


async def check_connected(client: WebOsClient, checks: Checks) -> None:
    """Check the TV info and requests."""
    info = client.tv_info
    checks.result(
        "tv info",
        passed=bool(info.hello and info.system),
        detail=f"{info.system.get('modelName')}, "
        f"webOS {info.hello.get('deviceOSReleaseVersion')}",
    )
    expected = {mac.lower() for mac in MAC_ADDRESSES}
    reported = set(client.mac_addresses.values())
    checks.result(
        "mac addresses",
        passed=bool(reported) and expected <= reported,
        detail=str(client.mac_addresses),
    )

    services = await client.get_services()
    checks.result("request", passed=bool(services), detail=f"{len(services)} services")

    try:
        await client.request("com.webos.service.nonexistent/getNothing")
    except WebOsTvServiceNotFoundError as err:
        checks.result("unknown service", passed=True, detail=str(err))
    else:
        checks.result("unknown service", passed=False, detail="no error raised")

    calls = 0

    async def failing_callback(_state: WebOsTvState) -> None:
        nonlocal calls
        calls += 1
        msg = "callback error, logged on purpose"
        raise RuntimeError(msg)

    try:
        await client.register_state_update_callback(failing_callback)
    except RuntimeError:
        checks.result("failing callback", passed=False, detail="error raised")
    else:
        checks.result("failing callback", passed=calls == 1, detail="error logged")
    finally:
        client.unregister_state_update_callback(failing_callback)


async def check_live_tv(client: WebOsClient, checks: Checks) -> None:
    """Check the channel subscriptions on Live TV and after leaving it."""
    if not client.tv_state.channels:
        checks.result("live tv", passed=None, detail="no channels configured")
        return

    previous_app = client.tv_state.current_app_id
    await client.launch_app(LIVE_TV_APP_ID)
    on_channel = await wait_for_state(
        client,
        lambda state: (
            state.current_app_id == LIVE_TV_APP_ID and state.current_channel is not None
        ),
    )
    channel = client.tv_state.current_channel or {}
    checks.result(
        "live tv channel",
        passed=on_channel and ep.GET_CURRENT_CHANNEL in client._subscriptions,  # noqa: SLF001
        detail=str(channel.get("channelName", channel.get("channelId"))),
    )

    if not previous_app or previous_app == LIVE_TV_APP_ID:
        checks.result("leave live tv", passed=None, detail="no other app to return to")
        return
    await client.launch_app(previous_app)
    left = await wait_for_state(
        client,
        lambda state: (
            state.current_app_id == previous_app and state.current_channel is None
        ),
    )
    subscribed = {ep.GET_CURRENT_CHANNEL, ep.GET_CHANNEL_INFO}.intersection(
        client._subscriptions  # noqa: SLF001
    )
    checks.result(
        "leave live tv",
        passed=left and not subscribed,
        detail=f"back on {previous_app}, channel subscriptions: {subscribed or 'none'}",
    )


async def check_reconnect(client: WebOsClient, checks: Checks) -> None:
    """Check disconnecting and connecting again."""
    await client.disconnect()
    disconnected = not client.is_connected()
    connected = await client.connect()
    checks.result(
        "reconnect",
        passed=disconnected and connected and client.is_connected(),
        detail=f"is_on={client.tv_state.is_on}",
    )


async def check_power_off(client: WebOsClient, checks: Checks) -> None:
    """Turn the TV off and check connecting fails once it is off."""
    await client.power_off()
    off = await wait_for_state(client, lambda state: not state.is_on)
    checks.result(
        "power off", passed=off, detail=str(client.tv_state.power_state or "closed")
    )
    await client.disconnect()

    # The TV keeps answering for a few seconds while it shuts down
    await asyncio.sleep(15)
    err = await connect_error(client)
    if err is None:
        state = client.tv_state.power_state
        await client.disconnect()
        checks.result(
            "connection error after off", passed=None, detail=f"still answers: {state}"
        )
        return
    checks.result("connection error after off", passed=True, detail=str(err))


async def main() -> None:
    """Run the checks."""
    logging.basicConfig(level=logging.WARNING)
    checks = Checks()
    client = WebOsClient(HOST, CLIENT_KEY)

    try:
        await check_off_and_wake(client, checks)
        if not client.is_connected():
            await client.connect()
        await check_connected(client, checks)
        await check_live_tv(client, checks)
        await check_reconnect(client, checks)
        await check_power_off(client, checks)
    finally:
        await client.disconnect()

    print(
        f"\n{'FAILED: ' + ', '.join(checks.failed) if checks.failed else 'All passed'}"
    )


if __name__ == "__main__":
    asyncio.run(main())
