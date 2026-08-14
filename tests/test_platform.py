from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from sampleproof import filesystem
from sampleproof.discovery import DiscoveryError, discover_wav_files


def test_safe_traversal_fails_closed_when_required_platform_primitives_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(filesystem, "_safe_platform_available", lambda: False)

    with pytest.raises(OSError) as raised:
        filesystem.require_safe_platform()

    assert raised.value.errno == errno.ENOTSUP


@pytest.mark.skipif(os.name != "nt", reason="Windows-specific fail-closed contract")
def test_windows_scan_fails_closed_instead_of_following_reparse_points(tmp_path: Path) -> None:
    with pytest.raises(DiscoveryError, match="POSIX"):
        discover_wav_files(tmp_path)
