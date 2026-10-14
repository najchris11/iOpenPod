"""Owned temporary media directories and liveness of their tool processes."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from storage._filesystem import is_link_or_reparse
from storage._writer_lock import _lock_file, _unlock_file
from storage.errors import DeviceBusyError
from storage.host_tools import host_tool_environment

if TYPE_CHECKING:
    from collections.abc import Generator

_MAGIC = b"\0iOpenPod media workspace v1\n"
_MARKER = ".iopenpod-owner.lock"
_GUARD = threading.Lock()
_LIVE: dict[Path, BinaryIO] = {}
_CHILDREN: dict[subprocess.Popen[bytes], tuple[Path, ...]] = {}
logger = logging.getLogger(__name__)


@contextmanager
def managed_media_directory() -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix="iopenpod-media-") as name:
        directory = Path(name).resolve(strict=True)
        with (directory / _MARKER).open("x+b", buffering=0) as lease:
            lease.write(b"\0")
            _lock_file(lease, "media workspace")
            lease.seek(0)
            lease.write(_MAGIC)
            with _GUARD:
                _LIVE[directory] = lease
            try:
                yield directory
            finally:
                with _GUARD:
                    del _LIVE[directory]
                # POSIX flock ownership follows the open file description. Do
                # not explicitly unlock: a surviving child must retain its lease.
                if os.name == "nt":
                    _unlock_file(lease)


def spawn_media_process(
    args: list[str],
    *,
    creationflags: int = 0,
    inherited_fds: tuple[int, ...] | None = None,
    workspace_paths: tuple[str, ...] = (),
) -> subprocess.Popen[bytes]:
    paths = (*args, *workspace_paths)
    with _GUARD:
        workspaces = tuple(
            (path, lease)
            for path, lease in _LIVE.items()
            if any(arg.startswith(str(path) + os.sep) for arg in paths)
        )
        lease_fds = (
            tuple(lease.fileno() for _, lease in workspaces) if os.name != "nt" else ()
        )
        markers = (
            tuple(path / f".child-{uuid.uuid4().hex}" for path, _ in workspaces)
            if os.name == "nt"
            else ()
        )
        for marker in markers:
            marker.write_bytes(b"starting")
        try:
            if inherited_fds is None and not lease_fds:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    env=host_tool_environment(),
                )
            else:
                process = subprocess.Popen(
                    args,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=creationflags,
                    pass_fds=(*(inherited_fds or ()), *lease_fds),
                    env=host_tool_environment(),
                )
        except BaseException:
            for marker in markers:
                marker.unlink(missing_ok=True)
            raise
        try:
            for marker in markers:
                marker.write_text(str(process.pid), encoding="ascii")
        except BaseException:
            process.kill()
            process.wait()
            for marker in markers:
                marker.unlink(missing_ok=True)
            raise
        _CHILDREN[process] = markers
        return process


def reap_media_process(process: subprocess.Popen[bytes]) -> None:
    process.wait()
    with _GUARD:
        for marker in _CHILDREN.pop(process, ()):
            marker.unlink(missing_ok=True)


def _private_directory(path: Path) -> bool:
    metadata = path.lstat()
    if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        return False
    if os.name == "nt":
        # Automatic Windows cleanup is restricted to the user's private tree;
        # customized shared TEMP roots are deliberately left for manual cleanup.
        return path.is_relative_to(Path.home().resolve())
    return metadata.st_uid == os.getuid() and metadata.st_mode & 0o077 == 0


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return True
    if os.name == "nt":
        from storage.platform.workspace_process_windows import process_alive

        return process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _children_in_use(directory: Path) -> bool:
    for marker in directory.glob(".child-*"):
        metadata = marker.lstat()
        if (
            is_link_or_reparse(metadata)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > 32
        ):
            return True
        try:
            pid = int(marker.read_text(encoding="ascii"))
        except (ValueError, UnicodeError):
            return True
        if _process_alive(pid):
            return True
    return False


def _legacy_quiescent(
    candidates: tuple[Path, ...], *, proc: Path = Path("/proc")
) -> bool:
    if sys.platform != "linux":
        return False
    try:
        for entry in proc.iterdir():
            if (
                not entry.name.isdecimal()
                or int(entry.name) == os.getpid()
                or entry.stat().st_uid != os.getuid()
            ):
                continue
            try:
                arguments = (
                    (entry / "cmdline").read_bytes().decode(errors="replace").casefold()
                )
                executable = str((entry / "exe").readlink()).casefold()
                mappings = (entry / "maps").read_text(errors="replace").casefold()
                if any(
                    word in arguments + executable + mappings
                    for word in (
                        "python",
                        "pyside",
                        "iopenpod",
                        "ffmpeg",
                        "ffprobe",
                        "fpcalc",
                    )
                ):
                    return False
                if any(str(path).casefold() in arguments for path in candidates):
                    return False
                for fd in (entry / "fd").iterdir():
                    target = str(fd.readlink())
                    if any(
                        target.startswith(str(path) + os.sep) for path in candidates
                    ):
                        return False
                if any(str(path).casefold() in arguments for path in candidates):
                    return False
            except FileNotFoundError:
                # The observed process exited. Newly created workspaces were not
                # in our initial candidate snapshot and cannot be reclaimed.
                if entry.exists():
                    return False
                continue
    except OSError:
        return False
    return True


def _legacy_layout(directory: Path) -> bool:
    entries = tuple(directory.iterdir())
    return bool(entries) and all(
        (
            entry.name == "source"
            or re.fullmatch(r"processed\.[a-z0-9]{1,8}", entry.name)
        )
        and not is_link_or_reparse(entry.lstat())
        and stat.S_ISREG(entry.lstat().st_mode)
        and entry.lstat().st_uid == os.getuid()
        for entry in entries
    )


def cleanup_media_workspaces(directory: Path | None = None) -> tuple[Path, ...]:
    """Best-effort startup reclamation; ambiguous ownership/liveness stays intact."""
    root = (directory or Path(tempfile.gettempdir())).resolve(strict=True)
    removed: list[Path] = []
    legacy: list[Path] = []
    legacy_identities: dict[Path, tuple[int, int, int]] = {}
    for path in root.glob("iopenpod-media-*"):
        try:
            if not _private_directory(path):
                continue
            before = path.stat()
            marker = path / _MARKER
            if not marker.exists():
                if os.name != "nt" and _legacy_layout(path):
                    legacy.append(path)
                    legacy_identities[path] = (
                        before.st_dev,
                        before.st_ino,
                        before.st_ctime_ns,
                    )
                continue
            marker_metadata = marker.lstat()
            if is_link_or_reparse(marker_metadata) or not stat.S_ISREG(
                marker_metadata.st_mode
            ):
                continue
            if os.name != "nt" and marker_metadata.st_uid != os.getuid():
                continue
            descriptor = os.open(marker, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "r+b", buffering=0) as lease:
                metadata = os.fstat(lease.fileno())
                if not stat.S_ISREG(metadata.st_mode) or is_link_or_reparse(metadata):
                    continue
                _lock_file(lease, "media workspace")
                lease.seek(0)
                if lease.read(len(_MAGIC) + 1) != _MAGIC or _children_in_use(path):
                    continue
                after = path.lstat()
                if is_link_or_reparse(after) or (before.st_dev, before.st_ino) != (
                    after.st_dev,
                    after.st_ino,
                ):
                    continue
                if os.name == "nt":
                    # Windows cannot remove the open lease file. Child evidence
                    # has been checked; no creator ever reuses this random name.
                    _unlock_file(lease)
                    lease.close()
                shutil.rmtree(path)
                removed.append(path)
        except DeviceBusyError:
            continue
        except OSError as error:
            logger.debug(
                "Media workspace cleanup deferred for %s: %s", path.name, error
            )
    if legacy and _legacy_quiescent(tuple(legacy)):
        for path in legacy:
            with suppress(OSError):
                metadata = path.lstat()
                if (
                    (metadata.st_dev, metadata.st_ino, metadata.st_ctime_ns)
                    == legacy_identities[path]
                    and _private_directory(path)
                    and _legacy_layout(path)
                ):
                    shutil.rmtree(path)
                    removed.append(path)
    return tuple(removed)
