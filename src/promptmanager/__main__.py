"""Allow running the application with ``python -m promptmanager``."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
