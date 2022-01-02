"""Silent connect/reconnect example."""
import asyncio
from contextlib import suppress
from datetime import datetime

from aiowebostv import WebOsClient
from aiowebostv.exceptions import WebOsTvCmdException
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

WEBOSTV_EXCEPTIONS = (
    OSError,
    ConnectionClosed,
    ConnectionClosedOK,
    ConnectionRefusedError,
    WebOsTvCmdException,
    asyncio.TimeoutError,
    asyncio.CancelledError,
)

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def main():
    """Silent connect/reconnect example, assuming TV is paired."""
    client = WebOsClient(HOST, CLIENT_KEY)

    while True:
        await asyncio.sleep(1)

        now = datetime.now().strftime("%H:%M:%S")
        is_connected = client.is_connected()
        is_on = client.is_on

        print(f"[{now}] Connected: {is_connected}, Powered on: {is_on}")

        if is_connected:
            continue

        with suppress(*WEBOSTV_EXCEPTIONS):
            await client.connect()


if __name__ == "__main__":
    asyncio.run(main())
