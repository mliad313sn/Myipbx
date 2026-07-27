"""Module entry point: ``python3 -m appliance``."""

from __future__ import annotations

import sys

from .server import run

if __name__ == "__main__":
    sys.exit(run())
