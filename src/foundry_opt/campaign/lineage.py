from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class IdeaLineage:
    idea_id: str
    parent_idea_ids: tuple[str, ...]
    mutation_class: str
    changed_paths: tuple[Path, ...]

    def __post_init__(self) -> None:
        for value in (
            self.idea_id,
            self.mutation_class,
            *self.parent_idea_ids,
        ):
            if not _IDENTIFIER.fullmatch(value):
                raise ValueError("lineage identifier is invalid")
        normalized: list[Path] = []
        for value in self.changed_paths:
            raw = str(value)
            windows = PureWindowsPath(raw)
            posix = PurePosixPath(raw.replace("\\", "/"))
            if (
                not raw
                or windows.drive
                or raw.startswith(("/", "\\"))
                or ".." in posix.parts
            ):
                raise ValueError("lineage paths must be repository-relative")
            normalized.append(Path(posix.as_posix()))
        object.__setattr__(self, "changed_paths", tuple(normalized))
