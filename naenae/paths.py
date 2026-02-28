"""Runtime path resolution for Nae Nae.

Resolution order for the writable data directory:
  1. NAENAE_HOME env var — set by install.sh in the launchd plist so that
     dev-clone installs keep their data next to the source tree.
  2. ~/.naenae/ — the default for pipx, Homebrew, and bootstrap installs.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Return the writable data directory, creating it if needed."""
    env = os.environ.get("NAENAE_HOME")
    d = Path(env) if env else Path.home() / ".naenae"
    d.mkdir(parents=True, exist_ok=True)
    return d
