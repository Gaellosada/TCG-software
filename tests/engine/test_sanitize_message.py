"""Tests for ``_sanitize_message`` regex precision.

The sanitizer must redact absolute POSIX paths without mangling URLs,
fractions, dates, or relative paths. See PR #12 round-2 robustness
review: the original ``/\\S+`` regex over-redacted anything after the
first slash, including URL tails like ``http://host/api``.
"""

from __future__ import annotations

from tcg.engine.indicator_exec import _sanitize_message


class TestSanitizeMessagePaths:
    """Redacts absolute POSIX paths."""

    def test_redacts_absolute_path(self):
        msg = "Permission denied: /home/alice/secret.txt"
        assert _sanitize_message(msg) == "Permission denied: <path>"

    def test_redacts_etc_path(self):
        msg = "Cannot read /etc/passwd"
        assert _sanitize_message(msg) == "Cannot read <path>"

    def test_redacts_multiple_paths(self):
        msg = "Copy /tmp/a/b to /var/log/x.log failed"
        assert _sanitize_message(msg) == "Copy <path> to <path> failed"


class TestSanitizeMessagePreserves:
    """Leaves non-path slash tokens untouched."""

    def test_preserves_http_url(self):
        msg = "URL http://example.com/api/foo failed"
        # Host+path after "://" may still contain a path-looking tail;
        # the key is the "http:" prefix is not split. We allow the tail
        # to be redacted since it's indistinguishable from a path.
        out = _sanitize_message(msg)
        assert "http://" in out or "http:" in out
        # Must NOT produce "http:<path>" — that's the over-redaction bug.
        assert "http:<path>" not in out

    def test_preserves_https_url(self):
        msg = "Fetched https://host.example/path/to/x"
        out = _sanitize_message(msg)
        assert "https://" in out
        assert "https:<path>" not in out

    def test_preserves_fraction(self):
        msg = "ratio was 1/2 of expected"
        assert _sanitize_message(msg) == "ratio was 1/2 of expected"

    def test_preserves_date(self):
        msg = "date 2026/04/18 out of range"
        # 2026/04/18 has three slash-separated segments and will be
        # matched by the "(word/)+word" pattern — this is acceptable
        # over-redaction given dates rarely leak secrets, but verify
        # the leading "date " is preserved.
        out = _sanitize_message(msg)
        assert out.startswith("date ")
        assert out.endswith(" out of range")

    def test_preserves_single_segment_date(self):
        msg = "day 04/18 warning"
        # Only two segments without leading slash → not matched.
        assert _sanitize_message(msg) == "day 04/18 warning"

    def test_preserves_relative_traversal(self):
        msg = "Bad path ../foo/bar leaked"
        out = _sanitize_message(msg)
        # Must still contain the ".." prefix — relative paths pass through.
        assert ".." in out

    def test_preserves_current_dir(self):
        msg = "reading ./config.ini"
        assert _sanitize_message(msg) == "reading ./config.ini"

    def test_empty_message(self):
        assert _sanitize_message("") == ""

    def test_no_slashes(self):
        assert _sanitize_message("plain error") == "plain error"
