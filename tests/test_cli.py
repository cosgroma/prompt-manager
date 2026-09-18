"""
Tests for the command line entry point (promptmanager.cli).

Covers argument parsing, the CLI-to-environment translation, and the optional
token middleware.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

from promptmanager import cli
from promptmanager.app import TOKEN_COOKIE, make_token_middleware


class TestParseArgs(unittest.TestCase):
    """Test command line parsing."""

    def test_defaults_bind_to_localhost(self):
        """The default bind address must not expose the unauthenticated API."""
        args = cli.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertIn(args.host, cli.LOCAL_HOSTS)

    def test_default_port(self):
        """A default port is supplied so the app runs with no arguments."""
        self.assertEqual(cli.parse_args([]).port, cli.DEFAULT_PORT)

    def test_optional_settings_default_to_none(self):
        """Unspecified options stay None so config.json keeps precedence."""
        args = cli.parse_args([])
        self.assertIsNone(args.db)
        self.assertIsNone(args.output_dir)
        self.assertIsNone(args.token)

    def test_output_dir_is_repeatable(self):
        """--output-dir accumulates so several galleries can be served."""
        args = cli.parse_args(["--output-dir", "/a", "--output-dir", "/b"])
        self.assertEqual(args.output_dir, ["/a", "/b"])

    def test_flags_parse(self):
        """Boolean flags are parsed as store_true."""
        args = cli.parse_args(["--no-monitor", "--open"])
        self.assertTrue(args.no_monitor)
        self.assertTrue(args.open)

    def test_port_is_an_integer(self):
        """--port is coerced to int for aiohttp."""
        self.assertEqual(cli.parse_args(["--port", "9000"]).port, 9000)


class TestApplyEnv(unittest.TestCase):
    """Test translation of CLI options into environment variables.

    The environment is the single override channel on top of config.json, so
    CLI flags are exported before the application is created.
    """

    def setUp(self):
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in (
            "PROMPT_MANAGER_DB",
            "PROMPT_MANAGER_OUTPUT_DIRS",
            "PROMPT_MANAGER_MONITORING",
        ):
            os.environ.pop(key, None)

    def tearDown(self):
        self._env.stop()

    def test_db_path_exported(self):
        """--db becomes PROMPT_MANAGER_DB."""
        cli.apply_env(cli.parse_args(["--db", "/tmp/custom.db"]))
        self.assertEqual(os.environ["PROMPT_MANAGER_DB"], "/tmp/custom.db")

    def test_db_path_expands_user(self):
        """A ~-relative database path is expanded."""
        cli.apply_env(cli.parse_args(["--db", "~/prompts.db"]))
        self.assertFalse(os.environ["PROMPT_MANAGER_DB"].startswith("~"))

    def test_nothing_exported_without_options(self):
        """With no options, the environment is left untouched."""
        cli.apply_env(cli.parse_args([]))
        self.assertNotIn("PROMPT_MANAGER_DB", os.environ)
        self.assertNotIn("PROMPT_MANAGER_OUTPUT_DIRS", os.environ)
        self.assertNotIn("PROMPT_MANAGER_MONITORING", os.environ)

    def test_output_dir_exported_as_absolute_path(self):
        """--output-dir is resolved to an absolute path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cli.apply_env(cli.parse_args(["--output-dir", tmpdir]))
            exported = os.environ["PROMPT_MANAGER_OUTPUT_DIRS"]
            self.assertTrue(os.path.isabs(exported))
            self.assertEqual(os.path.realpath(exported), os.path.realpath(tmpdir))

    def test_multiple_output_dirs_joined_with_pathsep(self):
        """Several directories are joined with the platform path separator."""
        with tempfile.TemporaryDirectory() as one, tempfile.TemporaryDirectory() as two:
            cli.apply_env(cli.parse_args(["--output-dir", one, "--output-dir", two]))
            parts = os.environ["PROMPT_MANAGER_OUTPUT_DIRS"].split(os.pathsep)
            self.assertEqual(len(parts), 2)

    def test_missing_output_dir_exits(self):
        """A nonexistent --output-dir is a hard error, not a silent no-op."""
        with self.assertRaises(SystemExit):
            cli.apply_env(cli.parse_args(["--output-dir", "/nonexistent/pm/path"]))

    def test_output_dir_pointing_at_a_file_exits(self):
        """A file where a directory is expected is rejected."""
        with tempfile.NamedTemporaryFile() as handle:
            with self.assertRaises(SystemExit):
                cli.apply_env(cli.parse_args(["--output-dir", handle.name]))

    def test_data_dir_exported_as_absolute_path(self):
        """--data-dir becomes PROMPT_MANAGER_HOME."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cli.apply_env(cli.parse_args(["--data-dir", tmpdir]))
            self.assertEqual(
                os.environ["PROMPT_MANAGER_HOME"], os.path.realpath(tmpdir)
            )

    def test_no_monitor_disables_monitoring(self):
        """--no-monitor exports the disable flag config.py reads."""
        cli.apply_env(cli.parse_args(["--no-monitor"]))
        self.assertEqual(os.environ["PROMPT_MANAGER_MONITORING"], "0")


class TestTokenMiddleware(AioHTTPTestCase):
    """Test the shared-token gate applied when --token is supplied."""

    TOKEN = "s3cret"

    async def get_application(self):
        app = web.Application()
        app.middlewares.append(make_token_middleware(self.TOKEN))

        async def handler(request):
            return web.json_response({"ok": True})

        app.router.add_get("/prompt_manager/stats", handler)
        app.router.add_delete("/prompt_manager/delete/{prompt_id}", handler)
        return app

    async def test_request_without_token_is_rejected(self):
        """Unauthenticated requests get 401, not data."""
        response = await self.client.get("/prompt_manager/stats")
        self.assertEqual(response.status, 401)

    async def test_wrong_token_is_rejected(self):
        """An incorrect token is refused."""
        response = await self.client.get("/prompt_manager/stats?token=wrong")
        self.assertEqual(response.status, 401)

    async def test_destructive_endpoint_is_gated(self):
        """Delete endpoints are covered by the same gate."""
        response = await self.client.delete("/prompt_manager/delete/1")
        self.assertEqual(response.status, 401)

    async def test_query_parameter_grants_access(self):
        """The token may be supplied as a query parameter."""
        response = await self.client.get(
            f"/prompt_manager/stats?token={self.TOKEN}", allow_redirects=False
        )
        self.assertEqual(response.status, 200)

    async def test_header_grants_access(self):
        """The token may be supplied as an X-PM-Token header."""
        response = await self.client.get(
            "/prompt_manager/stats", headers={"X-PM-Token": self.TOKEN}
        )
        self.assertEqual(response.status, 200)

    async def test_successful_auth_sets_cookie(self):
        """A successful query-parameter auth issues a session cookie.

        The bundled web UI does not know about tokens, so the cookie is what
        keeps its subsequent fetches working.
        """
        response = await self.client.get(f"/prompt_manager/stats?token={self.TOKEN}")
        self.assertEqual(response.status, 200)
        self.assertIn(TOKEN_COOKIE, response.cookies)

    async def test_cookie_alone_grants_later_requests(self):
        """After the handshake, plain requests succeed via the cookie."""
        await self.client.get(f"/prompt_manager/stats?token={self.TOKEN}")
        response = await self.client.get("/prompt_manager/stats")
        self.assertEqual(response.status, 200)

    async def test_rejection_body_explains_how_to_authenticate(self):
        """The 401 tells the user what to do rather than failing silently."""
        response = await self.client.get("/prompt_manager/stats")
        body = await response.json()
        self.assertIn("token", body["error"].lower())


if __name__ == "__main__":
    unittest.main()
