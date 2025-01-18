# aiowebostv
Python library to control LG webOS based TV devices.

Based on:
- `aiopylgtv` library at https://github.com/bendavid/aiopylgtv
- `bscpylgtv` library at https://github.com/chros73/bscpylgtv

## Requirements
- Python >= 3.11

## Install
```bash
pip install aiowebostv
```

## Install from Source
Run the following command inside this folder
```bash
pip install --upgrade .
```

## Examples
### Basic Example:
```python
import asyncio

from aiowebostv import WebOsClient

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def main():
    """Basic webOS client example."""

    client = WebOsClient(HOST, CLIENT_KEY)
    await client.connect()

    # Store this key for future use
    print(f"Client key: {client.client_key}")

    apps = await client.get_apps_all()
    for app in apps:
        print(app)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
```

### Subscribed State Updates Example:
```python
import asyncio
import signal

from aiowebostv import WebOsClient

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def on_state_change(client):
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


async def main():
    """Subscribed State Updates Example."""
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
```

Examples can be found in the `examples` folder
