"""Root test conftest.

Puts the ``tests/`` directory itself on ``sys.path`` so cross-directory test
helpers (e.g. ``_hold_pnl_oracle``) can be shared by BARE-name import between the
``tests/unit`` and ``tests/engine`` suites.

Why this is needed:
  * ``tests`` is NOT usable as a package name here — a same-named ``tests``
    package is installed in site-packages and shadows it, so ``from tests.X``
    resolves to the wrong module.
  * pytest's default ``prepend`` import mode only inserts each test file's OWN
    directory on ``sys.path`` — enough for a same-dir helper (the existing
    ``_stream_fakes`` / ``_golden_corpus`` bare-import precedent) but NOT for a
    helper shared across ``tests/unit`` and ``tests/engine``.

conftest.py files are imported by pytest before it collects the test modules
beneath them, so this insertion is in place by the time any suite imports the
shared helper.
"""

import sys
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
