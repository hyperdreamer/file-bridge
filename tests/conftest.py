from __future__ import annotations

import sys
from pathlib import Path

# Ensure the project root precedes any ambient main module on the path.
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)
del _project_root
