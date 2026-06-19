"""Entry point.

    python -m offleash            run the webhook server (inbound and outbound)
    python -m offleash call +1..  place one outbound call to a number

Outbound dialing lives here rather than as an open HTTP route so the server's
only exposed surface is the signed webhook receiver.
"""

from __future__ import annotations

import asyncio
import sys

from offleash.settings import get_settings
from offleash.telnyx import TelnyxClient


async def _place_call(to: str) -> None:
    settings = get_settings()
    client = TelnyxClient(settings)
    try:
        call_control_id = await client.dial(to)
        print(call_control_id)
    finally:
        await client.aclose()


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "call":
        if len(args) < 2:
            print("usage: python -m offleash call +1XXXXXXXXXX", file=sys.stderr)
            raise SystemExit(2)
        asyncio.run(_place_call(args[1]))
        return

    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "offleash.server:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
