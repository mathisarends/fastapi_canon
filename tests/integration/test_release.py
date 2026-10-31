import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "canon_release", Path(__file__).resolve().parents[2] / "scripts/release.py"
)
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = release
_spec.loader.exec_module(release)


@pytest.fixture
def release_repo(tmp_path: Path) -> Path:
    files = [
        "release.toml",
        "pyproject.toml",
        "uv.lock",
        "examples/reference-service/pyproject.toml",
        "packages/fastapi-canon/docs/streams.md",
        "packages/fastapi-canon/examples/streams/README.md",
        "packages/fastapi-streams/README.md",
        "packages/fastapi-canon/fastapi_canon/_version.py",
        "packages/fastapi-canon/fastapi_canon/__init__.py",
    ]
    for package in release.packages().values():
        files.extend(
            [
                f"{package.directory}/pyproject.toml",
                f"{package.directory}/CHANGELOG.md",
            ]
        )
        if package.name != "fastapi-canon":
            files.append(f"{package.directory}/{package.module}/__init__.py")
    for name in files:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(release.ROOT / name, destination)
    for arguments in [
        ["init", "--initial-branch=main"],
        ["add", "."],
        [
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release@example.test",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-m",
            "Prepared sources",
        ],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
    ]:
        subprocess.run(
            ["git", *arguments], cwd=tmp_path, check=True, capture_output=True
        )
    return tmp_path


def test_release_rejects_stale_installation_docs(release_repo: Path) -> None:
    path = release_repo / "packages/fastapi-canon/docs/streams.md"
    path.write_text("uv add fastapi-streams==0.0.1", encoding="utf-8")
    with pytest.raises(ValueError, match="install fastapi-canon extras"):
        release.validate_release("fastapi-canon", "0.9.0", root=release_repo)


def test_release_notes_resolve_links_against_the_release_commit(
    release_repo: Path,
) -> None:
    package = release.packages(release_repo)["fastapi-canon"]
    commit = "a" * 40
    path = release_repo / package.directory / "CHANGELOG.md"
    original = path.read_bytes()
    notes = release._release_notes(release_repo, package, "0.5.4", commit)
    assert (
        f"https://github.com/mathisarends/fastapi_canon/blob/{commit}/"
        "packages/fastapi-streams/CHANGELOG.md"
    ) in notes
    assert "../fastapi-streams" not in notes
    assert path.read_bytes() == original


def test_release_notes_retain_external_links_titles_and_fragments(
    release_repo: Path,
) -> None:
    package = release.packages(release_repo)["fastapi-canon"]
    path = release_repo / package.directory / "CHANGELOG.md"
    path.write_text(
        "## 0.4.0 - 2026-10-18\n\n"
        '[Guide](docs/codegen.md#validation "Read this")\n'
        "[Evidence](../../docs/releases.md#verified)\n"
        "[Section](#upgrade)\n"
        "[External](https://example.test/docs#part)\n"
        "[Email](mailto:maintainer@example.test)\n",
        encoding="utf-8",
    )
    commit = "b" * 40
    notes = release._release_notes(release_repo, package, "0.4.0", commit)
    base = f"https://github.com/mathisarends/fastapi_canon/blob/{commit}"
    assert (
        f'{base}/packages/fastapi-canon/docs/codegen.md#validation "Read this"' in notes
    )
    assert f"{base}/docs/releases.md#verified" in notes
    assert f"{base}/packages/fastapi-canon/CHANGELOG.md#upgrade" in notes
    assert "[External](https://example.test/docs#part)" in notes
    assert "[Email](mailto:maintainer@example.test)" in notes


@pytest.mark.parametrize(
    "heading", ["Breaking changes", "Breaking generated-code changes"]
)
@pytest.mark.parametrize(
    ("version", "previous", "accepted"),
    [
        ("0.1.1", "0.1.0", False),
        ("0.1.2", "0.1.1", False),
        ("0.2.0", "0.1.0", True),
        ("0.2.0", "0.1.1", True),
        ("0.2.1", "0.1.0", False),
        ("0.3.0", "0.1.0", False),
        ("0.2.0rc1", "0.1.0", True),
    ],
)
def test_breaking_pre_one_releases_require_next_minor(
    tmp_path: Path, heading: str, version: str, previous: str, accepted: bool
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        f"# Changelog\n\n## {version} - unreleased\n\n### {heading}\n\n"
        f"- Changed inputs.\n\n## {previous} - 2026-10-17\n\n- Previous release.\n",
        encoding="utf-8",
    )
    section = release._changelog(changelog, version)
    if accepted:
        release._check_breaking_version(changelog, version, section)
    else:
        with pytest.raises(ValueError, match="next minor version with patch zero"):
            release._check_breaking_version(changelog, version, section)


