import argparse
import ast
import configparser
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]
ALLOWED = frozenset({"fastapi-canon"})
NUMBER = r"(?:0|[1-9][0-9]*)"
VERSION = re.compile(rf"{NUMBER}\.{NUMBER}\.{NUMBER}(?:(?:a|b|rc){NUMBER})?")
REPORT = Path("dist/release-report/release.json")


@dataclass(frozen=True)
class Package:
    name: str
    directory: str
    module: str
    tag_prefix: str


def packages(root: Path = ROOT) -> dict[str, Package]:
    configuration = _toml(root / "release.toml")
    selected = {entry["name"]: Package(**entry) for entry in configuration["package"]}
    if set(selected) != ALLOWED or len(configuration["package"]) != len(ALLOWED):
        raise ValueError(
            "Only the workspace library distributions may be release targets"
        )
    for name, package in selected.items():
        if package.directory != f"packages/{name}":
            raise ValueError(f"Invalid package directory for {name}")
        if package.module != name.replace("-", "_"):
            raise ValueError(f"Invalid import module for {name}")
        if package.tag_prefix != f"{name}-v":
            raise ValueError(f"Invalid tag prefix for {name}")
    return selected


def validate_release(
    name: str,
    version: str,
    *,
    root: Path = ROOT,
    publish: bool = False,
    github: bool = False,
) -> dict[str, Any]:
    package = _selection(name, version, root)
    documentation = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_documentation.py"),
            "--root",
            str(root),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if documentation.returncode:
        raise ValueError(documentation.stdout + documentation.stderr)
    projects = {
        key: _toml(root / value.directory / "pyproject.toml")["project"]
        for key, value in packages(root).items()
    }
    if projects[name]["name"] != name or projects[name]["version"] != version:
        raise ValueError("Release version must match project.name and project.version")
    lock = _toml(root / "uv.lock")
    for key, item in packages(root).items():
        expected = projects[key]["version"]
        locked = [p["version"] for p in lock["package"] if p["name"] == key]
        if locked != [expected]:
            raise ValueError(f"uv.lock does not match {key}; run uv lock")
        _check_constant(root / item.directory / item.module / "_version.py", expected)
    root_dependencies = _toml(root / "pyproject.toml")["project"]["dependencies"]
    for key in projects:
        _check_pin(root_dependencies, key, projects[key]["version"])
    reference = _toml(root / "examples/reference-service/pyproject.toml")
    reference_canon = next(
        Requirement(value)
        for value in reference["project"]["dependencies"]
        if Requirement(value).name == "fastapi-canon"
    )
    if projects["fastapi-canon"]["version"] not in reference_canon.specifier:
        raise ValueError("Reference service excludes the prepared Canon version")
    changelog = root / package.directory / "CHANGELOG.md"
    section = _changelog(changelog, version)
    _check_breaking_version(changelog, version, section)
    if publish and not re.match(
        rf"## {re.escape(version)} - \d{{4}}-\d{{2}}-\d{{2}}$", section.splitlines()[0]
    ):
        raise ValueError("Finalize the changelog date and commit it before publication")
    commit = _git(root, "rev-parse", "HEAD")
    report: dict[str, Any] = {
        "package": name,
        "version": version,
        "commit": commit,
        "tag": package.tag_prefix + version,
        "pypi_url": f"https://pypi.org/project/{name}/{version}/",
        "checks": {"metadata": "passed"},
    }
    if github:
        if os.environ.get("GITHUB_REF") != "refs/heads/main":
            raise ValueError("Dispatch the release workflow from main")
        repository = _toml(root / "release.toml")["repository"]
        if os.environ.get("GITHUB_REPOSITORY") != repository:
            raise ValueError("Release workflow is running in the wrong repository")
        _git(root, "merge-base", "--is-ancestor", commit, "refs/remotes/origin/main")
        if _git(root, "status", "--porcelain", "--untracked-files=no"):
            raise ValueError("Release checkout contains tracked changes")
        runs = _gh(
            f"repos/{repository}/actions/workflows/ci.yml/runs"
            f"?head_sha={commit}&branch=main&event=push&per_page=100"
        )["workflow_runs"]
        runs = [
            run
            for run in runs
            if run.get("head_sha") == commit
            and run.get("head_branch") == "main"
            and run.get("event") == "push"
        ]
        if not runs or max(runs, key=lambda run: run["id"])["conclusion"] != "success":
            raise ValueError("The exact release commit must have passed ci.yml on main")
        ci = max(runs, key=lambda run: run["id"])
        report["checks"]["ci"] = ci["html_url"]
        _check_remote_tag(repository, report["tag"], commit)
        if _pypi(name, version) is not None:
            raise ValueError(
                "Version already exists on PyPI; use original run artifacts to resume"
            )
        report["workflow_url"] = (
            f"https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    return report


def artifact_hashes(
    name: str, version: str, directory: Path, root: Path = ROOT
) -> dict[str, str]:
    package = _selection(name, version, root)
    stem = f"{name.replace('-', '_')}-{version}"
    expected = {f"{stem}-py3-none-any.whl", f"{stem}.tar.gz"}
    actual = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and path.name != ".gitignore"
    }
    if actual != expected:
        raise ValueError(f"Artifact directory must contain only {sorted(expected)}")
    wheel = directory / f"{stem}-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        paths = archive.namelist()
        metadata = archive.read(f"{stem}.dist-info/METADATA")
        _check_artifact_metadata(metadata, name, version)
        if f"{package.module}/py.typed" not in paths:
            raise ValueError("Wheel is missing py.typed")
        for transport in ("sse", "websockets", "webrtc"):
            if not any(
                path.startswith(
                    f"{package.module}/integrations/{transport}/codegen/templates/"
                )
                for path in paths
            ):
                raise ValueError(f"Wheel is missing {transport} codegen templates")
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(f"{stem}.dist-info/entry_points.txt").decode())
        if entries["console_scripts"]["canon"] != "fastapi_canon.cli:main":
            raise ValueError("Wheel CLI entry point does not match Canon")
    with tarfile.open(directory / f"{stem}.tar.gz") as archive:
        member = archive.extractfile(f"{stem}/PKG-INFO")
        if member is None:
            raise ValueError("Sdist is missing PKG-INFO")
        _check_artifact_metadata(member.read(), name, version)
        if f"{stem}/{package.module}/py.typed" not in archive.getnames():
            raise ValueError("Sdist is missing py.typed")
    return {
        filename: hashlib.sha256((directory / filename).read_bytes()).hexdigest()
        for filename in sorted(expected)
    }


