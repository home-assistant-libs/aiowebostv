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
from pprint import pprint

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

    pprint(client.tv_info)
    pprint(client.tv_state)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
```

### Subscribed State Updates Example:
```python
import asyncio
import dataclasses
import signal
from datetime import UTC, datetime
from pprint import pprint

from aiowebostv import WebOsClient
from aiowebostv.models import WebOsTvState

HOST = "192.168.1.39"
# For first time pairing set key to None
CLIENT_KEY = "140cce792ae045920e14da4daa414582"


async def on_state_change(tv_state: WebOsTvState) -> None:
    """State changed callback."""
    now = datetime.now(UTC).astimezone().strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now}] State change:")
    # for the example, remove apps and inputs to make the output more readable
    state = dataclasses.replace(tv_state, apps={}, inputs={})
    pprint(state)


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
