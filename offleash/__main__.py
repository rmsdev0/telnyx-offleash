"""Entry point.

    python -m offleash             run the webhook server (inbound and outbound)
    python -m offleash call +1..   place one outbound call to a number
    python -m offleash bench ...   run the barge-in latency harness (see bench/)

Outbound dialing lives here rather than as an open HTTP route so the server's
only exposed surface is the signed webhook receiver.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

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


async def _serve_until_bench_done() -> None:
    """Run the webhook server, then exit once the bench run completes.

    The harness is attached inside the app lifespan (BENCH_ENABLE=1). We poll for
    it, wait for its finished event, then ask uvicorn to shut down so the process
    exits cleanly with the data captured.
    """
    import uvicorn

    from offleash.server import app

    settings = get_settings()
    config = uvicorn.Config(
        app, host=settings.host, port=settings.port, log_config=None
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        controller = None
        while controller is None and not serve_task.done():
            controller = getattr(app.state, "bench", None)
            if controller is None:
                await asyncio.sleep(0.1)
        if controller is not None:
            await controller.finished.wait()
    finally:
        server.should_exit = True
        await serve_task


def _run_bench(argv: list[str]) -> None:
    p = argparse.ArgumentParser(prog="offleash bench")
    p.add_argument("--from", dest="from_number", required=True, help="harness number")
    p.add_argument("--to", dest="to_number", default="", help="agent number")
    p.add_argument("--events", type=int, default=35, help="barge-in trials to collect")
    p.add_argument("--label", default="default", help="condition tag")
    p.add_argument("--out", default="", help="JSONL output path")
    p.add_argument("--stimulus", default="", help="barge-in utterance text")
    p.add_argument(
        "--barge-offset", type=float, default=None, help="seconds into speech"
    )
    p.add_argument("--max-calls", type=int, default=None, help="redial safety cap")
    p.add_argument(
        "--trials-per-call", type=int, default=None, help="barge-ins per call"
    )
    a = p.parse_args(argv)

    out = a.out or f"bench/data/run-{a.label}-{int(time.time())}.jsonl"
    os.environ["BENCH_ENABLE"] = "1"
    os.environ["BENCH_FROM"] = a.from_number
    os.environ["BENCH_TARGET_EVENTS"] = str(a.events)
    os.environ["BENCH_LABEL"] = a.label
    os.environ["BENCH_OUT"] = out
    if a.to_number:
        os.environ["BENCH_TO"] = a.to_number
    if a.stimulus:
        os.environ["BENCH_STIMULUS_TEXT"] = a.stimulus
    if a.barge_offset is not None:
        os.environ["BENCH_BARGE_OFFSET_S"] = str(a.barge_offset)
    if a.max_calls is not None:
        os.environ["BENCH_MAX_CALLS"] = str(a.max_calls)
    if a.trials_per_call is not None:
        os.environ["BENCH_TRIALS_PER_CALL"] = str(a.trials_per_call)

    asyncio.run(_serve_until_bench_done())
    print(out)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "call":
        if len(args) < 2:
            print("usage: python -m offleash call +1XXXXXXXXXX", file=sys.stderr)
            raise SystemExit(2)
        asyncio.run(_place_call(args[1]))
        return

    if args and args[0] == "bench":
        _run_bench(args[1:])
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