def publication_state(report: dict[str, Any]) -> str:
    document = _pypi(report["package"], report["version"])
    if document is None:
        return "absent"
    published = {
        item["filename"]: item["digests"]["sha256"] for item in document["urls"]
    }
    expected = report["artifacts"]
    if not published or any(
        expected.get(name) != digest for name, digest in published.items()
    ):
        raise ValueError(
            "PyPI already contains unexpected files or different artifact hashes"
        )
    return "complete" if published == expected else "partial"


def verify_published(report: dict[str, Any], root: Path = ROOT) -> None:
    for attempt in range(12):
        if publication_state(report) == "complete":
            break
        if attempt == 11:
            raise ValueError("PyPI does not yet expose all expected artifact hashes")
        time.sleep(5)
    combinations = [
        (f"fastapi-canon{extras}=={report['version']}",)
        for extras in (
            "",
            "[otel]",
            "[sse]",
            "[websockets]",
            "[webrtc]",
            "[sse,websockets,webrtc,aiortc,codegen,otel]",
        )
    ]
    for requirements in combinations:
        with tempfile.TemporaryDirectory(prefix="canon-pypi-") as temporary:
            environment = Path(temporary) / "venv"
            _run(["uv", "venv", "--python", sys.executable, str(environment)])
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            _run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    *requirements,
                    "httpx>=0.28",
                ]
            )
            _run(["uv", "pip", "check", "--python", str(python)])
            selected = [report["package"]]
            selected.extend(
                sorted(
                    Requirement(requirements[0]).extras
                    & {"sse", "websockets", "webrtc"}
                )
            )
            if "otel" in Requirement(requirements[0]).extras:
                selected.append("--otel")
            _run(
                [
                    str(python),
                    "-I",
                    str(root / "scripts/smoke_installed.py"),
                    *selected,
                ],
                cwd=Path(temporary),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare and verify Canon releases")
    parser.add_argument(
        "command", choices=("validate", "artifacts", "state", "verify-pypi", "finish")
    )
    parser.add_argument("--package", required=True, choices=sorted(ALLOWED))
    parser.add_argument("--version", required=True)
    parser.add_argument("--github", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--directory", type=Path, default=ROOT / "dist/release")
    parser.add_argument("--report", type=Path, default=ROOT / REPORT)
    args = parser.parse_args()
    try:
        _selection(args.package, args.version, ROOT)
        if args.command == "validate":
            report = validate_release(
                args.package, args.version, publish=args.publish, github=args.github
            )
            args.report.parent.mkdir(parents=True, exist_ok=True)
            _write_json(args.report, report)
            notes = _release_notes(
                ROOT, packages()[args.package], args.version, report["commit"]
            )
            (args.report.parent / "release-notes.md").write_text(
                notes + "\n", encoding="utf-8"
            )
            if output := os.environ.get("GITHUB_OUTPUT"):
                with Path(output).open("a", encoding="utf-8") as handle:
                    handle.write(f"tag={report['tag']}\ncommit={report['commit']}\n")
        else:
            report = json.loads(args.report.read_text("utf-8"))
            if (
                report["package"],
                report["version"],
                report["commit"],
                report["tag"],
            ) != (
                args.package,
                args.version,
                _git(ROOT, "rev-parse", "HEAD"),
                packages()[args.package].tag_prefix + args.version,
            ):
                raise ValueError(
                    "Release manifest does not match the requested "
                    "package/version/commit/tag"
                )
            hashes = artifact_hashes(args.package, args.version, args.directory)
            if args.command == "artifacts":
                report["artifacts"] = hashes
                report["checks"]["artifacts"] = "passed"
                _write_json(args.report, report)
            elif report.get("artifacts") != hashes:
                raise ValueError("Artifacts differ from the verified release manifest")
            elif args.command == "state":
                state = publication_state(report)
                print(f"PyPI publication state: {state}")
                if output := os.environ.get("GITHUB_OUTPUT"):
                    with Path(output).open("a", encoding="utf-8") as handle:
                        handle.write(f"state={state}\n")
            elif args.command == "finish":
                if publication_state(report) != "complete":
                    raise ValueError(
                        "Verify the complete PyPI release before finalizing"
                    )
                _finish(report, args.directory, args.report)
            else:
                verify_published(report)
        print(json.dumps(report, indent=2))
        if (summary := os.environ.get("GITHUB_STEP_SUMMARY")) and args.command in {
            "artifacts",
            "finish",
        }:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(f"### {report['package']} {report['version']}\n\n")
                handle.write(
                    f"Commit: `{report['commit']}`\n\nTag: `{report['tag']}`\n\n"
                )
                for filename, digest in report.get("artifacts", {}).items():
                    handle.write(f"- `{filename}`: SHA-256 `{digest}`\n")
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Release check failed: {error}\n")


def _selection(name: str, version: str, root: Path) -> Package:
    if name not in ALLOWED:
        raise ValueError("Only the workspace library distributions may be released")
    if VERSION.fullmatch(version) is None:
        raise ValueError("Use X.Y.Z or a canonical prerelease such as X.Y.Zrc1")
    return packages(root)[name]


def _check_constant(path: Path, expected: str) -> None:
    tree = ast.parse(path.read_text("utf-8"))
    versions = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in node.targets
        )
    ]
    if versions != [expected]:
        raise ValueError(f"{path.name} __version__ does not match project.version")