@pytest.mark.parametrize(("name", "version"), [("fastapi-canon", "0.9.0")])
def test_release_validates_sources_without_importing_business_modules(
    release_repo: Path, name: str, version: str
) -> None:
    package = release.packages(release_repo)[name]
    module = release_repo / package.directory / package.module / "__init__.py"
    module.write_text(
        module.read_text("utf-8") + '\nraise RuntimeError("do not execute")\n',
        encoding="utf-8",
    )
    result = release.validate_release(name, version, root=release_repo)
    assert result["tag"] == f"{name}-v{version}"
    assert len(result["commit"]) == 40


@pytest.mark.parametrize(
    "version",
    ["0.1", "v0.1.0", "00.1.0", "0.1.0rc01", "0.1.0+local", "0.1.0;echo unsafe"],
)
def test_release_rejects_noncanonical_versions(
    release_repo: Path, version: str
) -> None:
    with pytest.raises(ValueError, match="canonical prerelease"):
        release.validate_release("fastapi-canon", version, root=release_repo)


@pytest.mark.parametrize(
    "name",
    [
        "canon-workspace",
        "fastapi-canon-reference",
        "arbitrary",
        "fastapi-streams",
        "fastapi-rpckit",
        "fastapi-webrtc",
    ],
)
def test_release_excludes_nonlibrary_targets(release_repo: Path, name: str) -> None:
    with pytest.raises(ValueError, match="Only the workspace library"):
        release.validate_release(name, "0.1.0", root=release_repo)


@pytest.mark.parametrize(
    ("path", "before", "after", "message"),
    [
        ("uv.lock", 'version = "0.9.0"', 'version = "0.4.0"', "uv.lock"),
        (
            "packages/fastapi-canon/fastapi_canon/_version.py",
            '__version__ = "0.9.0"',
            '__version__ = "9.9.9"',
            "__version__",
        ),
        ("pyproject.toml", "==0.9.0", "==0.0.1", "Dependency pin"),
        (
            "examples/reference-service/pyproject.toml",
            "fastapi-canon>=0.9,<0.10",
            "fastapi-canon>=0.4,<0.5",
            "Reference service",
        ),
        ("packages/fastapi-canon/CHANGELOG.md", "## 0.9.0", "## 9.9.9", "Changelog"),
    ],
)
def test_release_rejects_inconsistent_metadata(
    release_repo: Path, path: str, before: str, after: str, message: str
) -> None:
    target = release_repo / path
    target.write_text(
        target.read_text("utf-8").replace(before, after), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=message):
        release.validate_release("fastapi-canon", "0.9.0", root=release_repo)


def test_public_canon_verification_checks_each_optional_integration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release, "publication_state", lambda report: "complete")
    commands: list[list[str]] = []
    monkeypatch.setattr(
        release, "_run", lambda command, **kwargs: commands.append(command)
    )
    release.verify_published({"package": "fastapi-canon", "version": "0.6.0"})
    installs = [
        command for command in commands if command[:3] == ["uv", "pip", "install"]
    ]
    assert len(installs) == 6
    assert any("fastapi-canon[otel]==0.6.0" in command for command in installs)
    assert any("fastapi-canon[websockets]==0.6.0" in command for command in installs)
    assert any(
        "fastapi-canon[sse,websockets,webrtc,aiortc,codegen,otel]==0.6.0" in command
        for command in installs
    )
    smokes = [command for command in commands if "-I" in command]
    assert [command[3:] for command in smokes] == [
        ["fastapi-canon"],
        ["fastapi-canon", "--otel"],
        ["fastapi-canon", "sse"],
        ["fastapi-canon", "websockets"],
        ["fastapi-canon", "webrtc"],
        ["fastapi-canon", "sse", "webrtc", "websockets", "--otel"],
    ]


