from __future__ import annotations

import json
import os
from pathlib import Path
import zipfile

import pytest

from foundry_opt.packaging import (
    BundleError,
    BundleRequest,
    SecretSourceFileError,
    UnsafeSourcePathError,
    build_source_bundle,
)


def test_build_source_bundle_is_binary_safe_and_deterministic(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "main.py").write_text("print('hello')\n", encoding="utf-8")
    binary = b"\x00\xff\x10PNG\r\n\x1a\n"
    (repository / "asset.bin").write_bytes(binary)
    nested = repository / "skills"
    nested.mkdir()
    (nested / "skill.txt").write_text("skill", encoding="utf-8")

    first = build_source_bundle(
        BundleRequest(repository, tmp_path / "first.zip")
    )
    os.utime(repository / "main.py", (2_000_000_000, 2_000_000_000))
    second = build_source_bundle(
        BundleRequest(repository, tmp_path / "second.zip")
    )

    assert first.sha256 == second.sha256
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.included_files == ("asset.bin", "main.py", "skills/skill.txt")
    assert first.byte_size == first.path.stat().st_size
    with zipfile.ZipFile(first.path) as archive:
        assert archive.namelist() == list(first.included_files)
        assert archive.read("asset.bin") == binary
        assert {item.date_time for item in archive.infolist()} == {
            (1980, 1, 1, 0, 0, 0)
        }
        assert all("\\" not in item.filename for item in archive.infolist())

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["sha256"] == first.sha256
    assert manifest["included_files"] == list(first.included_files)
    assert "credential" not in json.dumps(manifest).casefold()


def test_build_source_bundle_applies_declared_and_mandatory_exclusions(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "config").write_text("ignored", encoding="utf-8")
    (repository / ".azure").mkdir()
    (repository / ".azure" / "state").write_text("ignored", encoding="utf-8")
    (repository / ".venv").mkdir()
    (repository / ".venv" / "module.py").write_text("ignored", encoding="utf-8")
    (repository / "__pycache__").mkdir()
    (repository / "__pycache__" / "main.pyc").write_bytes(b"ignored")
    (repository / "dist").mkdir()
    (repository / "dist" / "agent.whl").write_bytes(b"ignored")
    (repository / ".foundry-opt").mkdir()
    (repository / ".foundry-opt" / "evidence.json").write_text(
        "ignored", encoding="utf-8"
    )
    (repository / "src").mkdir()
    (repository / "src" / "main.py").write_text("included", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_main.py").write_text(
        "excluded", encoding="utf-8"
    )

    artifact = build_source_bundle(
        BundleRequest(
            repository_root=repository,
            output_path=tmp_path / "bundle.zip",
            include=("src/**", "tests/**"),
            exclude=("tests/**",),
        )
    )

    assert artifact.included_files == ("src/main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons[".git/"] == "mandatory: version-control metadata"
    assert reasons[".azure/"] == "mandatory: Azure local state"
    assert reasons[".venv/"] == "mandatory: virtual environment"
    assert reasons["__pycache__/"] == "mandatory: cache"
    assert reasons["dist/"] == "mandatory: build artifact"
    assert reasons[".foundry-opt/"] == "mandatory: optimizer evidence"
    assert reasons["tests/"] == "declared exclude: tests/**"


def test_build_source_bundle_keeps_nested_env_build_and_dist_source(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    for name in ("env", "build", "dist"):
        root_artifact = repository / name
        root_artifact.mkdir(parents=True)
        (root_artifact / "generated.txt").write_text(
            "excluded",
            encoding="utf-8",
        )
        nested_source = repository / "src" / name
        nested_source.mkdir(parents=True)
        (nested_source / "module.py").write_text(
            f"NAME = {name!r}\n",
            encoding="utf-8",
        )

    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "bundle.zip")
    )

    assert artifact.included_files == (
        "src/build/module.py",
        "src/dist/module.py",
        "src/env/module.py",
    )
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["build/"] == "mandatory: build artifact"
    assert reasons["dist/"] == "mandatory: build artifact"
    assert reasons["env/"] == "mandatory: virtual environment"


