"""Silent connect/reconnect example."""

import asyncio
import signal
from contextlib import suppress
from datetime import UTC, datetime

import aiohttp

from aiowebostv import WebOsClient
from aiowebostv.exceptions import WebOsTvCommandError

WEBOSTV_EXCEPTIONS = (
    ConnectionResetError,
    WebOsTvCommandError,
    aiohttp.ClientConnectorError,
    aiohttp.ServerDisconnectedError,
    asyncio.CancelledError,
    asyncio.TimeoutError,
)

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def main() -> None:
    """Silent connect/reconnect example, assuming TV is paired."""
    client = WebOsClient(HOST, CLIENT_KEY)

    sig_event = asyncio.Event()
    signal.signal(signal.SIGINT, lambda _exit_code, _frame: sig_event.set())

    # Turn the TV on or off using the remote or ctrl-c to exit
    while not sig_event.is_set():
        await asyncio.sleep(1)

        now = datetime.now(UTC).astimezone().strftime("%H:%M:%S.%f")[:-3]
        is_connected = client.is_connected()
        is_on = client.is_on

        print(f"[{now}] Connected: {is_connected}, Powered on: {is_on}")

        if is_connected:
            continue

        with suppress(*WEBOSTV_EXCEPTIONS):
            await client.connect()

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
