"""Atomic publication of a new, non-overwriting report directory."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import sys
from contextlib import suppress
from pathlib import Path

from sampleproof.filesystem import (
    directory_fd_matches_path,
    open_child_directory,
    open_directory_nofollow,
)
from sampleproof.report import render_json, render_manifest, render_markdown
from sampleproof.scan import ScanResult


class PacketError(RuntimeError):
    """Raised when a report packet cannot be published safely."""


REPORT_NAMES = (
    "sampleproof-manifest.jsonl",
    "sampleproof-report.json",
    "sampleproof-report.md",
)


def _has_symlink_component(path: Path) -> bool:
    """Check the unresolved absolute path so resolving cannot hide a linked parent."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            return True
    return False


def _write_text_at(directory_fd: int, name: str, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n", closefd=True) as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _raise_rename_error(destination: Path) -> None:
    error_number = ctypes.get_errno()
    raise OSError(error_number, os.strerror(error_number), destination)


def _rename_noreplace_at(
    parent_fd: int, source_name: str, destination_name: str, destination: Path
) -> None:
    """Atomically rename a same-parent entry by descriptor without replacing a target."""

    if sys.platform == "darwin":
        renameatx_np = getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)
        if renameatx_np is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable", destination)
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        rename_excl = 0x00000004
        if (
            renameatx_np(
                parent_fd,
                os.fsencode(source_name),
                parent_fd,
                os.fsencode(destination_name),
                rename_excl,
            )
            != 0
        ):
            _raise_rename_error(destination)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable", destination)
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        rename_noreplace = 1
        if (
            renameat2(
                parent_fd,
                os.fsencode(source_name),
                parent_fd,
                os.fsencode(destination_name),
                rename_noreplace,
            )
            != 0
        ):
            _raise_rename_error(destination)
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable", destination)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename a same-parent directory only when the target is absent."""

    source = Path(os.path.abspath(os.fspath(source)))
    destination = Path(os.path.abspath(os.fspath(destination)))
    if source.parent != destination.parent or not source.name or not destination.name:
        raise OSError(errno.EINVAL, "source and destination must name same-parent entries")
    parent_fd = open_directory_nofollow(source.parent)
    try:
        if not directory_fd_matches_path(parent_fd, source.parent):
            raise OSError(errno.ESTALE, "publication parent changed identity", destination)
        _rename_noreplace_at(parent_fd, source.name, destination.name, destination)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _create_staging_directory(parent_fd: int, destination_name: str) -> tuple[str, int]:
    prefix = destination_name[:80]
    for _ in range(128):
        name = f".{prefix}.sampleproof-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            return name, open_child_directory(parent_fd, name)
        except BaseException:
            os.rmdir(name, dir_fd=parent_fd)
            raise
    raise OSError(errno.EEXIST, "could not allocate a unique staging directory")


def _cleanup_directory(parent_fd: int, name: str, directory_fd: int) -> None:
    for report_name in REPORT_NAMES:
        with suppress(FileNotFoundError):
            os.unlink(report_name, dir_fd=directory_fd)
    os.fsync(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    os.fsync(parent_fd)


def build_packet(result: ScanResult, output: str | Path) -> tuple[Path, ...]:
    """Build three reports in staging, then publish a previously absent directory."""

    requested = Path(os.path.abspath(os.fspath(output)))
    if requested.name in {"", ".", ".."}:
        raise PacketError("output must name a new directory")
    requested_parent = requested.parent
    if _has_symlink_component(requested_parent):
        raise PacketError(f"output parent contains a symbolic link: {requested_parent}")
    parent = requested_parent
    destination = parent / requested.name
    source = Path(os.path.abspath(os.fspath(result.source_root)))
    if destination == source or destination.is_relative_to(source):
        raise PacketError("output must be outside the scanned source root")

    try:
        parent_fd = open_directory_nofollow(parent)
    except OSError as exc:
        raise PacketError(
            f"output parent is not a safely traversable real directory: {parent}: "
            f"{exc.strerror or exc}"
        ) from exc

    staging_name: str | None = None
    staging_fd: int | None = None
    published = False
    try:
        if not directory_fd_matches_path(parent_fd, parent):
            raise PacketError("output parent changed identity before staging")
        if _entry_exists(parent_fd, destination.name):
            raise PacketError(f"output already exists and will not be overwritten: {destination}")

        staging_name, staging_fd = _create_staging_directory(parent_fd, destination.name)
        _write_text_at(staging_fd, "sampleproof-report.json", render_json(result))
        _write_text_at(staging_fd, "sampleproof-report.md", render_markdown(result))
        _write_text_at(staging_fd, "sampleproof-manifest.jsonl", render_manifest(result))
        os.fsync(staging_fd)

        if not directory_fd_matches_path(parent_fd, parent):
            raise PacketError("output parent changed identity before publication")
        _rename_noreplace_at(parent_fd, staging_name, destination.name, destination)
        published = True
        os.fsync(parent_fd)
        if not directory_fd_matches_path(parent_fd, parent):
            raise PacketError("output parent changed identity during publication")
    except BaseException as exc:
        cleanup_error: OSError | None = None
        if staging_name is not None and staging_fd is not None:
            try:
                _cleanup_directory(
                    parent_fd,
                    destination.name if published else staging_name,
                    staging_fd,
                )
            except OSError as error:
                cleanup_error = error
        if staging_fd is not None:
            os.close(staging_fd)
            staging_fd = None
        os.close(parent_fd)
        if isinstance(exc, PacketError):
            if cleanup_error is not None:
                raise PacketError(f"{exc}; staging cleanup also failed: {cleanup_error}") from exc
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if cleanup_error is not None:
            raise PacketError(
                f"cannot build report packet: {exc}; staging cleanup also failed: {cleanup_error}"
            ) from exc
        raise PacketError(f"cannot build report packet: {exc}") from exc

    if staging_fd is not None:
        os.close(staging_fd)
    os.close(parent_fd)

    return tuple(destination / name for name in REPORT_NAMES)
