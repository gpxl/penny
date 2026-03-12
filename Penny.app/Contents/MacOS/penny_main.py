#!/usr/bin/env python3
"""Penny entry point — sets up sys.path explicitly before importing the app.

This avoids the macOS launchd sandbox PermissionError that occurs when
Python's import machinery tries to stat '' (CWD) via -m.
"""
import sys
import os

# Insert the project root explicitly so 'penny' package is always findable,
# regardless of CWD or sandbox restrictions on path resolution.
_here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _here not in sys.path:
    sys.path.insert(0, _here)

# Remove '' from sys.path to avoid CWD-based permission errors in launchd sandbox.
sys.path = [p for p in sys.path if p != '']

from penny.app import main  # noqa: E402
main()
