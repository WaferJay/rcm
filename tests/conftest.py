"""Shared pytest fixtures for the rcm test suite."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repository root is on sys.path so `import rcm` works without `pip install -e .`.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
