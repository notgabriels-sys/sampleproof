"""Stable machine-readable and human-readable sampleproof reports."""

from __future__ import annotations

import html
import json
from typing import Any

from sampleproof import __version__
from sampleproof.scan import FileScan, Finding, ScanResult

DELIVERY_STATE = "RENDERED — QC INCOMPLETE"

DEFINITIONS = {
    "dc_offset": (
        "Arithmetic mean of normalized raw PCM samples per channel; no filtering is applied."
    ),
    "digital_zero": "A frame is digital zero only when every channel sample equals zero.",
    "file_sha256": "SHA-256 over every byte of the source file.",
    "full_scale_samples": (
        "Count of integer samples equal to either numeric endpoint; this is not a clipping "
        "determination."
    ),
    "pcm_sha256": (
        "SHA-256 over the sampleproof-pcm-v1 domain, channel count, sample rate, bit depth, "
        "frame count, and exact PCM data bytes."
    ),
    "sample_peak": (
        "Maximum absolute decoded PCM sample normalized by 2^(bit_depth-1); sample peak is "
        "not true peak."
    ),
}


def _finding_dict(finding: Finding) -> dict[str, Any]:
    return {
        "code": finding.code,
        "expected": finding.expected,
        "message": finding.message,
        "observed": finding.observed,
        "paths": list(finding.paths),
        "severity": finding.severity,
    }


def _file_dict(file_result: FileScan) -> dict[str, Any]:
    analysis = file_result.analysis
    if analysis is None:
        wave_format = None
        signal = None
        pcm_sha256 = None
    else:
        wave_format = {
            "audio_format": analysis.format.audio_format,
            "bits_per_sample": analysis.format.bits_per_sample,
            "block_align": analysis.format.block_align,
            "byte_rate": analysis.format.byte_rate,
            "channels": analysis.format.channels,
            "duration_seconds": analysis.format.duration_seconds,
            "frame_count": analysis.format.frame_count,
            "sample_rate": analysis.format.sample_rate,
        }
        signal = {
            "all_zero": analysis.signal.all_zero,
            "channels": [
                {
                    "channel": item.channel,
                    "dc_offset": item.dc_offset,
                    "full_scale_samples": item.full_scale_samples,
                    "sample_peak": item.sample_peak,
                    "sample_peak_dbfs": item.sample_peak_dbfs,
                }
                for item in analysis.signal.channels
            ],
            "first_nonzero_frame": analysis.signal.first_nonzero_frame,
            "full_scale_frames": analysis.signal.full_scale_frames,
            "full_scale_samples": analysis.signal.full_scale_samples,
            "last_nonzero_frame": analysis.signal.last_nonzero_frame,
            "leading_zero_frames": analysis.signal.leading_zero_frames,
            "sample_peak": analysis.signal.sample_peak,
            "sample_peak_dbfs": analysis.signal.sample_peak_dbfs,
            "trailing_zero_frames": analysis.signal.trailing_zero_frames,
        }
        pcm_sha256 = analysis.hashes.pcm_sha256
    error = None
    if file_result.error_code is not None:
        error = {"code": file_result.error_code, "message": file_result.error_message}
    return {
        "error": error,
        "findings": [_finding_dict(item) for item in file_result.findings],
        "format": wave_format,
        "hashes": {
            "file_sha256": file_result.file_sha256,
            "pcm_sha256": pcm_sha256,
        },
        "outcome": file_result.outcome,
        "path": file_result.relative_path,
        "signal": signal,
        "size_bytes": file_result.size_bytes,
    }


def result_to_dict(result: ScanResult) -> dict[str, Any]:
    """Convert a result to the stable version-1 report schema."""

    errors = sum(item.severity == "error" for item in result.findings)
    warnings = sum(item.severity == "warning" for item in result.findings)
    return {
        "brief": {
            "delivery": {
                "license": result.config.delivery.license,
                "pack_id": result.config.delivery.pack_id,
                "title": result.config.delivery.title,
                "version": result.config.delivery.version,
            },
            "pcm": {
                "allowed_bit_depths": list(result.config.pcm.allowed_bit_depths),
                "allowed_channels": list(result.config.pcm.allowed_channels),
                "allowed_sample_rates": list(result.config.pcm.allowed_sample_rates),
            },
            "policy": {
                "all_zero": result.config.policy.all_zero,
                "duplicate_pcm": result.config.policy.duplicate_pcm,
                "max_abs_dc_offset": result.config.policy.max_abs_dc_offset,
                "max_full_scale_samples": result.config.policy.max_full_scale_samples,
                "max_sample_peak_dbfs": result.config.policy.max_sample_peak_dbfs,
            },
            "schema_version": result.config.schema_version,
        },
        "definitions": DEFINITIONS,
        "delivery_state": DELIVERY_STATE,
        "duplicate_groups": [
            {"paths": list(group.paths), "pcm_sha256": group.pcm_sha256}
            for group in result.duplicate_groups
        ],
        "files": [_file_dict(item) for item in result.files],
        "findings": [_finding_dict(item) for item in result.findings],
        "result": {
            "complete": result.complete,
            "error_count": errors,
            "file_count": len(result.files),
            "outcome": result.outcome,
            "warning_count": warnings,
        },
        "schema_version": 1,
        "source_root": ".",
        "tool": {"name": "sampleproof", "version": __version__},
    }


