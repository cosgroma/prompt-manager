"""REST API module for PromptManager.

Split into domain-specific mixins for maintainability:
- PromptRoutesMixin: prompt CRUD, tags, categories, bulk ops, export
- ImageRoutesMixin: gallery, thumbnails, image serving and linking
- AdminRoutesMixin: duplicates, stats, settings, diagnostics, maintenance, backup, scan
- LoggingRoutesMixin: log management endpoints
- AutotagRoutesMixin: auto-tagging model management and tagging
"""

import asyncio
import datetime
import functools
import gzip as gzip_module
import os
from pathlib import Path

from aiohttp import web

from .prompts import PromptRoutesMixin
from .images import ImageRoutesMixin
from .admin import AdminRoutesMixin
from .logging_routes import LoggingRoutesMixin
from .autotag_routes import AutotagRoutesMixin
from .lora_integration import LoraIntegrationMixin

from ..database.operations import PromptDatabase
from ..ingest import prompt_extraction
from ..paths import WEB_DIR
from ..utils.logging_config import get_logger

# ── Gzip compression middleware ────────────────────────────────────────
_GZIP_MIN_SIZE = 1024  # Only compress bodies larger than 1 KB
_GZIP_TYPES = frozenset(
    (
        "application/json",
        "text/html",
        "text/css",
        "application/javascript",
        "text/plain",
    )
)


@web.middleware
async def gzip_middleware(request, handler):
    """Compress PromptManager responses when the client accepts gzip."""
    response = await handler(request)

    if not request.path.startswith("/prompt_manager/"):
        return response

    # Only handle regular Response objects (not StreamResponse/WebSocket)
    if not isinstance(response, web.Response):
        return response

    if "gzip" not in request.headers.get("Accept-Encoding", ""):
        return response

    if "Content-Encoding" in response.headers:
        return response

    body = response.body
    if body is None or len(body) < _GZIP_MIN_SIZE:
        return response

    content_type = response.content_type or ""
    if not any(ct in content_type for ct in _GZIP_TYPES):
        return response

    compressed = gzip_module.compress(body, compresslevel=6)
    if len(compressed) >= len(body):
        return response

    response.body = compressed
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Vary"] = "Accept-Encoding"
    return response


