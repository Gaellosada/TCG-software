import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests (requires live PostgreSQL: dwh + app-data)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed."""
    if config.getoption("--run-integration"):
        return

    skip_integration = pytest.mark.skip(
        reason="needs --run-integration option (live PostgreSQL: dwh + app-data)"
    )
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)
