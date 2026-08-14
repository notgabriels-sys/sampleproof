"""Bounded classic RIFF/WAVE integer-PCM parsing and streamed measurement."""

from __future__ import annotations

import errno
import hashlib
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from sampleproof.filesystem import open_directory_nofollow, open_regular_beneath


class WaveError(ValueError):
    """Raised when a file is not a supported, structurally valid PCM WAVE file."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        file_size_bytes: int | None = None,
        file_sha256: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.file_size_bytes = file_size_bytes
        self.file_sha256 = file_sha256


@dataclass(frozen=True)
class WaveFormat:
    audio_format: int
    channels: int
    sample_rate: int
    byte_rate: int
    block_align: int
    bits_per_sample: int
    frame_count: int
    duration_seconds: float


@dataclass(frozen=True)
class ChannelSignal:
    channel: int
    sample_peak: float
    sample_peak_dbfs: float | None
    dc_offset: float
    full_scale_samples: int


@dataclass(frozen=True)
class SignalMetrics:
    sample_peak: float
    sample_peak_dbfs: float | None
    full_scale_samples: int
    full_scale_frames: int
    all_zero: bool
    leading_zero_frames: int
    trailing_zero_frames: int
    first_nonzero_frame: int | None
    last_nonzero_frame: int | None
    channels: tuple[ChannelSignal, ...]


@dataclass(frozen=True)
class Hashes:
    file_sha256: str
    pcm_sha256: str


@dataclass(frozen=True)
class WaveAnalysis:
    file_size_bytes: int
    format: WaveFormat
    signal: SignalMetrics
    hashes: Hashes


@dataclass(frozen=True)
class _ParsedHeader:
    audio_format: int
    channels: int
    sample_rate: int
    byte_rate: int
    block_align: int
    bits_per_sample: int
    data_offset: int
    data_size: int


def _read_exact(handle: BinaryIO, size: int, *, code: str, message: str) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise WaveError(code, message)
    return value


def _parse_header(handle: BinaryIO, file_size: int) -> _ParsedHeader:
    if file_size < 12:
        raise WaveError("container_too_short", "file is too short for a RIFF/WAVE header")
    header = _read_exact(
        handle,
        12,
        code="container_too_short",
        message="file is too short for a RIFF/WAVE header",
    )
    if header[:4] != b"RIFF":
        raise WaveError("not_riff", "container must start with RIFF")
    declared_size = struct.unpack_from("<I", header, 4)[0]
    if declared_size + 8 != file_size:
        raise WaveError("riff_size_mismatch", "RIFF size does not match the file size")
    if header[8:] != b"WAVE":
        raise WaveError("not_wave", "RIFF form type must be WAVE")

    fmt: tuple[int, int, int, int, int, int] | None = None
    data_location: tuple[int, int] | None = None
    position = 12
    while position < file_size:
        if file_size - position < 8:
            raise WaveError("chunk_bounds", "chunk header exceeds RIFF bounds")
        handle.seek(position)
        chunk_header = _read_exact(
            handle, 8, code="chunk_bounds", message="chunk header exceeds RIFF bounds"
        )
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack_from("<I", chunk_header, 4)[0]
        payload_offset = position + 8
        payload_end = payload_offset + chunk_size
        padded_end = payload_end + (chunk_size & 1)
        if payload_end > file_size or padded_end > file_size:
            raise WaveError("chunk_bounds", "chunk payload exceeds RIFF bounds")

        if chunk_id == b"fmt ":
            if fmt is not None:
                raise WaveError("duplicate_fmt", "duplicate fmt chunk")
            if chunk_size < 16:
                raise WaveError("short_fmt", "fmt chunk must contain at least 16 bytes")
            if chunk_size not in {16, 18}:
                raise WaveError(
                    "invalid_fmt_extension",
                    "classic PCM fmt extension must be absent or a zero-length extension",
                )
            handle.seek(payload_offset)
            fmt = struct.unpack(
                "<HHIIHH",
                _read_exact(
                    handle, 16, code="short_fmt", message="fmt chunk must contain 16 bytes"
                ),
            )
            if chunk_size == 18:
                extension_size = struct.unpack(
                    "<H",
                    _read_exact(
                        handle,
                        2,
                        code="invalid_fmt_extension",
                        message="classic PCM fmt extension is truncated",
                    ),
                )[0]
                if extension_size != 0:
                    raise WaveError(
                        "invalid_fmt_extension",
                        "classic PCM fmt extension must declare zero extra bytes",
                    )
        elif chunk_id == b"data":
            if data_location is not None:
                raise WaveError("duplicate_data", "duplicate data chunk")
            data_location = (payload_offset, chunk_size)
        position = padded_end

    if fmt is None:
        raise WaveError("missing_fmt", "missing fmt chunk")
    if data_location is None:
        raise WaveError("missing_data", "missing data chunk")

    audio_format, channels, sample_rate, byte_rate, block_align, bits_per_sample = fmt
    if audio_format == 0xFFFE:
        raise WaveError(
            "unsupported_extensible",
            "WAVE_FORMAT_EXTENSIBLE is explicitly unsupported in sampleproof v0.1",
        )
    if audio_format != 1:
        raise WaveError("unsupported_encoding", "format must be classic integer PCM (format tag 1)")
    if channels not in {1, 2}:
        raise WaveError("unsupported_channels", "format must be mono or stereo")
    if sample_rate < 1:
        raise WaveError("invalid_sample_rate", "sample rate must be positive")
    if bits_per_sample not in {8, 16, 24, 32}:
        raise WaveError("unsupported_bit_depth", "bit depth must be 8, 16, 24, or 32")
    expected_block_align = channels * (bits_per_sample // 8)
    if block_align != expected_block_align:
        raise WaveError("invalid_block_align", "block align is inconsistent with PCM format")
    if byte_rate != sample_rate * block_align:
        raise WaveError("invalid_byte_rate", "byte rate is inconsistent with PCM format")
    data_offset, data_size = data_location
    if data_size == 0:
        raise WaveError("empty_data", "data chunk is empty")
    if data_size % block_align:
        raise WaveError("partial_frame", "data chunk must contain whole frames")

    return _ParsedHeader(
        audio_format=audio_format,
        channels=channels,
        sample_rate=sample_rate,
        byte_rate=byte_rate,
        block_align=block_align,
        bits_per_sample=bits_per_sample,
        data_offset=data_offset,
        data_size=data_size,
    )


def _file_sha256(handle: BinaryIO) -> str:
    """Hash the bytes behind the already-open file identity."""

    digest = hashlib.sha256()
    handle.seek(0)
    while block := handle.read(1024 * 1024):
        digest.update(block)
    return digest.hexdigest()


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int, int]:
    """Return inexpensive identity/change facts for one open file handle."""

    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _snapshot_file(source: BinaryIO, snapshot: BinaryIO) -> tuple[str, int]:
    """Copy one open source identity to bounded-memory scratch while hashing its bytes."""

    digest = hashlib.sha256()
    copied = 0
    source.seek(0)
    while block := source.read(1024 * 1024):
        snapshot.write(block)
        digest.update(block)
        copied += len(block)
    snapshot.flush()
    return digest.hexdigest(), copied


def _open_error(error: OSError) -> WaveError:
    if error.errno == errno.ELOOP:
        return WaveError("symlink_not_allowed", "symbolic-link WAV paths are not allowed")
    if error.errno in {errno.ENOTDIR, errno.EINVAL}:
        return WaveError(
            "unsafe_path",
            "WAV path is not a safely traversable regular file beneath the source root",
        )
    return WaveError("read_error", f"cannot open WAV file safely: {error.strerror or error}")


def _open_regular_file_beneath(root_fd: int, relative_path: str) -> BinaryIO:
    """Open a regular WAV below a fixed root without following any path component."""

    try:
        descriptor = open_regular_beneath(root_fd, relative_path)
    except OSError as error:
        raise _open_error(error) from error
    return os.fdopen(descriptor, "rb", closefd=True)


def _path_still_names_source(
    root_fd: int, relative_path: str, source_identity: tuple[int, int, int, int, int]
) -> bool:
    try:
        descriptor = open_regular_beneath(root_fd, relative_path)
    except OSError:
        return False
    try:
        return _file_identity(os.fstat(descriptor)) == source_identity
    finally:
        os.close(descriptor)


def _decode_sample(raw: bytes, bits_per_sample: int) -> int:
    if bits_per_sample == 8:
        return raw[0] - 128
    return int.from_bytes(raw, byteorder="little", signed=True)


def _dbfs(value: float) -> float | None:
    return 20.0 * math.log10(value) if value > 0.0 else None


def _measure(
    handle: BinaryIO, header: _ParsedHeader, block_frames: int
) -> tuple[SignalMetrics, str]:
    bytes_per_sample = header.bits_per_sample // 8
    scale = float(1 << (header.bits_per_sample - 1))
    minimum = -(1 << (header.bits_per_sample - 1))
    maximum = (1 << (header.bits_per_sample - 1)) - 1
    frame_count = header.data_size // header.block_align
    sums = [0] * header.channels
    peaks = [0] * header.channels
    full_scale_by_channel = [0] * header.channels
    full_scale_frames = 0
    full_scale_samples = 0
    first_nonzero: int | None = None
    last_nonzero: int | None = None

    pcm_digest = hashlib.sha256()
    pcm_digest.update(b"sampleproof-pcm-v1\x00")
    pcm_digest.update(
        struct.pack(
            "<HIHQ",
            header.channels,
            header.sample_rate,
            header.bits_per_sample,
            frame_count,
        )
    )

    handle.seek(header.data_offset)
    remaining = header.data_size
    frame_index = 0
    block_size = block_frames * header.block_align
    while remaining:
        raw_block = _read_exact(
            handle,
            min(block_size, remaining),
            code="read_error",
            message="PCM data ended before its declared size",
        )
        remaining -= len(raw_block)
        pcm_digest.update(raw_block)
        for offset in range(0, len(raw_block), header.block_align):
            frame = raw_block[offset : offset + header.block_align]
            frame_nonzero = False
            frame_full_scale = False
            for channel_index in range(header.channels):
                sample_offset = channel_index * bytes_per_sample
                value = _decode_sample(
                    frame[sample_offset : sample_offset + bytes_per_sample],
                    header.bits_per_sample,
                )
                sums[channel_index] += value
                peaks[channel_index] = max(peaks[channel_index], abs(value))
                if value != 0:
                    frame_nonzero = True
                if value in (minimum, maximum):
                    full_scale_samples += 1
                    full_scale_by_channel[channel_index] += 1
                    frame_full_scale = True
            if frame_nonzero:
                if first_nonzero is None:
                    first_nonzero = frame_index
                last_nonzero = frame_index
            if frame_full_scale:
                full_scale_frames += 1
            frame_index += 1

    channel_metrics = tuple(
        ChannelSignal(
            channel=index + 1,
            sample_peak=peak / scale,
            sample_peak_dbfs=_dbfs(peak / scale),
            dc_offset=sums[index] / frame_count / scale,
            full_scale_samples=full_scale_by_channel[index],
        )
        for index, peak in enumerate(peaks)
    )
    peak = max(peaks) / scale
    all_zero = first_nonzero is None
    leading_zero_frames = frame_count if all_zero else first_nonzero
    trailing_zero_frames = frame_count if all_zero else frame_count - 1 - last_nonzero
    return (
        SignalMetrics(
            sample_peak=peak,
            sample_peak_dbfs=_dbfs(peak),
            full_scale_samples=full_scale_samples,
            full_scale_frames=full_scale_frames,
            all_zero=all_zero,
            leading_zero_frames=leading_zero_frames,
            trailing_zero_frames=trailing_zero_frames,
            first_nonzero_frame=first_nonzero,
            last_nonzero_frame=last_nonzero,
            channels=channel_metrics,
        ),
        pcm_digest.hexdigest(),
    )


def analyze_wav_beneath(
    root_fd: int, relative_path: str, *, block_frames: int = 65_536
) -> WaveAnalysis:
    """Analyze one relative WAV below an already-open, fixed source root."""

    if isinstance(block_frames, bool) or not isinstance(block_frames, int) or block_frames < 1:
        raise ValueError("block_frames must be a positive integer")
    try:
        with (
            _open_regular_file_beneath(root_fd, relative_path) as source,
            tempfile.TemporaryFile(mode="w+b") as snapshot,
        ):
            before = os.fstat(source.fileno())
            file_size = before.st_size
            file_sha256, copied_size = _snapshot_file(source, snapshot)
            if copied_size != file_size:
                raise WaveError(
                    "file_changed",
                    "WAV file changed while its open-file evidence was being captured",
                )
            snapshot.seek(0)
            parse_error: WaveError | None = None
            try:
                header = _parse_header(snapshot, file_size)
                signal, pcm_sha256 = _measure(snapshot, header, block_frames)
            except WaveError as exc:
                parse_error = exc

            after_measurement = os.fstat(source.fileno())
            final_file_sha256 = _file_sha256(source)
            after_verification = os.fstat(source.fileno())
            path_matches = _path_still_names_source(
                root_fd,
                relative_path,
                _file_identity(after_verification),
            )
            after_path_validation = os.fstat(source.fileno())
            if (
                _file_identity(before) != _file_identity(after_measurement)
                or _file_identity(before) != _file_identity(after_verification)
                or _file_identity(before) != _file_identity(after_path_validation)
                or final_file_sha256 != file_sha256
                or not path_matches
            ):
                raise WaveError(
                    "file_changed",
                    "WAV file changed while its open-file evidence was being captured",
                ) from parse_error
            if parse_error is not None:
                parse_error.file_size_bytes = file_size
                parse_error.file_sha256 = file_sha256
                raise parse_error
    except WaveError:
        raise
    except OSError as exc:
        raise WaveError(
            "read_error", f"cannot read WAV file safely: {exc.strerror or exc}"
        ) from exc

    frame_count = header.data_size // header.block_align
    return WaveAnalysis(
        file_size_bytes=file_size,
        format=WaveFormat(
            audio_format=header.audio_format,
            channels=header.channels,
            sample_rate=header.sample_rate,
            byte_rate=header.byte_rate,
            block_align=header.block_align,
            bits_per_sample=header.bits_per_sample,
            frame_count=frame_count,
            duration_seconds=frame_count / header.sample_rate,
        ),
        signal=signal,
        hashes=Hashes(file_sha256=file_sha256, pcm_sha256=pcm_sha256),
    )


def analyze_wav(path: str | Path, *, block_frames: int = 65_536) -> WaveAnalysis:
    """Parse and measure one supported WAV without following any path component."""

    wav_path = Path(path).absolute()
    if not wav_path.name:
        raise WaveError("unsafe_path", "WAV path must name a regular file")
    try:
        parent_fd = open_directory_nofollow(wav_path.parent)
    except OSError as error:
        raise _open_error(error) from error
    try:
        return analyze_wav_beneath(parent_fd, wav_path.name, block_frames=block_frames)
    finally:
        os.close(parent_fd)


def verify_file_facts_beneath(
    root_fd: int,
    relative_path: str,
    *,
    expected_size: int,
    expected_sha256: str,
) -> bool:
    """Recheck one reported path against its earlier size and whole-file digest."""

    try:
        with _open_regular_file_beneath(root_fd, relative_path) as source:
            before = os.fstat(source.fileno())
            if before.st_size != expected_size:
                return False
            observed_sha256 = _file_sha256(source)
            after_hash = os.fstat(source.fileno())
            path_matches = _path_still_names_source(
                root_fd,
                relative_path,
                _file_identity(after_hash),
            )
            after_path_validation = os.fstat(source.fileno())
            return (
                _file_identity(before) == _file_identity(after_hash)
                and _file_identity(before) == _file_identity(after_path_validation)
                and observed_sha256 == expected_sha256
                and path_matches
            )
    except (OSError, WaveError):
        return False
