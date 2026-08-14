from __future__ import annotations

from pathlib import Path

import pytest

from sampleproof.config import ConfigError, load_config

VALID_BRIEF = """\
schema_version = 1

[delivery]
pack_id = "form-under-load-01"
title = "FORM UNDER LOAD 01"
version = "1.0.0"
license = "Commercial sample license"

[pcm]
allowed_sample_rates = [44100, 48000]
allowed_bit_depths = [24]
allowed_channels = [1, 2]

[policy]
max_sample_peak_dbfs = -0.1
max_full_scale_samples = 0
max_abs_dc_offset = 0.001
all_zero = "fail"
duplicate_pcm = "warn"
"""


def write_brief(path: Path, text: str = VALID_BRIEF) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_returns_normalized_declared_policy(tmp_path: Path) -> None:
    config = load_config(write_brief(tmp_path / "brief.toml"))

    assert config.schema_version == 1
    assert config.delivery.pack_id == "form-under-load-01"
    assert config.delivery.title == "FORM UNDER LOAD 01"
    assert config.delivery.version == "1.0.0"
    assert config.delivery.license == "Commercial sample license"
    assert config.pcm.allowed_sample_rates == (44100, 48000)
    assert config.pcm.allowed_bit_depths == (24,)
    assert config.pcm.allowed_channels == (1, 2)
    assert config.policy.max_sample_peak_dbfs == -0.1
    assert config.policy.max_full_scale_samples == 0
    assert config.policy.max_abs_dc_offset == 0.001
    assert config.policy.all_zero == "fail"
    assert config.policy.duplicate_pcm == "warn"


def test_policy_has_only_conservative_structural_defaults(tmp_path: Path) -> None:
    text = VALID_BRIEF.replace(
        "[policy]\nmax_sample_peak_dbfs = -0.1\nmax_full_scale_samples = 0\n"
        'max_abs_dc_offset = 0.001\nall_zero = "fail"\nduplicate_pcm = "warn"\n',
        "",
    )

    config = load_config(write_brief(tmp_path / "brief.toml", text))

    assert config.policy.max_sample_peak_dbfs is None
    assert config.policy.max_full_scale_samples is None
    assert config.policy.max_abs_dc_offset is None
    assert config.policy.all_zero == "fail"
    assert config.policy.duplicate_pcm == "fail"


@pytest.mark.parametrize(
    ("mutation", "message_fragment"),
    [
        ("schema_version = 2", "schema_version"),
        ("schema_version = 1.0", "schema_version"),
        ("schema_version = true", "schema_version"),
        ('license = ""', "delivery.license"),
        ("allowed_sample_rates = []", "allowed_sample_rates"),
        ("allowed_sample_rates = [true]", "allowed_sample_rates"),
        ("allowed_bit_depths = [20]", "allowed_bit_depths"),
        ("allowed_channels = [3]", "allowed_channels"),
        ("max_sample_peak_dbfs = 0.1", "max_sample_peak_dbfs"),
        ("max_full_scale_samples = -1", "max_full_scale_samples"),
        ("max_abs_dc_offset = 1.1", "max_abs_dc_offset"),
        ('all_zero = "maybe"', "all_zero"),
        ('all_zero = ["fail"]', "all_zero"),
        ('duplicate_pcm = { invalid = "fail" }', "duplicate_pcm"),
    ],
)
def test_load_config_rejects_invalid_declared_values(
    tmp_path: Path, mutation: str, message_fragment: str
) -> None:
    original = next(
        line for line in VALID_BRIEF.splitlines() if line.startswith(mutation.split(" =")[0])
    )
    text = VALID_BRIEF.replace(original, mutation)

    with pytest.raises(ConfigError, match=message_fragment):
        load_config(write_brief(tmp_path / "brief.toml", text))


@pytest.mark.parametrize(
    "text",
    [
        VALID_BRIEF.replace("[delivery]\n", "unexpected = 1\n\n[delivery]\n"),
        VALID_BRIEF.replace("[delivery]\n", '[delivery]\nunexpected = "x"\n'),
        VALID_BRIEF.replace("[pcm]\n", "[pcm]\nunexpected = 1\n"),
        VALID_BRIEF.replace("[policy]\n", "[policy]\nunexpected = 1\n"),
    ],
)
def test_load_config_rejects_unknown_keys_at_every_level(tmp_path: Path, text: str) -> None:
    with pytest.raises(ConfigError, match="unknown"):
        load_config(write_brief(tmp_path / "brief.toml", text))


def test_load_config_wraps_missing_and_malformed_input(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write_brief(tmp_path / "bad.toml", "[broken"))


def test_load_config_wraps_invalid_utf8_as_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.toml"
    path.write_bytes(b"schema_version = 1\n\xff")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)
