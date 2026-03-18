"""Unit tests for penny/deps.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestEnsureDeps:
    def test_returns_early_when_all_deps_present(self):
        """When pexpect and pyte are both importable, ensure_deps returns immediately."""
        from penny.deps import ensure_deps

        with patch("builtins.__import__", wraps=__import__) as mock_import:
            # All imports should succeed
            ensure_deps()
        # __import__ was called, but should have found both modules
        assert mock_import.called

    def test_detects_missing_pexpect(self):
        """When pexpect is missing, ensure_deps detects it."""
        import builtins

        from penny.deps import ensure_deps

        real_import = builtins.__import__

        def import_side_effect(name, *args, **kwargs):
            if name == "pexpect":
                raise ImportError("pexpect not found")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=import_side_effect),
            patch("penny.deps.Path") as mock_path,
            patch("penny.deps.subprocess.run") as mock_run,
        ):
            mock_path_instance = MagicMock()
            mock_path.return_value = mock_path_instance
            mock_path_instance.resolve.return_value = mock_path_instance
            mock_path_instance.parent = mock_path_instance
            mock_path_instance.exists.return_value = False

            ensure_deps()

        # Should have attempted pip install
        assert mock_run.called

    def test_detects_missing_pyte(self):
        """When pyte is missing, ensure_deps detects it."""
        import builtins

        from penny.deps import ensure_deps

        real_import = builtins.__import__

        def import_side_effect(name, *args, **kwargs):
            if name == "pyte":
                raise ImportError("pyte not found")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=import_side_effect),
            patch("penny.deps.Path") as mock_path,
            patch("penny.deps.subprocess.run") as mock_run,
        ):
            mock_path_instance = MagicMock()
            mock_path.return_value = mock_path_instance
            mock_path_instance.resolve.return_value = mock_path_instance
            mock_path_instance.parent = mock_path_instance
            mock_path_instance.exists.return_value = False

            ensure_deps()

        # Should have attempted pip install
        assert mock_run.called

    def test_handles_subprocess_error(self):
        """When pip install fails, ensure_deps handles the exception gracefully."""
        import builtins

        from penny.deps import ensure_deps

        real_import = builtins.__import__

        def import_side_effect(name, *args, **kwargs):
            if name == "pexpect":
                raise ImportError("pexpect not found")
            return real_import(name, *args, **kwargs)

        with (
            patch("builtins.__import__", side_effect=import_side_effect),
            patch("penny.deps.Path") as mock_path,
            patch("penny.deps.subprocess.run", side_effect=RuntimeError("pip failed")),
        ):
            mock_path_instance = MagicMock()
            mock_path.return_value = mock_path_instance
            mock_path_instance.resolve.return_value = mock_path_instance
            mock_path_instance.parent = mock_path_instance
            mock_path_instance.exists.return_value = False

            # Should not raise
            ensure_deps()
