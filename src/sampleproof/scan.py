"""Pack scanning and declared-policy assessment."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sampleproof.config import Config
from sampleproof.discovery import DiscoveryError, discover_wav_files, open_source_root
from sampleproof.filesystem import directory_fd_matches_path
from sampleproof.wave import (
    WaveAnalysis,
    WaveError,
    analyze_wav_beneath,
    verify_file_facts_beneath,
)


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str
    paths: tuple[str, ...]
    observed: Any = None
    expected: Any = None


@dataclass(frozen=True)
class FileScan:
    relative_path: str
    size_bytes: int | None
    file_sha256: str | None
    outcome: str
    analysis: WaveAnalysis | None
    findings: tuple[Finding, ...]
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DuplicateGroup:
    pcm_sha256: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class ScanResult:
    config: Config
    source_root: Path
    outcome: str
    complete: bool
    files: tuple[FileScan, ...]
    duplicate_groups: tuple[DuplicateGroup, ...]
    findings: tuple[Finding, ...]


def _finding(
    code: str,
    message: str,
    path: str,
    *,
    observed: Any,
    expected: Any,
) -> Finding:
    return Finding(
        severity="error",
        code=code,
        message=message,
        paths=(path,),
        observed=observed,
        expected=expected,
    )


def _action_finding(
    action: str,
    code: str,
    message: str,
    paths: tuple[str, ...],
    *,
    observed: Any,
    expected: Any,
) -> Finding | None:
    if action == "allow":
        return None
    return Finding(
        severity="warning" if action == "warn" else "error",
        code=code,
        message=message,
        paths=paths,
        observed=observed,
        expected=expected,
    )


def _assess_file(config: Config, path: str, analysis: WaveAnalysis) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    wave_format = analysis.format
    signal = analysis.signal
    if wave_format.sample_rate not in config.pcm.allowed_sample_rates:
        findings.append(
            _finding(
                "sample_rate_not_allowed",
                "sample rate is outside the declared allowlist",
                path,
                observed=wave_format.sample_rate,
                expected=list(config.pcm.allowed_sample_rates),
            )
        )
    if wave_format.bits_per_sample not in config.pcm.allowed_bit_depths:
        findings.append(
            _finding(
                "bit_depth_not_allowed",
                "bit depth is outside the declared allowlist",
                path,
                observed=wave_format.bits_per_sample,
                expected=list(config.pcm.allowed_bit_depths),
            )
        )
    if wave_format.channels not in config.pcm.allowed_channels:
        findings.append(
            _finding(
                "channel_count_not_allowed",
                "channel count is outside the declared allowlist",
                path,
                observed=wave_format.channels,
                expected=list(config.pcm.allowed_channels),
            )
        )
    peak_limit = config.policy.max_sample_peak_dbfs
    if (
        peak_limit is not None
        and signal.sample_peak_dbfs is not None
        and signal.sample_peak_dbfs > peak_limit
    ):
        findings.append(
            _finding(
                "sample_peak_exceeded",
                "sample peak exceeds the declared maximum",
                path,
                observed=signal.sample_peak_dbfs,
                expected=peak_limit,
            )
        )
    full_scale_limit = config.policy.max_full_scale_samples
    if full_scale_limit is not None and signal.full_scale_samples > full_scale_limit:
        findings.append(
            _finding(
                "full_scale_samples_exceeded",
                "full-scale sample count exceeds the declared maximum",
                path,
                observed=signal.full_scale_samples,
                expected=full_scale_limit,
            )
        )
    dc_limit = config.policy.max_abs_dc_offset
    max_abs_dc = max(abs(item.dc_offset) for item in signal.channels)
    if dc_limit is not None and max_abs_dc > dc_limit:
        findings.append(
            _finding(
                "dc_offset_exceeded",
                "absolute raw-sample DC offset exceeds the declared maximum",
                path,
                observed=max_abs_dc,
                expected=dc_limit,
            )
        )
    if signal.all_zero:
        item = _action_finding(
            config.policy.all_zero,
            "all_zero",
            "file contains digital zero in every frame",
            (path,),
            observed=True,
            expected=False,
        )
        if item is not None:
            findings.append(item)
    return tuple(findings)


def _outcome_for_findings(findings: tuple[Finding, ...]) -> str:
    if any(item.severity == "error" for item in findings):
        return "fail"
    if findings:
        return "warn"
    return "pass"


def _scan_open_root(config: Config, root: Path, root_fd: int) -> ScanResult:
    discovered = discover_wav_files(root, root_fd=root_fd)
    if not discovered:
        finding = Finding(
            severity="error",
            code="no_wav_files",
            message="source root contains no regular WAV files",
            paths=(),
        )
        return ScanResult(
            config=config,
            source_root=root,
            outcome="incomplete",
            complete=False,
            files=(),
            duplicate_groups=(),
            findings=(finding,),
        )

    file_results: list[FileScan] = []
    findings: list[Finding] = []
    incomplete = False
    hashes: dict[str, list[str]] = {}
    for item in discovered:
        try:
            analysis = analyze_wav_beneath(root_fd, item.relative_path)
        except WaveError as exc:
            incomplete = True
            finding = Finding(
                severity="error",
                code="invalid_wav",
                message=str(exc),
                paths=(item.relative_path,),
                observed=exc.code,
                expected="supported classic integer PCM WAV",
            )
            findings.append(finding)
            file_results.append(
                FileScan(
                    relative_path=item.relative_path,
                    size_bytes=exc.file_size_bytes,
                    file_sha256=exc.file_sha256,
                    outcome="error",
                    analysis=None,
                    findings=(finding,),
                    error_code=exc.code,
                    error_message=str(exc),
                )
            )
            continue

        file_findings = _assess_file(config, item.relative_path, analysis)
        findings.extend(file_findings)
        file_results.append(
            FileScan(
                relative_path=item.relative_path,
                size_bytes=analysis.file_size_bytes,
                file_sha256=analysis.hashes.file_sha256,
                outcome=_outcome_for_findings(file_findings),
                analysis=analysis,
                findings=file_findings,
            )
        )
        hashes.setdefault(analysis.hashes.pcm_sha256, []).append(item.relative_path)

    if not incomplete:
        final_discovered = discover_wav_files(root, root_fd=root_fd)
        initial_paths = tuple(item.relative_path for item in discovered)
        final_paths = tuple(item.relative_path for item in final_discovered)
        if final_paths != initial_paths:
            raise DiscoveryError("source WAV inventory changed during scan")

    for item in file_results:
        if item.size_bytes is None or item.file_sha256 is None:
            continue
        if not verify_file_facts_beneath(
            root_fd,
            item.relative_path,
            expected_size=item.size_bytes,
            expected_sha256=item.file_sha256,
        ):
            raise DiscoveryError(f"source WAV changed after measurement: {item.relative_path}")

    duplicate_groups = tuple(
        DuplicateGroup(pcm_sha256=digest, paths=tuple(paths))
        for digest, paths in sorted(hashes.items(), key=lambda pair: (tuple(pair[1]), pair[0]))
        if len(paths) > 1
    )
    duplicate_findings: list[Finding] = []
    for group in duplicate_groups:
        finding = _action_finding(
            config.policy.duplicate_pcm,
            "duplicate_pcm",
            "files contain the same canonical PCM stream and format",
            group.paths,
            observed=group.pcm_sha256,
            expected="unique canonical PCM fingerprint",
        )
        if finding is not None:
            duplicate_findings.append(finding)
            findings.append(finding)

    if duplicate_findings:
        by_path: dict[str, list[Finding]] = {}
        for finding in duplicate_findings:
            for path in finding.paths:
                by_path.setdefault(path, []).append(finding)
        file_results = [
            replace(
                item,
                findings=item.findings + tuple(by_path.get(item.relative_path, ())),
                outcome=_outcome_for_findings(
                    item.findings + tuple(by_path.get(item.relative_path, ()))
                ),
            )
            if item.analysis is not None
            else item
            for item in file_results
        ]

    if incomplete:
        outcome = "incomplete"
    elif any(item.severity == "error" for item in findings):
        outcome = "fail"
    else:
        outcome = "pass"
    return ScanResult(
        config=config,
        source_root=root,
        outcome=outcome,
        complete=not incomplete,
        files=tuple(file_results),
        duplicate_groups=duplicate_groups,
        findings=tuple(findings),
    )


def scan_pack(config: Config, source_root: str | Path) -> ScanResult:
    """Analyze every discovered WAV beneath one fixed, no-follow source-root handle."""

    root, root_fd = open_source_root(source_root)
    try:
        result = _scan_open_root(config, root, root_fd)
        if not directory_fd_matches_path(root_fd, root):
            raise DiscoveryError("source root changed identity during scan")
        return result
    finally:
        os.close(root_fd)
