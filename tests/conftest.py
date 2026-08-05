from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from threading import Thread
from urllib.parse import parse_qs, urlsplit

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
        subprocess.run(
            (
                self.real_git,
                "remote",
                "set-url",
                "origin",
                str(self.real_origin),
            ),
            cwd=self.repository_root,
            check=True,
            capture_output=True,
        )
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


class _GitSmartHttpServer:
    def __init__(
        self,
        *,
        git: str,
        repository: str,
        shadow: Path,
    ) -> None:
        repository_path = f"/{repository}"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                parsed = urlsplit(self.path)
                services = parse_qs(parsed.query).get("service", ())
                if (
                    parsed.path != f"{repository_path}/info/refs"
                    or len(services) != 1
                    or services[0]
                    not in {"git-upload-pack", "git-receive-pack"}
                ):
                    self.send_error(404)
                    return
                service = services[0]
                completed = subprocess.run(
                    (
                        git,
                        service.removeprefix("git-"),
                        "--stateless-rpc",
                        "--advertise-refs",
                        str(shadow),
                    ),
                    check=False,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    self.send_error(500)
                    return
                announcement = f"# service={service}\n".encode("ascii")
                prefix = (
                    f"{len(announcement) + 4:04x}".encode("ascii")
                    + announcement
                    + b"0000"
                )
                self._send(
                    prefix + completed.stdout,
                    f"application/x-{service}-advertisement",
                )

            def do_POST(self) -> None:
                parsed = urlsplit(self.path)
                service = parsed.path.removeprefix(
                    f"{repository_path}/"
                )
                if (
                    parsed.query
                    or service
                    not in {"git-upload-pack", "git-receive-pack"}
                ):
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                completed = subprocess.run(
                    (
                        git,
                        service.removeprefix("git-"),
                        "--stateless-rpc",
                        str(shadow),
                    ),
                    input=self.rfile.read(length),
                    check=False,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    self.send_error(500)
                    return
                self._send(
                    completed.stdout,
                    f"application/x-{service}-result",
                )

            def _send(self, content: bytes, content_type: str) -> None:
                self.send_response(200)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        self.endpoint = (
            f"http://127.0.0.1:{self._server.server_port}/{repository}"
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class CopilotGitProxy:
    def __init__(self, root: Path) -> None:
        git = shutil.which("git")
        if git is None:
            raise RuntimeError("git is unavailable")
        self._root = root
        self._git = git
        self._counter = 0
        self._servers: list[_GitSmartHttpServer] = []

    def install(
        self,
        repository_root: Path,
        real_origin: Path,
        *,
        acknowledgement: str,
        repository: str = "microsoft-foundry/luffy-test-agents-repo",
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
            self._install_http_proxy_hook(
                shadow,
                real_origin,
                acknowledgement=acknowledgement,
                synthetic=synthetic,
            )
            server = _GitSmartHttpServer(
                git=self._git,
                repository=repository,
                shadow=shadow,
            )
            self._servers.append(server)
            self._run(
                "remote",
                "set-url",
                "origin",
                server.endpoint,
                cwd=repository_root,
            )
        else:
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

    def close(self) -> None:
        for server in reversed(self._servers):
            server.close()
        self._servers.clear()

    def _install_http_proxy_hook(
        self,
        shadow: Path,
        real_origin: Path,
        *,
        acknowledgement: str,
        synthetic: str,
    ) -> None:
        hook = shadow / "hooks" / "post-receive"
        hook.write_text(
            "#!/bin/sh\n"
            'git_dir="${GIT_DIR:-.}"\n'
            "zero=0000000000000000000000000000000000000000\n"
            "while read old new ref; do\n"
            '  case "$ref" in\n'
            "    refs/heads/foundry-opt/state/*|"
            "refs/heads/foundry-opt/design/*)\n"
            f"      case {_shell_quote(acknowledgement)} in\n"
            "        absent)\n"
            '          git --git-dir="$git_dir" update-ref '
            '-d "$ref" "$new"\n'
            "          ;;\n"
            "        expected)\n"
            '          if [ "$old" = "$zero" ]; then\n'
            '            git --git-dir="$git_dir" update-ref '
            '-d "$ref" "$new"\n'
            "          else\n"
            '            git --git-dir="$git_dir" update-ref '
            '"$ref" "$old" "$new"\n'
            "          fi\n"
            "          ;;\n"
            "        proposed)\n"
            "          ;;\n"
            "        unrelated)\n"
            '          git --git-dir="$git_dir" update-ref '
            f'"$ref" {_shell_quote(synthetic)} "$new"\n'
            "          ;;\n"
            "      esac\n"
            "      ;;\n"
            "    refs/heads/*)\n"
            '      git --git-dir="$git_dir" push --quiet --force '
            f"{_shell_quote(real_origin.resolve().as_posix())} "
            '"$new:$ref"\n'
            "      ;;\n"
            "  esac\n"
            "done\n",
            encoding="utf-8",
            newline="\n",
        )
        hook.chmod(0o755)

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
    proxy = CopilotGitProxy(tmp_path)
    try:
        yield proxy
    finally:
        proxy.close()


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
