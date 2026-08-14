from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import ip_address
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterator, Mapping
from urllib.parse import SplitResult, urlsplit
from uuid import uuid4


class GitTransportError(RuntimeError):
    pass


_CHECKOUT_CREDENTIALS = re.compile(
    r"^git-credentials-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}\.config$",
    re.IGNORECASE,
)
_MAX_AUTHORIZATION_HEADER = 8192


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
        "--local",
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
        "config",
        "--local",
        "--get-all",
        f"remote.{remote}.pushurl",
    )
    if completed.returncode not in {0, 1}:
        return None
    push_urls = (
        completed.stdout.decode("utf-8").splitlines()
        if completed.returncode == 0
        else []
    )
    if push_urls not in ([], [raw_url]):
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
    if not _supported_remote_url(raw_url):
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


def list_remote_heads(
    root: Path,
    remote: SafePushRemote,
    patterns: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    if not patterns:
        raise ValueError("at least one remote head pattern is required")
    with _isolated_git_transport(root, remote.url) as (
        transport_git_dir,
        environment,
    ):
        arguments = [
            "git",
            transport_git_dir,
            "ls-remote",
            "--heads",
            remote.url,
            *patterns,
        ]
        completed = _run(
            root,
            *arguments,
            environment=environment,
        )
    if completed.returncode != 0:
        raise GitTransportError("remote head inventory failed")
    try:
        lines = completed.stdout.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise GitTransportError(
            "remote head inventory metadata is invalid"
        ) from error
    result: list[tuple[str, str]] = []
    for line in lines:
        fields = line.split()
        if len(fields) != 2 or not fields[1].startswith("refs/heads/"):
            raise GitTransportError(
                "remote head inventory metadata is invalid"
            )
        result.append((fields[0], fields[1]))
    return tuple(result)


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


def atomic_compare_and_swap_delete(
    root: Path,
    remote: SafePushRemote,
    *,
    refs: Mapping[str, str | None],
    guard_ref: str | None = None,
    guard_revision: str | None = None,
) -> int:
    if not refs:
        raise ValueError("at least one ref is required")
    if (guard_ref is None) != (guard_revision is None):
        raise ValueError(
            "guard_ref and guard_revision must either both be set or both be absent"
        )
    with _isolated_git_transport(root, remote.url) as (
        transport_git_dir,
        environment,
    ):
        arguments = [
            "git",
            transport_git_dir,
            "push",
            "--atomic",
        ]
        if guard_ref is not None and guard_revision is not None:
            arguments.append(
                f"--force-with-lease={guard_ref}:{guard_revision}"
            )
        for ref, revision in sorted(refs.items()):
            arguments.append(
                f"--force-with-lease={ref}:{revision or ''}"
            )
        arguments.append(remote.url)
        if guard_ref is not None and guard_revision is not None:
            arguments.append(f"{guard_revision}:{guard_ref}")
        arguments.extend(f":{ref}" for ref in sorted(refs))
        completed = _run(
            root,
            *arguments,
            environment=environment,
        )
    return completed.returncode


def isolated_compare_and_swap_push(
    root: Path,
    remote_url: str,
    *,
    source_revision: str,
    destination_ref: str,
    expected_revision: str | None,
) -> GitPushResult:
    with _isolated_git_transport(
        root,
        remote_url,
        preserve_http_auth=False,
    ) as (
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
    with _isolated_git_transport(
        root,
        remote_url,
        preserve_http_auth=False,
    ) as (
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
        return _fetch_revision(
            root,
            transport_git_dir,
            environment,
            remote.url,
            ref,
        )


def isolated_fetch_revision(
    root: Path,
    remote_url: str,
    ref: str,
) -> str:
    with _isolated_git_transport(
        root,
        remote_url,
        write_to_source_objects=True,
        preserve_http_auth=False,
    ) as (transport_git_dir, environment):
        return _fetch_revision(
            root,
            transport_git_dir,
            environment,
            remote_url,
            ref,
        )


def _fetch_revision(
    root: Path,
    transport_git_dir: str,
    environment: dict[str, str],
    remote_url: str,
    ref: str,
) -> str:
    fetched = _run(
        root,
        "git",
        transport_git_dir,
        "fetch",
        "--quiet",
        remote_url,
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


@contextmanager
def _isolated_git_transport(
    root: Path,
    remote_url: str,
    *,
    write_to_source_objects: bool = False,
    preserve_http_auth: bool = True,
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
        header = (
            _trusted_http_header(root, remote_url)
            if preserve_http_auth
            else None
        )
        if header is not None:
            environment["GIT_CONFIG_COUNT"] = "1"
            environment["GIT_CONFIG_KEY_0"] = (
                f"http.{remote_url.rstrip('/')}/.extraheader"
            )
            environment["GIT_CONFIG_VALUE_0"] = header
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


def _trusted_http_header(
    root: Path,
    remote_url: str,
) -> str | None:
    parsed = urlsplit(remote_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    records = _http_authentication_records(root)
    candidates = tuple(
        record
        for record in records
        if record[2].casefold().startswith("http.")
        and record[2].casefold().endswith(".extraheader")
    )
    if not candidates:
        return None
    effective = _run(
        root,
        "git",
        "config",
        "-z",
        "--get-urlmatch",
        "http.extraheader",
        remote_url,
    )
    if effective.returncode not in {0, 1}:
        raise GitTransportError(
            "Git authentication configuration could not be read"
        )
    if effective.returncode == 1:
        if len(candidates) != 1:
            raise GitTransportError(
                "Git authentication configuration is ambiguous"
            )
        scope, origin, _, value = candidates[0]
        _validate_authorization_header(value)
        if scope != "local":
            raise GitTransportError(
                "Git authentication configuration origin is not trusted"
            )
        local_config = _local_config_path(root)
        _validate_local_config(local_config)
        origin_path = _config_origin_path(root, origin)
        if _same_path(origin_path, local_config):
            raise GitTransportError(
                "Git authentication configuration scope does not match"
            )
        _validate_checkout_credentials_origin(
            root,
            origin_path,
            records,
        )
        return None
    value = _single_null_terminated_value(
        effective.stdout,
        "Git authentication configuration is malformed",
    )
    _validate_authorization_header(value)
    applicable = tuple(
        record
        for record in candidates
        if _http_extraheader_matches_url(root, record[2], remote_url)
    )
    if (
        len(candidates) != 1
        or len(applicable) != 1
        or applicable[0][3] != value
    ):
        raise GitTransportError(
            "Git authentication configuration is ambiguous"
        )
    scope, origin, _, _ = applicable[0]
    if scope != "local":
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )
    local_config = _local_config_path(root)
    _validate_local_config(local_config)
    origin_path = _config_origin_path(root, origin)
    if not _same_path(origin_path, local_config):
        _validate_checkout_credentials_origin(
            root,
            origin_path,
            records,
        )
    _validate_authorization_destination(parsed)
    return value


def _http_authentication_records(
    root: Path,
) -> tuple[tuple[str, str, str, str], ...]:
    completed = _run(
        root,
        "git",
        "config",
        "-z",
        "--show-origin",
        "--show-scope",
        "--includes",
        "--get-regexp",
        r"^(include\.path|includeif\..*\.path|http\..*\.extraheader)$",
    )
    if completed.returncode not in {0, 1}:
        raise GitTransportError(
            "Git authentication configuration could not be read"
        )
    if completed.returncode == 1:
        return ()
    try:
        fields = completed.stdout.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        raise GitTransportError(
            "Git authentication configuration is malformed"
        ) from error
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3 != 0:
        raise GitTransportError(
            "Git authentication configuration is malformed"
        )
    records: list[tuple[str, str, str, str]] = []
    for offset in range(0, len(fields), 3):
        scope, origin, entry = fields[offset : offset + 3]
        key, separator, value = entry.partition("\n")
        if not separator or not scope or not origin or not key:
            raise GitTransportError(
                "Git authentication configuration is malformed"
            )
        records.append((scope, origin, key, value))
    return tuple(records)


def _validate_checkout_credentials_origin(
    root: Path,
    origin_path: Path,
    records: tuple[tuple[str, str, str, str], ...],
) -> None:
    runner_temp_value = os.environ.get("RUNNER_TEMP")
    if not runner_temp_value:
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )
    runner_temp = Path(runner_temp_value)
    if not runner_temp.is_absolute():
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )
    try:
        if runner_temp.is_symlink():
            raise GitTransportError(
                "Git authentication configuration origin is not trusted"
            )
        runner_temp = runner_temp.resolve(strict=True)
        resolved_origin = origin_path.resolve(strict=True)
    except OSError as error:
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        ) from error
    if (
        origin_path.is_symlink()
        or not resolved_origin.is_file()
        or not resolved_origin.is_relative_to(runner_temp)
        or resolved_origin.parent != runner_temp
        or not _CHECKOUT_CREDENTIALS.fullmatch(resolved_origin.name)
    ):
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )
    local_config = _local_config_path(root)
    includes = tuple(
        record
        for record in records
        if record[0] == "local"
        and _same_path(_config_origin_path(root, record[1]), local_config)
        and record[2].casefold().startswith("includeif.gitdir:")
        and record[2].casefold().endswith(".path")
        and _trusted_checkout_include_key(root, record[2])
        and _same_path(
            _config_value_path(root, record[3]),
            resolved_origin,
        )
    )
    if len(includes) != 1:
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )


