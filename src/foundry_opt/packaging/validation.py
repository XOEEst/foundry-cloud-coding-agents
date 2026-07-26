from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
import sys
import tomllib
from typing import Any

import yaml

from foundry_opt.preflight.interfaces import CommandRunner
from foundry_opt.preflight.redaction import redact


@dataclass(frozen=True)
class ValidationCommand:
    arguments: tuple[str, ...] = field(repr=False)
    working_directory: Path = Path(".")

    def __post_init__(self) -> None:
        if not self.arguments or any(not argument for argument in self.arguments):
            raise ValueError("validation command arguments must not be empty")


@dataclass(frozen=True)
class ValidationRequest:
    repository_root: Path
    commands: tuple[tuple[str, ...] | ValidationCommand, ...] = field(
        default=(),
        repr=False,
    )
    entry_point: tuple[str, ...] = field(default=(), repr=False)
    secrets: tuple[str, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )


@dataclass(frozen=True)
class ValidationResult:
    command: tuple[str, ...]
    working_directory: Path
    passed: bool
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ValidationReport:
    results: tuple[ValidationResult, ...]
    discovered: bool

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results)


def run_validation(
    request: ValidationRequest,
    command_runner: CommandRunner,
) -> ValidationReport:
    root = request.repository_root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("repository_root must be an existing directory")

    if request.commands:
        commands = _explicit_commands(root, request.commands)
        discovered = True
    else:
        commands = _discover_commands(root)
        discovered = bool(commands)
        if not commands:
            commands = _fallback_commands(root, request.entry_point)

    results: list[ValidationResult] = []
    for validation_command in commands:
        command = validation_command.arguments
        cwd = validation_command.working_directory
        try:
            result = command_runner.run(command, cwd=cwd)
            exit_code = result.exit_code
            stdout = result.stdout
            stderr = result.stderr
        except Exception as error:
            exit_code = int(getattr(error, "exit_code", 1))
            stdout = str(getattr(error, "stdout", ""))
            stderr = str(getattr(error, "stderr", "")) or type(error).__name__
        results.append(
            ValidationResult(
                command=tuple(
                    _redact_output(argument, request.secrets)
                    for argument in command
                ),
                working_directory=cwd,
                passed=exit_code == 0,
                exit_code=exit_code,
                stdout=_redact_output(stdout, request.secrets),
                stderr=_redact_output(stderr, request.secrets),
            )
        )
    return ValidationReport(tuple(results), discovered)


def _explicit_commands(
    root: Path,
    commands: tuple[tuple[str, ...] | ValidationCommand, ...],
) -> tuple[ValidationCommand, ...]:
    normalized: list[ValidationCommand] = []
    for command in commands:
        if isinstance(command, ValidationCommand):
            normalized.append(
                ValidationCommand(
                    command.arguments,
                    _contained_working_directory(
                        root,
                        command.working_directory,
                    ),
                )
            )
        else:
            normalized.append(ValidationCommand(tuple(command), root))
    return tuple(normalized)


def _discover_commands(root: Path) -> tuple[ValidationCommand, ...]:
    workflow_commands = _workflow_commands(root)
    commands = list(workflow_commands)
    kinds = {_command_kind(command.arguments) for command in commands}

    pyproject = _read_toml(root / "pyproject.toml")
    tool = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
    inferred = (
        ("test", ("python", "-m", "pytest"))
        if isinstance(tool, dict) and "pytest" in tool
        else None,
        ("lint", ("ruff", "check", "."))
        if isinstance(tool, dict) and "ruff" in tool
        else None,
        ("type", ("mypy", "."))
        if isinstance(tool, dict) and "mypy" in tool
        else None,
        ("type", ("pyright",))
        if isinstance(tool, dict) and "pyright" in tool
        else None,
    )
    for item in inferred:
        if item is not None and item[0] not in kinds:
            commands.append(ValidationCommand(item[1], root))
            kinds.add(item[0])

    setup_cfg = root / "setup.cfg"
    if setup_cfg.is_file():
        text = setup_cfg.read_text(encoding="utf-8", errors="replace").casefold()
        for kind, section, command in (
            ("test", "[tool:pytest]", ("python", "-m", "pytest")),
            ("lint", "[flake8]", ("flake8", ".")),
            ("type", "[mypy]", ("mypy", ".")),
        ):
            if section in text and kind not in kinds:
                commands.append(ValidationCommand(command, root))
                kinds.add(kind)

    if (root / "tox.ini").is_file() and "test" not in kinds:
        commands.append(ValidationCommand(("tox",), root))
        kinds.add("test")
    if (root / "noxfile.py").is_file() and "test" not in kinds:
        commands.append(ValidationCommand(("nox",), root))
        kinds.add("test")

    for kind, paths, command in (
        ("test", ("pytest.ini",), ("python", "-m", "pytest")),
        ("lint", (".flake8",), ("flake8", ".")),
        ("type", ("mypy.ini", ".mypy.ini"), ("mypy", ".")),
        ("type", ("pyrightconfig.json",), ("pyright",)),
    ):
        if kind not in kinds and any((root / path).is_file() for path in paths):
            commands.append(ValidationCommand(command, root))
            kinds.add(kind)

    return _deduplicate(commands)


