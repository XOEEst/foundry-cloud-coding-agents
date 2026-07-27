from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath


@dataclass(frozen=True)
class BundleRequest:
    repository_root: Path
    output_path: Path
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    dependency_resolution: str = "remote_build"
    evidence_paths: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not self.include:
            raise ValueError("include must contain at least one pattern")
        if any(not pattern.strip() for pattern in self.include + self.exclude):
            raise ValueError("bundle patterns must not be empty")
        if self.dependency_resolution not in {"remote_build", "bundled"}:
            raise ValueError("dependency_resolution is invalid")
        normalized_evidence_paths: list[Path] = []
        for value in self.evidence_paths:
            raw = str(value)
            windows_path = PureWindowsPath(raw)
            posix_path = PurePosixPath(raw.replace("\\", "/"))
            if (
                not raw
                or windows_path.drive
                or raw.startswith(("/", "\\"))
                or posix_path == PurePosixPath(".")
                or ".." in posix_path.parts
            ):
                raise ValueError(
                    "evidence_paths must be repository-relative paths"
                )
            normalized_evidence_paths.append(Path(posix_path.as_posix()))
        object.__setattr__(
            self,
            "evidence_paths",
            tuple(dict.fromkeys(normalized_evidence_paths)),
        )


@dataclass(frozen=True)
class ExcludedFile:
    path: str
    reason: str


@dataclass(frozen=True)
class BundleArtifact:
    path: Path
    sha256: str
    included_files: tuple[str, ...]
    excluded_files: tuple[ExcludedFile, ...]
    byte_size: int
    manifest_path: Path


class BundleError(RuntimeError):
    """Base class for source bundle failures."""


class EmptySourceBundleError(BundleError):
    def __init__(self) -> None:
        super().__init__("Source bundle must not be empty.")


class UnsafeSourcePathError(BundleError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Unsafe source path: {path}")


class SecretSourceFileError(BundleError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Secret-shaped source file is not allowed: {path}")
