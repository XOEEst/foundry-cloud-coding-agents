from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import subprocess

from foundry_opt.preflight.interfaces import CommandResult


class CommandError(RuntimeError):
    def __init__(self, arguments: Sequence[str]) -> None:
        self.arguments = tuple(arguments)
        super().__init__(f"Command failed: {self.arguments[0]}")


class CommandNotFoundError(CommandError):
    def __init__(self, arguments: Sequence[str]) -> None:
        super().__init__(arguments)
        self.executable = self.arguments[0]


class CommandExitError(CommandError):
    def __init__(
        self,
        arguments: Sequence[str],
        *,
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(arguments)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class SubprocessCommandRunner:
    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        environment: Mapping[str, str] | None = None,
        input_text: str | None = None,
    ) -> CommandResult:
        command = list(arguments)
        if not command:
            raise ValueError("arguments must contain an executable")

        options = {
            "cwd": cwd,
            "shell": False,
            "capture_output": True,
            "text": True,
            "check": False,
        }
        if environment is not None:
            options["env"] = {**os.environ, **environment}
        if input_text is not None:
            options["input"] = input_text
        try:
            completed = subprocess.run(command, **options)
        except FileNotFoundError as error:
            raise CommandNotFoundError(command) from error

        if completed.returncode != 0:
            raise CommandExitError(
                command,
                exit_code=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        return CommandResult(
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