def test_publication_requires_final_changelog(release_repo: Path) -> None:
    changelog = release_repo / "packages/fastapi-canon/CHANGELOG.md"
    lines = changelog.read_text("utf-8").splitlines()
    heading = next(
        index for index, line in enumerate(lines) if line.startswith("## 0.9.0")
    )
    lines[heading] = "## 0.9.0 (unreleased)"
    changelog.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Finalize the changelog"):
        release.validate_release(
            "fastapi-canon", "0.9.0", root=release_repo, publish=True
        )


def test_publication_accepts_final_changelog(release_repo: Path) -> None:
    path = release_repo / "packages/fastapi-canon/CHANGELOG.md"
    path.write_text(
        path.read_text("utf-8").replace(
            "## 0.9.0 - unreleased", "## 0.9.0 - 2026-10-30"
        ),
        encoding="utf-8",
    )
    report = release.validate_release(
        "fastapi-canon", "0.9.0", root=release_repo, publish=True
    )
    assert report["version"] == "0.9.0"


def test_github_release_rejects_former_repository(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mathisarends/canon")
    monkeypatch.setattr(
        release, "_gh", lambda endpoint: pytest.fail("Do not contact the former home")
    )
    with pytest.raises(ValueError, match="wrong repository"):
        release.validate_release(
            "fastapi-canon", "0.9.0", root=release_repo, github=True
        )


def test_github_release_requires_successful_ci_for_exact_commit(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mathisarends/fastapi_canon")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setattr(release, "_gh", lambda endpoint: {"workflow_runs": []})
    with pytest.raises(ValueError, match="exact release commit"):
        release.validate_release(
            "fastapi-canon", "0.9.0", root=release_repo, github=True
        )


def test_github_release_rejects_existing_pypi_version(
    release_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", "mathisarends/fastapi_canon")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")

    def github(endpoint: str, **kwargs):
        return (
            {
                "workflow_runs": [
                    {
                        "id": 1,
                        "conclusion": "success",
                        "html_url": "https://example.test/ci",
                        "head_sha": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], cwd=release_repo, text=True
                        ).strip(),
                        "head_branch": "main",
                        "event": "push",
                    }
                ]
            }
            if "actions/workflows" in endpoint
            else None
        )

    monkeypatch.setattr(release, "_gh", github)
    monkeypatch.setattr(release, "_pypi", lambda name, version: {})
    with pytest.raises(ValueError, match="already exists on PyPI"):
        release.validate_release(
            "fastapi-canon", "0.9.0", root=release_repo, github=True
        )


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        (None, "absent"),
        ({"wheel.whl": "a"}, "partial"),
        ({"wheel.whl": "a", "source.tar.gz": "b"}, "complete"),
    ],
)
def test_publication_resume_classifies_only_matching_original_artifacts(
    monkeypatch: pytest.MonkeyPatch, published: dict | None, expected: str
) -> None:
    document = (
        None
        if published is None
        else {
            "urls": [
                {"filename": filename, "digests": {"sha256": digest}}
                for filename, digest in published.items()
            ]
        }
    )
    monkeypatch.setattr(release, "_pypi", lambda name, version: document)
    report = {
        "package": "fastapi-rpckit",
        "version": "0.1.0",
        "artifacts": {"wheel.whl": "a", "source.tar.gz": "b"},
    }
    assert release.publication_state(report) == expected


def test_publication_resume_rejects_different_pypi_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release,
        "_pypi",
        lambda name, version: {
            "urls": [{"filename": "wheel.whl", "digests": {"sha256": "unexpected"}}]
        },
    )
    with pytest.raises(ValueError, match="different artifact hashes"):
        release.publication_state(
            {
                "package": "fastapi-rpckit",
                "version": "0.1.0",
                "artifacts": {"wheel.whl": "expected"},
            }
        )


def test_release_workflow_keeps_dry_run_without_publish_permissions() -> None:
    source = (release.ROOT / ".github/workflows/release.yml").read_text("utf-8")
    prepare = source.split("  prepare:", 1)[1].split("  publish:", 1)[0]
    assert "id-token: write" not in prepare
    assert "contents: write" not in prepare
    assert "default: true" in source
    assert "if: ${{ !inputs.dry_run }}" in source
    assert "environment:\n      name: pypi" in source


def test_artifact_validation_rejects_unrelated_uploads(tmp_path: Path) -> None:
    (tmp_path / "unrelated.whl").write_bytes(b"not a release")
    with pytest.raises(ValueError, match="must contain only"):
        release.artifact_hashes("fastapi-canon", "0.2.0", tmp_path)
