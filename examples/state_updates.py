"""Subscribed state updates example."""

import asyncio
import signal

from aiowebostv import WebOsClient

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def on_state_change(client: WebOsClient) -> None:
    """State changed callback."""
    print("State changed:")
    print(f"System info: {client.system_info}")
    print(f"Software info: {client.software_info}")
    print(f"Hello info: {client.hello_info}")
    print(f"Channel info: {client.channel_info}")
    print(f"Apps: {client.apps}")
    print(f"Inputs: {client.inputs}")
    print(f"Powered on: {client.power_state}")
    print(f"App Id: {client.current_app_id}")
    print(f"Channels: {client.channels}")
    print(f"Current channel: {client.current_channel}")
    print(f"Muted: {client.muted}")
    print(f"Volume: {client.volume}")
    print(f"Sound output: {client.sound_output}")


async def main() -> None:
    """Subscribe State Updates Example."""
    client = WebOsClient(HOST, CLIENT_KEY)
    await client.register_state_update_callback(on_state_change)
    await client.connect()

    # Store this key for future use
    print(f"Client key: {client.client_key}")

    # Change something using the remote to get updates or ctrl-c to exit
    sig_event = asyncio.Event()
    signal.signal(signal.SIGINT, lambda _exit_code, _frame: sig_event.set())
    await sig_event.wait()

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
