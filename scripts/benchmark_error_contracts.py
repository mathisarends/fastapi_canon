"""Reproducible source-checker timings; generated projects and reports stay in dist/."""

import argparse
import cProfile
import hashlib
import json
import platform
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from time import perf_counter

from fastapi_canon._checks.contracts import _Checker
from fastapi_canon._checks.source import _Source

ROOT = Path(__file__).resolve().parents[1]


def _project(root: Path, features: int, routes: int, depth: int) -> None:
    (root / "errors.py").write_text(
        "from fastapi_canon import Error, ErrorRegistry\n"
        "class Failure(Exception): pass\n"
        "class Missing(Failure): pass\n"
        "class Unauthorized(Exception): pass\n"
        "missing = Error(Missing, status=404, code='missing', title='Missing')\n"
        "unauthorized = Error(Unauthorized, status=401, code='unauthorized', "
        "title='Unauthorized')\n"
        "registry = ErrorRegistry(errors=[missing, unauthorized])\n",
        encoding="utf-8",
    )
    helpers = [
        "from typing import Annotated",
        "from fastapi import Depends",
        "from errors import Missing, Unauthorized",
        "def authenticate(): raise Unauthorized()",
        "Identity = Annotated[str, Depends(authenticate)]",
        "def step_0(): raise Missing()",
    ]
    for level in range(1, depth + 1):
        helpers.extend([f"def step_{level}():", *[f"    step_{level - 1}()"] * 2])
    helpers.extend(
        [
            "class Gateway:",
            f"    def execute(self): step_{depth}()",
            "class Service:",
            "    def __init__(self, gateway: Gateway): self._gateway = gateway",
            "    def run(self): self._gateway.execute()",
        ]
    )
    (root / "shared.py").write_text("\n".join(helpers) + "\n", encoding="utf-8")
    for feature in range(features):
        lines = [
            "from dishka import FromDishka",
            "from fastapi_canon import CanonRouter",
            "from errors import registry, missing, unauthorized",
            "from shared import Service, Identity",
            "router = CanonRouter(error_registry=registry, raises=[unauthorized])",
        ]
        for route in range(routes):
            declared = "[]" if route % 5 == 0 else "[missing]"
            lines.extend(
                [
                    f"@router.get('/{feature}/{route}', raises={declared})",
                    f"def endpoint_{route}("
                    "service: FromDishka[Service], identity: Identity):",
                    "    return service.run()",
                ]
            )
        (root / f"feature_{feature}.py").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )


def _measure(root: Path, rounds: int, profile: Path | None) -> dict:
    samples: dict[str, list[float]] = {
        "index_ms": [],
        "analysis_ms": [],
        "report_ms": [],
        "total_ms": [],
        "cli_ms": [],
    }
    fingerprints = set()
    for _ in range(rounds):
        started = perf_counter()
        source = _Source.read([root])
        indexed = perf_counter()
        report = _Checker(source).check()
        analyzed = perf_counter()
        payload = report.as_dict()
        for operation in payload["operations"]:
            operation["path"] = str(Path(operation["path"]).relative_to(root))
            for diagnostic in operation["diagnostics"]:
                diagnostic["path"] = str(Path(diagnostic["path"]).relative_to(root))
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        reported = perf_counter()
        fingerprints.add(fingerprint)
        samples["index_ms"].append((indexed - started) * 1000)
        samples["analysis_ms"].append((analyzed - indexed) * 1000)
        samples["report_ms"].append((reported - analyzed) * 1000)
        samples["total_ms"].append((reported - started) * 1000)
        started = perf_counter()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "fastapi_canon.check_error_contracts",
                str(root),
                "--strict",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != int(bool(report.gaps)):
            raise RuntimeError(result.stderr)
        samples["cli_ms"].append((perf_counter() - started) * 1000)
    if len(fingerprints) != 1:
        raise RuntimeError("non-deterministic analysis results")
    if profile:
        profiler = cProfile.Profile()
        profiler.runcall(lambda: _Checker(_Source.read([root])).check())
        profiler.dump_stats(profile)
    return {
        "files": len(source.paths),
        "operations": len(report.operations),
        "contract_gaps": report.gaps,
        "incomplete_operations": report.incomplete,
        "fingerprint": fingerprints.pop(),
        "median_ms": {
            name: statistics.median(values) for name, values in samples.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=int, default=100)
    parser.add_argument("--routes-per-feature", type=int, default=10)
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    if min(args.features, args.routes_per_feature, args.rounds) < 1 or args.depth < 0:
        parser.error(
            "features, routes and rounds must be positive; depth must be nonnegative"
        )
    (ROOT / "dist").mkdir(exist_ok=True)
    results = {
        "python": sys.version,
        "platform": platform.platform(),
        "rounds": args.rounds,
        "features": args.features,
        "routes_per_feature": args.routes_per_feature,
        "depth": args.depth,
        "scenarios": {
            "reference": _measure(
                ROOT / "examples/reference-service/src", args.rounds, None
            )
        },
    }
    with tempfile.TemporaryDirectory(
        prefix="canon-check-benchmark-", dir=ROOT / "dist"
    ) as temporary:
        root = Path(temporary)
        _project(root, args.features, args.routes_per_feature, args.depth)
        results["scenarios"]["synthetic"] = _measure(root, args.rounds, args.profile)
    if args.compare:
        before = json.loads(args.compare.read_text("utf-8"))
        for name, scenario in results["scenarios"].items():
            if scenario["fingerprint"] != before["scenarios"][name]["fingerprint"]:
                raise RuntimeError(f"analysis results changed for {name}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
