"""Command line entry point for PromptManager.

Usage:
    promptmanager --output-dir ~/ComfyUI/output
    promptmanager --port 8189 --db ~/prompts.db --output-dir ./output
    promptmanager --host 0.0.0.0 --token secret123
    python -m promptmanager --help

The server binds to localhost by default because the API is unauthenticated
and includes destructive endpoints. Use --token when binding to a public
interface.
"""

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .paths import DATA_DIR_ENV

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8189

# Hosts that keep the server reachable only from this machine.
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def parse_args(argv=None):
    """Parse command line arguments.

    Args:
        argv: Argument list to parse, or None to use sys.argv.

    Returns:
        argparse.Namespace with the parsed options.
    """
    parser = argparse.ArgumentParser(
        prog="promptmanager",
        description="Prompt library and gallery for AI-generated images.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Interface to bind. Use 0.0.0.0 to expose on the network.",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port to listen on."
    )
    parser.add_argument(
        "--output-dir",
        action="append",
        default=None,
        metavar="DIR",
        help="Directory of generated images to monitor and serve. "
        "Repeat for multiple directories.",
    )
    parser.add_argument(
        "--db",
        default=None,
        metavar="PATH",
        help="SQLite database file. Defaults to prompts.db in the data directory.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        metavar="DIR",
        help="Directory for the database, config.json, logs and models. "
        f"Defaults to ${DATA_DIR_ENV} or ~/.promptmanager.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Require this token for API access. Visit the server once with "
        "?token=VALUE to set a session cookie.",
    )
    parser.add_argument(
        "--no-monitor",
        action="store_true",
        help="Disable filesystem watching for new images.",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the gallery in a browser once the server is up.",
    )
    return parser.parse_args(argv)


def apply_env(args):
    """Translate CLI options into the environment variables config reads.

    The environment is the single override channel on top of config.json, so
    CLI flags, containers and shells all configure the application the same
    way. Must run before the rest of the package is imported, because the
    logging system picks its directory from the data dir on first use.

    Args:
        args: Parsed arguments from :func:`parse_args`.

    Raises:
        SystemExit: If an --output-dir does not exist.
    """
    if args.data_dir:
        os.environ[DATA_DIR_ENV] = str(Path(args.data_dir).expanduser().resolve())

    if args.db:
        os.environ["PROMPT_MANAGER_DB"] = str(Path(args.db).expanduser().resolve())

    if args.output_dir:
        resolved = []
        for raw in args.output_dir:
            path = Path(raw).expanduser().resolve()
            if not path.is_dir():
                sys.exit(f"error: --output-dir does not exist: {path}")
            resolved.append(str(path))
        os.environ["PROMPT_MANAGER_OUTPUT_DIRS"] = os.pathsep.join(resolved)

    if args.no_monitor:
        os.environ["PROMPT_MANAGER_MONITORING"] = "0"


def describe_startup(args, app_url):
    """Print a startup banner describing the running configuration.

    Args:
        args: Parsed CLI arguments.
        app_url: Base URL the server is reachable at.
    """
    from .app import LANDING_PATH
    from .config import GalleryConfig
    from .database.operations import _resolve_db_path
    from .paths import get_data_dir

    if not GalleryConfig.MONITORING_DIRECTORIES:
        monitoring = "(none — pass --output-dir or add one in Settings)"
    elif not GalleryConfig.MONITORING_ENABLED:
        monitoring = ", ".join(GalleryConfig.MONITORING_DIRECTORIES) + " (not watching)"
    else:
        monitoring = ", ".join(GalleryConfig.MONITORING_DIRECTORIES)

    print()
    print(f"PromptManager {__version__}")
    print(f"  Data dir   : {get_data_dir()}")
    print(f"  Database   : {_resolve_db_path(None)}")
    print(f"  Images     : {monitoring}")
    print(f"  Gallery    : {app_url}{LANDING_PATH}")
    print(f"  Admin      : {app_url}/prompt_manager/admin")

    if args.token:
        print(f"  Auth       : token required — {app_url}{LANDING_PATH}?token=...")
    elif args.host not in LOCAL_HOSTS:
        print()
        print(
            f"  WARNING: bound to {args.host} with no authentication. The API "
            "includes\n           destructive endpoints (delete, restore, "
            "maintenance). Use --token."
        )
    print(flush=True)


def main(argv=None):
    """Entry point for the PromptManager server.

    Args:
        argv: Argument list, or None to use sys.argv.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    apply_env(args)

    from aiohttp import web

    from .app import LANDING_PATH, create_app

    app = create_app(token=args.token)

    display_host = "localhost" if args.host in ("0.0.0.0", "::") else args.host
    app_url = f"http://{display_host}:{args.port}"
    describe_startup(args, app_url)

    if args.open:
        import webbrowser

        webbrowser.open(f"{app_url}{LANDING_PATH}")

    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
