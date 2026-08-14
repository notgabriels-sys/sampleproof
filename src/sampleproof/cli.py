"""Command-line interface for sampleproof."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from sampleproof import __version__
from sampleproof.config import ConfigError, load_config
from sampleproof.discovery import DiscoveryError
from sampleproof.packet import PacketError, build_packet
from sampleproof.report import render_json, render_markdown
from sampleproof.scan import ScanResult, scan_pack


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="sampleproof",
        description="Deterministic, declared-policy QC for classic integer PCM WAV sample packs.",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command")

    check = commands.add_parser("check", help="analyze a pack and print one report")
    check.add_argument("brief", type=Path, help="version-1 TOML brief")
    check.add_argument("source_root", type=Path, help="directory scanned recursively")
    check.add_argument("--json", action="store_true", help="print JSON instead of Markdown")

    build = commands.add_parser("build", help="analyze and publish a new report packet")
    build.add_argument("brief", type=Path, help="version-1 TOML brief")
    build.add_argument("source_root", type=Path, help="directory scanned recursively")
    build.add_argument("--output", type=Path, required=True, help="new output directory")
    build.add_argument("--json", action="store_true", help="print JSON instead of Markdown")
    return parser


def _exit_code(result: ScanResult) -> int:
    return {"pass": 0, "fail": 2, "incomplete": 1}[result.outcome]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its documented process exit code."""

    parser = _parser()
    args = parser.parse_args(argv)
    if args.version:
        print(f"sampleproof {__version__}")
        return 0
    if args.command is None:
        parser.error("a command is required")

    try:
        config = load_config(args.brief)
        result = scan_pack(config, args.source_root)
        if args.command == "build":
            build_packet(result, args.output)
        sys.stdout.write(render_json(result) if args.json else render_markdown(result))
        return _exit_code(result)
    except (ConfigError, DiscoveryError, PacketError, OSError) as exc:
        print(f"sampleproof: error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("sampleproof: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover - exercised through package entry points
    raise SystemExit(main())