def _workflow_commands(root: Path) -> tuple[ValidationCommand, ...]:
    directory = root / ".github" / "workflows"
    if not directory.is_dir():
        return ()
    commands: list[ValidationCommand] = []
    for path in sorted((*directory.glob("*.yml"), *directory.glob("*.yaml"))):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(document, dict):
            continue
        workflow_directory = _default_working_directory(document)
        jobs = document.get("jobs")
        if not isinstance(jobs, dict):
            continue
        for job in jobs.values():
            if not isinstance(job, dict):
                continue
            job_directory = (
                _default_working_directory(job) or workflow_directory
            )
            steps = job.get("steps")
            if not isinstance(steps, list):
                continue
            for step in steps:
                if not isinstance(step, dict) or not isinstance(
                    step.get("run"), str
                ):
                    continue
                working_directory = (
                    step.get("working-directory")
                    or job_directory
                    or Path(".")
                )
                resolved_directory = _contained_working_directory(
                    root,
                    working_directory,
                )
                for line in _logical_lines(step["run"]):
                    parsed = _parse_validation_command(line)
                    if parsed is not None:
                        commands.append(
                            ValidationCommand(parsed, resolved_directory)
                        )
    return _deduplicate(commands)


def _default_working_directory(value: dict[str, Any]) -> str | Path | None:
    defaults = value.get("defaults")
    if not isinstance(defaults, dict):
        return None
    run_defaults = defaults.get("run")
    if not isinstance(run_defaults, dict):
        return None
    working_directory = run_defaults.get("working-directory")
    return (
        working_directory
        if isinstance(working_directory, (str, Path))
        else None
    )


def _contained_working_directory(
    root: Path,
    value: str | Path,
) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("validation working directory escapes repository")
    return resolved


