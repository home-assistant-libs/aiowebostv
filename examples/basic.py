"""Basic webOS client example."""

import asyncio
from pprint import pprint

from aiowebostv import WebOsClient

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def main() -> None:
    """Webos client example."""
    client = WebOsClient(HOST, CLIENT_KEY)
    await client.connect()

    # Store this key for future use
    print(f"Client key: {client.client_key}")

    pprint(client.tv_info)
    pprint(client.tv_state)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
