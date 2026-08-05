from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterator
from urllib.parse import urlsplit
from uuid import uuid4


class GitTransportError(RuntimeError):
    pass


@dataclass(frozen=True)
class SafePushRemote:
    name: str
    url: str
    isolated: bool


@dataclass(frozen=True)
class GitPushResult:
    before: str | None
    after: str | None
    returncode: int | None


def configured_remote_url(root: Path, remote: str) -> str | None:
    completed = _run(
        root,
        "git",
        "config",
        "--get-all",
        f"remote.{remote}.url",
    )
    if completed.returncode != 0:
        return None
    values = completed.stdout.decode("utf-8").splitlines()
    return values[0] if len(values) == 1 else None


def resolve_safe_push_remote(
    root: Path,
    remote: str,
) -> SafePushRemote | None:
    raw_url = configured_remote_url(root, remote)
    if raw_url is None:
        return None
    completed = _run(
        root,
        "git",
        "remote",
        "get-url",
        "--push",
        "--all",
        remote,
    )
    if completed.returncode != 0:
        return None
    push_urls = completed.stdout.decode("utf-8").splitlines()
    if push_urls != [raw_url]:
        return None
    if not _supported_remote_url(raw_url):
        return None
    return SafePushRemote(
        name=remote,
        url=raw_url,
        isolated=True,
    )


def resolve_safe_fetch_remote(
    root: Path,
    remote: str,
) -> SafePushRemote | None:
    raw_url = configured_remote_url(root, remote)
    if raw_url is None:
        return None
    completed = _run(
        root,
        "git",
        "remote",
        "get-url",
        "--all",
        remote,
    )
    if completed.returncode != 0:
        return None
    fetch_urls = completed.stdout.decode("utf-8").splitlines()
    if fetch_urls != [raw_url] or not _supported_remote_url(raw_url):
        return None
    return SafePushRemote(
        name=remote,
        url=raw_url,
        isolated=True,
    )


def remote_revision(
    root: Path,
    remote: SafePushRemote,
    ref: str,
) -> str | None:
    with _isolated_git_transport(root, remote.url) as (
        transport_git_dir,
        environment,
    ):
        return _remote_revision(
            root,
            transport_git_dir,
            environment,
            remote.url,
            ref,
        )


def compare_and_swap_push(
    root: Path,
    remote: SafePushRemote,
    *,
    source_revision: str,
    destination_ref: str,
    expected_revision: str | None,
) -> GitPushResult:
    with _isolated_git_transport(root, remote.url) as (
        transport_git_dir,
        environment,
    ):
        return _compare_and_swap_push(
            root,
            transport_git_dir,
            environment,
            remote.url,
            source_revision=source_revision,
            destination_ref=destination_ref,
            expected_revision=expected_revision,
        )


def isolated_compare_and_swap_push(
    root: Path,
    remote_url: str,
    *,
    source_revision: str,
    destination_ref: str,
    expected_revision: str | None,
) -> GitPushResult:
    with _isolated_git_transport(root, remote_url) as (
        transport_git_dir,
        environment,
    ):
        return _compare_and_swap_push(
            root,
            transport_git_dir,
            environment,
            remote_url,
            source_revision=source_revision,
            destination_ref=destination_ref,
            expected_revision=expected_revision,
        )


def isolated_remote_revision(
    root: Path,
    remote_url: str,
    ref: str,
) -> str | None:
    with _isolated_git_transport(root, remote_url) as (
        transport_git_dir,
        environment,
    ):
        return _remote_revision(
            root,
            transport_git_dir,
            environment,
            remote_url,
            ref,
        )


def _compare_and_swap_push(
    root: Path,
    transport_git_dir: str | None,
    environment: dict[str, str] | None,
    remote: str,
    *,
    source_revision: str,
    destination_ref: str,
    expected_revision: str | None,
) -> GitPushResult:
    before = _remote_revision(
        root,
        transport_git_dir,
        environment,
        remote,
        destination_ref,
    )
    if before != expected_revision:
        return GitPushResult(before, before, None)
    lease = (
        f"--force-with-lease={destination_ref}:"
        f"{expected_revision or ''}"
    )
    arguments = ["git"]
    if transport_git_dir is not None:
        arguments.append(transport_git_dir)
    arguments.extend(
        (
            "push",
            lease,
            remote,
            f"{source_revision}:{destination_ref}",
        )
    )
    pushed = _run(
        root,
        *arguments,
        environment=environment,
    )
    if pushed.returncode != 0:
        return GitPushResult(
            before,
            before,
            pushed.returncode,
        )
    after = _remote_revision(
        root,
        transport_git_dir,
        environment,
        remote,
        destination_ref,
    )
    return GitPushResult(before, after, pushed.returncode)


def _remote_revision(
    root: Path,
    transport_git_dir: str | None,
    environment: dict[str, str] | None,
    remote: str,
    ref: str,
) -> str | None:
    arguments = ["git"]
    if transport_git_dir is not None:
        arguments.append(transport_git_dir)
    arguments.extend(("ls-remote", "--heads", remote, ref))
    completed = _run(
        root,
        *arguments,
        environment=environment,
    )
    if completed.returncode != 0:
        raise GitTransportError("remote revision query failed")
    output = completed.stdout.decode("utf-8").strip()
    if not output:
        return None
    fields = output.split()
    if len(fields) != 2 or fields[1] != ref:
        raise GitTransportError("remote revision metadata is invalid")
    return fields[0]


