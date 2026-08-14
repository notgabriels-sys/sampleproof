from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from sampleproof.config import Config, DeliveryConfig, PcmConfig, PolicyConfig
from sampleproof.discovery import DiscoveryError
from sampleproof.scan import scan_pack
from tests.wav_helpers import make_wav


def config(
    *,
    rates: tuple[int, ...] = (48_000,),
    depths: tuple[int, ...] = (16,),
    channels: tuple[int, ...] = (1,),
    peak: float | None = None,
    full_scale: int | None = None,
    dc: float | None = None,
    all_zero: str = "fail",
    duplicate_pcm: str = "fail",
) -> Config:
    return Config(
        schema_version=1,
        delivery=DeliveryConfig(
            pack_id="form-under-load-01",
            title="FORM UNDER LOAD 01",
            version="1.0.0",
            license="Commercial sample license",
        ),
        pcm=PcmConfig(
            allowed_sample_rates=rates,
            allowed_bit_depths=depths,
            allowed_channels=channels,
        ),
        policy=PolicyConfig(
            max_sample_peak_dbfs=peak,
            max_full_scale_samples=full_scale,
            max_abs_dc_offset=dc,
            all_zero=all_zero,
            duplicate_pcm=duplicate_pcm,
        ),
    )


def finding_codes(result) -> list[tuple[str, str, tuple[str, ...]]]:
    return [(item.severity, item.code, item.paths) for item in result.findings]


def test_scan_groups_pcm_duplicates_and_warning_does_not_fail(tmp_path: Path) -> None:
    (tmp_path / "b.wav").write_bytes(make_wav([(0,), (100,)]))
    (tmp_path / "a.wav").write_bytes(
        make_wav([(0,), (100,)], before_fmt=[(b"JUNK", b"different container")])
    )

    result = scan_pack(config(duplicate_pcm="warn", all_zero="allow"), tmp_path)

    assert result.outcome == "pass"
    assert result.complete is True
    assert [item.relative_path for item in result.files] == ["a.wav", "b.wav"]
    assert len(result.duplicate_groups) == 1
    assert result.duplicate_groups[0].paths == ("a.wav", "b.wav")
    assert finding_codes(result) == [("warning", "duplicate_pcm", ("a.wav", "b.wav"))]


def test_scan_applies_all_declared_format_and_signal_limits(tmp_path: Path) -> None:
    (tmp_path / "hot.wav").write_bytes(
        make_wav([(-32768, -32768), (-32768, -32768)], channels=2, sample_rate=44_100)
    )

    result = scan_pack(
        config(
            rates=(48_000,),
            depths=(24,),
            channels=(1,),
            peak=-0.1,
            full_scale=0,
            dc=0.001,
            all_zero="allow",
            duplicate_pcm="allow",
        ),
        tmp_path,
    )

    assert result.outcome == "fail"
    assert result.complete is True
    assert finding_codes(result) == [
        ("error", "sample_rate_not_allowed", ("hot.wav",)),
        ("error", "bit_depth_not_allowed", ("hot.wav",)),
        ("error", "channel_count_not_allowed", ("hot.wav",)),
        ("error", "sample_peak_exceeded", ("hot.wav",)),
        ("error", "full_scale_samples_exceeded", ("hot.wav",)),
        ("error", "dc_offset_exceeded", ("hot.wav",)),
    ]
    assert result.files[0].outcome == "fail"


def test_all_zero_and_duplicate_actions_can_fail_or_be_allowed(tmp_path: Path) -> None:
    payload = make_wav([(0,), (0,)])
    (tmp_path / "one.wav").write_bytes(payload)
    (tmp_path / "two.wav").write_bytes(payload)

    failed = scan_pack(config(), tmp_path)
    allowed = scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)

    assert failed.outcome == "fail"
    assert finding_codes(failed) == [
        ("error", "all_zero", ("one.wav",)),
        ("error", "all_zero", ("two.wav",)),
        ("error", "duplicate_pcm", ("one.wav", "two.wav")),
    ]
    assert allowed.outcome == "pass"
    assert allowed.findings == ()
    assert len(allowed.duplicate_groups) == 1


def test_malformed_file_makes_scan_incomplete_but_other_files_are_measured(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.wav").write_bytes(b"not a wav")
    (tmp_path / "good.wav").write_bytes(make_wav([(0,), (1,)]))

    result = scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)

    assert result.outcome == "incomplete"
    assert result.complete is False
    assert [item.outcome for item in result.files] == ["error", "pass"]
    assert result.files[0].error_code == "container_too_short"
    assert result.files[0].analysis is None
    assert result.files[0].file_sha256 == hashlib.sha256(b"not a wav").hexdigest()
    assert result.files[1].analysis is not None
    assert result.files[1].file_sha256 == result.files[1].analysis.hashes.file_sha256
    assert finding_codes(result) == [("error", "invalid_wav", ("bad.wav",))]


