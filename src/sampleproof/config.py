"""Strict TOML configuration for sampleproof."""

from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a brief cannot be read or does not satisfy the schema."""


@dataclass(frozen=True)
class DeliveryConfig:
    pack_id: str
    title: str
    version: str
    license: str


@dataclass(frozen=True)
class PcmConfig:
    allowed_sample_rates: tuple[int, ...]
    allowed_bit_depths: tuple[int, ...]
    allowed_channels: tuple[int, ...]


@dataclass(frozen=True)
class PolicyConfig:
    max_sample_peak_dbfs: float | None = None
    max_full_scale_samples: int | None = None
    max_abs_dc_offset: float | None = None
    all_zero: str = "fail"
    duplicate_pcm: str = "fail"


@dataclass(frozen=True)
class Config:
    schema_version: int
    delivery: DeliveryConfig
    pcm: PcmConfig
    policy: PolicyConfig


def _reject_unknown(table: dict[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        location = f" in {where}" if where else ""
        raise ConfigError(f"unknown key{location}: {unknown[0]}")


def _require_table(root: dict[str, Any], name: str) -> dict[str, Any]:
    value = root.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a TOML table")
    return value


def _required_text(table: dict[str, Any], key: str, where: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _integer_list(
    table: dict[str, Any], key: str, *, allowed: set[int] | None = None
) -> tuple[int, ...]:
    value = table.get(key)
    if not isinstance(value, list) or not value:
        raise ConfigError(f"pcm.{key} must be a non-empty array")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
        raise ConfigError(f"pcm.{key} must contain only integers")
    if len(set(value)) != len(value):
        raise ConfigError(f"pcm.{key} must not contain duplicates")
    if allowed is not None and any(item not in allowed for item in value):
        choices = ", ".join(str(item) for item in sorted(allowed))
        raise ConfigError(f"pcm.{key} values must be one of: {choices}")
    if allowed is None and any(item < 1 or item > 768_000 for item in value):
        raise ConfigError(f"pcm.{key} values must be between 1 and 768000")
    return tuple(value)


def _optional_float(
    table: dict[str, Any], key: str, *, minimum: float | None = None, maximum: float | None = None
) -> float | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"policy.{key} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"policy.{key} must be a finite number")
    if minimum is not None and result < minimum:
        raise ConfigError(f"policy.{key} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ConfigError(f"policy.{key} must be at most {maximum}")
    return result


def _optional_nonnegative_integer(table: dict[str, Any], key: str) -> int | None:
    value = table.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfigError(f"policy.{key} must be a non-negative integer")
    return value


def _action(table: dict[str, Any], key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or value not in {"allow", "warn", "fail"}:
        raise ConfigError(f"policy.{key} must be allow, warn, or fail")
    return value


def load_config(path: str | Path) -> Config:
    """Read and validate a version-1 sampleproof brief."""

    brief_path = Path(path)
    try:
        with brief_path.open("rb") as handle:
            root = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"cannot read brief: {brief_path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read brief: {brief_path}: {exc}") from exc
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"invalid TOML in brief: {exc}") from exc

    _reject_unknown(root, {"schema_version", "delivery", "pcm", "policy"}, "")
    schema_version = root.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        raise ConfigError("schema_version must be integer 1")

    delivery = _require_table(root, "delivery")
    _reject_unknown(delivery, {"pack_id", "title", "version", "license"}, "delivery")
    delivery_config = DeliveryConfig(
        pack_id=_required_text(delivery, "pack_id", "delivery"),
        title=_required_text(delivery, "title", "delivery"),
        version=_required_text(delivery, "version", "delivery"),
        license=_required_text(delivery, "license", "delivery"),
    )

    pcm = _require_table(root, "pcm")
    _reject_unknown(
        pcm,
        {"allowed_sample_rates", "allowed_bit_depths", "allowed_channels"},
        "pcm",
    )
    pcm_config = PcmConfig(
        allowed_sample_rates=_integer_list(pcm, "allowed_sample_rates"),
        allowed_bit_depths=_integer_list(pcm, "allowed_bit_depths", allowed={8, 16, 24, 32}),
        allowed_channels=_integer_list(pcm, "allowed_channels", allowed={1, 2}),
    )

    policy_value = root.get("policy", {})
    if not isinstance(policy_value, dict):
        raise ConfigError("policy must be a TOML table")
    policy = policy_value
    _reject_unknown(
        policy,
        {
            "max_sample_peak_dbfs",
            "max_full_scale_samples",
            "max_abs_dc_offset",
            "all_zero",
            "duplicate_pcm",
        },
        "policy",
    )
    policy_config = PolicyConfig(
        max_sample_peak_dbfs=_optional_float(policy, "max_sample_peak_dbfs", maximum=0.0),
        max_full_scale_samples=_optional_nonnegative_integer(policy, "max_full_scale_samples"),
        max_abs_dc_offset=_optional_float(policy, "max_abs_dc_offset", minimum=0.0, maximum=1.0),
        all_zero=_action(policy, "all_zero", "fail"),
        duplicate_pcm=_action(policy, "duplicate_pcm", "fail"),
    )

    return Config(
        schema_version=1,
        delivery=delivery_config,
        pcm=pcm_config,
        policy=policy_config,
    )