def _check_pin(dependencies: list[str], name: str, version: str) -> None:
    found = [
        Requirement(value) for value in dependencies if Requirement(value).name == name
    ]
    if len(found) != 1 or str(found[0].specifier) != f"=={version}":
        raise ValueError(f"Dependency pin must be {name}=={version}")


def _changelog(path: Path, version: str) -> str:
    text = path.read_text("utf-8")
    matches = list(
        re.finditer(rf"^## {re.escape(version)}(?:\s[^\n]*)?$", text, re.MULTILINE)
    )
    if len(matches) != 1:
        raise ValueError(f"Changelog must contain exactly one section for {version}")
    tail = text[matches[0].start() :]
    return re.split(r"\n## ", tail, maxsplit=1)[0].strip()


def _release_notes(root: Path, package: Package, version: str, commit: str) -> str:
    section = _changelog(root / package.directory / "CHANGELOG.md", version)
    repository = _toml(root / "release.toml")["repository"]
    base = (
        f"https://github.com/{repository}/blob/{commit}/"
        f"{package.directory}/CHANGELOG.md"
    )
    # Changelog links use inline Markdown destinations. Retain titles, fragments
    # and absolute URLs; resolve relative paths from the original document.
    return re.sub(
        r"(?P<prefix>\[[^\]\n]*\]\(<?)(?P<url>[^\s<>)]*)(?P<suffix>>?(?:\s+\"[^\"]*\")?\))",
        lambda match: (
            match["prefix"] + urllib.parse.urljoin(base, match["url"]) + match["suffix"]
        ),
        section,
    )


