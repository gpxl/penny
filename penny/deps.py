"""Auto-install missing Python dependencies at startup.

pexpect and pyte are declared in requirements.txt and installed by install.sh,
but a broken PATH or partial install can leave them missing.  Rather than
silently degrading, we detect the gap and pip-install them on the spot.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REQUIRED = ("pexpect", "pyte")


def ensure_deps() -> None:
    """Import-check required packages; auto-install any that are missing."""
    missing = []
    for pkg in _REQUIRED:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)

    if not missing:
        return

    req_file = Path(__file__).resolve().parent.parent / "requirements.txt"
    print(f"[penny] Missing dependencies: {', '.join(missing)} — installing…", flush=True)

    cmd = [sys.executable, "-m", "pip", "install", "--break-system-packages"]
    if req_file.exists():
        cmd += ["-r", str(req_file)]
    else:
        cmd += missing

    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
        print("[penny] Dependencies installed successfully.", flush=True)
    except Exception as exc:
        print(f"[penny] Failed to install dependencies: {exc}", flush=True)
        print("[penny] Run: pip install pexpect pyte", flush=True)
