import argparse
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_documentation(root: Path = ROOT) -> list[str]:
    """Check active installation examples, leaving historical records intact."""
    errors: list[str] = []
    canon = tomllib.loads(
        (root / "packages/fastapi-canon/pyproject.toml").read_text("utf-8")
    )
    legacy = {"fastapi-streams", "fastapi-rpckit", "fastapi-webrtc"}
    for dependencies in (
        canon["project"]["dependencies"],
        *canon["project"]["optional-dependencies"].values(),
    ):
        if any(re.split(r"[\[<>=]", value)[0] in legacy for value in dependencies):
            errors.append("Canon dependencies must not install legacy distributions")
    for folder in ("docs", "packages", "examples", "guides"):
        for path in (root / folder).rglob("*.md"):
            if path.name == "CHANGELOG.md" or "node_modules" in path.parts:
                continue
            text = path.read_text("utf-8")
            if re.search(
                r"(?:uv add|pip install) [^\n]*fastapi-(?:streams|rpckit|webrtc)", text
            ):
                errors.append(f"{path.relative_to(root)}: install fastapi-canon extras")
            if re.search(r"from fastapi_(?:streams|rpckit|webrtc)\b", text):
                errors.append(f"{path.relative_to(root)}: use fastapi_canon imports")
            if re.search(r"from fastapi_canon\.(?:sse|websockets|webrtc)\b", text):
                errors.append(
                    f"{path.relative_to(root)}: use Canon integration namespaces"
                )
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    failures = check_documentation(parser.parse_args().root)
    for failure in failures:
        print(failure)
    raise SystemExit(bool(failures))
