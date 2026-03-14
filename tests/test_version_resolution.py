"""Tests for orchestrator.main.resolve_versions."""

import json
from unittest.mock import patch, MagicMock

from orchestrator.main import resolve_versions, _pypi_version_cache


class TestResolveVersions:
    def setup_method(self):
        """Clear the module-level cache before each test."""
        _pypi_version_cache.clear()

    @patch("orchestrator.main._fetch_pypi_version")
    def test_resolves_single_package(self, mock_fetch):
        mock_fetch.return_value = ("requests", "2.31.0")
        result = resolve_versions(["requests"])
        assert result == {"requests": "2.31.0"}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_skips_pinned_packages(self, mock_fetch):
        result = resolve_versions(["requests==2.31.0"])
        mock_fetch.assert_not_called()
        assert result == {}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_strips_version_specifiers(self, mock_fetch):
        mock_fetch.return_value = ("requests", "2.31.0")
        result = resolve_versions(["requests>=2.0"])
        assert result == {"requests": "2.31.0"}
        mock_fetch.assert_called_once_with("requests")

    @patch("orchestrator.main._fetch_pypi_version")
    def test_caching_behavior(self, mock_fetch):
        mock_fetch.return_value = ("flask", "3.0.0")
        # First call should query
        result1 = resolve_versions(["flask"])
        assert result1 == {"flask": "3.0.0"}
        assert mock_fetch.call_count == 1

        # Second call should use cache
        result2 = resolve_versions(["flask"])
        assert result2 == {"flask": "3.0.0"}
        # Still only 1 call — cache hit
        assert mock_fetch.call_count == 1

    @patch("orchestrator.main._fetch_pypi_version")
    def test_failed_fetch_skipped(self, mock_fetch):
        mock_fetch.return_value = ("badpkg", None)
        result = resolve_versions(["badpkg"])
        assert result == {}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_exception_in_future_skipped(self, mock_fetch):
        mock_fetch.side_effect = RuntimeError("network error")
        result = resolve_versions(["pkg"])
        assert result == {}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_multiple_packages(self, mock_fetch):
        def fake_fetch(name):
            versions = {"numpy": "1.26.0", "pandas": "2.1.0"}
            return (name, versions.get(name))

        mock_fetch.side_effect = fake_fetch
        result = resolve_versions(["numpy", "pandas"])
        assert result == {"numpy": "1.26.0", "pandas": "2.1.0"}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_empty_package_list(self, mock_fetch):
        result = resolve_versions([])
        assert result == {}
        mock_fetch.assert_not_called()

    @patch("orchestrator.main._fetch_pypi_version")
    def test_mixed_pinned_and_unpinned(self, mock_fetch):
        mock_fetch.return_value = ("flask", "3.0.0")
        result = resolve_versions(["requests==2.31.0", "flask"])
        assert "requests" not in result
        assert result == {"flask": "3.0.0"}

    @patch("orchestrator.main._fetch_pypi_version")
    def test_tilde_specifier_stripped(self, mock_fetch):
        mock_fetch.return_value = ("django", "5.0.0")
        result = resolve_versions(["django~=4.2"])
        assert result == {"django": "5.0.0"}
        mock_fetch.assert_called_once_with("django")

    @patch("orchestrator.main.urllib.request.urlopen")
    def test_fetch_pypi_version_integration(self, mock_urlopen):
        """Test _fetch_pypi_version with mocked urlopen."""
        from orchestrator.main import _fetch_pypi_version

        response_data = json.dumps({
            "info": {"version": "2.31.0"}
        }).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = response_data
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        name, version = _fetch_pypi_version("requests")
        assert name == "requests"
        assert version == "2.31.0"

    @patch("orchestrator.main.urllib.request.urlopen")
    def test_fetch_pypi_version_timeout(self, mock_urlopen):
        """Test _fetch_pypi_version returns None on timeout."""
        from orchestrator.main import _fetch_pypi_version
        import socket

        mock_urlopen.side_effect = socket.timeout("timed out")

        name, version = _fetch_pypi_version("requests")
        assert name == "requests"
        assert version is None
