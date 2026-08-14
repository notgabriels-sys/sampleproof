from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from sampleproof import __version__
from sampleproof.cli import main
from tests.test_config import VALID_BRIEF
from tests.wav_helpers import make_wav


def brief(path: Path, *, all_zero: str = "allow") -> Path:
    text = VALID_BRIEF.replace('all_zero = "fail"', f'all_zero = "{all_zero}"')
    text = text.replace('duplicate_pcm = "warn"', 'duplicate_pcm = "allow"')
    text = text.replace("allowed_bit_depths = [24]", "allowed_bit_depths = [16]")
    path.write_text(text, encoding="utf-8")
    return path


def test_version_works_through_main_and_python_module(capsys, tmp_path: Path) -> None:
    assert __version__ == version("sampleproof")
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"sampleproof {__version__}\n"
    assert captured.err == ""

    completed = subprocess.run(
        [sys.executable, "-m", "sampleproof", "--version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert completed.stdout == f"sampleproof {__version__}\n"
    assert completed.stderr == ""


def test_check_prints_markdown_and_returns_zero_for_pass(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "sound.wav").write_bytes(make_wav([(0,), (1,)]))

    code = main(["check", str(brief(tmp_path / "brief.toml")), str(source)])

    captured = capsys.readouterr()
    assert code == 0
    assert "# sampleproof report" in captured.out
    assert "Policy result: **PASS**" in captured.out
    assert captured.err == ""


def test_check_json_and_exit_two_for_completed_policy_failure(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "zero.wav").write_bytes(make_wav([(0,), (0,)]))

    code = main(
        ["check", str(brief(tmp_path / "brief.toml", all_zero="fail")), str(source), "--json"]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["result"] == {
        "complete": True,
        "error_count": 1,
        "file_count": 1,
        "outcome": "fail",
        "warning_count": 0,
    }
    assert captured.err == ""


def test_check_returns_one_for_incomplete_analysis(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "broken.wav").write_bytes(b"broken")

    code = main(["check", str(brief(tmp_path / "brief.toml")), str(source), "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["result"]["outcome"] == "incomplete"


def test_build_publishes_packet_and_keeps_policy_exit_semantics(tmp_path: Path, capsys) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "zero.wav").write_bytes(make_wav([(0,), (0,)]))
    output = tmp_path / "packet"

    code = main(
        [
            "build",
            str(brief(tmp_path / "brief.toml", all_zero="fail")),
            str(source),
            "--output",
            str(output),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["result"]["outcome"] == "fail"
    assert sorted(path.name for path in output.iterdir()) == [
        "sampleproof-manifest.jsonl",
        "sampleproof-report.json",
        "sampleproof-report.md",
    ]


def test_operational_errors_use_stderr_and_exit_one(tmp_path: Path, capsys) -> None:
    code = main(["check", str(tmp_path / "missing.toml"), str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.out == ""
    assert captured.err.startswith("sampleproof: error:")
    assert "cannot read brief" in captured.err


def test_invalid_cli_usage_uses_operational_exit_one(capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["check"])

    captured = capsys.readouterr()
    assert raised.value.code == 1
    assert "usage:" in captured.err
