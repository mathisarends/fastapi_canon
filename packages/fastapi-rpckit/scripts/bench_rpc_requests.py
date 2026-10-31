import argparse
import asyncio
import cProfile
import io
import json
import platform
import pstats
import statistics
import sys
import time
from pathlib import Path

from fastapi_canon.integrations.websockets import RpcChannel, RpcModel, RpcService
from fastapi_canon.integrations.websockets.testing import InMemorySocket
from pydantic import BaseModel


class Echo(RpcModel):
    value: int
    text: str


class PlainEcho(BaseModel):
    value: int
    text: str


bench = RpcChannel("bench")


@bench.method()
async def echo(params: Echo) -> Echo:
    return params


@bench.method()
async def plain(params: PlainEcho) -> PlainEcho:
    return params


@bench.method()
async def items(params: Echo) -> list[Echo]:
    return [params] * 10


service = RpcService()
service.socket("/rpc", channels=(bench,))


def _frame(request_id: int, method: str, length: int) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {"value": request_id, "text": "x" * length},
        }
    )


async def _connection():
    socket = InMemorySocket("/rpc")
    task = asyncio.create_task(service.serve(socket))
    await asyncio.sleep(0)
    return socket, task


async def _close(socket: InMemorySocket, task: asyncio.Task) -> None:
    await socket.client_disconnect()
    await asyncio.gather(task, return_exceptions=True)


async def sequential(method: str, count: int, length: int) -> float:
    """Median round-trip microseconds for one request at a time."""
    socket, task = await _connection()
    frames = [_frame(index, method, length) for index in range(count)]
    samples = []
    try:
        for frame in frames:
            started = time.perf_counter()
            await socket.client_send(frame)
            await socket.client_receive()
            samples.append(time.perf_counter() - started)
    finally:
        await _close(socket, task)
    return statistics.median(samples) * 1e6


async def pipelined(method: str, count: int, length: int, window: int) -> float:
    """Requests per second with up to `window` requests in flight."""
    socket, task = await _connection()
    frames = [_frame(index, method, length) for index in range(count)]
    try:
        started = time.perf_counter()
        sent = received = 0
        while received < count:
            while sent < count and sent - received < window:
                await socket.client_send(frames[sent])
                sent += 1
            await socket.client_receive()
            received += 1
        elapsed = time.perf_counter() - started
    finally:
        await _close(socket, task)
    return count / elapsed


async def run(args: argparse.Namespace) -> dict[str, object]:
    results = []
    for method in ("bench.echo", "bench.plain", "bench.items"):
        for length in (16, 16_384):
            count = args.requests if length < 1024 else max(200, args.requests // 10)
            await sequential(method, min(count, 200), length)  # warm up adapters
            results.append(
                {
                    "method": method,
                    "text_bytes": length,
                    "requests": count,
                    "sequential_round_trip_us": round(
                        statistics.median(
                            [
                                await sequential(method, count, length)
                                for _ in range(args.repeat)
                            ]
                        ),
                        2,
                    ),
                    "pipelined_requests_per_s": round(
                        statistics.median(
                            [
                                await pipelined(method, count, length, args.window)
                                for _ in range(args.repeat)
                            ]
                        )
                    ),
                }
            )
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "scope": (
            "In-process runtime path over InMemorySocket: frame decode, envelope "
            "and params validation, dispatch, result serialization and queueing. "
            "Excludes network, ASGI server and WebSocket framing."
        ),
        "window": args.window,
        "repeat": args.repeat,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure RPC request throughput")
    parser.add_argument("--requests", type=int, default=5_000)
    parser.add_argument("--window", type=int, default=64)
    parser.add_argument("--repeat", type=int, default=5, help="median of N runs")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--profile", action="store_true", help="print the top cProfile entries"
    )
    args = parser.parse_args()
    if min(args.requests, args.window, args.repeat) < 1:
        parser.error("--requests, --window and --repeat must be positive")
    if args.profile:
        profiler = cProfile.Profile()
        profiler.enable()
        asyncio.run(pipelined("bench.echo", args.requests, 16, args.window))
        profiler.disable()
        stream = io.StringIO()
        pstats.Stats(profiler, stream=stream).sort_stats("tottime").print_stats(25)
        print(stream.getvalue())
        return
    report = asyncio.run(run(args))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
