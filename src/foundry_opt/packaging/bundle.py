from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from foundry_opt.packaging.models import (
    BundleArtifact,
    BundleRequest,
    ExcludedFile,
    SecretSourceFileError,
    UnsafeSourcePathError,
)


_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MANDATORY_DIRECTORIES = {
    ".git": "version-control metadata",
    ".azure": "Azure local state",
    ".venv": "virtual environment",
    "venv": "virtual environment",
    "env": "virtual environment",
    ".tox": "virtual environment",
    ".nox": "virtual environment",
    "__pycache__": "cache",
    ".pytest_cache": "cache",
    ".mypy_cache": "cache",
    ".ruff_cache": "cache",
    ".cache": "cache",
    "node_modules": "cache",
    ".foundry-opt": "optimizer evidence",
    ".foundry_opt": "optimizer evidence",
    "foundry-opt-evidence": "optimizer evidence",
    "optimizer-evidence": "optimizer evidence",
    "dist": "build artifact",
    "build": "build artifact",
    "htmlcov": "build artifact",
}
_MANDATORY_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".whl",
    ".egg",
    ".coverage",
}
_SECRET_EXACT_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets.json",
    "secret.json",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "service-account.json",
    "service_account.json",
    "auth.json",
    "azureprofile.json",
}
_SECRET_SUFFIXES = {
    ".pem",
    ".pfx",
    ".p12",
    ".key",
    ".keystore",
    ".jks",
}


def build_source_bundle(request: BundleRequest) -> BundleArtifact:
    root = request.repository_root.expanduser().resolve()
    if not root.is_dir():
        raise UnsafeSourcePathError(request.repository_root)

    output = _resolve_output(request.output_path, root)
    manifest_path = output.with_name(f"{output.name}.manifest.json")
    generated_paths = {output, manifest_path, output.with_suffix(f"{output.suffix}.partial")}

    included: list[tuple[str, Path]] = []
    excluded: list[ExcludedFile] = []
    for archive_path, source_path in _source_files(root, excluded):
        mandatory_reason = _mandatory_exclusion(archive_path)
        if mandatory_reason is not None:
            excluded.append(
                ExcludedFile(archive_path, f"mandatory: {mandatory_reason}")
            )
            continue
        resolved = _contained_file(source_path, root)
        if resolved in generated_paths:
            excluded.append(ExcludedFile(archive_path, "generated bundle output"))
            continue
        if _secret_shaped(archive_path):
            raise SecretSourceFileError(archive_path)

        exclude_pattern = _first_match(archive_path, request.exclude)
        if exclude_pattern is not None:
            excluded.append(
                ExcludedFile(
                    archive_path,
                    f"declared exclude: {exclude_pattern}",
                )
            )
            continue
        if _first_match(archive_path, request.include) is None:
            excluded.append(ExcludedFile(archive_path, "not included"))
            continue
        included.append((archive_path, resolved))

    included.sort(key=lambda item: item[0])
    excluded.sort(key=lambda item: item.path)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(f"{output.suffix}.partial")
    try:
        with zipfile.ZipFile(
            partial,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for archive_path, source_path in included:
                info = zipfile.ZipInfo(archive_path, _FIXED_TIMESTAMP)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (
                    stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR
                    | stat.S_IRGRP | stat.S_IROTH
                ) << 16
                archive.writestr(info, source_path.read_bytes(), compresslevel=9)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)

    digest = _sha256(output)
    artifact = BundleArtifact(
        path=output,
        sha256=digest,
        included_files=tuple(path for path, _ in included),
        excluded_files=tuple(excluded),
        byte_size=output.stat().st_size,
        manifest_path=manifest_path,
    )
    _write_manifest(artifact)
    return artifact


def _resolve_output(path: Path, root: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.resolve()


def _source_files(root: Path, excluded: list[ExcludedFile]):
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            relative = (current_path / name).relative_to(root).as_posix()
            mandatory_reason = _mandatory_directory_exclusion(name)
            if mandatory_reason is not None:
                excluded.append(
                    ExcludedFile(
                        f"{relative}/",
                        f"mandatory: {mandatory_reason}",
                    )
                )
                continue
            candidate = current_path / name
            if candidate.is_symlink():
                raise UnsafeSourcePathError(candidate)
            retained_directories.append(name)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            candidate = current_path / name
            relative = candidate.relative_to(root)
            yield relative.as_posix(), candidate


def _contained_file(path: Path, root: Path) -> Path:
    if path.is_symlink():
        raise UnsafeSourcePathError(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeSourcePathError(path) from error
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise UnsafeSourcePathError(path)
    return resolved


def _mandatory_exclusion(archive_path: str) -> str | None:
    path = PurePosixPath(archive_path)
    if path.name.casefold() == ".git":
        return "version-control metadata"
    for part in path.parts[:-1]:
        reason = _mandatory_directory_exclusion(part)
        if reason is not None:
            return reason
    name = path.name.casefold()
    if name in {".coverage", "coverage.xml"}:
        return "build artifact"
    if any(name.endswith(suffix) for suffix in _MANDATORY_FILE_SUFFIXES):
        return "build artifact"
    return None


def _mandatory_directory_exclusion(name: str) -> str | None:
    normalized = name.casefold()
    reason = _MANDATORY_DIRECTORIES.get(normalized)
    if reason is not None:
        return reason
    if normalized.endswith(".egg-info"):
        return "build artifact"
    return None


def _secret_shaped(archive_path: str) -> bool:
    name = PurePosixPath(archive_path).name.casefold()
    if name in _SECRET_EXACT_NAMES or name.startswith(".env."):
        return True
    if any(name.endswith(suffix) for suffix in _SECRET_SUFFIXES):
        return True
    stem = PurePosixPath(name).stem
    if stem in {"secret", "secrets", "token", "tokens"}:
        return True
    return any(
        marker in stem
        for marker in (
            "client-secret",
            "client_secret",
            "credentials",
            "service-account",
            "service_account",
            "private-key",
            "private_key",
        )
    )


def _first_match(path: str, patterns: tuple[str, ...]) -> str | None:
    candidate = PurePosixPath(path)
    for pattern in patterns:
        normalized = pattern.replace("\\", "/")
        if (
            candidate.match(normalized)
            or fnmatch.fnmatchcase(path, normalized)
            or (
                normalized.startswith("**/")
                and candidate.match(normalized[3:])
            )
        ):
            return pattern
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_manifest(artifact: BundleArtifact) -> None:
    payload = {
        "archive": artifact.path.name,
        "byte_size": artifact.byte_size,
        "excluded_files": [
            {"path": item.path, "reason": item.reason}
            for item in artifact.excluded_files
        ],
        "included_files": list(artifact.included_files),
        "sha256": artifact.sha256,
    }
    artifact.manifest_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