def render_json(result: ScanResult) -> str:
    """Render the stable report schema as UTF-8 JSON text."""

    return (
        json.dumps(
            result_to_dict(result), ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def _html_visible(value: str) -> str:
    return "".join(
        f"&#x{ord(character):02X};"
        if ord(character) < 32 or ord(character) == 127
        else html.escape(character, quote=False)
        for character in value
    )


def _markdown_text(value: str) -> str:
    escaped = _html_visible(value)
    for character in "\\`*_{}[]()#+-.!|":
        escaped = escaped.replace(character, f"&#{ord(character)};")
    return escaped


def _markdown_code(value: str) -> str:
    escaped = _html_visible(value).replace("|", "&#124;").replace("`", "&#96;")
    return f"<code>{escaped}</code>"


def render_markdown(result: ScanResult) -> str:
    """Render a concise report with the method limits kept in view."""

    lines = [
        "# sampleproof report",
        "",
        f"Delivery state: **{DELIVERY_STATE}**",
        "",
        f"Policy result: **{result.outcome.upper()}**",
        "",
        f"Pack: **{_markdown_text(result.config.delivery.title)}** "
        f"({_markdown_code(result.config.delivery.version)})",
        "",
        f"Files discovered: **{len(result.files)}**. "
        f"Analysis complete: **{'yes' if result.complete else 'no'}**.",
        "",
        "## Findings",
        "",
    ]
    if result.findings:
        for finding in result.findings:
            paths = ", ".join(_markdown_code(path) for path in finding.paths)
            suffix = f" ({paths})" if paths else ""
            lines.append(
                f"- **{finding.severity.upper()}** `{finding.code}`: "
                f"{_markdown_text(finding.message)}{suffix}"
            )
    else:
        lines.append("- No policy findings.")
    lines.extend(
        [
            "",
            "## Files",
            "",
            "| Path | Result | Format | Peak (dBFS) | Full-scale samples |",
            "|---|---:|---|---:|---:|",
        ]
    )
    for item in result.files:
        if item.analysis is None:
            format_text = "unsupported / invalid"
            peak = "—"
            full_scale = "—"
        else:
            wave_format = item.analysis.format
            format_text = (
                f"{wave_format.sample_rate} Hz / {wave_format.bits_per_sample}-bit / "
                f"{wave_format.channels} ch"
            )
            peak_value = item.analysis.signal.sample_peak_dbfs
            peak = "-inf" if peak_value is None else f"{peak_value:.6f}"
            full_scale = str(item.analysis.signal.full_scale_samples)
        lines.append(
            f"| {_markdown_code(item.relative_path)} | {item.outcome.upper()} | "
            f"{format_text} | {peak} | {full_scale} |"
        )
    lines.extend(
        [
            "",
            "## Method boundaries",
            "",
            "- Reported peak is sample peak, not true peak or inter-sample peak.",
            "- Full-scale endpoint counting does not determine clipping.",
            "- DC offset is the arithmetic mean of raw normalized samples; no filter is applied.",
            "- This tool does not assess loudness, phase, loop quality, naming, metadata, rights, "
            "or artistic suitability.",
            "",
        ]
    )
    return "\n".join(lines)


def render_manifest(result: ScanResult) -> str:
    """Render a newline-delimited JSON integrity manifest for all discovered WAVs."""

    entries = [
        json.dumps(
            {
                "path": item.relative_path,
                "sha256": item.file_sha256,
                "size_bytes": item.size_bytes,
            },
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        for item in result.files
    ]
    return "\n".join(entries) + ("\n" if entries else "")
