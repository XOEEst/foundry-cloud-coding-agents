from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode

from foundry_opt.config.models import OptimizerConfig


@dataclass(frozen=True)
class ConfigIssue:
    path: tuple[str | int, ...]
    message: str
    code: str


class ConfigLoadError(ValueError):
    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = tuple(issues)
        summary = "; ".join(
            f"{'.'.join(map(str, issue.path)) or '<root>'}: {issue.message}"
            for issue in self.issues
        )
        super().__init__(summary)


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping keys must be strings",
                key_node.start_mark,
            )
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_config(path: Path) -> OptimizerConfig:
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=_UniqueKeyLoader)
    except ConstructorError as error:
        code = (
            "duplicate_key"
            if str(error.problem).startswith("duplicate key:")
            else "invalid_key"
        )
        raise ConfigLoadError(
            [ConfigIssue((), str(error.problem), code)]
        ) from error
    except yaml.YAMLError as error:
        raise ConfigLoadError([ConfigIssue((), str(error), "invalid_yaml")]) from error
    except OSError as error:
        raise ConfigLoadError([ConfigIssue((), str(error), "read_error")]) from error
    except UnicodeError as error:
        raise ConfigLoadError(
            [ConfigIssue((), str(error), "invalid_encoding")]
        ) from error

    if not isinstance(document, dict):
        raise ConfigLoadError(
            [
                ConfigIssue(
                    (),
                    "configuration document must be a mapping",
                    "invalid_document",
                )
            ]
        )

    try:
        return OptimizerConfig.model_validate(document)
    except ValidationError as error:
        issues = [
            ConfigIssue(
                tuple(item["loc"]),
                item["msg"].removeprefix("Value error, "),
                item["type"],
            )
            for item in error.errors(include_url=False, include_input=False)
        ]
        raise ConfigLoadError(issues) from error
