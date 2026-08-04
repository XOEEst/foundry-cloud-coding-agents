from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    """Isolate real Git fixtures owned by concurrent pytest processes."""
    if config.option.basetemp is None:
        config.option.basetemp = str(
            config.rootpath / ".pytest-tmp" / f"process-{os.getpid()}"
        )


@dataclass(frozen=True)
class CopilotGitProxyInstallation:
    repository_root: Path
    real_origin: Path
    real_git: str

    def disable(self) -> None:
        for key in (
            "remote.origin.receivepack",
            "remote.origin.uploadpack",
        ):
            subprocess.run(
                (
                    self.real_git,
                    "config",
                    "--unset-all",
                    key,
                ),
                cwd=self.repository_root,
                check=False,
                capture_output=True,
            )

    def real_revision(self, ref: str) -> str | None:
        result = subprocess.run(
            (
                self.real_git,
                f"--git-dir={self.real_origin}",
                "rev-parse",
                "--verify",
                f"{ref}^{{commit}}",
            ),
            check=False,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None


class CopilotGitProxy:
    def __init__(self, root: Path) -> None:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("git is unavailable")
        self._root = root
        self._git = git
        self._counter = 0

    def install(
        self,
        repository_root: Path,
        real_origin: Path,
        *,
        acknowledgement: str,
        repository: str = "octo-org/optimizer",
        loopback_origin: bool = True,
    ) -> CopilotGitProxyInstallation:
        if acknowledgement not in {
            "absent",
            "expected",
            "proposed",
            "unrelated",
        }:
            raise ValueError("unsupported proxy acknowledgement")
        self._counter += 1
        proxy_root = self._root / f"git-proxy-{self._counter}"
        shadow = proxy_root / "shadow.git"
        acknowledgement_repository = proxy_root / "ack.git"
        proxy_root.mkdir()
        self._run(
            "clone",
            "--mirror",
            str(real_origin),
            str(shadow),
        )
        self._run(
            "clone",
            "--mirror",
            str(real_origin),
            str(acknowledgement_repository),
        )
        synthetic = self._run(
            f"--git-dir={acknowledgement_repository}",
            "rev-parse",
            "refs/heads/main^{commit}",
        ).stdout.strip()
        receive_script = proxy_root / "receive.py"
        receive_script.write_text(
            textwrap.dedent(
                f"""
                from __future__ import annotations

                import subprocess

                ACK = {str(acknowledgement_repository)!r}
                GIT = {self._git!r}
                MODE = {acknowledgement!r}
                REAL = {str(real_origin)!r}
                SHADOW = {str(shadow)!r}
                SYNTHETIC = {synthetic!r}
                PRIVATE_PREFIXES = (
                    "refs/heads/foundry-opt/state/",
                    "refs/heads/foundry-opt/design/",
                )


                def refs(git_dir: str) -> dict[str, str]:
                    result = subprocess.run(
                        (
                            GIT,
                            f"--git-dir={{git_dir}}",
                            "for-each-ref",
                            "--format=%(refname) %(objectname)",
                            "refs/heads",
                        ),
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    return dict(
                        line.split(" ", 1)
                        for line in result.stdout.splitlines()
                        if line
                    )


                def fetch_ref(target: str, source: str, ref: str) -> None:
                    subprocess.run(
                        (
                            GIT,
                            f"--git-dir={{target}}",
                            "fetch",
                            "--quiet",
                            source,
                            f"+{{ref}}:{{ref}}",
                        ),
                        check=True,
                    )


                before = refs(SHADOW)
                status = subprocess.call((GIT, "receive-pack", SHADOW))
                if status != 0:
                    raise SystemExit(status)
                after = refs(SHADOW)
                for ref, revision in after.items():
                    if before.get(ref) == revision:
                        continue
                    if ref.startswith(PRIVATE_PREFIXES):
                        if MODE == "absent":
                            subprocess.run(
                                (
                                    GIT,
                                    f"--git-dir={{ACK}}",
                                    "update-ref",
                                    "-d",
                                    ref,
                                ),
                                check=False,
                            )
                        elif MODE == "proposed":
                            fetch_ref(ACK, SHADOW, ref)
                        elif MODE == "unrelated":
                            subprocess.run(
                                (
                                    GIT,
                                    f"--git-dir={{ACK}}",
                                    "update-ref",
                                    ref,
                                    SYNTHETIC,
                                ),
                                check=True,
                            )
                        previous = before.get(ref)
                        if previous is None:
                            subprocess.run(
                                (
                                    GIT,
                                    f"--git-dir={{SHADOW}}",
                                    "update-ref",
                                    "-d",
                                    ref,
                                ),
                                check=False,
                            )
                        else:
                            subprocess.run(
                                (
                                    GIT,
                                    f"--git-dir={{SHADOW}}",
                                    "update-ref",
                                    ref,
                                    previous,
                                ),
                                check=True,
                            )
                    elif ref.startswith("refs/heads/"):
                        subprocess.run(
                            (
                                GIT,
                                f"--git-dir={{SHADOW}}",
                                "push",
                                "--quiet",
                                "--force",
                                REAL,
                                f"{{revision}}:{{ref}}",
                            ),
                            check=True,
                        )
                        fetch_ref(ACK, SHADOW, ref)
                """
            ).lstrip(),
            encoding="utf-8",
        )
        upload_script = proxy_root / "upload.py"
        upload_script.write_text(
            textwrap.dedent(
                f"""
                import subprocess

                raise SystemExit(
                    subprocess.call(
                        (
                            {self._git!r},
                            "upload-pack",
                            {str(acknowledgement_repository)!r},
                        )
                    )
                )
                """
            ).lstrip(),
            encoding="utf-8",
        )
        if loopback_origin:
            endpoint = (
                f"http://localhost:{43000 + self._counter}/{repository}"
            )
            self._run(
                "remote",
                "set-url",
                "origin",
                endpoint,
                cwd=repository_root,
            )
            self._run(
                "config",
                f"url.{real_origin.resolve().as_uri()}.insteadOf",
                endpoint,
                cwd=repository_root,
            )
        python = Path(sys.executable).as_posix()
        self._run(
            "config",
            "remote.origin.receivepack",
            f'"{python}" "{receive_script.as_posix()}"',
            cwd=repository_root,
        )
        self._run(
            "config",
            "remote.origin.uploadpack",
            f'"{python}" "{upload_script.as_posix()}"',
            cwd=repository_root,
        )
        return CopilotGitProxyInstallation(
            repository_root,
            real_origin,
            self._git,
        )

    def _run(
        self,
        *arguments: str,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (self._git, *arguments),
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )


@pytest.fixture
def copilot_git_proxy(tmp_path: Path) -> CopilotGitProxy:
    return CopilotGitProxy(tmp_path)
