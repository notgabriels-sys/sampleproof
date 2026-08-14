from __future__ import annotations

import hashlib
import math
import os
import struct
from pathlib import Path

import pytest

from sampleproof import wave as wave_module
from sampleproof.wave import WaveError, analyze_wav
from tests.wav_helpers import chunk, make_wav


@pytest.mark.parametrize(
    ("bits", "minimum", "digital_zero", "maximum", "expected_dc"),
    [
        (8, 0, 128, 255, (-1.0 + 127 / 128) / 3),
        (16, -32768, 0, 32767, (-1.0 + 32767 / 32768) / 3),
        (24, -8388608, 0, 8388607, (-1.0 + 8388607 / 8388608) / 3),
        (32, -2147483648, 0, 2147483647, (-1.0 + 2147483647 / 2147483648) / 3),
    ],
)
def test_analyze_wav_decodes_each_supported_integer_width(
    tmp_path: Path,
    bits: int,
    minimum: int,
    digital_zero: int,
    maximum: int,
    expected_dc: float,
) -> None:
    path = tmp_path / f"mono-{bits}.wav"
    path.write_bytes(make_wav([(minimum,), (digital_zero,), (maximum,)], bits_per_sample=bits))

    result = analyze_wav(path, block_frames=1)

    assert result.format.audio_format == 1
    assert result.format.channels == 1
    assert result.format.sample_rate == 48_000
    assert result.format.bits_per_sample == bits
    assert result.format.frame_count == 3
    assert result.format.duration_seconds == 3 / 48_000
    assert result.signal.sample_peak == 1.0
    assert result.signal.sample_peak_dbfs == 0.0
    assert result.signal.full_scale_samples == 2
    assert result.signal.full_scale_frames == 2
    assert result.signal.channels[0].dc_offset == pytest.approx(expected_dc, abs=1e-15)
    assert result.signal.all_zero is False
    assert result.signal.leading_zero_frames == 0
    assert result.signal.trailing_zero_frames == 0
    assert result.signal.first_nonzero_frame == 0
    assert result.signal.last_nonzero_frame == 2


