"""
Tests for promptmanager.ingest.image_monitor.

The monitor links new images using only the metadata embedded in the file, so
these tests exercise it end to end against real PNGs and a temp database.
"""

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from promptmanager.config import GalleryConfig
from promptmanager.database.operations import PromptDatabase
from promptmanager.ingest.image_monitor import ImageGenerationHandler, ImageMonitor
from promptmanager.utils.hashing import generate_prompt_hash
from tests.test_prompt_extraction import api_prompt, write_png


class MonitorTestCase(unittest.TestCase):
    """Base class providing a temp database, output dir and clean config."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = os.path.join(self.tmp.name, "output")
        os.makedirs(self.output_dir)
        self.db = PromptDatabase(os.path.join(self.tmp.name, "test.db"))

        self._saved = (
            GalleryConfig.MONITORING_ENABLED,
            list(GalleryConfig.MONITORING_DIRECTORIES),
            GalleryConfig.PROCESSING_DELAY,
        )
        GalleryConfig.MONITORING_ENABLED = True
        GalleryConfig.MONITORING_DIRECTORIES = []
        GalleryConfig.PROCESSING_DELAY = 0.05

    def tearDown(self):
        (
            GalleryConfig.MONITORING_ENABLED,
            GalleryConfig.MONITORING_DIRECTORIES,
            GalleryConfig.PROCESSING_DELAY,
        ) = self._saved
        self.tmp.cleanup()


class TestImageGenerationHandler(MonitorTestCase):
    def test_thumbnails_and_non_images_are_ignored(self):
        handler = ImageGenerationHandler(self.db)
        self.assertTrue(handler.is_image_file("/out/a.png"))
        self.assertTrue(handler.is_image_file("/out/sub/A.JPG"))
        self.assertFalse(handler.is_image_file("/out/thumbnails/a_thumb.png"))
        self.assertFalse(handler.is_image_file("/out/notes.txt"))

    def test_new_image_creates_and_links_prompt(self):
        path = os.path.join(self.output_dir, "a.png")
        write_png(path, {"prompt": api_prompt()})
        on_ingest = MagicMock()

        ImageGenerationHandler(self.db, on_ingest).process_new_image(path)

        prompt = self.db.get_prompt_by_hash(generate_prompt_hash("a red fox"))
        self.assertIsNotNone(prompt)
        images = self.db.get_prompt_images(prompt["id"])
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["width"], 8)
        on_ingest.assert_called_once()

    def test_image_without_prompt_is_not_linked_to_recent_prompt(self):
        """Regression: there is no 'most recent prompt' fallback any more."""
        prompt_id = self.db.save_prompt("unrelated", None, None, None, None, "h")
        path = os.path.join(self.output_dir, "plain.png")
        write_png(path)

        ImageGenerationHandler(self.db).process_new_image(path)

        self.assertEqual(self.db.get_prompt_images(prompt_id), [])

    def test_vanished_file_is_skipped(self):
        handler = ImageGenerationHandler(self.db)
        handler.process_new_image(os.path.join(self.output_dir, "gone.png"))

    def test_processing_errors_do_not_propagate(self):
        path = os.path.join(self.output_dir, "a.png")
        write_png(path, {"prompt": api_prompt()})
        handler = ImageGenerationHandler(self.db)
        with patch(
            "promptmanager.ingest.image_monitor.ingest_image",
            side_effect=RuntimeError("boom"),
        ):
            handler.process_new_image(path)


class TestImageMonitor(MonitorTestCase):
    def test_no_directories_means_not_running(self):
        monitor = ImageMonitor(self.db)
        monitor.start_monitoring()
        self.assertFalse(monitor.get_status()["running"])

    def test_disabled_in_config(self):
        GalleryConfig.MONITORING_ENABLED = False
        monitor = ImageMonitor(self.db)
        monitor.start_monitoring([self.output_dir])
        self.assertFalse(monitor.get_status()["running"])

    def test_missing_directory_is_skipped(self):
        monitor = ImageMonitor(self.db)
        monitor.start_monitoring([os.path.join(self.tmp.name, "missing")])
        self.assertFalse(monitor.get_status()["running"])

    def test_uses_configured_directories(self):
        GalleryConfig.MONITORING_DIRECTORIES = [self.output_dir]
        monitor = ImageMonitor(self.db)
        try:
            monitor.start_monitoring()
            status = monitor.get_status()
            self.assertTrue(status["running"])
            self.assertEqual(status["monitored_directories"], [self.output_dir])
        finally:
            monitor.stop_monitoring()
        self.assertFalse(monitor.get_status()["running"])

    def test_restart_picks_up_new_directories(self):
        other = os.path.join(self.tmp.name, "other")
        os.makedirs(other)
        GalleryConfig.MONITORING_DIRECTORIES = [self.output_dir]
        monitor = ImageMonitor(self.db)
        try:
            monitor.start_monitoring()
            GalleryConfig.MONITORING_DIRECTORIES = [other]
            monitor.restart_monitoring()
            self.assertEqual(monitor.monitored_directories, [other])
        finally:
            monitor.stop_monitoring()

    def test_set_database_updates_running_handler(self):
        monitor = ImageMonitor(self.db)
        try:
            monitor.start_monitoring([self.output_dir])
            replacement = MagicMock()
            monitor.set_database(replacement)
            self.assertIs(monitor.db_manager, replacement)
            self.assertIs(monitor.handler.db_manager, replacement)
        finally:
            monitor.stop_monitoring()

    def test_end_to_end_new_file_is_ingested(self):
        """A file dropped into a watched directory ends up linked in the DB."""
        monitor = ImageMonitor(self.db)
        try:
            monitor.start_monitoring([self.output_dir])
            write_png(
                os.path.join(self.output_dir, "new.png"),
                {"prompt": api_prompt(positive="watched image")},
            )

            prompt = None
            deadline = time.time() + 10
            while time.time() < deadline and not prompt:
                time.sleep(0.1)
                prompt = self.db.get_prompt_by_hash(
                    generate_prompt_hash("watched image")
                )
                if prompt and not self.db.get_prompt_images(prompt["id"]):
                    prompt = None
        finally:
            monitor.stop_monitoring()

        self.assertIsNotNone(prompt, "image was not ingested within 10s")


if __name__ == "__main__":
    unittest.main()
