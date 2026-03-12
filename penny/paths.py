"""Runtime path resolution for Penny.

Resolution order for the writable data directory:
  1. PENNY_HOME env var — set by install.sh in the launchd plist so that
     dev-clone installs keep their data next to the source tree.
  2. ~/.penny/ — the default for pipx and bootstrap installs.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Return the writable data directory, creating it if needed."""
    env = os.environ.get("PENNY_HOME")
    d = Path(env) if env else Path.home() / ".penny"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d
