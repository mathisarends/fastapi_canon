"""Find missing route error contracts: ``python -m fastapi_canon.check_error_contracts``."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from fastapi_canon._checks.contracts import _check_error_contracts


def _main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fastapi_canon.check_error_contracts",
        description="Check explicit application exceptions against CanonRouter raises declarations without importing application code.",
    )
    parser.add_argument(
        "roots",
        nargs="+",
        type=Path,
        help="Python import roots, e.g. src (not src/app)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 for missing declarations or mappings",
    )
    parser.add_argument(
        "--require-complete",
        action="store_true",
        help="also exit 1 for incomplete analysis",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(arguments)
    try:
        report = _check_error_contracts(args.roots)
    except (OSError, SyntaxError, UnicodeError, ValueError) as error:
        parser.exit(2, f"error-contract check: {error}\n")
    if args.format == "json":
        print(json.dumps(report.as_dict(), indent=2))
    else:
        for operation in report.operations:
            for diagnostic in operation.diagnostics:
                print(
                    f"{diagnostic.path}:{diagnostic.line}: {diagnostic.code} [{operation.function}] {diagnostic.message}"
                )
                if diagnostic.trace:
                    print(f"  {' -> '.join(diagnostic.trace)}")
        print(
            f"Analyzed {len(report.operations)} operations; {report.gaps} contract gaps; {report.incomplete} incomplete operations."
        )
        if not report.operations:
            print(
                "No supported CanonRouter operations found; check the import roots and supported declaration forms."
            )
    return int(
        bool(
            (args.strict and report.gaps)
            or (args.require_complete and (report.incomplete or not report.operations))
        )
    )


if __name__ == "__main__":
    raise SystemExit(_main())
