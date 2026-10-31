import argparse
import json
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    item["name"]: (Path(item["directory"]).name, item["module"])
    for item in tomllib.loads((ROOT / "release.toml").read_text("utf-8"))["package"]
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify independently installed wheels"
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument(
        "--dependencies", choices=("latest", "minimum"), default="latest"
    )
    args = parser.parse_args()
    wheels = {}
    projects = {}
    results = []
    for name, (directory, module) in PACKAGES.items():
        output = ROOT / "dist" / name
        project = tomllib.loads(
            (ROOT / "packages" / directory / "pyproject.toml").read_text("utf-8")
        )["project"]
        projects[name] = project
        if args.artifacts and list(
            args.artifacts.glob(f"{name.replace('-', '_')}-{project['version']}-*.whl")
        ):
            output = args.artifacts.resolve()
        else:
            _run("uv", "build", "--package", name, "--out-dir", str(output))
        (wheel,) = output.glob(f"{name.replace('-', '_')}-{project['version']}-*.whl")
        with zipfile.ZipFile(wheel) as artifact:
            assert f"{module}/py.typed" in artifact.namelist()
            assert not any(path.endswith("uv.lock") for path in artifact.namelist())
        (sdist,) = output.glob(f"{name.replace('-', '_')}-{project['version']}.tar.gz")
        _run(sys.executable, "-m", "twine", "check", "--strict", str(wheel), str(sdist))
        wheels[name] = str(wheel)

    combinations = tuple(
        ("fastapi-canon" + (f"[{extras}]" if extras else ""),)
        for extras in (
            "",
            "otel",
            "sse",
            "websockets",
            "webrtc",
            "webrtc,aiortc",
            "codegen",
            "sse,codegen",
            "websockets,codegen",
            "sse,websockets,codegen",
            "sse,websockets,webrtc,aiortc,codegen,otel",
        )
    )
    for requirements in combinations:
        with tempfile.TemporaryDirectory(prefix="canon-artifacts-") as temporary:
            environment = Path(temporary) / "venv"
            _run("uv", "venv", "--python", args.python, str(environment))
            python = environment / (
                "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
            )
            artifacts = []
            for requirement in requirements:
                name, _, extras = requirement.partition("[")
                artifacts.append(wheels[name] + (f"[{extras}" if extras else ""))
            constraints = (
                _minimum_dependencies(requirements, projects)
                if args.dependencies == "minimum"
                else []
            )
            _run(
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                *(["--resolution", "lowest-direct"] if constraints else []),
                *artifacts,
                *constraints,
                "httpx>=0.28",
            )
            _run("uv", "pip", "check", "--python", str(python))
            extras = Requirement(requirements[0]).extras
            names = ["fastapi-canon", *sorted(extras & {"sse", "websockets", "webrtc"})]
            _run(
                str(python),
                "-I",
                str(ROOT / "scripts/smoke_installed.py"),
                *names,
                *(
                    ["--otel"]
                    if any(
                        "otel" in Requirement(item).extras
                        and Requirement(item).name == "fastapi-canon"
                        for item in requirements
                    )
                    else []
                ),
                cwd=Path(temporary),
                timeout=120,
            )
            installed = subprocess.check_output(
                ["uv", "pip", "list", "--python", str(python), "--format", "json"],
                text=True,
            )
            results.append(
                {"requirements": requirements, "installed": json.loads(installed)}
            )
    if args.dependencies == "latest":
        _check_reference(wheels["fastapi-canon"], args.python)
    report = ROOT / "dist" / f"compatibility-{args.dependencies}.json"
    report.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    print(f"Compatibility results: {report}", flush=True)


def _minimum_dependencies(requirements: tuple[str, ...], projects: dict) -> list[str]:
    lower_bounds: dict[str, Version] = {}
    dependencies = []
    for selected in requirements:
        requirement = Requirement(selected)
        project = projects[requirement.name]
        dependencies.extend(project["dependencies"])
        for extra in requirement.extras:
            dependencies.extend(project["optional-dependencies"][extra])
    parsed = [Requirement(dependency) for dependency in dependencies]
    for dependency in parsed:
        if dependency.name in PACKAGES:
            continue  # Local wheels are already explicit install requirements.
        if dependency.marker is not None:
            raise ValueError(
                f"Minimum matrix needs explicit marker handling: {dependency}"
            )
        bounds = [
            Version(specifier.version)
            for specifier in dependency.specifier
            if specifier.operator in (">=", "==")
        ]
        if not bounds:
            raise ValueError(f"No inclusive minimum for {dependency}")
        lower_bounds[dependency.name] = max(
            *bounds, lower_bounds.get(dependency.name, Version("0"))
        )
    for dependency in parsed:
        if (
            dependency.name in lower_bounds
            and lower_bounds[dependency.name] not in dependency.specifier
        ):
            raise ValueError(f"Minimum bounds conflict with {dependency}")
    return [f"{name}>={version}" for name, version in sorted(lower_bounds.items())]


def _check_reference(canon_wheel: str, python_version: str) -> None:
    reference = ROOT / "examples/reference-service"
    project = tomllib.loads((reference / "pyproject.toml").read_text("utf-8"))[
        "project"
    ]
    output = ROOT / "dist/reference-service"
    _run("uv", "build", "--package", project["name"], "--out-dir", str(output))
    (wheel,) = output.glob(f"fastapi_canon_reference-{project['version']}-*.whl")
    with tempfile.TemporaryDirectory(prefix="canon-reference-") as temporary:
        environment = Path(temporary) / "venv"
        _run("uv", "venv", "--python", python_version, str(environment))
        python = environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        _run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            canon_wheel,
            str(wheel),
            "httpx>=0.28",
            "pytest>=8.3",
            "pytest-asyncio>=1",
            "httpx2>=2.12,<3",
        )
        _run("uv", "pip", "check", "--python", str(python))
        _run(
            str(python),
            "-I",
            "-c",
            "import app, fastapi_canon, importlib.util, sys; from pathlib import Path; "
            "assert Path(app.__file__).resolve().is_relative_to(Path(sys.prefix)); "
            "assert Path(fastapi_canon.__file__).resolve()"
            ".is_relative_to(Path(sys.prefix)); "
            "assert importlib.util.find_spec('fastapi_canon.integrations.sse') "
            "is not None; "
            "assert importlib.util.find_spec('fastapi_canon.integrations.websockets') "
            "is not None",
            cwd=Path(temporary),
        )

        _run(
            str(python),
            "-I",
            "-m",
            "pytest",
            "-q",
            "-c",
            str(reference / "pyproject.toml"),
            str(reference / "tests"),
            cwd=Path(temporary),
        )
        _run(
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            canon_wheel + "[otel]",
            str(wheel) + "[otel]",
        )
        _run("uv", "pip", "check", "--python", str(python))
        _run(
            str(python),
            "-I",
            "-m",
            "pytest",
            "-q",
            "-c",
            str(reference / "pyproject.toml"),
            str(reference / "tests/test_telemetry.py"),
            str(reference / "tests/test_telemetry_demo.py"),
            cwd=Path(temporary),
        )


def _run(*command: str, cwd: Path = ROOT, timeout: float | None = None) -> None:
    print(" ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True, timeout=timeout)


if __name__ == "__main__":
    main()
