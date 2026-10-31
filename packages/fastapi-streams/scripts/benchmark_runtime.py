"""Measure generated SDKs without network latency or performance assertions."""

import argparse
import asyncio
import importlib
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Literal

ROOT = Path(__file__).resolve().parents[1]


async def benchmark(args, directory):
    # Select source before importing, so one harness can compare revisions with
    # the same interpreter, dependencies, payloads and measurement parameters.
    sys.path.insert(0, str(args.source_dir.resolve()))
    import fastapi_canon.integrations.sse
    import httpx
    from fastapi import FastAPI, Query
    from fastapi import Path as PathParameter
    from fastapi_canon.integrations.sse import StreamRouter
    from fastapi_canon.integrations.sse.codegen.ir import build_ir
    from fastapi_canon.integrations.sse.codegen.openapi import export_schema
    from fastapi_canon.integrations.sse.codegen.python import generate_python
    from fastapi_canon.integrations.sse.codegen.typescript import generate_typescript
    from fastapi_canon.integrations.sse.codegen.writer import write_files
    from pydantic import BaseModel, Field

    class Token(BaseModel):
        type: Literal["token"]
        text: str

    class Item(BaseModel):
        name: str
        values: list[float]

    class Batch(BaseModel):
        type: Literal["batch"]
        items: list[Item]

    event_type = Annotated[Token | Batch, Field(discriminator="type")]
    router = StreamRouter()

    async def events(
        run_id: Annotated[str, PathParameter(pattern=r"^run_[a-z0-9]+$")],
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ):
        yield Token(type="token", text=run_id[:limit])

    events.__annotations__["return"] = AsyncIterator[event_type]
    router.stream("/runs/{run_id}/events", operation_id="events", tags=["bench"])(
        events
    )
    app = FastAPI()
    app.include_router(router)
    contract = build_ir(export_schema(app))
    payloads = {
        "small": json.dumps(
            {"type": "token", "text": "Hello 世界 👋"}, ensure_ascii=False
        ),
        "nested": json.dumps(
            {
                "type": "batch",
                "items": [
                    {"name": f"item-{index}", "values": [1.25, 2.5, 3.75]}
                    for index in range(20)
                ],
            }
        ),
        "unknown": json.dumps({"type": "future", "text": "skip"}),
    }
    payload_path = directory / "payloads.json"
    payload_path.write_text(json.dumps(payloads), encoding="utf-8")
    report = {
        "version": 1,
        "source": str(args.source_dir.resolve()),
        "imported_from": fastapi_canon.integrations.sse.__file__,
        "generator_version": fastapi_canon.integrations.sse.__version__,
        "platform": platform.platform(),
        "events_per_stream": args.events,
        "iterations": args.iterations,
        "rounds": args.rounds,
        "fragment_bytes": 7,
        "payload_bytes": {key: len(value.encode()) for key, value in payloads.items()},
        "runtimes": {},
    }
    if args.runtime in {"all", "python"}:
        write_files(directory / "python_sdk", generate_python(contract))
        sys.path.insert(0, str(directory))
        sdk = importlib.import_module("python_sdk")
        runtime = importlib.import_module("python_sdk.runtime")

        class Bytes(httpx.AsyncByteStream):
            def __init__(self, chunks):
                self.chunks = chunks

            async def __aiter__(self):
                for chunk in self.chunks:
                    yield chunk

        active_chunks = []

        def respond(_request):
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=Bytes(active_chunks),
            )

        results = {}
        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http,
            sdk.Client("http://benchmark.test", http_client=http) as client,
        ):
            adapter = client.bench._events_event_adapter

            async def prepare():
                stream = client.bench.events(run_id="run_abc", limit=100)
                await stream.aclose()

            results["request_preparation"] = await _measure(
                prepare, args.iterations * args.events, args, 1
            )
            for name in ("small", "nested"):

                async def decode(payload=payloads[name]):
                    adapter.validate_json(payload)

                results[f"decode_validate_{name}"] = await _measure(
                    decode, args.iterations * args.events, args, 1
                )

            for name in ("small", "nested", "fragmented", "unknown_ignore"):
                payload = payloads["nested" if name == "nested" else "small"]
                text = ("data: " + payload + "\n\n") * args.events
                count = args.events
                if name == "unknown_ignore":
                    text = ("data: " + payloads["unknown"] + "\n\n") * args.events
                    text += "data: " + payloads["small"] + "\n\n"
                    count += 1
                raw = text.encode()
                active_chunks = (
                    [raw[index : index + 7] for index in range(0, len(raw), 7)]
                    if name == "fragmented"
                    else [raw]
                )

                async def consume(ignore=name == "unknown_ignore"):
                    received = 0
                    async for _ in client.bench.events(
                        run_id="run_abc",
                        options=sdk.RequestOptions(
                            unknown_events="ignore" if ignore else "error"
                        ),
                    ):
                        received += 1
                    if received != (1 if ignore else args.events):
                        raise RuntimeError("Incomplete benchmark stream")

                results[f"stream_{name}"] = await _measure(
                    consume, args.iterations, args, count
                )
                if name != "unknown_ignore":

                    async def frame():
                        response = respond(None)
                        async for _ in runtime.iter_sse(response):
                            pass
                        await response.aclose()

                    results[f"frame_{name}"] = await _measure(
                        frame, args.iterations, args, count
                    )
        report["runtimes"]["python"] = {
            "version": sys.version,
            "httpx": httpx.__version__,
            "event_validation": "generated Pydantic models and supported constraints",
            "results": results,
        }
    if args.runtime in {"all", "typescript"}:
        sdk_dir = directory / "typescript_sdk"
        write_files(sdk_dir, generate_typescript(contract))
        (directory / "package.json").write_text('{"type":"module"}', encoding="utf-8")
        node = shutil.which("node")
        tsc = ROOT / "node_modules/typescript/bin/tsc"
        if node is None or not tsc.is_file():
            raise SystemExit("Install Node and run npm ci --ignore-scripts first")
        subprocess.run(
            [
                node,
                str(tsc),
                "--target",
                "ES2022",
                "--module",
                "NodeNext",
                "--moduleResolution",
                "NodeNext",
                "--strict",
                "--skipLibCheck",
                "--lib",
                "ES2022,DOM,DOM.Iterable",
                str(sdk_dir / "index.ts"),
            ],
            check=True,
        )
        result = subprocess.run(
            [
                node,
                str(ROOT / "scripts/benchmark_runtime.mjs"),
                str(sdk_dir),
                str(payload_path),
                str(args.events),
                str(args.iterations),
                str(args.rounds),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        report["runtimes"]["typescript"] = json.loads(result.stdout)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=ROOT)
    parser.add_argument(
        "--runtime", choices=("all", "python", "typescript"), default="all"
    )
    parser.add_argument("--events", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument(
        "--memory", action="store_true", help="Separate Python peak-memory sample"
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if min(args.events, args.iterations, args.rounds) < 1:
        parser.error("events, iterations and rounds must be positive")
    with tempfile.TemporaryDirectory(prefix="streams-benchmark-") as temporary:
        report = asyncio.run(benchmark(args, Path(temporary)))
    content = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8", newline="\n")
    else:
        print(content, end="")


async def _measure(operation, iterations, args, units):
    for _ in range(3):
        await operation()
    wall, cpu = [], []
    for _ in range(args.rounds):
        started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        for _ in range(iterations):
            await operation()
        cpu.append((time.process_time_ns() - cpu_started) / iterations / units / 1000)
        wall.append((time.perf_counter_ns() - started) / iterations / units / 1000)
    median = statistics.median(wall)
    result = {
        "iterations_per_round": iterations,
        "units_per_iteration": units,
        "median_us_per_unit": median,
        "cpu_median_us_per_unit": statistics.median(cpu),
        "units_per_second": 1_000_000 / median,
        "samples_us_per_unit": wall,
    }
    if args.memory:
        tracemalloc.start()
        try:
            await operation()
            result["peak_traced_bytes_per_iteration"] = tracemalloc.get_traced_memory()[
                1
            ]
        finally:
            tracemalloc.stop()
    return result


if __name__ == "__main__":
    main()