def test_stereo_metrics_use_channel_samples_and_whole_zero_frames(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    path.write_bytes(make_wav([(0, 0), (0, 1000), (0, -1000), (0, 0)]))

    result = analyze_wav(path, block_frames=2)

    assert result.signal.sample_peak == pytest.approx(1000 / 32768)
    assert result.signal.sample_peak_dbfs == pytest.approx(20 * math.log10(1000 / 32768))
    assert result.signal.channels[0].sample_peak == 0.0
    assert result.signal.channels[0].sample_peak_dbfs is None
    assert result.signal.channels[0].dc_offset == 0.0
    assert result.signal.channels[1].sample_peak == pytest.approx(1000 / 32768)
    assert result.signal.channels[1].dc_offset == 0.0
    assert result.signal.leading_zero_frames == 1
    assert result.signal.trailing_zero_frames == 1
    assert result.signal.first_nonzero_frame == 1
    assert result.signal.last_nonzero_frame == 2


def test_all_zero_file_reports_unambiguous_zero_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "zero.wav"
    path.write_bytes(make_wav([(0,), (0,), (0,)]))

    result = analyze_wav(path)

    assert result.signal.sample_peak == 0.0
    assert result.signal.sample_peak_dbfs is None
    assert result.signal.all_zero is True
    assert result.signal.leading_zero_frames == 3
    assert result.signal.trailing_zero_frames == 3
    assert result.signal.first_nonzero_frame is None
    assert result.signal.last_nonzero_frame is None


def test_parser_honors_unknown_chunks_and_odd_payload_padding(tmp_path: Path) -> None:
    path = tmp_path / "chunks.wav"
    path.write_bytes(
        make_wav(
            [(1,), (2,)],
            before_fmt=[(b"JUNK", b"x")],
            between_fmt_and_data=[(b"LIST", b"abc")],
            after_data=[(b"cue ", b"")],
        )
    )

    result = analyze_wav(path)

    assert result.format.frame_count == 2
    assert result.signal.channels[0].dc_offset == pytest.approx(1.5 / 32768)


def test_classic_pcm_fmt_accepts_only_the_canonical_or_zero_extension_shape(
    tmp_path: Path,
) -> None:
    valid = make_wav([(0,), (1,)])
    body = valid[12:]
    fmt_payload = body[8:24]
    data_chunk = body[24:]

    extended = chunk(b"fmt ", fmt_payload + b"\x00\x00") + data_chunk
    extended_payload = b"RIFF" + struct.pack("<I", len(extended) + 4) + b"WAVE" + extended
    accepted = tmp_path / "pcm-extended-zero.wav"
    accepted.write_bytes(extended_payload)
    assert analyze_wav(accepted).format.frame_count == 2

    for suffix, extension in (("odd", b"\x00"), ("nonzero", b"\x01\x00")):
        invalid_body = chunk(b"fmt ", fmt_payload + extension) + data_chunk
        payload = b"RIFF" + struct.pack("<I", len(invalid_body) + 4) + b"WAVE" + invalid_body
        path = tmp_path / f"pcm-{suffix}.wav"
        path.write_bytes(payload)
        with pytest.raises(WaveError, match="fmt extension"):
            analyze_wav(path)


def test_hashes_distinguish_container_bytes_from_canonical_pcm(tmp_path: Path) -> None:
    plain = make_wav([(1,), (-2,), (3,)])
    decorated = make_wav([(1,), (-2,), (3,)], before_fmt=[(b"JUNK", b"metadata")])
    other_rate = make_wav([(1,), (-2,), (3,)], sample_rate=44_100)
    plain_path = tmp_path / "plain.wav"
    decorated_path = tmp_path / "decorated.wav"
    other_path = tmp_path / "other.wav"
    plain_path.write_bytes(plain)
    decorated_path.write_bytes(decorated)
    other_path.write_bytes(other_rate)

    plain_result = analyze_wav(plain_path)
    decorated_result = analyze_wav(decorated_path)
    other_result = analyze_wav(other_path)

    assert plain_result.hashes.file_sha256 == hashlib.sha256(plain).hexdigest()
    assert decorated_result.hashes.file_sha256 == hashlib.sha256(decorated).hexdigest()
    assert plain_result.hashes.file_sha256 != decorated_result.hashes.file_sha256
    assert plain_result.hashes.pcm_sha256 == decorated_result.hashes.pcm_sha256
    assert plain_result.hashes.pcm_sha256 != other_result.hashes.pcm_sha256


def test_analysis_fails_if_the_visible_file_path_is_replaced_during_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replacing a pathname mid-analysis must not combine facts from two files."""
    original_payload = make_wav([(0,), (1000,), (-1000,)])
    replacement_payload = make_wav([(0,), (20_000,), (-20_000,), (0,)])
    path = tmp_path / "source.wav"
    path.write_bytes(original_payload)
    replacement = tmp_path / "replacement.wav"
    replacement.write_bytes(replacement_payload)
    original_measure = wave_module._measure

    def replace_path_after_measure(handle, header, block_frames):
        measured = original_measure(handle, header, block_frames)
        replacement.replace(path)
        return measured

    monkeypatch.setattr(wave_module, "_measure", replace_path_after_measure)

    with pytest.raises(WaveError) as raised:
        analyze_wav(path)

    assert path.read_bytes() == replacement_payload
    assert raised.value.code == "file_changed"


def test_analysis_rejects_an_in_place_change_during_the_open_handle_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed inode must not emit a mixed hash/measurement record."""
    path = tmp_path / "source.wav"
    original_payload = make_wav([(0,), (1000,), (-1000,)])
    replacement_payload = make_wav([(0,), (2000,), (-2000,)])
    assert len(original_payload) == len(replacement_payload)
    path.write_bytes(original_payload)
    original_measure = wave_module._measure

    def rewrite_path_after_measure(handle, header, block_frames):
        measured = original_measure(handle, header, block_frames)
        path.write_bytes(replacement_payload)
        return measured

    monkeypatch.setattr(wave_module, "_measure", rewrite_path_after_measure)

    with pytest.raises(WaveError) as raised:
        analyze_wav(path)

    assert raised.value.code == "file_changed"


def test_analysis_rejects_same_size_rewrite_even_if_mtime_is_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "source.wav"
    original_payload = make_wav([(0,), (1000,), (-1000,)])
    replacement_payload = make_wav([(0,), (2000,), (-2000,)])
    assert len(original_payload) == len(replacement_payload)
    path.write_bytes(original_payload)
    original_stat = path.stat()
    original_hash = wave_module._file_sha256

    def hash_then_rewrite(handle):
        digest = original_hash(handle)
        path.write_bytes(replacement_payload)
        os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        return digest

    monkeypatch.setattr(wave_module, "_file_sha256", hash_then_rewrite)

    with pytest.raises(WaveError) as raised:
        analyze_wav(path)

    assert raised.value.code == "file_changed"
    assert path.read_bytes() == replacement_payload


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "too short"),
        (b"RIFX" + struct.pack("<I", 4) + b"WAVE", "RIFF"),
        (b"RIFF" + struct.pack("<I", 99) + b"WAVE", "size"),
        (b"RIFF" + struct.pack("<I", 4) + b"NOPE", "WAVE"),
        (b"RIFF" + struct.pack("<I", 12) + b"WAVE" + b"fmt " + struct.pack("<I", 99), "bounds"),
    ],
)
def test_parser_rejects_broken_container_bounds(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    path = tmp_path / "broken.wav"
    path.write_bytes(payload)

    with pytest.raises(WaveError, match=message):
        analyze_wav(path)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_fmt", "fmt"),
        ("missing_data", "data"),
        ("duplicate_fmt", "duplicate fmt"),
        ("duplicate_data", "duplicate data"),
        ("float", "integer PCM"),
        ("extensible", "WAVE_FORMAT_EXTENSIBLE"),
        ("three_channels", "mono or stereo"),
        ("twenty_bits", "bit depth"),
        ("bad_block_align", "block align"),
        ("bad_byte_rate", "byte rate"),
        ("partial_frame", "whole frames"),
        ("zero_sample_rate", "sample rate"),
        ("short_fmt", "fmt"),
        ("empty_data", "empty"),
    ],
)
def test_parser_rejects_unsupported_or_inconsistent_format(
    tmp_path: Path, mutation: str, message: str
) -> None:
    valid = make_wav([(0,), (1,)])
    body = valid[12:]
    if mutation == "missing_fmt":
        data_offset = body.index(b"data")
        body = body[data_offset:]
    elif mutation == "missing_data":
        body = body[: body.index(b"data")]
    elif mutation == "duplicate_fmt":
        fmt_end = 8 + struct.unpack_from("<I", body, 4)[0]
        body = body[:fmt_end] + body
    elif mutation == "duplicate_data":
        data_offset = body.index(b"data")
        body += body[data_offset:]
    elif mutation == "float":
        body = bytearray(body)
        struct.pack_into("<H", body, 8, 3)
        body = bytes(body)
    elif mutation == "extensible":
        body = bytearray(body)
        struct.pack_into("<H", body, 8, 0xFFFE)
        body = bytes(body)
    elif mutation == "three_channels":
        body = bytearray(body)
        struct.pack_into("<H", body, 10, 3)
        body = bytes(body)
    elif mutation == "twenty_bits":
        body = bytearray(body)
        struct.pack_into("<H", body, 22, 20)
        body = bytes(body)
    elif mutation == "bad_block_align":
        body = bytearray(body)
        struct.pack_into("<H", body, 20, 99)
        body = bytes(body)
    elif mutation == "bad_byte_rate":
        body = bytearray(body)
        struct.pack_into("<I", body, 16, 99)
        body = bytes(body)
    elif mutation == "partial_frame":
        fmt_chunk = body[:24]
        body = fmt_chunk + chunk(b"data", b"\x00")
    elif mutation == "zero_sample_rate":
        body = bytearray(body)
        struct.pack_into("<I", body, 12, 0)
        body = bytes(body)
    elif mutation == "short_fmt":
        body = chunk(b"fmt ", b"\x01\x00") + body[body.index(b"data") :]
    elif mutation == "empty_data":
        body = body[: body.index(b"data")] + chunk(b"data", b"")
    payload = b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + bytes(body)
    path = tmp_path / f"{mutation}.wav"
    path.write_bytes(payload)

    with pytest.raises(WaveError, match=message):
        analyze_wav(path)


def test_analysis_requires_a_positive_stream_block_size(tmp_path: Path) -> None:
    path = tmp_path / "valid.wav"
    path.write_bytes(make_wav([(0,)]))

    with pytest.raises(ValueError, match="block_frames"):
        analyze_wav(path, block_frames=0)
