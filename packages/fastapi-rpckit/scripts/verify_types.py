import json
import subprocess
import sys
import tomllib
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    minimum = config["tool"]["canon-websockets"]["typecheck"]["min_completeness"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyright",
            "--verifytypes",
            "fastapi_canon.integrations.websockets",
            "--ignoreexternal",
            "--pythonpath",
            sys.executable,
            "--pythonversion",
            "3.12",
            "--pythonplatform",
            "Linux",
            "--outputjson",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout:
        print(result.stderr, file=sys.stderr)
        return result.returncode or 1
    report = json.loads(result.stdout)
    completeness = report["typeCompleteness"]
    score = completeness["completenessScore"]
    counts = completeness["exportedSymbolCounts"]
    print(
        f"Public type completeness: {score:.1%} (minimum {minimum:.1%}); "
        f"{counts['withAmbiguousType']} ambiguous, "
        f"{counts['withUnknownType']} unknown exported symbols"
    )
    if score < minimum:
        for symbol in completeness["symbols"]:
            if symbol["isExported"] and not symbol["isTypeKnown"]:
                print(symbol["name"])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
