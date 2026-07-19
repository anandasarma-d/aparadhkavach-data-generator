"""Puts scripts/ on sys.path so tests/ can `import feature_builder` / `import quickml_scorer`
directly, matching how those modules import each other when run as `python3 scripts/*.py`
(Python auto-prepends a directly-executed script's own directory to sys.path) — pytest has no
equivalent auto-behavior for a separate tests/ directory, so this conftest.py is the one place
that gap is bridged, per pytest's own documented pattern for flat (non-package) script layouts.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
