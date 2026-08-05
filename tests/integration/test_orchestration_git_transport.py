from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest

from foundry_opt.orchestration.git_transport import (
    compare_and_swap_push,
    fetch_revision,
    GitTransportError,
    isolated_remote_revision,
    remote_revision,
    resolve_safe_fetch_remote,
    resolve_safe_push_remote,
)


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments),
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class _AuthenticatedGitServer:
    def __init__(
        self,
        repository: Path,
        *,
        authorization: str | None,
    ) -> None:
        repository_path = "/private/repository.git"
        git = "git"
        self.authorized_requests = 0
        self.unexpected_authorization = False

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(handler) -> None:
                if not handler._authorize():
                    return
                parsed = urlsplit(handler.path)
                services = parse_qs(parsed.query).get("service", ())
                if (
                    parsed.path != f"{repository_path}/info/refs"
                    or len(services) != 1
                    or services[0]
                    not in {"git-upload-pack", "git-receive-pack"}
                ):
                    handler.send_error(404)
                    return
                service = services[0]
                completed = subprocess.run(
                    (
                        git,
                        service.removeprefix("git-"),
                        "--stateless-rpc",
                        "--advertise-refs",
                        str(repository),
                    ),
                    check=False,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    handler.send_error(500)
                    return
                announcement = f"# service={service}\n".encode("ascii")
                content = (
                    f"{len(announcement) + 4:04x}".encode("ascii")
                    + announcement
                    + b"0000"
                    + completed.stdout
                )
                handler._send(
                    content,
                    f"application/x-{service}-advertisement",
                )

            def do_POST(handler) -> None:
                if not handler._authorize():
                    return
                parsed = urlsplit(handler.path)
                service = parsed.path.removeprefix(
                    f"{repository_path}/"
                )
                if (
                    parsed.query
                    or service
                    not in {"git-upload-pack", "git-receive-pack"}
                ):
                    handler.send_error(404)
                    return
                try:
                    length = int(
                        handler.headers.get("Content-Length", "0")
                    )
                except ValueError:
                    handler.send_error(400)
                    return
                completed = subprocess.run(
                    (
                        git,
                        service.removeprefix("git-"),
                        "--stateless-rpc",
                        str(repository),
                    ),
                    input=handler.rfile.read(length),
                    check=False,
                    capture_output=True,
                )
                if completed.returncode != 0:
                    handler.send_error(500)
                    return
                handler._send(
                    completed.stdout,
                    f"application/x-{service}-result",
                )

            def _authorize(handler) -> bool:
                actual = handler.headers.get("Authorization")
                if authorization is None:
                    if actual is not None:
                        self.unexpected_authorization = True
                    return True
                if actual != authorization:
                    handler.send_response(401)
                    handler.send_header(
                        "WWW-Authenticate",
                        'Basic realm="private Git"',
                    )
                    handler.send_header("Content-Length", "0")
                    handler.end_headers()
                    return False
                self.authorized_requests += 1
                return True

            def _send(
                handler,
                content: bytes,
                content_type: str,
            ) -> None:
                handler.send_response(200)
                handler.send_header("Cache-Control", "no-cache")
                handler.send_header("Content-Type", content_type)
                handler.send_header("Content-Length", str(len(content)))
                handler.end_headers()
                handler.wfile.write(content)

            def log_message(
                self,
                format: str,
                *args: object,
            ) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = Thread(
            target=self._server.serve_forever,
            daemon=True,
        )
        self._thread.start()
        self.endpoint = (
            f"http://127.0.0.1:{self._server.server_port}"
            f"{repository_path}"
        )

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _private_repository(
    tmp_path: Path,
) -> tuple[Path, Path, str, str]:
    origin = tmp_path / "origin.git"
    writer = tmp_path / "writer"
    repository = tmp_path / "repository"
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(writer))
    _git(writer, "config", "user.name", "Transport Writer")
    _git(writer, "config", "user.email", "writer@example.invalid")
    (writer / "README.md").write_text("base\n", encoding="utf-8")
    _git(writer, "add", "README.md")
    _git(writer, "commit", "-m", "base")
    _git(writer, "remote", "add", "origin", str(origin))
    _git(writer, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(origin), str(repository))
    _git(repository, "config", "user.name", "Transport Client")
    _git(repository, "config", "user.email", "client@example.invalid")
    (writer / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(writer, "add", "remote.txt")
    _git(writer, "commit", "-m", "remote update")
    _git(writer, "push", "origin", "main")
    remote_revision_value = _git(writer, "rev-parse", "HEAD")
    local_base = _git(repository, "rev-parse", "HEAD")
    return repository, origin, local_base, remote_revision_value


def _checkout_credentials(
    repository: Path,
    runner_temp: Path,
    remote_url: str,
    *headers: str,
) -> Path:
    runner_temp.mkdir(exist_ok=True)
    credentials = runner_temp / f"git-credentials-{uuid4()}.config"
    parsed = urlsplit(remote_url)
    credentials.write_text(
        f'[http "{parsed.scheme}://{parsed.netloc}/"]\n'
        + "".join(f"\textraheader = {header}\n" for header in headers),
        encoding="utf-8",
    )
    git_dir = (repository / ".git").resolve().as_posix()
    _git(
        repository,
        "config",
        f"includeIf.gitdir:{git_dir}.path",
        str(credentials),
    )
    _git(
        repository,
        "config",
        f"includeIf.gitdir:{git_dir}/worktrees/*.path",
        str(credentials),
    )
    _git(
        repository,
        "config",
        "includeIf.gitdir:/github/workspace/repository/.git.path",
        f"/github/runner_temp/{credentials.name}",
    )
    _git(
        repository,
        "config",
        (
            "includeIf.gitdir:/github/workspace/repository/"
            ".git/worktrees/*.path"
        ),
        f"/github/runner_temp/{credentials.name}",
    )
    return credentials


def test_actions_checkout_includeif_authenticates_isolated_transport(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, expected_remote = _private_repository(tmp_path)
    authorization = "Basic dHJhbnNwb3J0OnRlc3Q="
    server = _AuthenticatedGitServer(
        origin,
        authorization=authorization,
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            server.endpoint,
            f"Authorization: {authorization}",
        )
        fetch_remote = resolve_safe_fetch_remote(repository, "origin")
        push_remote = resolve_safe_push_remote(repository, "origin")

        assert fetch_remote is not None
        assert push_remote is not None
        assert remote_revision(
            repository,
            fetch_remote,
            "refs/heads/main",
        ) == expected_remote
        assert fetch_revision(
            repository,
            fetch_remote,
            "refs/heads/main",
        ) == expected_remote
        (repository / "local.txt").write_text("local\n", encoding="utf-8")
        _git(repository, "add", "local.txt")
        _git(repository, "commit", "-m", "local update")
        local_revision = _git(repository, "rev-parse", "HEAD")

        pushed = compare_and_swap_push(
            repository,
            push_remote,
            source_revision=local_revision,
            destination_ref="refs/heads/main",
            expected_revision=expected_remote,
        )

        assert pushed.before == expected_remote
        assert pushed.after == local_revision
        assert pushed.returncode == 0
        assert server.authorized_requests > 0
        assert not any(
            repository.joinpath(".git").glob(
                "foundry-git-global-*.config"
            )
        )
    finally:
        server.close()


def test_header_for_another_host_fails_closed(
    tmp_path: Path,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(origin, authorization=None)
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        _git(
            repository,
            "config",
            "http.https://other.example/.extraheader",
            "Authorization: Basic b3RoZXI=",
        )

        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="scope does not match",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.unexpected_authorization is False
    finally:
        server.close()


def test_unrelated_checkout_header_allows_public_remote_without_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, expected_remote = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(origin, authorization=None)
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            "https://github.com/private/repository.git",
            "Authorization: Basic dHJ1c3RlZA==",
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        assert remote_revision(
            repository,
            remote,
            "refs/heads/main",
        ) == expected_remote
        assert server.unexpected_authorization is False
    finally:
        server.close()


def test_multiple_matching_authorization_values_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic b25l",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            server.endpoint,
            "Authorization: Basic b25l",
            "Authorization: Basic dHdv",
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_matching_and_mismatched_authorization_values_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic bWF0Y2g=",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            server.endpoint,
            "Authorization: Basic bWF0Y2g=",
        )
        _git(
            repository,
            "config",
            "http.https://other.example/.extraheader",
            "Authorization: Basic b3RoZXI=",
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_arbitrary_include_authentication_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic dW50cnVzdGVk",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        outside = tmp_path / "untrusted-auth.config"
        parsed = urlsplit(server.endpoint)
        outside.write_text(
            f'[http "{parsed.scheme}://{parsed.netloc}/"]\n'
            "\textraheader = Authorization: Basic dW50cnVzdGVk\n",
            encoding="utf-8",
        )
        git_dir = (repository / ".git").resolve().as_posix()
        _git(
            repository,
            "config",
            f"includeIf.gitdir:{git_dir}.path",
            str(outside),
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_control_character_authorization_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic Y29udHJvbA==",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        credentials = _checkout_credentials(
            repository,
            runner_temp,
            server.endpoint,
            "Authorization: Basic Y29udHJvbA==",
        )
        content = credentials.read_bytes()
        credentials.write_bytes(
            content.replace(
                b"Basic Y29udHJvbA==",
                b"Basic Y29u\x01HJvbA==",
            )
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


@pytest.mark.parametrize(
    "header",
    (
        "X-Proxy-Authorization: Basic b3RoZXI=",
        f"Authorization: {'x' * 8193}",
    ),
)
def test_non_authorization_or_oversized_header_fails_closed(
    tmp_path: Path,
    monkeypatch,
    header: str,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic bmV2ZXI=",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            server.endpoint,
            header,
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_global_authorization_origin_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic Z2xvYmFs",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        global_config = tmp_path / "global.config"
        parsed = urlsplit(server.endpoint)
        global_config.write_text(
            f'[http "{parsed.scheme}://{parsed.netloc}/"]\n'
            "\textraheader = Authorization: Basic Z2xvYmFs\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="origin is not trusted",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_checkout_credentials_symlink_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, _ = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(
        origin,
        authorization="Basic c3ltbGluaw==",
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        target = tmp_path / "credential-target.config"
        parsed = urlsplit(server.endpoint)
        target.write_text(
            f'[http "{parsed.scheme}://{parsed.netloc}/"]\n'
            "\textraheader = Authorization: Basic c3ltbGluaw==\n",
            encoding="utf-8",
        )
        credentials = (
            runner_temp / f"git-credentials-{uuid4()}.config"
        )
        try:
            credentials.symlink_to(target)
        except OSError:
            pytest.skip("symbolic links are unavailable")
        git_dir = (repository / ".git").resolve().as_posix()
        _git(
            repository,
            "config",
            f"includeIf.gitdir:{git_dir}.path",
            str(credentials),
        )
        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None

        with pytest.raises(
            GitTransportError,
            match="authentication configuration",
        ):
            remote_revision(
                repository,
                remote,
                "refs/heads/main",
            )

        assert server.authorized_requests == 0
    finally:
        server.close()


def test_direct_local_authorization_still_authenticates_exact_remote(
    tmp_path: Path,
) -> None:
    repository, origin, _, expected_remote = _private_repository(tmp_path)
    authorization = "Basic bG9jYWw="
    server = _AuthenticatedGitServer(
        origin,
        authorization=authorization,
    )
    try:
        _git(repository, "remote", "set-url", "origin", server.endpoint)
        parsed = urlsplit(server.endpoint)
        _git(
            repository,
            "config",
            f"http.{parsed.scheme}://{parsed.netloc}/.extraheader",
            f"Authorization: {authorization}",
        )

        remote = resolve_safe_fetch_remote(repository, "origin")
        assert remote is not None
        assert remote_revision(
            repository,
            remote,
            "refs/heads/main",
        ) == expected_remote
        assert server.authorized_requests > 0
    finally:
        server.close()


def test_copilot_proxy_url_does_not_import_github_authorization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository, origin, _, expected_remote = _private_repository(tmp_path)
    server = _AuthenticatedGitServer(origin, authorization=None)
    try:
        runner_temp = tmp_path / "runner-temp"
        monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
        _checkout_credentials(
            repository,
            runner_temp,
            "https://github.com/private/repository.git",
            "Authorization: Basic cHJveHktdG9rZW4=",
        )

        assert isolated_remote_revision(
            repository,
            server.endpoint,
            "refs/heads/main",
        ) == expected_remote
        assert server.unexpected_authorization is False
    finally:
        server.close()
