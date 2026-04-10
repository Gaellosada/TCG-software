import pytest


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed or
    the test is explicitly selected."""
    skip_integration = pytest.mark.skip(
        reason="needs --run-integration option or live MongoDB"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
