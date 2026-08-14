from sampleproof import __version__


def test_package_exposes_its_public_version() -> None:
    assert __version__ == "0.1.0"