def _config_origin_path(root: Path, origin: str) -> Path:
    if not origin.startswith("file:"):
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        )
    return _config_value_path(root, origin.removeprefix("file:"))


def _config_value_path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _local_config_path(root: Path) -> Path:
    common_dir = Path(
        _git_text(root, "rev-parse", "--git-common-dir")
    )
    if not common_dir.is_absolute():
        common_dir = root / common_dir
    return common_dir / "config"


def _validate_local_config(local_config: Path) -> None:
    try:
        if local_config.is_symlink() or not local_config.is_file():
            raise GitTransportError(
                "Git authentication configuration origin is not trusted"
            )
    except OSError as error:
        raise GitTransportError(
            "Git authentication configuration origin is not trusted"
        ) from error


def _trusted_checkout_include_key(root: Path, key: str) -> bool:
    prefix = "includeif.gitdir:"
    suffix = ".path"
    lowered = key.casefold()
    if not lowered.startswith(prefix) or not lowered.endswith(suffix):
        return False
    condition = key[len(prefix) : -len(suffix)].replace("\\", "/")
    git_dir = Path(
        _git_text(root, "rev-parse", "--absolute-git-dir")
    ).resolve(strict=False).as_posix()
    common_dir = _local_config_path(root).parent.resolve(
        strict=False
    ).as_posix()
    normalized_condition = os.path.normcase(condition.rstrip("/"))
    if normalized_condition == os.path.normcase(git_dir.rstrip("/")):
        return True
    worktrees = f"{common_dir}/worktrees/"
    return (
        os.path.normcase(git_dir).startswith(os.path.normcase(worktrees))
        and normalized_condition
        == os.path.normcase(f"{worktrees}*".rstrip("/"))
    )


