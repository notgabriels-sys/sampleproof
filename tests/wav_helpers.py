from __future__ import annotations

import struct
from collections.abc import Iterable, Sequence


def chunk(chunk_id: bytes, payload: bytes, *, include_padding: bool = True) -> bytes:
    padding = b"\x00" if include_padding and len(payload) % 2 else b""
    return chunk_id + struct.pack("<I", len(payload)) + payload + padding


def encode_sample(value: int, bits_per_sample: int) -> bytes:
    if bits_per_sample == 8:
        return bytes([value])
    return value.to_bytes(bits_per_sample // 8, "little", signed=True)


def pcm_bytes(frames: Iterable[Sequence[int]], bits_per_sample: int) -> bytes:
    return b"".join(encode_sample(sample, bits_per_sample) for frame in frames for sample in frame)


def make_wav(
    frames: Sequence[Sequence[int]],
    *,
    bits_per_sample: int = 16,
    channels: int | None = None,
    sample_rate: int = 48_000,
    audio_format: int = 1,
    before_fmt: Sequence[tuple[bytes, bytes]] = (),
    between_fmt_and_data: Sequence[tuple[bytes, bytes]] = (),
    after_data: Sequence[tuple[bytes, bytes]] = (),
) -> bytes:
    channel_count = channels if channels is not None else len(frames[0])
    block_align = channel_count * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    fmt = struct.pack(
        "<HHIIHH",
        audio_format,
        channel_count,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    body = b"".join(chunk(kind, payload) for kind, payload in before_fmt)
    body += chunk(b"fmt ", fmt)
    body += b"".join(chunk(kind, payload) for kind, payload in between_fmt_and_data)
    body += chunk(b"data", pcm_bytes(frames, bits_per_sample))
    body += b"".join(chunk(kind, payload) for kind, payload in after_data)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WAVE" + body