def test_malformed_file_replaced_during_analysis_fails_without_stale_path_facts(
    tmp_path: Path, monkeypatch
) -> None:
    original_payload = b"not a wav"
    replacement_payload = b"different replacement bytes"
    path = tmp_path / "bad.wav"
    path.write_bytes(original_payload)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(replacement_payload)
    from sampleproof import wave as wave_module

    original_parse = wave_module._parse_header

    def parse_then_replace(handle, file_size):
        try:
            return original_parse(handle, file_size)
        finally:
            replacement.replace(path)

    monkeypatch.setattr(wave_module, "_parse_header", parse_then_replace)

    result = scan_pack(config(), tmp_path)

    assert path.read_bytes() == replacement_payload
    assert result.outcome == "incomplete"
    assert result.files[0].error_code == "file_changed"
    assert result.files[0].size_bytes is None
    assert result.files[0].file_sha256 is None


def test_no_wav_files_is_an_incomplete_scan_not_a_clean_pass(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("nothing to analyze", encoding="utf-8")

    result = scan_pack(config(), tmp_path)

    assert result.outcome == "incomplete"
    assert result.complete is False
    assert result.files == ()
    assert finding_codes(result) == [("error", "no_wav_files", ())]


def test_scan_rejects_a_file_replaced_by_a_symlink_after_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    """A discovered pack path must never be reopened through an external symlink."""
    inside = tmp_path / "inside.wav"
    inside.write_bytes(make_wav([(0,), (100,)], sample_rate=44_100))
    outside = tmp_path.parent / "outside-replacement.wav"
    outside.write_bytes(make_wav([(0,), (20_000,)], sample_rate=48_000))
    from sampleproof import scan as scan_module

    original_discover = scan_module.discover_wav_files

    def discover_then_replace(root, *, root_fd=None):
        discovered = original_discover(root, root_fd=root_fd)
        inside.unlink()
        inside.symlink_to(outside)
        return discovered

    monkeypatch.setattr(scan_module, "discover_wav_files", discover_then_replace)

    result = scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)

    assert result.outcome == "incomplete"
    assert result.complete is False
    assert result.files[0].analysis is None
    assert result.files[0].error_code == "symlink_not_allowed"
    assert result.files[0].file_sha256 is None


def test_scan_rejects_an_intermediate_directory_replaced_after_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    """No intermediate pack component may redirect analysis outside the source root."""
    nested = tmp_path / "nested"
    nested.mkdir()
    inside = nested / "inside.wav"
    inside.write_bytes(make_wav([(0,), (100,)], sample_rate=44_100))
    external = tmp_path.parent / "external-replacement-directory"
    external.mkdir()
    (external / "inside.wav").write_bytes(make_wav([(0,), (20_000,)], sample_rate=48_000))
    from sampleproof import scan as scan_module

    original_discover = scan_module.discover_wav_files

    def discover_then_replace_directory(root, *, root_fd=None):
        discovered = original_discover(root, root_fd=root_fd)
        inside.unlink()
        nested.rmdir()
        nested.symlink_to(external, target_is_directory=True)
        return discovered

    monkeypatch.setattr(scan_module, "discover_wav_files", discover_then_replace_directory)

    result = scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)

    assert result.outcome == "incomplete"
    assert result.files[0].analysis is None
    assert result.files[0].error_code in {"symlink_not_allowed", "unsafe_path"}
    assert result.files[0].file_sha256 is None


def test_scan_fails_closed_if_the_visible_source_root_is_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "inside.wav").write_bytes(make_wav([(0,), (100,)], sample_rate=44_100))
    original_source = tmp_path / "original-source"
    external = tmp_path / "external-source"
    external.mkdir()
    (external / "inside.wav").write_bytes(make_wav([(0,), (20_000,)], sample_rate=48_000))
    from sampleproof import scan as scan_module

    original_discover = scan_module.discover_wav_files

    def discover_then_replace_root(root, *, root_fd=None):
        discovered = original_discover(root, root_fd=root_fd)
        source.rename(original_source)
        source.symlink_to(external, target_is_directory=True)
        return discovered

    monkeypatch.setattr(scan_module, "discover_wav_files", discover_then_replace_root)

    with pytest.raises(DiscoveryError, match="changed identity"):
        scan_pack(config(all_zero="allow", duplicate_pcm="allow"), source)


def test_scan_fails_if_a_measured_file_changes_before_the_report_is_returned(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "inside.wav"
    path.write_bytes(make_wav([(0,), (100,)], sample_rate=48_000))
    replacement = make_wav([(0,), (20_000,)], sample_rate=48_000)
    from sampleproof import scan as scan_module

    original_analyze = scan_module.analyze_wav_beneath

    def analyze_then_replace(root_fd, relative_path):
        analysis = original_analyze(root_fd, relative_path)
        path.write_bytes(replacement)
        return analysis

    monkeypatch.setattr(scan_module, "analyze_wav_beneath", analyze_then_replace)

    with pytest.raises(DiscoveryError, match="changed after measurement"):
        scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)


def test_scan_fails_if_a_wav_is_added_after_initial_discovery(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "inside.wav").write_bytes(make_wav([(0,), (100,)], sample_rate=48_000))
    from sampleproof import scan as scan_module

    original_analyze = scan_module.analyze_wav_beneath

    def analyze_then_add(root_fd, relative_path):
        analysis = original_analyze(root_fd, relative_path)
        (tmp_path / "added.wav").write_bytes(make_wav([(0,), (200,)], sample_rate=48_000))
        return analysis

    monkeypatch.setattr(scan_module, "analyze_wav_beneath", analyze_then_add)

    with pytest.raises(DiscoveryError, match="inventory changed"):
        scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)