def _validate_authorization_header(value: str) -> None:
    prefix, separator, credential = value.partition(":")
    if (
        not separator
        or prefix.casefold() != "authorization"
        or not credential.strip()
        or len(value) > _MAX_AUTHORIZATION_HEADER
        or any(
            ord(character) < 32 or ord(character) > 126
            for character in value
        )
    ):
        raise GitTransportError(
            "Git authentication configuration is invalid"
        )


def _validate_authorization_destination(parsed: SplitResult) -> None:
    if parsed.scheme != "http":
        return
    try:
        address = ip_address(parsed.hostname or "")
    except ValueError as error:
        raise GitTransportError(
            "Git authentication cannot use cleartext HTTP"
        ) from error
    if not address.is_loopback:
        raise GitTransportError(
            "Git authentication cannot use cleartext HTTP"
        )


def _http_extraheader_matches_url(
    root: Path,
    key: str,
    remote_url: str,
) -> bool:
    lowered = key.casefold()
    prefix = "http."
    suffix = ".extraheader"
    if not lowered.startswith(prefix) or not lowered.endswith(suffix):
        return False
    scope = key[len(prefix) : -len(suffix)]
    probe = _run_urlmatch(root, scope, remote_url)
    return probe == scope


def _run_urlmatch(
    root: Path,
    scope: str,
    remote_url: str,
) -> str | None:
    common_dir = _local_config_path(root).parent
    probe = common_dir / (
        f"foundry-git-urlmatch-{os.getpid()}-{uuid4().hex}.config"
    )
    try:
        probe.write_text("", encoding="utf-8")
        configured = _run(
            root,
            "git",
            "config",
            "--file",
            str(probe),
            f"http.{scope}.extraheader",
            scope,
        )
        if configured.returncode != 0:
            raise GitTransportError(
                "Git authentication configuration scope is invalid"
            )
        completed = _run(
            root,
            "git",
            "config",
            "--file",
            str(probe),
            "-z",
            "--get-urlmatch",
            "http.extraheader",
            remote_url,
        )
        if completed.returncode == 1:
            return None
        if completed.returncode != 0:
            raise GitTransportError(
                "Git authentication configuration could not be matched"
            )
        return _single_null_terminated_value(
            completed.stdout,
            "Git authentication configuration is malformed",
        )
    finally:
        probe.unlink(missing_ok=True)


def _single_null_terminated_value(
    output: bytes,
    error_message: str,
) -> str:
    try:
        fields = output.decode("utf-8").split("\0")
    except UnicodeDecodeError as error:
        raise GitTransportError(error_message) from error
    if len(fields) != 2 or fields[1] != "":
        raise GitTransportError(error_message)
    return fields[0]


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
