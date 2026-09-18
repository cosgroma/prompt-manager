"""
Tests for the application factory (promptmanager.app).
"""

import os
import tempfile

from aiohttp.test_utils import AioHTTPTestCase

from promptmanager.app import API_KEY, MONITOR_KEY, create_app
from promptmanager.config import GalleryConfig
from tests.test_prompt_extraction import api_prompt, write_png


class AppTestCase(AioHTTPTestCase):
    """Base class building an isolated app on a temp database."""

    TOKEN = None

    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.output_dir = os.path.join(self.tmp.name, "output")
        os.makedirs(self.output_dir)
        self._saved_dirs = list(GalleryConfig.MONITORING_DIRECTORIES)
        GalleryConfig.MONITORING_DIRECTORIES = [self.output_dir]
        return create_app(
            token=self.TOKEN,
            db_path=os.path.join(self.tmp.name, "test.db"),
            load_config=False,
            start_monitor=False,
        )

    async def tearDownAsync(self):
        await super().tearDownAsync()
        GalleryConfig.MONITORING_DIRECTORIES = self._saved_dirs
        self.tmp.cleanup()


class TestCreateApp(AppTestCase):
    async def test_root_redirects_to_gallery(self):
        response = await self.client.get("/", allow_redirects=False)
        self.assertEqual(response.status, 302)
        self.assertEqual(response.headers["Location"], "/prompt_manager/gallery")

    async def test_web_ui_pages_are_served_from_package_data(self):
        for path in ("/prompt_manager/gallery", "/prompt_manager/admin"):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertIn("text/html", response.headers["Content-Type"])

    async def test_static_assets_are_served(self):
        response = await self.client.get("/prompt_manager/js/admin.js")
        self.assertEqual(response.status, 200)

    async def test_large_responses_are_gzipped(self):
        response = await self.client.get(
            "/prompt_manager/admin", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")

    async def test_api_uses_the_given_database(self):
        api = self.app[API_KEY]
        self.assertEqual(api.db.model.db_path, os.path.join(self.tmp.name, "test.db"))

    async def test_monitor_is_attached_to_api(self):
        self.assertIs(self.app[API_KEY].monitor, self.app[MONITOR_KEY])

    async def test_settings_report_configured_directories(self):
        response = await self.client.get("/prompt_manager/settings")
        data = await response.json()
        self.assertEqual(data["settings"]["monitored_directories"], [self.output_dir])

    async def test_scan_imports_prompts_from_image_metadata(self):
        write_png(
            os.path.join(self.output_dir, "a.png"),
            {"prompt": api_prompt(positive="scanned fox")},
        )
        write_png(os.path.join(self.output_dir, "plain.png"))

        response = await self.client.post("/prompt_manager/scan")
        body = await response.text()

        self.assertIn('"type": "complete"', body)
        self.assertIn('"added": 1', body)
        results = self.app[API_KEY].db.search_prompts(text="scanned fox")
        self.assertEqual(len(results), 1)


class TestCreateAppWithToken(AppTestCase):
    TOKEN = "s3cret"

    async def test_requests_are_gated(self):
        response = await self.client.get("/prompt_manager/stats")
        self.assertEqual(response.status, 401)

    async def test_token_grants_access(self):
        response = await self.client.get(
            "/prompt_manager/stats", headers={"X-PM-Token": "s3cret"}
        )
        self.assertEqual(response.status, 200)


class TestMonitorLifecycle(AioHTTPTestCase):
    """The monitor starts and stops with the application."""

    async def get_application(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (
            GalleryConfig.MONITORING_ENABLED,
            list(GalleryConfig.MONITORING_DIRECTORIES),
        )
        GalleryConfig.MONITORING_ENABLED = True
        GalleryConfig.MONITORING_DIRECTORIES = [self.tmp.name]
        return create_app(
            db_path=os.path.join(self.tmp.name, "test.db"), load_config=False
        )

    async def tearDownAsync(self):
        await super().tearDownAsync()
        self.stopped = not self.app[MONITOR_KEY].get_status()["running"]
        (
            GalleryConfig.MONITORING_ENABLED,
            GalleryConfig.MONITORING_DIRECTORIES,
        ) = self._saved
        self.tmp.cleanup()
        assert self.stopped, "monitor still running after app cleanup"

    async def test_monitor_runs_while_app_is_up(self):
        self.assertTrue(self.app[MONITOR_KEY].get_status()["running"])
