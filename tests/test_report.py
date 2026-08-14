from __future__ import annotations

import json
from pathlib import Path

from sampleproof.report import (
    _markdown_code,
    _markdown_text,
    render_json,
    render_manifest,
    render_markdown,
)
from sampleproof.scan import scan_pack
from tests.test_scan import config
from tests.wav_helpers import make_wav


def test_json_report_exposes_measurements_policy_and_method_boundaries(tmp_path: Path) -> None:
    wav = make_wav([(-32768,), (0,)])
    (tmp_path / "kick.wav").write_bytes(wav)
    result = scan_pack(config(all_zero="allow", duplicate_pcm="allow", full_scale=1), tmp_path)

    rendered = render_json(result)
    payload = json.loads(rendered)

    assert rendered.endswith("\n")
    assert rendered == render_json(result)
    assert payload["schema_version"] == 1
    assert payload["tool"] == {"name": "sampleproof", "version": "0.1.0"}
    assert payload["delivery_state"] == "RENDERED — QC INCOMPLETE"
    assert payload["source_root"] == "."
    assert payload["result"] == {
        "complete": True,
        "error_count": 0,
        "file_count": 1,
        "outcome": "pass",
        "warning_count": 0,
    }
    assert payload["brief"]["delivery"]["pack_id"] == "form-under-load-01"
    assert payload["brief"]["pcm"] == {
        "allowed_bit_depths": [16],
        "allowed_channels": [1],
        "allowed_sample_rates": [48000],
    }
    assert payload["brief"]["policy"] == {
        "all_zero": "allow",
        "duplicate_pcm": "allow",
        "max_abs_dc_offset": None,
        "max_full_scale_samples": 1,
        "max_sample_peak_dbfs": None,
    }
    file_payload = payload["files"][0]
    assert file_payload["path"] == "kick.wav"
    assert file_payload["outcome"] == "pass"
    assert file_payload["error"] is None
    assert file_payload["format"]["frame_count"] == 2
    assert file_payload["signal"]["sample_peak"] == 1.0
    assert file_payload["signal"]["sample_peak_dbfs"] == 0.0
    assert file_payload["signal"]["full_scale_samples"] == 1
    assert file_payload["signal"]["channels"][0]["dc_offset"] == -0.5
    assert file_payload["hashes"]["file_sha256"] == result.files[0].file_sha256
    assert len(file_payload["hashes"]["pcm_sha256"]) == 64
    assert "not true peak" in payload["definitions"]["sample_peak"]
    assert "not a clipping" in payload["definitions"]["full_scale_samples"]


def test_json_report_preserves_parser_error_and_incomplete_state(tmp_path: Path) -> None:
    (tmp_path / "bad.wav").write_bytes(b"bad")

    payload = json.loads(render_json(scan_pack(config(), tmp_path)))

    assert payload["result"]["outcome"] == "incomplete"
    assert payload["result"]["complete"] is False
    assert payload["files"][0]["format"] is None
    assert payload["files"][0]["signal"] is None
    assert payload["files"][0]["hashes"]["pcm_sha256"] is None
    assert payload["files"][0]["error"]["code"] == "container_too_short"
    assert str(tmp_path) not in render_json(scan_pack(config(), tmp_path))


def test_markdown_report_is_readable_without_overstating_qc(tmp_path: Path) -> None:
    (tmp_path / "zero.wav").write_bytes(make_wav([(0,), (0,)]))
    report = render_markdown(scan_pack(config(all_zero="warn", duplicate_pcm="allow"), tmp_path))

    assert "# sampleproof report" in report
    assert "RENDERED — QC INCOMPLETE" in report
    assert "Policy result: **PASS**" in report
    assert "`all_zero`" in report
    assert "zero.wav" in report
    assert "sample peak, not true peak" in report.lower()
    assert "does not determine clipping" in report.lower()
    assert report.endswith("\n")


def test_markdown_report_escapes_a_backtick_in_a_source_filename(tmp_path: Path) -> None:
    (tmp_path / "source`forged.wav").write_bytes(make_wav([(0,), (1,)]))

    report = render_markdown(scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path))

    assert "<code>source&#96;forged.wav</code>" in report


def test_markdown_helpers_render_controls_html_and_delimiters_as_visible_text() -> None:
    code = _markdown_code("line\n\x1b`|<tag>")
    prose = _markdown_text("# forged\r\n<script>**pass**")

    assert code == "<code>line&#x0A;&#x1B;&#96;&#124;&lt;tag&gt;</code>"
    assert "\n" not in code and "\x1b" not in code
    assert "<script>" not in prose
    assert "\n#" not in prose


def test_jsonl_manifest_covers_every_discovered_file_in_path_order(tmp_path: Path) -> None:
    (tmp_path / "bad.wav").write_bytes(b"bad")
    (tmp_path / "good.wav").write_bytes(make_wav([(0,), (1,)]))
    result = scan_pack(config(all_zero="allow", duplicate_pcm="allow"), tmp_path)

    lines = render_manifest(result).splitlines()

    assert [json.loads(line)["path"] for line in lines] == ["bad.wav", "good.wav"]
    assert all(len(json.loads(line)["sha256"]) == 64 for line in lines)
    assert [json.loads(line)["size_bytes"] for line in lines] == [
        len(b"bad"),
        len(make_wav([(0,), (1,)])),
    ]
