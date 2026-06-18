import os
from pathlib import Path

import pytest
from dotenv import dotenv_values


def _app_db_creds_present() -> bool:
    """True when app-data PostgreSQL credentials are configured.

    Checks the live environment first, then the repo-root ``.env`` (the same
    source ``load_app_db_config`` reads).  Integration tests that hit the real
    ``tcg_app_data`` schema gate their module-level ``skipif`` on this so they
    are silently skipped on a machine with no DB credentials.

    Lives in ``conftest`` so the four persistence integration modules share one
    copy; they import it with a bare ``from conftest import _app_db_creds_present``
    (``tests/integration`` is not a package, so pytest's prepend import mode
    puts this directory on ``sys.path`` and the bare name resolves here).
    """
    env = dotenv_values(Path(__file__).resolve().parents[2] / ".env")
    user = os.environ.get("APP_DB_USER") or env.get("APP_DB_USER")
    password = os.environ.get("APP_DB_PASSWORD") or env.get("APP_DB_PASSWORD")
    return bool(user and password)


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