class PromptManagerAPI(
    PromptRoutesMixin,
    ImageRoutesMixin,
    AdminRoutesMixin,
    LoggingRoutesMixin,
    AutotagRoutesMixin,
    LoraIntegrationMixin,
):
    """REST API handler for PromptManager operations and web interface.

    This class provides comprehensive REST API endpoints for managing prompts,
    images, and system operations. It handles database interactions, file
    operations, image processing, and web UI serving.

    Provides both JSON API endpoints and static file serving for the web
    interface. Routes are registered on an aiohttp ``RouteTableDef`` by
    :meth:`add_routes`; the application factory in ``promptmanager.app``
    wires everything together.

    Attributes:
        logger: Configured logger instance for API operations
        db (PromptDatabase): Database connection and operations handler
        monitor: The ImageMonitor watching output directories, or None when
            the application has not attached one.
    """

    def __init__(self, db=None):
        """Initialize the PromptManager API with database connection and cleanup.

        Args:
            db: PromptDatabase to use. A default one is opened when omitted.
        """
        self.logger = get_logger("prompt_manager.api")
        self.logger.info("Initializing PromptManager API")

        self.db = db if db is not None else PromptDatabase()
        self.monitor = None
        self._cached_output_dir = None  # Lazy-cached by _find_comfyui_output_dir()
        self._html_cache = {}  # Cached HTML file contents keyed by path
        self._gallery_cache = {}  # dict: path_str -> (files, timestamp)
        self._gallery_cache_ttl = 30  # Cache TTL in seconds

        # Run cleanup on initialization to remove any existing duplicates
        try:
            removed = self.db.cleanup_duplicates()
            if removed > 0:
                self.logger.info(
                    f"Startup cleanup: removed {removed} duplicate prompts"
                )
        except Exception as e:
            self.logger.error(f"Startup cleanup failed: {e}")

        self.logger.info("PromptManager API initialization completed")

    async def _run_in_executor(self, func, *args, **kwargs):
        """Run a blocking function in the default thread pool executor.

        Prevents synchronous database calls, file I/O, and PIL operations
        from blocking the aiohttp event loop.
        """
        loop = asyncio.get_event_loop()
        if kwargs:
            call = functools.partial(func, *args, **kwargs)
            return await loop.run_in_executor(None, call)
        return await loop.run_in_executor(None, func, *args)

    def invalidate_gallery_cache(self):
        """Invalidate the cached gallery file listing.

        Called by the image monitor when new files are detected.
        """
        self._gallery_cache = {}

    def add_routes(self, routes):
        """Register all API routes.

        Args:
            routes: aiohttp RouteTableDef to register handlers on.
        """

        # Test route to verify registration works
        @routes.get("/prompt_manager/test")
        async def test_route(request):
            return web.json_response(
                {
                    "success": True,
                    "message": "PromptManager API is working!",
                    "timestamp": str(datetime.datetime.now()),
                }
            )

        # ── Web UI serving routes ─────────────────────────────────────

        @routes.get("/prompt_manager/web")
        async def serve_web_ui(request):
            try:
                html_path = str(WEB_DIR / "index.html")

                if os.path.exists(html_path):
                    with open(html_path, "r", encoding="utf-8") as f:
                        html_content = f.read()

                    return web.Response(
                        text=html_content, content_type="text/html", charset="utf-8"
                    )
                else:
                    return web.Response(
                        text="<h1>Web UI not found</h1><p>HTML file not located at expected path.</p>",
                        content_type="text/html",
                        status=404,
                    )

            except Exception as e:
                self.logger.exception("Failed to load web UI")
                return web.Response(
                    text="<h1>Error</h1><p>Failed to load web UI. Check server logs for details.</p>",
                    content_type="text/html",
                    status=500,
                )

        @routes.get("/prompt_manager/gallery.html")
        async def serve_gallery_ui(request):
            try:
                html_path = str(WEB_DIR / "metadata.html")

                if html_path not in self._html_cache:
                    if os.path.exists(html_path):
                        with open(html_path, "r", encoding="utf-8") as f:
                            self._html_cache[html_path] = f.read()
                    else:
                        return web.Response(
                            text="<h1>Gallery not found</h1><p>gallery.html file not located at expected path.</p>",
                            content_type="text/html",
                            status=404,
                        )

                return web.Response(
                    text=self._html_cache[html_path],
                    content_type="text/html",
                    charset="utf-8",
                )

            except Exception as e:
                self.logger.exception("Failed to load gallery")
                return web.Response(
                    text="<h1>Error</h1><p>Failed to load gallery. Check server logs for details.</p>",
                    content_type="text/html",
                    status=500,
                )

        @routes.get("/prompt_manager/admin")
        async def serve_admin_ui(request):
            try:
                html_path = str(WEB_DIR / "admin.html")

                if html_path not in self._html_cache:
                    if os.path.exists(html_path):
                        with open(html_path, "r", encoding="utf-8") as f:
                            self._html_cache[html_path] = f.read()
                    else:
                        return web.Response(
                            text="<h1>Admin UI not found</h1>",
                            content_type="text/html",
                            status=404,
                        )

                return web.Response(
                    text=self._html_cache[html_path],
                    content_type="text/html",
                    charset="utf-8",
                )

            except Exception as e:
                self.logger.exception("Failed to load admin UI")
                return web.Response(
                    text="<h1>Error</h1><p>Failed to load admin UI. Check server logs for details.</p>",
                    content_type="text/html",
                    status=500,
                )

        @routes.get("/prompt_manager/gallery")
        async def serve_gallery_admin_ui(request):
            try:
                html_path = str(WEB_DIR / "gallery.html")

                if html_path not in self._html_cache:
                    if os.path.exists(html_path):
                        with open(html_path, "r", encoding="utf-8") as f:
                            self._html_cache[html_path] = f.read()
                    else:
                        return web.Response(
                            text="<h1>Gallery not found</h1><p>gallery.html file not located at expected path.</p>",
                            content_type="text/html",
                            status=404,
                        )

                return web.Response(
                    text=self._html_cache[html_path],
                    content_type="text/html",
                    charset="utf-8",
                )

            except Exception as e:
                self.logger.exception("Failed to load gallery")
                return web.Response(
                    text="<h1>Error</h1><p>Failed to load gallery. Check server logs for details.</p>",
                    content_type="text/html",
                    status=500,
                )

        # ── Static file serving ───────────────────────────────────────

        @routes.get("/prompt_manager/lib/{filepath:.*}")
        async def serve_lib_static(request):
            """Serve static library files (JS, CSS) from web/lib directory."""
            MIME_TYPES = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".map": "application/json",
            }

            filepath = request.match_info.get("filepath", "")

            # Security: prevent directory traversal
            if ".." in filepath or filepath.startswith("/"):
                return web.Response(text="Forbidden", status=403)

            file_path = str(WEB_DIR / "lib" / filepath)

            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                return web.Response(text=f"Not Found: {filepath}", status=404)

            ext = os.path.splitext(file_path)[1].lower()
            content_type = MIME_TYPES.get(ext, "application/octet-stream")

            with open(file_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type=content_type)

        @routes.get("/prompt_manager/js/{filepath:.*}")
        async def serve_js_static(request):
            """Serve static JavaScript files from web/js directory."""
            MIME_TYPES = {
                ".js": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".map": "application/json",
            }

            filepath = request.match_info.get("filepath", "")

            if ".." in filepath or filepath.startswith("/"):
                return web.Response(text="Forbidden", status=403)

            file_path = str(WEB_DIR / "js" / filepath)

            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                return web.Response(text=f"Not Found: {filepath}", status=404)

            ext = os.path.splitext(file_path)[1].lower()
            content_type = MIME_TYPES.get(ext, "application/octet-stream")

            with open(file_path, "rb") as f:
                content = f.read()

            return web.Response(body=content, content_type=content_type)

        # ── Register domain-specific routes from mixins ───────────────

        self._register_prompt_routes(routes)
        self._register_image_routes(routes)
        self._register_admin_routes(routes)
        self._register_logging_routes(routes)
        self._register_autotag_routes(routes)
        self._register_lora_routes(routes)

        self.logger.info("All routes registered")

    # ── Shared utilities used by multiple mixins ──────────────────────

    def _enrich_images(self, images):
        """Add url and thumbnail_url to a flat list of image dicts."""
        from urllib.parse import quote as url_quote

        output_dirs = self._get_all_output_dirs()

        for image in images:
            image_path_str = image.get("image_path", "")
            if not image_path_str:
                continue

            img_path = Path(image_path_str)

            # Set fallback url via image ID
            if image.get("id"):
                image["url"] = f"/prompt_manager/images/{image['id']}/file"

            # Try each root to compute relative path and thumbnail URL
            for output_path in output_dirs:
                try:
                    rel_path = img_path.resolve().relative_to(output_path.resolve())
                    image["relative_path"] = str(rel_path)
                    image["url"] = (
                        f"/prompt_manager/images/serve/{url_quote(rel_path.as_posix(), safe='/')}"
                    )

                    # Check for thumbnail
                    rel_no_ext = rel_path.with_suffix("")
                    thumb_rel = (
                        f"thumbnails/{rel_no_ext.as_posix()}_thumb{rel_path.suffix}"
                    )
                    thumb_abs = output_path / thumb_rel
                    if thumb_abs.exists():
                        image["thumbnail_url"] = (
                            f"/prompt_manager/images/serve/{url_quote(thumb_rel, safe='/')}"
                        )
                    break  # Found matching root, stop searching
                except (ValueError, RuntimeError):
                    continue  # Try next root

        return images

    def _enrich_prompt_images(self, prompts):
        """Add url and thumbnail_url to each image in prompt results."""
        for prompt in prompts:
            self._enrich_images(prompt.get("images", []))
        return prompts

    def _clean_nan_recursive(self, obj):
        """Recursively clean NaN values from nested data structures."""
        if isinstance(obj, dict):
            return {key: self._clean_nan_recursive(value) for key, value in obj.items()}
        elif isinstance(obj, list):
            return [self._clean_nan_recursive(item) for item in obj]
        elif isinstance(obj, float) and str(obj) == "nan":
            return None
        else:
            return obj

    def _find_comfyui_output_dir(self):
        """Get the primary output directory images are served from.

        This is the first valid entry of GalleryConfig.MONITORING_DIRECTORIES
        (set with --output-dir, PROMPT_MANAGER_OUTPUT_DIRS, or in Settings).
        The result is cached; the cache is invalidated when the configured
        directories change via save_settings().

        Returns:
            str or None: Absolute path to the output directory, or None if no
                        valid directory is configured.
        """
        if self._cached_output_dir is not None:
            return self._cached_output_dir

        from ..config import GalleryConfig

        for configured in GalleryConfig.MONITORING_DIRECTORIES:
            configured_dir = Path(configured).expanduser().resolve()
            if configured_dir.is_dir():
                self._cached_output_dir = str(configured_dir)
                return self._cached_output_dir
            self.logger.warning(f"Configured directory does not exist: {configured}")

        self.logger.warning(
            "No output directory configured. Pass --output-dir or add one in Settings."
        )
        return None

    def _get_all_output_dirs(self):
        """Get all configured output directories that exist.

        Returns:
            List[Path]: Valid output directory paths, possibly empty.
        """
        from ..config import GalleryConfig

        output_dirs = []
        for d in GalleryConfig.MONITORING_DIRECTORIES:
            p = Path(d).expanduser().resolve()
            if p.is_dir() and p not in output_dirs:
                output_dirs.append(p)

        return output_dirs

    # ── Embedded-metadata helpers (shared with the image monitor) ─────

    def _extract_comfyui_metadata(self, image_path):
        """Read the raw text chunks embedded in an image."""
        return prompt_extraction.read_embedded_metadata(image_path)

    def _parse_comfyui_prompt(self, metadata):
        """Parse ComfyUI and A1111 prompt data from raw metadata."""
        return prompt_extraction.parse_embedded_metadata(metadata)

    def _extract_readable_prompt(self, parsed_data):
        """Extract human-readable prompt text from parsed metadata."""
        return prompt_extraction.extract_readable_prompt(parsed_data)

    def _get_node_inputs(self, node):
        """Get a node's inputs as a dict, for either serialization format."""
        return prompt_extraction.get_node_inputs(node)

    def _find_text_in_node(self, node):
        """Find prompt text held by a text-encoder node."""
        return prompt_extraction.find_text_in_node(node)

    def _extract_positive_prompt_from_comfyui_data(self, data):
        """Extract the positive prompt from ComfyUI prompt or workflow data."""
        return prompt_extraction.extract_positive_prompt(data)
