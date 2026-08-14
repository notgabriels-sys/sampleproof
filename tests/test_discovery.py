from __future__ import annotations

import errno
from pathlib import Path

import pytest

from sampleproof.discovery import DiscoveryError, discover_wav_files


def test_discovery_is_recursive_case_insensitive_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "z.WAV").write_bytes(b"z")
    (tmp_path / "A.wav").write_bytes(b"a")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "b.WaV").write_bytes(b"b")
    (tmp_path / "nested" / "ignore.aiff").write_bytes(b"x")

    found = discover_wav_files(tmp_path)

    assert [item.relative_path for item in found] == ["A.wav", "nested/b.WaV", "z.WAV"]
    assert [item.path for item in found] == [
        tmp_path / "A.wav",
        tmp_path / "nested" / "b.WaV",
        tmp_path / "z.WAV",
    ]


def test_discovery_excludes_symlinked_files_and_directories(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.wav"
    outside.write_bytes(b"outside")
    (tmp_path / "inside.wav").write_bytes(b"inside")
    (tmp_path / "linked.wav").symlink_to(outside)
    external_directory = tmp_path.parent / "external-audio"
    external_directory.mkdir()
    (external_directory / "external.wav").write_bytes(b"external")
    (tmp_path / "linked-dir").symlink_to(external_directory, target_is_directory=True)

    found = discover_wav_files(tmp_path)

    assert [item.relative_path for item in found] == ["inside.wav"]


def test_discovery_rejects_a_symbolic_link_source_root(tmp_path: Path) -> None:
    real_source = tmp_path / "real-source"
    real_source.mkdir()
    linked_source = tmp_path / "linked-source"
    linked_source.symlink_to(real_source, target_is_directory=True)

    with pytest.raises(DiscoveryError, match="source root"):
        discover_wav_files(linked_source)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_discovery_requires_a_real_directory(tmp_path: Path, kind: str) -> None:
    source = tmp_path / kind
    if kind == "file":
        source.write_bytes(b"not a directory")

    with pytest.raises(DiscoveryError, match="source root"):
        discover_wav_files(source)


def test_discovery_fails_closed_when_a_visible_subtree_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A descriptor-relative permission error must not turn a partial inventory into a pass."""

    (tmp_path / "blocked").mkdir()
    from sampleproof import discovery as discovery_module

    original_open = discovery_module.open_child_directory

    def fail_for_blocked(parent_fd: int, name: str) -> int:
        if name == "blocked":
            raise OSError(errno.EACCES, "permission denied")
        return original_open(parent_fd, name)

    monkeypatch.setattr(discovery_module, "open_child_directory", fail_for_blocked)

    with pytest.raises(DiscoveryError, match="blocked"):
        discover_wav_files(tmp_path)