def _check_breaking_version(path: Path, version: str, section: str) -> None:
    target = Version(version)
    if target.major != 0 or not re.search(
        r"^### Breaking\b", section, re.MULTILINE | re.IGNORECASE
    ):
        return
    headings = re.findall(
        rf"^## ({VERSION.pattern})(?:\s[^\n]*)?$",
        path.read_text("utf-8"),
        re.MULTILINE,
    )
    previous = next(
        (
            Version(value)
            for value in headings[headings.index(version) + 1 :]
            if not Version(value).is_prerelease
        ),
        None,
    )
    if target.micro != 0 or (
        previous is not None and target.minor != previous.minor + 1
    ):
        raise ValueError(
            "Breaking changes before 1.0 require the next minor version with "
            "patch zero: 0.x.y -> 0.(x+1).0"
        )


def _check_artifact_metadata(content: bytes, name: str, version: str) -> None:
    metadata = BytesParser().parsebytes(content)
    if metadata["Name"] != name or metadata["Version"] != version:
        raise ValueError("Artifact metadata does not match the release target")
    if name != "fastapi-canon":
        forbidden = ALLOWED - {name}
        if any(
            Requirement(value).name in forbidden
            for value in metadata.get_all("Requires-Dist", [])
        ):
            raise ValueError("Extensions must not depend on Canon or each other")


def _pypi(name: str, version: str) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{version}/json", timeout=30
        ) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _check_remote_tag(repository: str, tag: str, commit: str) -> None:
    ref = _gh(f"repos/{repository}/git/ref/tags/{tag}", missing_ok=True)
    if ref is None:
        return
    target = ref["object"]
    while target["type"] == "tag":
        target = _gh(f"repos/{repository}/git/tags/{target['sha']}")["object"]
    if target["type"] != "commit" or target["sha"] != commit:
        raise ValueError("Release tag already points to a different commit")


def _finish(report: dict[str, Any], directory: Path, manifest: Path) -> None:
    repository = _toml(ROOT / "release.toml")["repository"]
    _check_remote_tag(repository, report["tag"], report["commit"])
    if (
        _gh(f"repos/{repository}/releases/tags/{report['tag']}", missing_ok=True)
        is not None
    ):
        print("Matching GitHub release already exists")
        return
    command = [
        "gh",
        "release",
        "create",
        report["tag"],
        "--repo",
        repository,
        "--target",
        report["commit"],
        "--title",
        f"{report['package']} {report['version']}",
        "--notes-file",
        str(manifest.parent / "release-notes.md"),
    ]
    if any(character.isalpha() for character in report["version"]):
        command.append("--prerelease")
    command.extend(str(directory / filename) for filename in report["artifacts"])
    command.append(str(manifest))
    _run(command)


def _gh(endpoint: str, *, missing_ok: bool = False) -> Any:
    result = subprocess.run(
        ["gh", "api", endpoint], capture_output=True, text=True, check=False
    )
    if result.returncode:
        if missing_ok and "HTTP 404" in result.stderr:
            return None
        raise ValueError(f"GitHub API failed for {endpoint}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.check_output(["git", *arguments], cwd=root, text=True).strip()


def _toml(path: Path) -> dict[str, Any]:
    return tomllib.loads(path.read_text("utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _run(command: list[str], *, cwd: Path = ROOT) -> None:
    subprocess.run(command, cwd=cwd, check=True)


if __name__ == "__main__":
    main()
