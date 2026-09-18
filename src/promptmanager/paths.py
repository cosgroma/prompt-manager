"""Filesystem locations used by PromptManager.

Everything the application writes — the database, config.json, logs and
downloaded models — lives under a single data directory so that the installed
package itself stays read-only.

The data directory is ``$PROMPT_MANAGER_HOME`` when set, otherwise
``~/.promptmanager``. This module has no dependencies on the rest of the
package so that logging and configuration can both import it.
"""

import os
from pathlib import Path

DATA_DIR_ENV = "PROMPT_MANAGER_HOME"
DEFAULT_DATA_DIR_NAME = ".promptmanager"

PACKAGE_DIR = Path(__file__).resolve().parent

#: Static web assets shipped inside the package.
WEB_DIR = PACKAGE_DIR / "web"


def get_data_dir(create: bool = True) -> Path:
    """Get the directory holding the database, config, logs and models.

    Args:
        create: Create the directory if it does not exist.

    Returns:
        Absolute path to the data directory.
    """
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        data_dir = Path(override).expanduser()
    else:
        data_dir = Path.home() / DEFAULT_DATA_DIR_NAME

    data_dir = data_dir.resolve()
    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_config_file() -> Path:
    """Get the path of the JSON configuration file.

    Returns:
        ``config.json`` inside the data directory.
    """
    return get_data_dir() / "config.json"


def get_log_dir() -> Path:
    """Get the directory log files are written to, creating it if needed.

    Returns:
        ``logs`` inside the data directory.
    """
    log_dir = get_data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_models_dir() -> Path:
    """Get the directory downloaded models are stored in.

    Returns:
        ``models`` inside the data directory (not created).
    """
    return get_data_dir() / "models"
