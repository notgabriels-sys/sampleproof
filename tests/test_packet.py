from __future__ import annotations

from pathlib import Path

import pytest

from sampleproof.packet import PacketError, _rename_noreplace, build_packet
from sampleproof.report import render_json, render_manifest, render_markdown
from sampleproof.scan import scan_pack
from tests.test_scan import config
from tests.wav_helpers import make_wav


def result_for(source: Path):
    (source / "sound.wav").write_bytes(make_wav([(0,), (1,)]))
    return scan_pack(config(all_zero="allow", duplicate_pcm="allow"), source)


def test_build_packet_publishes_exact_report_set_to_new_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    output = tmp_path / "packet"

    written = build_packet(result, output)

    assert output.is_dir()
    assert {path.name for path in written} == {
        "sampleproof-manifest.jsonl",
        "sampleproof-report.json",
        "sampleproof-report.md",
    }
    assert {path.name for path in output.iterdir()} == {path.name for path in written}
    assert (output / "sampleproof-report.json").read_text("utf-8") == render_json(result)
    assert (output / "sampleproof-report.md").read_text("utf-8") == render_markdown(result)
    assert (output / "sampleproof-manifest.jsonl").read_text("utf-8") == render_manifest(result)
    assert not list(tmp_path.glob(".packet.sampleproof-*"))


def test_build_packet_never_overwrites_an_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    output = tmp_path / "packet"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("user data", encoding="utf-8")

    with pytest.raises(PacketError, match="already exists"):
        build_packet(result, output)

    assert sentinel.read_text("utf-8") == "user data"
    assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]


def test_publish_primitive_refuses_even_an_existing_empty_directory(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "report.txt").write_text("complete", encoding="utf-8")
    destination = tmp_path / "destination"
    destination.mkdir()

    with pytest.raises(FileExistsError):
        _rename_noreplace(staging, destination)

    assert (staging / "report.txt").read_text("utf-8") == "complete"
    assert destination.is_dir()
    assert not list(destination.iterdir())


def test_build_packet_rejects_any_output_inside_the_scanned_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)

    with pytest.raises(PacketError, match="outside"):
        build_packet(result, source / "packet")

    assert not (source / "packet").exists()


def test_build_packet_cleans_staging_if_rendering_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    output = tmp_path / "packet"

    def fail_render(_result) -> str:
        raise RuntimeError("synthetic report failure")

    monkeypatch.setattr("sampleproof.packet.render_markdown", fail_render)

    with pytest.raises(PacketError, match="synthetic report failure"):
        build_packet(result, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".packet.sampleproof-*"))


def test_build_packet_cleans_staging_when_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    output = tmp_path / "packet"

    def interrupt_render(_result) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("sampleproof.packet.render_markdown", interrupt_render)

    with pytest.raises(KeyboardInterrupt):
        build_packet(result, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".packet.sampleproof-*"))


def test_build_packet_rejects_a_symbolic_link_output_parent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    real_parent = tmp_path / "real-output"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-output"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(PacketError, match="symbolic link"):
        build_packet(result, linked_parent / "packet")

    assert not (real_parent / "packet").exists()


def test_build_packet_cannot_be_redirected_by_a_parent_swap_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    result = result_for(source)
    parent = tmp_path / "output-parent"
    parent.mkdir()
    original_parent = tmp_path / "original-parent"
    external = tmp_path / "external-parent"
    external.mkdir()
    output = parent / "packet"
    from sampleproof import packet as packet_module

    original_create_staging = packet_module._create_staging_directory

    def swap_parent_then_make_staging(parent_fd: int, destination_name: str):
        parent.rename(original_parent)
        parent.symlink_to(external, target_is_directory=True)
        return original_create_staging(parent_fd, destination_name)

    monkeypatch.setattr(packet_module, "_create_staging_directory", swap_parent_then_make_staging)

    with pytest.raises(PacketError):
        build_packet(result, output)

    assert not (external / "packet").exists()
    assert not list(external.glob(".packet.sampleproof-*"))
    assert not list(original_parent.glob(".packet.sampleproof-*"))
