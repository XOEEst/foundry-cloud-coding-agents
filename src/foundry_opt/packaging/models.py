from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BundleRequest:
    repository_root: Path
    output_path: Path
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.include:
            raise ValueError("include must contain at least one pattern")
        if any(not pattern.strip() for pattern in self.include + self.exclude):
            raise ValueError("bundle patterns must not be empty")


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


class UnsafeSourcePathError(BundleError):
    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(f"Unsafe source path: {path}")


class SecretSourceFileError(BundleError):
    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"Secret-shaped source file is not allowed: {path}")
