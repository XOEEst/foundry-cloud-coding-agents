from __future__ import annotations

import fnmatch
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import stat
import zipfile

from foundry_opt.packaging.models import (
    BundleArtifact,
    BundleRequest,
    EmptySourceBundleError,
    ExcludedFile,
    SecretSourceFileError,
    UnsafeSourcePathError,
)


_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MANDATORY_DIRECTORIES = {
    ".git": "version-control metadata",
    ".azure": "Azure local state",
    ".venv": "virtual environment",
    ".tox": "virtual environment",
    ".nox": "virtual environment",
    "__pycache__": "cache",
    ".pytest_cache": "cache",
    ".pytest-tmp": "cache",
    ".mypy_cache": "cache",
    ".ruff_cache": "cache",
    ".cache": "cache",
    "node_modules": "cache",
    ".foundry-opt": "optimizer evidence",
    ".foundry_opt": "optimizer evidence",
    "foundry-opt-evidence": "optimizer evidence",
    "optimizer-evidence": "optimizer evidence",
    "htmlcov": "build artifact",
}
_ROOT_MANDATORY_DIRECTORIES = {
    "venv": "virtual environment",
    "env": "virtual environment",
    "dist": "build artifact",
    "build": "build artifact",
}
_MANDATORY_CACHE_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".coverage",
}
_MANDATORY_BUILD_FILE_SUFFIXES = {
    ".whl",
    ".egg",
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
    partial_path = output.with_suffix(f"{output.suffix}.partial")
    generated_paths = {output, manifest_path, partial_path}
    _reject_artifact_collisions(generated_paths)
    bundled_runtime_roots = _bundled_runtime_roots(request)

    included: list[tuple[str, bytes]] = []
    excluded: list[ExcludedFile] = []
    for archive_path, source_path in _source_files(
        root,
        excluded,
        request.exclude,
        bundled_runtime_roots,
    ):
        mandatory_reason = _mandatory_exclusion(
            archive_path,
            bundled_runtime_roots,
        )
        if mandatory_reason is not None:
            excluded.append(
                ExcludedFile(archive_path, f"mandatory: {mandatory_reason}")
            )
            continue
        resolved, content = _snapshot_contained_file(source_path, root)
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
        included.append((archive_path, content))

    included.sort(key=lambda item: item[0])
    excluded.sort(key=lambda item: item.path)
    if not included:
        raise EmptySourceBundleError()
    output.parent.mkdir(parents=True, exist_ok=True)
    bundle_stream = BytesIO()
    with zipfile.ZipFile(
        bundle_stream,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for archive_path, content in included:
            info = zipfile.ZipInfo(archive_path, _FIXED_TIMESTAMP)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (
                stat.S_IFREG | stat.S_IRUSR | stat.S_IWUSR
                | stat.S_IRGRP | stat.S_IROTH
            ) << 16
            archive.writestr(info, content, compresslevel=9)
    bundle_bytes = bundle_stream.getvalue()
    digest = hashlib.sha256(bundle_bytes).hexdigest()
    artifact = BundleArtifact(
        path=output,
        sha256=digest,
        included_files=tuple(path for path, _ in included),
        excluded_files=tuple(excluded),
        byte_size=len(bundle_bytes),
        manifest_path=manifest_path,
    )
    output_state = _write_exclusive(output, bundle_bytes)
    try:
        _write_exclusive(manifest_path, _manifest_bytes(artifact))
    except Exception:
        _unlink_owned(output, output_state)
        raise
    return artifact


def _resolve_output(path: Path, root: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = root / expanded
    return expanded.parent.resolve() / expanded.name


def _reject_artifact_collisions(paths: set[Path]) -> None:
    for path in sorted(paths):
        if os.path.lexists(path):
            raise UnsafeSourcePathError(path)


def _source_files(
    root: Path,
    excluded: list[ExcludedFile],
    exclude_patterns: tuple[str, ...],
    bundled_runtime_roots: frozenset[str],
):
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            relative = (current_path / name).relative_to(root).as_posix()
            mandatory_reason = _mandatory_directory_exclusion(
                relative,
                bundled_runtime_roots,
            )
            if mandatory_reason is not None:
                excluded.append(
                    ExcludedFile(
                        f"{relative}/",
                        f"mandatory: {mandatory_reason}",
                    )
                )
                continue
            exclude_pattern = _excluded_directory_pattern(
                relative,
                exclude_patterns,
            )
            if exclude_pattern is not None:
                excluded.append(
                    ExcludedFile(
                        f"{relative}/",
                        f"declared exclude: {exclude_pattern}",
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


def _excluded_directory_pattern(
    archive_path: str,
    patterns: tuple[str, ...],
) -> str | None:
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").rstrip("/")
        if normalized.endswith("/**"):
            directory_pattern = normalized[:-3].rstrip("/")
        elif not any(character in normalized for character in "*?["):
            directory_pattern = normalized
        else:
            continue
        if _first_match(archive_path, (directory_pattern,)) is not None:
            return pattern
    return None


def _bundled_runtime_roots(request: BundleRequest) -> frozenset[str]:
    if request.dependency_resolution != "bundled":
        return frozenset()
    return frozenset(
        directory
        for directory in ("node_modules", "dist")
        if _explicitly_includes_root(directory, request.include)
    )


def _explicitly_includes_root(
    directory: str,
    patterns: tuple[str, ...],
) -> bool:
    prefixes = (f"{directory}/", f"**/{directory}/")
    for pattern in patterns:
        normalized = pattern.replace("\\", "/").removeprefix("./")
        if normalized == directory or normalized.startswith(prefixes):
            return True
    return False


def _snapshot_contained_file(path: Path, root: Path) -> tuple[Path, bytes]:
    try:
        before = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise UnsafeSourcePathError(path) from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not resolved.is_relative_to(root)
    ):
        raise UnsafeSourcePathError(path)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise UnsafeSourcePathError(path) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(before) != _file_identity(opened)
            or _file_state(before) != _file_state(opened)
        ):
            raise UnsafeSourcePathError(path)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if after.st_nlink != 1 or _file_state(opened) != _file_state(after):
            raise UnsafeSourcePathError(path)
        return resolved, b"".join(chunks)
    finally:
        os.close(descriptor)


def _file_identity(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _file_state(value: os.stat_result) -> tuple[int, int]:
    return value.st_size, value.st_mtime_ns


def _mandatory_exclusion(
    archive_path: str,
    bundled_runtime_roots: frozenset[str],
) -> str | None:
    path = PurePosixPath(archive_path)
    if path.name.casefold() == ".git":
        return "version-control metadata"
    for index in range(len(path.parts) - 1):
        directory = PurePosixPath(*path.parts[:index + 1]).as_posix()
        reason = _mandatory_directory_exclusion(
            directory,
            bundled_runtime_roots,
        )
        if reason is not None:
            return reason
    name = path.name.casefold()
    if name in {".coverage", "coverage.xml"}:
        return "build artifact"
    if any(
        name.endswith(suffix)
        for suffix in _MANDATORY_CACHE_FILE_SUFFIXES
    ):
        return "build artifact"
    if any(
        name.endswith(suffix)
        for suffix in _MANDATORY_BUILD_FILE_SUFFIXES
    ) and not (
        path.parts
        and path.parts[0].casefold() == "dist"
        and "dist" in bundled_runtime_roots
    ):
        return "build artifact"
    return None


def _mandatory_directory_exclusion(
    archive_path: str,
    bundled_runtime_roots: frozenset[str],
) -> str | None:
    path = PurePosixPath(archive_path)
    normalized = path.name.casefold()
    top_level = path.parts[0].casefold()
    if len(path.parts) == 1:
        reason = _ROOT_MANDATORY_DIRECTORIES.get(normalized)
        if reason is not None and normalized not in bundled_runtime_roots:
            return reason
    reason = _MANDATORY_DIRECTORIES.get(normalized)
    if (
        normalized == "node_modules"
        and top_level == "node_modules"
        and "node_modules" in bundled_runtime_roots
    ):
        reason = None
    if reason is not None:
        return reason
    if normalized.endswith(".egg-info") and not (
        top_level == "dist" and "dist" in bundled_runtime_roots
    ):
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


def _manifest_bytes(artifact: BundleArtifact) -> bytes:
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
    return (
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _write_exclusive(path: Path, content: bytes) -> os.stat_result:
    try:
        stream = path.open("xb")
    except FileExistsError as error:
        raise UnsafeSourcePathError(path) from error
    try:
        stream.write(content)
        stream.flush()
        return os.fstat(stream.fileno())
    except Exception:
        state = os.fstat(stream.fileno())
        stream.close()
        _unlink_owned(path, state)
        raise
    finally:
        if not stream.closed:
            stream.close()


def _unlink_owned(path: Path, expected: os.stat_result) -> None:
    try:
        actual = os.stat(path, follow_symlinks=False)
    except OSError:
        return
    if (
        _file_identity(actual) == _file_identity(expected)
        and _file_state(actual) == _file_state(expected)
    ):
        path.unlink(missing_ok=True)
