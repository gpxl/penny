"""Unit tests for penny/paths.py — data directory resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from penny.paths import data_dir


class TestDataDir:
    def test_defaults_to_home_penny(self, tmp_path):
        with patch("penny.paths.Path.home", return_value=tmp_path), \
             patch.dict("os.environ", {}, clear=True):
            result = data_dir()
        assert result == tmp_path / ".penny"

    def test_penny_home_env_overrides_default(self, tmp_path):
        custom = tmp_path / "custom_home"
        with patch.dict("os.environ", {"PENNY_HOME": str(custom)}):
            result = data_dir()
        assert result == custom

    def test_creates_directory_if_missing(self, tmp_path):
        target = tmp_path / "new_dir"
        assert not target.exists()
        with patch.dict("os.environ", {"PENNY_HOME": str(target)}):
            data_dir()
        assert target.exists()

    def test_directory_mode_is_700(self, tmp_path):
        target = tmp_path / "secure_dir"
        with patch.dict("os.environ", {"PENNY_HOME": str(target)}):
            result = data_dir()
        mode = result.stat().st_mode & 0o777
        assert mode == 0o700

    def test_returns_path_object(self, tmp_path):
        with patch.dict("os.environ", {"PENNY_HOME": str(tmp_path / "x")}):
            result = data_dir()
        assert isinstance(result, Path)

    def test_no_penny_home_in_env(self, tmp_path):
        import os
        env = {k: v for k, v in os.environ.items() if k != "PENNY_HOME"}
        with patch("penny.paths.Path.home", return_value=tmp_path), \
             patch.dict("os.environ", env, clear=True):
            result = data_dir()
        assert result == tmp_path / ".penny"
