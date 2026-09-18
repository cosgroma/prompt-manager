"""
Tests for environment-variable configuration overrides.

The standalone entrypoint and container deployments configure PromptManager
through the environment rather than config.json, so these overrides must take
precedence over the loaded config and must not fire when unset.
"""

import os
import tempfile
import unittest
from unittest.mock import patch


from promptmanager.database.operations import _resolve_db_path
from promptmanager.config import GalleryConfig, PromptManagerConfig, apply_env_overrides

from promptmanager.paths import get_data_dir

DATA_DIR = str(get_data_dir())

ENV_KEYS = (
    "PROMPT_MANAGER_DB",
    "PROMPT_MANAGER_OUTPUT_DIRS",
    "PROMPT_MANAGER_MONITORING",
)


class EnvOverrideTestCase(unittest.TestCase):
    """Base class that restores global config state after each test."""

    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ENV_KEYS:
            os.environ.pop(key, None)

        self._orig_db = PromptManagerConfig.DEFAULT_DB_PATH
        self._orig_dirs = list(GalleryConfig.MONITORING_DIRECTORIES)
        self._orig_monitoring = GalleryConfig.MONITORING_ENABLED

    def tearDown(self):
        self._env.stop()
        PromptManagerConfig.DEFAULT_DB_PATH = self._orig_db
        GalleryConfig.MONITORING_DIRECTORIES = self._orig_dirs
        GalleryConfig.MONITORING_ENABLED = self._orig_monitoring


class TestApplyEnvOverrides(EnvOverrideTestCase):
    """Test apply_env_overrides() against the config classes."""

    def test_no_env_leaves_config_untouched(self):
        """With nothing set, loaded config values survive."""
        PromptManagerConfig.DEFAULT_DB_PATH = "from_config.db"
        GalleryConfig.MONITORING_DIRECTORIES = ["/from/config"]

        apply_env_overrides()

        self.assertEqual(PromptManagerConfig.DEFAULT_DB_PATH, "from_config.db")
        self.assertEqual(GalleryConfig.MONITORING_DIRECTORIES, ["/from/config"])

    def test_db_env_overrides_config(self):
        """PROMPT_MANAGER_DB wins over config.json."""
        PromptManagerConfig.DEFAULT_DB_PATH = "from_config.db"
        os.environ["PROMPT_MANAGER_DB"] = "/tmp/from_env.db"

        apply_env_overrides()

        self.assertEqual(PromptManagerConfig.DEFAULT_DB_PATH, "/tmp/from_env.db")

    def test_output_dirs_env_overrides_config(self):
        """PROMPT_MANAGER_OUTPUT_DIRS replaces the configured directories."""
        GalleryConfig.MONITORING_DIRECTORIES = ["/from/config"]
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = tmpdir

            apply_env_overrides()

            self.assertEqual(len(GalleryConfig.MONITORING_DIRECTORIES), 1)
            self.assertEqual(
                os.path.realpath(GalleryConfig.MONITORING_DIRECTORIES[0]),
                os.path.realpath(tmpdir),
            )

    def test_multiple_output_dirs_are_split(self):
        """Several directories separated by os.pathsep are parsed individually."""
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = os.pathsep.join([one, two])

            apply_env_overrides()

            self.assertEqual(len(GalleryConfig.MONITORING_DIRECTORIES), 2)

    def test_output_dirs_are_absolute(self):
        """Relative directories are resolved so the CWD cannot change meaning."""
        os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = "relative/output"

        apply_env_overrides()

        self.assertTrue(
            all(os.path.isabs(p) for p in GalleryConfig.MONITORING_DIRECTORIES)
        )

    def test_empty_output_dirs_is_ignored(self):
        """An empty value must not wipe the configured directories."""
        GalleryConfig.MONITORING_DIRECTORIES = ["/from/config"]
        os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = ""

        apply_env_overrides()

        self.assertEqual(GalleryConfig.MONITORING_DIRECTORIES, ["/from/config"])

    def test_separator_only_value_is_ignored(self):
        """A value of just separators yields no directories and changes nothing."""
        GalleryConfig.MONITORING_DIRECTORIES = ["/from/config"]
        os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = os.pathsep

        apply_env_overrides()

        self.assertEqual(GalleryConfig.MONITORING_DIRECTORIES, ["/from/config"])

    def test_monitoring_can_be_disabled(self):
        """PROMPT_MANAGER_MONITORING=0 turns off the filesystem watcher."""
        GalleryConfig.MONITORING_ENABLED = True
        os.environ["PROMPT_MANAGER_MONITORING"] = "0"

        apply_env_overrides()

        self.assertFalse(GalleryConfig.MONITORING_ENABLED)

    def test_monitoring_can_be_enabled(self):
        """PROMPT_MANAGER_MONITORING=1 turns the watcher back on."""
        GalleryConfig.MONITORING_ENABLED = False
        os.environ["PROMPT_MANAGER_MONITORING"] = "1"

        apply_env_overrides()

        self.assertTrue(GalleryConfig.MONITORING_ENABLED)


class TestResolveDbPathFromEnv(EnvOverrideTestCase):
    """Test that _resolve_db_path() honours the environment."""

    def test_env_used_when_no_explicit_path(self):
        """PROMPT_MANAGER_DB supplies the path when none is passed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "env.db")
            os.environ["PROMPT_MANAGER_DB"] = target

            self.assertEqual(_resolve_db_path(None), target)

    def test_explicit_path_beats_env(self):
        """An explicit argument still wins over the environment."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.environ["PROMPT_MANAGER_DB"] = os.path.join(tmpdir, "env.db")
            explicit = os.path.join(tmpdir, "explicit.db")

            self.assertEqual(_resolve_db_path(explicit), explicit)

    def test_relative_env_path_resolved_to_data_dir(self):
        """A relative env path resolves against the data directory, not the CWD."""
        os.environ["PROMPT_MANAGER_DB"] = "env_data/prompts.db"

        result = _resolve_db_path(None)

        self.assertEqual(result, os.path.join(DATA_DIR, "env_data", "prompts.db"))
        # Clean up the directory the resolver creates.
        created = os.path.join(DATA_DIR, "env_data")
        if os.path.isdir(created):
            os.rmdir(created)

    def test_empty_env_falls_through_to_config(self):
        """An empty variable must not be treated as a path."""
        os.environ["PROMPT_MANAGER_DB"] = ""

        result = _resolve_db_path(None)

        self.assertTrue(os.path.isabs(result))
        self.assertNotEqual(os.path.basename(result), "")


if __name__ == "__main__":
    unittest.main()
