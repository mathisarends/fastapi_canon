from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from fastapi_canon.codegen.config import load_config


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fastapi-canon generate")
    parser.add_argument(
        "--config",
        type=Path,
        help="Configuration path (default: fastapi-canon.toml, then pyproject.toml)",
    )
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Read saved contracts without importing application code",
    )
    parser.add_argument(
        "--check", action="store_true", help="Check for drift without writing"
    )
    parser.add_argument(
        "--diff", action="store_true", help="Show the proposed diff; implies --check"
    )
    args = parser.parse_args(argv)
    try:
        from fastapi_canon.codegen.generate import generate

        path = args.config or (
            Path("fastapi-canon.toml")
            if Path("fastapi-canon.toml").exists()
            else Path("pyproject.toml")
        )
        changed, report = generate(
            load_config(path),
            targets=tuple(args.target),
            offline=args.offline,
            check=args.check or args.diff,
            diff=args.diff,
        )
        if report:
            print(report)
        return int(changed and (args.check or args.diff))
    except Exception as exc:
        print(f"fastapi-canon generate: {exc}", file=sys.stderr)
        return 2