def _parse_validation_command(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if any(operator in stripped for operator in ("&&", "||", ";", "|", ">", "<")):
        return None
    try:
        command = tuple(shlex.split(stripped, posix=True))
    except ValueError:
        return None
    if (
        not command
        or command[0].casefold() == "env"
        or ("=" in command[0] and not command[0].startswith("-"))
    ):
        return None
    return command if _command_kind(command) is not None else None


def _command_kind(command: tuple[str, ...]) -> str | None:
    lowered = [Path(part).name.casefold() for part in command]
    meaningful = [
        part
        for part in lowered
        if part not in {"uv", "run", "poetry", "pipenv", "python", "python3", "-m"}
    ]
    if any(part in {"pytest", "tox", "nox", "unittest"} for part in meaningful):
        return "test"
    if any(part in {"ruff", "flake8", "pylint"} for part in meaningful):
        return "lint"
    if any(part in {"mypy", "pyright", "pyre"} for part in meaningful):
        return "type"
    if lowered and lowered[0] in {"make", "hatch"}:
        if any(part in {"test", "tests"} for part in lowered[1:]):
            return "test"
        if "lint" in lowered[1:]:
            return "lint"
        if any(part in {"typecheck", "type-check", "typing"} for part in lowered[1:]):
            return "type"
    return None


def _fallback_commands(
    root: Path,
    entry_point: tuple[str, ...],
) -> tuple[ValidationCommand, ...]:
    executable = sys.executable
    commands: list[ValidationCommand] = []
    compile_target = "src" if (root / "src").is_dir() else "."
    syntax_check = (
        "import pathlib;"
        f"root=pathlib.Path({compile_target!r});"
        "files=sorted(root.rglob('*.py'));"
        "[compile(path.read_bytes(),str(path),'exec') for path in files]"
    )
    commands.append(ValidationCommand((executable, "-c", syntax_check), root))

    pyproject = _read_toml(root / "pyproject.toml")
    imports = _import_names(root)
    for import_name in imports:
        prefix = "import sys;sys.path.insert(0,'src');" if (
            root / "src"
        ).is_dir() else "import sys;"
        prefix += "sys.dont_write_bytecode=True;"
        commands.append(
            ValidationCommand(
                (executable, "-c", f"{prefix}import {import_name}"),
                root,
            )
        )

    if (root / "pyproject.toml").is_file():
        package_check = (
            "import pathlib,tomllib;"
            "d=tomllib.loads(pathlib.Path('pyproject.toml').read_text("
            "encoding='utf-8'));"
            "assert isinstance(d.get('project'),dict);"
            "assert d['project'].get('name')"
        )
        commands.append(ValidationCommand((executable, "-c", package_check), root))
    elif (root / "setup.py").is_file():
        commands.append(
            ValidationCommand((executable, "setup.py", "--name"), root)
        )
    elif (root / "setup.cfg").is_file():
        package_check = (
            "import configparser;"
            "c=configparser.ConfigParser();c.read('setup.cfg');"
            "assert c.get('metadata','name',fallback='').strip()"
        )
        commands.append(ValidationCommand((executable, "-c", package_check), root))

    if entry_point:
        commands.append(_entry_point_smoke(root, executable, entry_point))
        return tuple(commands)

    scripts = (
        pyproject.get("project", {}).get("scripts", {})
        if isinstance(pyproject, dict)
        else {}
    )
    startup_added = False
    if isinstance(scripts, dict):
        for name, target in sorted(scripts.items()):
            if not isinstance(target, str) or ":" not in target:
                continue
            module, function = target.split(":", 1)
            path_setup = (
                "import sys;sys.dont_write_bytecode=True;"
                "sys.path.insert(0,'src');"
                if (root / "src").is_dir()
                else "import sys;sys.dont_write_bytecode=True;"
            )
            invocation = (
                f"{path_setup}import functools,importlib;"
                f"entry=functools.reduce(getattr,{function!r}.split('.'),"
                f"importlib.import_module({module!r}));"
                f"sys.argv=[{name!r},'--help'];entry()"
            )
            smoke = (
                "import os,subprocess,sys;"
                f"subprocess.run([sys.executable,'-c',{invocation!r}],"
                "check=True,timeout=10,"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
                "env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})"
            )
            commands.append(ValidationCommand((executable, "-c", smoke), root))
            startup_added = True
    if not startup_added and imports:
        module = imports[0]
        path_setup = (
            "import sys;sys.dont_write_bytecode=True;"
            "sys.path.insert(0,'src');"
            if (root / "src").is_dir()
            else "import sys;sys.dont_write_bytecode=True;"
        )
        invocation = (
            f"{path_setup}sys.argv=[{module!r},'--help'];"
            f"import runpy;runpy.run_module({module!r},run_name='__main__')"
        )
        smoke = (
            "import os,subprocess,sys;"
            f"subprocess.run([sys.executable,'-c',{invocation!r}],"
            "check=True,timeout=10,"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
            "env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})"
        )
        commands.append(ValidationCommand((executable, "-c", smoke), root))
    return tuple(commands)


def _entry_point_smoke(
    root: Path,
    executable: str,
    entry_point: tuple[str, ...],
) -> ValidationCommand:
    arguments = list(entry_point)
    if not arguments or any(not argument for argument in arguments):
        raise ValueError("entry_point must not be empty")
    executable_name = Path(arguments[0]).name.casefold()
    if executable_name in {"python", "python3", "python.exe"}:
        arguments[0] = executable
    for argument in arguments[1:]:
        if argument.casefold().endswith(".py"):
            source = Path(argument)
            if source.is_absolute():
                resolved = source.resolve()
            else:
                resolved = (root / source).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("entry_point source escapes repository")
    smoke = (
        "import os,subprocess;"
        f"subprocess.run({arguments!r}+['--help'],"
        "check=True,timeout=10,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,"
        "env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})"
    )
    return ValidationCommand((executable, "-c", smoke), root)


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _import_names(root: Path) -> tuple[str, ...]:
    base = root / "src" if (root / "src").is_dir() else root
    names: list[str] = []
    for path in sorted(base.iterdir()):
        if (
            path.is_dir()
            and (path / "__init__.py").is_file()
            and path.name.isidentifier()
        ):
            names.append(path.name)
        elif (
            path.is_file()
            and path.suffix.casefold() == ".py"
            and path.stem.isidentifier()
            and path.stem not in {"__init__", "conftest", "setup"}
        ):
            names.append(path.stem)
    return tuple(names)


def _deduplicate(
    commands: list[ValidationCommand] | tuple[ValidationCommand, ...],
) -> tuple[ValidationCommand, ...]:
    unique: dict[tuple[tuple[str, ...], Path], ValidationCommand] = {}
    for command in commands:
        unique.setdefault(
            (command.arguments, command.working_directory),
            command,
        )
    return tuple(unique.values())


def _logical_lines(value: str) -> tuple[str, ...]:
    lines: list[str] = []
    pending = ""
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if pending:
            line = f"{pending} {line}"
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        lines.append(line)
        pending = ""
    if pending:
        lines.append(pending)
    return tuple(lines)


def _redact_output(value: str, secrets: tuple[str, ...] = ()) -> str:
    redacted = redact(value, secrets) or ""
    redacted = re.sub(
        r"(?i)(\b[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|CREDENTIAL|"
        r"API_KEY|PRIVATE_KEY|CONNECTION_STRING)[A-Z0-9_]*"
        r"\b\s*[:=]\s*)(?!\[REDACTED\])[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
    return re.sub(
        r"(?i)(\b(?:token|secret|password|credential|api[-_]?key)"
        r"\b\s*[:=]\s*)(?!\[REDACTED\])[^\s,;]+",
        r"\1[REDACTED]",
        redacted,
    )