def test_build_source_bundle_allows_explicit_bundled_runtime_dependencies(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "main.py").write_text("main = True\n", encoding="utf-8")
    package = repository / "node_modules" / "runtime-package"
    package.mkdir(parents=True)
    (package / "index.js").write_text("export default 1;\n", encoding="utf-8")
    cache = repository / "node_modules" / ".cache"
    cache.mkdir()
    (cache / "state.json").write_text("generated", encoding="utf-8")
    dist = repository / "dist"
    dist.mkdir()
    (dist / "agent.js").write_text("console.log('agent');\n", encoding="utf-8")
    (dist / "runtime.whl").write_bytes(b"bundled-wheel")
    metadata = dist / "runtime.egg-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: runtime\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "bundle.zip",
            include=("src/**", "node_modules/**", "dist/**"),
            dependency_resolution="bundled",
        )
    )

    assert artifact.included_files == (
        "dist/agent.js",
        "dist/runtime.egg-info/METADATA",
        "dist/runtime.whl",
        "node_modules/runtime-package/index.js",
        "src/main.py",
    )
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["node_modules/.cache/"] == "mandatory: cache"


def test_build_source_bundle_remote_build_excludes_runtime_artifacts(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "main.py").write_text("main = True\n", encoding="utf-8")
    package = repository / "node_modules" / "runtime-package"
    package.mkdir(parents=True)
    (package / "index.js").write_text("export default 1;\n", encoding="utf-8")
    dist = repository / "dist"
    dist.mkdir()
    (dist / "agent.js").write_text("console.log('agent');\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "bundle.zip",
            include=("src/**", "node_modules/**", "dist/**"),
            dependency_resolution="remote_build",
        )
    )

    assert artifact.included_files == ("src/main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["node_modules/"] == "mandatory: cache"
    assert reasons["dist/"] == "mandatory: build artifact"


def test_build_source_bundle_bundled_mode_requires_explicit_runtime_include(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    (repository / "src").mkdir(parents=True)
    (repository / "src" / "main.py").write_text("main = True\n", encoding="utf-8")
    package = repository / "node_modules" / "runtime-package"
    package.mkdir(parents=True)
    (package / "index.js").write_text("export default 1;\n", encoding="utf-8")
    dist = repository / "dist"
    dist.mkdir()
    (dist / "agent.js").write_text("console.log('agent');\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "bundle.zip",
            dependency_resolution="bundled",
        )
    )

    assert artifact.included_files == ("src/main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["node_modules/"] == "mandatory: cache"
    assert reasons["dist/"] == "mandatory: build artifact"


def test_build_source_bundle_rejects_secrets_in_bundled_dependencies(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    package = repository / "node_modules" / "runtime-package"
    package.mkdir(parents=True)
    (package / "index.js").write_text("export default 1;\n", encoding="utf-8")
    (package / ".env").write_text("TOKEN=must-not-leak\n", encoding="utf-8")

    with pytest.raises(SecretSourceFileError):
        build_source_bundle(
            BundleRequest(
                repository,
                tmp_path / "bundle.zip",
                include=("node_modules/**",),
                dependency_resolution="bundled",
            )
        )


def test_build_source_bundle_excludes_root_git_file(tmp_path: Path) -> None:
    repository = tmp_path / "worktree"
    repository.mkdir()
    (repository / ".git").write_text(
        "gitdir: C:/outside/repository/.git/worktrees/demo\n",
        encoding="utf-8",
    )
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "bundle.zip")
    )

    assert artifact.included_files == ("main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons[".git"] == "mandatory: version-control metadata"


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        ".npmrc",
        "credentials.json",
        "client-secret.txt",
        "id_rsa",
        "secrets.yaml",
    ],
)
def test_build_source_bundle_rejects_secret_shaped_files(
    tmp_path: Path,
    relative_path: str,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / relative_path).write_text("must-not-leak", encoding="utf-8")

    with pytest.raises(SecretSourceFileError) as raised:
        build_source_bundle(BundleRequest(repository, tmp_path / "bundle.zip"))

    assert relative_path in str(raised.value)
    assert "must-not-leak" not in str(raised.value)
    assert not (tmp_path / "bundle.zip").exists()


def test_build_source_bundle_rejects_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    link = repository / "linked.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(UnsafeSourcePathError):
        build_source_bundle(BundleRequest(repository, tmp_path / "bundle.zip"))


def test_build_source_bundle_rejects_multiply_linked_source_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("outside", encoding="utf-8")
    linked = repository / "linked.py"
    try:
        os.link(outside, linked)
    except OSError:
        pytest.skip("hardlink creation is unavailable")

    with pytest.raises(UnsafeSourcePathError):
        build_source_bundle(BundleRequest(repository, tmp_path / "bundle.zip"))

    assert not (tmp_path / "bundle.zip").exists()


