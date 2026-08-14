"""Deterministic descriptor-relative WAV discovery without following links."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sampleproof.filesystem import (
    directory_fd_matches_path,
    open_child_directory,
    open_directory_nofollow,
)


class DiscoveryError(ValueError):
    """Raised when a source root cannot be scanned safely."""


@dataclass(frozen=True)
class DiscoveredFile:
    path: Path
    relative_path: str


def _display_error(relative_directory: PurePosixPath, name: str | None = None) -> str:
    path = relative_directory / name if name is not None else relative_directory
    rendered = path.as_posix()
    return "." if rendered in {"", "."} else rendered


def _discovery_error(display: str, error: OSError) -> DiscoveryError:
    detail = error.strerror or str(error)
    return DiscoveryError(f"cannot read source subtree {display}: {detail}")


def open_source_root(source_root: str | Path) -> tuple[Path, int]:
    """Open one real source root component-by-component and return its stable handle."""

    root = Path(source_root).absolute()
    try:
        descriptor = open_directory_nofollow(root)
    except OSError as error:
        detail = error.strerror or str(error)
        raise DiscoveryError(
            f"source root is not a safely traversable real directory: {root}: {detail}"
        ) from error
    return root, descriptor


def _discover_directory(
    root: Path,
    directory_fd: int,
    relative_directory: PurePosixPath,
    discovered: list[DiscoveredFile],
) -> None:
    try:
        with os.scandir(directory_fd) as iterator:
            names = sorted(
                (entry.name for entry in iterator),
                key=lambda name: (name.casefold(), name),
            )
    except OSError as error:
        raise _discovery_error(_display_error(relative_directory), error) from error

    for name in names:
        display = _display_error(relative_directory, name)
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = open_child_directory(directory_fd, name)
                try:
                    _discover_directory(
                        root,
                        child_fd,
                        relative_directory / name,
                        discovered,
                    )
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                continue
        except OSError as error:
            raise _discovery_error(display, error) from error

        if Path(name).suffix.casefold() != ".wav":
            continue
        relative_path = (relative_directory / name).as_posix()
        discovered.append(
            DiscoveredFile(
                path=root / Path(*PurePosixPath(relative_path).parts),
                relative_path=relative_path,
            )
        )


def discover_wav_files(
    source_root: str | Path, *, root_fd: int | None = None
) -> tuple[DiscoveredFile, ...]:
    """Return regular WAV paths below a fixed real root in stable path order."""

    root = Path(source_root).absolute()
    if root_fd is None:
        root, descriptor = open_source_root(root)
    else:
        try:
            descriptor = os.dup(root_fd)
        except OSError as error:
            raise _discovery_error(".", error) from error
        try:
            opened = os.fstat(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise _discovery_error(".", error) from error
        if not stat.S_ISDIR(opened.st_mode) or not directory_fd_matches_path(descriptor, root):
            os.close(descriptor)
            raise DiscoveryError("source root changed identity before discovery")

    try:
        discovered: list[DiscoveredFile] = []
        try:
            _discover_directory(root, descriptor, PurePosixPath(), discovered)
        except RecursionError as error:
            raise DiscoveryError("source tree exceeds the safe recursion depth") from error
        discovered.sort(key=lambda item: (item.relative_path.casefold(), item.relative_path))
        return tuple(discovered)
    finally:
        os.close(descriptor)
