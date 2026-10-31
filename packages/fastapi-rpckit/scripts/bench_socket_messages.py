import argparse
import json
import platform
import statistics
import sys
import timeit
from pathlib import Path

from fastapi_canon.integrations.websockets._socket_messages import SocketMessages
from pydantic import BaseModel


class Message(BaseModel):
    value: int
    payload: str


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure typed socket JSON CPU cost")
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    messages = SocketMessages.create(Message, Message)

    def measure(function, number: int) -> float:
        return (
            statistics.median(timeit.repeat(function, number=number, repeat=5))
            * 1e6
            / number
        )

    results = []
    for length in (0, 65_536):
        message = Message(value=41, payload="x" * length)
        raw = message.model_dump_json()
        value = message.model_dump()
        count = args.iterations if length == 0 else max(100, args.iterations // 20)
        results.append(
            {
                "bytes": len(raw.encode()),
                "iterations": count,
                "validate_incoming_us": measure(
                    lambda raw=raw: messages.receive(raw), count
                ),
                "validate_and_encode_outgoing_us": measure(
                    lambda message=message: messages.send(message), count
                ),
                "stdlib_json_loads_us": measure(lambda raw=raw: json.loads(raw), count),
                "stdlib_json_dumps_us": measure(
                    lambda value=value: json.dumps(value), count
                ),
            }
        )
    report = {
        "python": sys.version,
        "platform": platform.platform(),
        "scope": (
            "JSON validation/serialization only; excludes sockets, queues, "
            "network and application"
        ),
        "adapters_build_us": measure(
            lambda: SocketMessages.create(Message, Message), 100
        ),
        "results": results,
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")


if __name__ == "__main__":
    main()