def test_build_source_bundle_uses_validated_file_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "main.py"
    source.write_bytes(b"validated-source")
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"raced-outside-source")
    original_read_bytes = Path.read_bytes

    def replace_before_reopen(path: Path) -> bytes:
        if path == source:
            path.unlink()
            try:
                path.symlink_to(outside)
            except OSError:
                path.write_bytes(outside.read_bytes())
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", replace_before_reopen)

    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "bundle.zip")
    )

    with zipfile.ZipFile(artifact.path) as archive:
        assert archive.read("main.py") == b"validated-source"


def test_build_source_bundle_prunes_excluded_symlink_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside-venv"
    outside.mkdir()
    (outside / "secret.py").write_text("outside", encoding="utf-8")
    link = repository / ".venv"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "bundle.zip")
    )

    assert artifact.included_files == ("main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons[".venv/"] == "mandatory: virtual environment"


def test_build_source_bundle_prunes_declared_excluded_symlink_directories(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside-generated"
    outside.mkdir()
    (outside / "secret.py").write_text("outside", encoding="utf-8")
    link = repository / "generated"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "bundle.zip",
            exclude=("generated/**",),
        )
    )

    assert artifact.included_files == ("main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["generated/"] == "declared exclude: generated/**"


def test_build_source_bundle_mandatorily_prunes_pytest_tmp_recursively(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    pytest_tmp = repository / "tests" / ".pytest-tmp"
    pytest_tmp.mkdir(parents=True)
    (pytest_tmp / "captured.py").write_text("generated", encoding="utf-8")
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(repository, tmp_path / "bundle.zip")
    )

    assert artifact.included_files == ("main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons["tests/.pytest-tmp/"] == "mandatory: cache"


def test_build_source_bundle_excludes_optimizer_and_configured_evidence_paths(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    built_in_campaign = (
        repository
        / ".foundry-optimizer"
        / "campaigns"
        / "campaign-001"
    )
    built_in_campaign.mkdir(parents=True)
    (built_in_campaign / "evaluation.json").write_text(
        "sensitive optimizer evidence",
        encoding="utf-8",
    )
    configured_campaign = (
        repository
        / "artifacts"
        / "optimization-evidence"
        / "campaign-002"
    )
    configured_campaign.mkdir(parents=True)
    (configured_campaign / "candidate.json").write_text(
        "sensitive candidate evidence",
        encoding="utf-8",
    )
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")

    artifact = build_source_bundle(
        BundleRequest(
            repository,
            tmp_path / "bundle.zip",
            evidence_paths=(Path("artifacts/optimization-evidence"),),
        )
    )

    assert artifact.included_files == ("main.py",)
    reasons = {entry.path: entry.reason for entry in artifact.excluded_files}
    assert reasons[".foundry-optimizer/"] == (
        "mandatory: optimizer evidence"
    )
    assert reasons["artifacts/optimization-evidence/"] == (
        "mandatory: optimizer evidence"
    )
    with zipfile.ZipFile(artifact.path) as archive:
        content = b"".join(archive.read(name) for name in archive.namelist())
    assert b"sensitive optimizer evidence" not in content
    assert b"sensitive candidate evidence" not in content


def test_build_source_bundle_rejects_empty_bundle(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    output = tmp_path / "bundle.zip"

    with pytest.raises(BundleError, match="empty"):
        build_source_bundle(BundleRequest(repository, output))

    assert not output.exists()
    assert not output.with_name("bundle.zip.manifest.json").exists()


def test_build_source_bundle_requires_output_outside_repository_root_parent(
    tmp_path: Path,
) -> None:
    repository_file = tmp_path / "not-a-directory"
    repository_file.write_text("x", encoding="utf-8")

    with pytest.raises(UnsafeSourcePathError):
        build_source_bundle(
            BundleRequest(repository_file, tmp_path / "bundle.zip")
        )


@pytest.mark.parametrize("collision", ["output", "manifest", "partial"])
def test_build_source_bundle_rejects_repository_artifact_collisions(
    tmp_path: Path,
    collision: str,
) -> None:
    repository = tmp_path / "repo"
    output_directory = repository / "artifacts"
    output_directory.mkdir(parents=True)
    (repository / "main.py").write_text("print('ok')\n", encoding="utf-8")
    output = output_directory / "agent.zip"
    collision_path = {
        "output": output,
        "manifest": output.with_name(f"{output.name}.manifest.json"),
        "partial": output.with_suffix(f"{output.suffix}.partial"),
    }[collision]
    collision_path.write_bytes(b"repository-source-must-survive")

    with pytest.raises(UnsafeSourcePathError):
        build_source_bundle(BundleRequest(repository, output))

    assert collision_path.read_bytes() == b"repository-source-must-survive"
