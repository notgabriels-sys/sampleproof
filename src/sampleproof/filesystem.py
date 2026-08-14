"""Descriptor-relative POSIX traversal primitives that never follow path links."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path, PurePosixPath


def _safe_platform_available() -> bool:
    required_dir_fd_functions = (os.open, os.mkdir, os.unlink, os.rmdir, os.stat)
    return not (
        os.name != "posix"
        or not hasattr(os, "O_NOFOLLOW")
        or any(function not in os.supports_dir_fd for function in required_dir_fd_functions)
        or os.scandir not in os.supports_fd
    )


def require_safe_platform() -> None:
    """Fail closed where the standard library cannot provide the required traversal contract."""

    if not _safe_platform_available():
        raise OSError(
            errno.ENOTSUP,
            "sampleproof v0.1 safe traversal is available only on POSIX platforms",
        )


def _directory_flags() -> int:
    return (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _regular_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)


def _validate_component(component: str) -> None:
    if component in {"", ".", ".."} or "/" in component or "\x00" in component:
        raise OSError(errno.EINVAL, "unsafe path component")


def open_directory_nofollow(path: Path) -> int:
    """Open every component of an existing absolute/relative directory without links."""

    require_safe_platform()
    absolute = Path(path).absolute()
    parts = absolute.parts
    descriptor = os.open(absolute.anchor, _directory_flags())
    try:
        for component in parts[1:]:
            _validate_component(component)
            child = os.open(component, _directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.ENOTDIR, "path is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_child_directory(parent_fd: int, name: str) -> int:
    """Open one real directory immediately below an already verified directory handle."""

    require_safe_platform()
    _validate_component(name)
    descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    except BaseException:
        os.close(descriptor)
        raise
    if not is_directory:
        os.close(descriptor)
        raise OSError(errno.ENOTDIR, "child path is not a directory")
    return descriptor


def open_regular_beneath(root_fd: int, relative_path: str) -> int:
    """Open a regular file beneath a fixed root, rejecting links in every component."""

    require_safe_platform()
    path = PurePosixPath(relative_path)
    if path.is_absolute() or not path.parts:
        raise OSError(errno.EINVAL, "file path must be relative")
    for component in path.parts:
        _validate_component(component)

    directory_fd = os.dup(root_fd)
    try:
        for component in path.parts[:-1]:
            child = open_child_directory(directory_fd, component)
            os.close(directory_fd)
            directory_fd = child
        descriptor = os.open(path.parts[-1], _regular_flags(), dir_fd=directory_fd)
        try:
            is_regular = stat.S_ISREG(os.fstat(descriptor).st_mode)
        except BaseException:
            os.close(descriptor)
            raise
        if not is_regular:
            os.close(descriptor)
            raise OSError(errno.EINVAL, "file path is not a regular file")
        return descriptor
    finally:
        os.close(directory_fd)


def directory_fd_matches_path(descriptor: int, path: Path) -> bool:
    """Check whether a visible path still names the already-open real directory."""

    try:
        visible_descriptor = open_directory_nofollow(path)
    except OSError:
        return False
    try:
        visible = os.fstat(visible_descriptor)
        opened = os.fstat(descriptor)
        return stat.S_ISDIR(visible.st_mode) and (visible.st_dev, visible.st_ino) == (
            opened.st_dev,
            opened.st_ino,
        )
    finally:
        os.close(visible_descriptor)
