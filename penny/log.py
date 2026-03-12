"""Structured logging with rotation for Penny.

Provides a pre-configured logger that writes to both stderr (for launchd
capture) and a rotating log file (~/.penny/logs/penny.log).  Rotation
prevents unbounded growth: 5 MB per file, 3 backup files (20 MB max).

Usage:
    from penny.log import logger
    logger.info("config reloaded")
    logger.warning("skipped bad entry: %s", entry)
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import data_dir

# Module-level logger — all of Penny shares one logger named "penny".
logger = logging.getLogger("penny")

_INITIALISED = False

# Rotation settings
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
_BACKUP_COUNT = 3              # keep 3 rotated copies (~20 MB total)
_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _ensure_init() -> None:
    """Lazily initialise handlers on first use.

    Called automatically when the module is imported.  Safe to call
    multiple times (idempotent).
    """
    global _INITIALISED
    if _INITIALISED:
        return
    _INITIALISED = True

    logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT)

    # Stderr handler — captured by launchd and visible in Console.app
    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)

    # Rotating file handler — prevents unbounded disk growth
    log_dir = data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "penny.log"

    try:
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError:
        # If the log file can't be opened (permissions, disk full), continue
        # with stderr only — logging should never crash the app.
        pass


# Auto-initialise on import
_ensure_init()
