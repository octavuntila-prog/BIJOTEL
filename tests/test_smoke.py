"""F0 smoke test: package imports correctly."""


def test_import():
    """Verify bijotel imports and exposes version."""
    import bijotel

    assert bijotel.__version__ == "0.3.0"


def test_subpackages_importable():
    """Verify all subpackages are importable (placeholder check)."""
    import bijotel.adapters  # noqa: F401
    import bijotel.cli  # noqa: F401
    import bijotel.core  # noqa: F401
    import bijotel.exporters  # noqa: F401
    import bijotel.processors  # noqa: F401
