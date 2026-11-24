#!/usr/bin/env python3
"""Allow ``python -I /path/to/road_lottery`` to run the CLI."""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from roadtool.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
