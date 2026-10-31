"""Canon commands with lazily loaded transport tools."""

import argparse
from collections.abc import Sequence

from fastapi_canon._version import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="canon")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("sse", "Export SSE contracts and generate Python or TypeScript clients"),
        ("websockets", "Export OpenRPC contracts and generate WebSocket clients"),
        ("webrtc", "Export WebRTC contracts and generate browser clients"),
        ("check", "Check application contracts without importing application code"),
    ):
        commands.add_parser(name, help=description, add_help=False)
    args, remaining = parser.parse_known_args(argv)
    if args.command == "sse":
        from fastapi_canon.integrations.sse.codegen.cli import main as sse_main

        return sse_main(remaining)
    elif args.command == "websockets":
        from fastapi_canon.integrations.websockets.codegen.cli import (
            main as websockets_main,
        )

        return websockets_main(remaining)
    elif args.command == "webrtc":
        from fastapi_canon.integrations.webrtc.codegen.cli import main as webrtc_main

        return webrtc_main(remaining)
    else:
        checker = argparse.ArgumentParser(prog="canon check")
        checker.add_argument("kind", choices=("errors",))
        _, remaining = checker.parse_known_args(remaining)
        from fastapi_canon.check_error_contracts import _main as check_main

        return check_main(remaining)