def fetch_revision(
    root: Path,
    remote: SafePushRemote,
    ref: str,
) -> str:
    with _isolated_git_transport(
        root,
        remote.url,
        write_to_source_objects=True,
    ) as (transport_git_dir, environment):
        fetched = _run(
            root,
            "git",
            transport_git_dir,
            "fetch",
            "--quiet",
            remote.url,
            ref,
            environment=environment,
        )
        if fetched.returncode != 0:
            raise GitTransportError("isolated Git fetch failed")
        revision = _run(
            root,
            "git",
            transport_git_dir,
            "rev-parse",
            "FETCH_HEAD^{commit}",
            environment=environment,
        )
        if revision.returncode != 0:
            raise GitTransportError(
                "isolated fetched revision is unavailable"
            )
        return revision.stdout.decode("ascii").strip()


def isolated_fetch_revision(
    root: Path,
    remote_url: str,
    ref: str,
) -> str:
    return fetch_revision(
        root,
        SafePushRemote("isolated", remote_url, True),
        ref,
    )


@contextmanager
def _isolated_git_transport(
    root: Path,
    remote_url: str,
    *,
    write_to_source_objects: bool = False,
) -> Iterator[tuple[str, dict[str, str]]]:
    common_dir = Path(_git_text(root, "rev-parse", "--git-common-dir"))
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    source_objects = Path(
        _git_text(root, "rev-parse", "--git-path", "objects")
    )
    if not source_objects.is_absolute():
        source_objects = root / source_objects
    identifier = f"{os.getpid()}-{uuid4().hex}"
    transport = common_dir / f"foundry-git-transport-{identifier}"
    global_config = common_dir / f"foundry-git-global-{identifier}.config"
    global_config.write_text("", encoding="utf-8")
    environment = {
        "_FOUNDRY_OPT_CLEAN_GIT_ENV": "1",
        "ALL_PROXY": "",
        "GIT_CONFIG_COUNT": "0",
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_PARAMETERS": "",
        "GIT_TERMINAL_PROMPT": "0",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "NO_PROXY": "*",
        "all_proxy": "",
        "http_proxy": "",
        "https_proxy": "",
        "no_proxy": "*",
    }
    try:
        for key, value in _trusted_http_headers(root, remote_url):
            configured = _run(
                root,
                "git",
                "config",
                "--file",
                str(global_config),
                "--add",
                key,
                value,
                environment=environment,
            )
            if configured.returncode != 0:
                raise GitTransportError(
                    "isolated Git authentication configuration failed"
                )
        initialized = _run(
            root,
            "git",
            "init",
            "--bare",
            str(transport),
            environment=environment,
        )
        if initialized.returncode != 0:
            raise GitTransportError(
                "isolated Git transport initialization failed"
            )
        environment.update(
            {
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(
                    source_objects.resolve()
                ),
                "GIT_OBJECT_DIRECTORY": str(
                    (
                        source_objects
                        if write_to_source_objects
                        else transport / "objects"
                    ).resolve()
                ),
            }
        )
        yield f"--git-dir={transport}", environment
    finally:
        shutil.rmtree(transport, ignore_errors=True)
        global_config.unlink(missing_ok=True)


def _supported_remote_url(value: str) -> bool:
    if Path(value).is_absolute():
        return True
    parsed = urlsplit(value)
    return parsed.scheme in {"file", "http", "https"}


def _trusted_http_headers(
    root: Path,
    remote_url: str,
) -> tuple[tuple[str, str], ...]:
    parsed = urlsplit(remote_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ()
    completed = _run(
        root,
        "git",
        "config",
        "--local",
        "--get-regexp",
        r"^http\..*\.extraheader$",
    )
    if completed.returncode not in {0, 1}:
        raise GitTransportError(
            "Git authentication configuration could not be read"
        )
    allowed_keys = {
        f"http.{parsed.scheme}://{parsed.netloc}/.extraheader",
        f"http.{remote_url.rstrip('/')}/.extraheader",
    }
    headers: list[tuple[str, str]] = []
    for line in completed.stdout.decode("utf-8").splitlines():
        key, separator, value = line.partition(" ")
        if (
            separator != " "
            or key not in allowed_keys
            or len(value) > 8192
            or any(ord(character) < 32 for character in value)
            or not value.casefold().startswith("authorization:")
        ):
            continue
        headers.append((key, value))
    return tuple(headers)


def _git_text(root: Path, *arguments: str) -> str:
    completed = _run(root, "git", *arguments)
    if completed.returncode != 0:
        raise GitTransportError("Git transport discovery failed")
    return completed.stdout.decode("utf-8").strip()


def _run(
    root: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    process_environment = os.environ.copy()
    clean_git_environment = (
        environment is not None
        and environment.get("_FOUNDRY_OPT_CLEAN_GIT_ENV") == "1"
    )
    if clean_git_environment:
        for name in tuple(process_environment):
            if name.startswith("GIT_") or name in {
                "SSH_ASKPASS",
                "SSH_AUTH_SOCK",
            }:
                process_environment.pop(name, None)
    if environment:
        process_environment.update(
            {
                name: value
                for name, value in environment.items()
                if name != "_FOUNDRY_OPT_CLEAN_GIT_ENV"
            }
        )
    return subprocess.run(
        arguments,
        cwd=root,
        env=process_environment,
        capture_output=True,
        check=False,
    )
