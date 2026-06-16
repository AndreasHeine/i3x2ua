from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_SERVER_VERSION_FALLBACK = "master"
_SERVER_VERSION_FILE = Path(__file__).resolve().parents[1] / "server-version.txt"


@lru_cache(maxsize=1)
def get_server_version() -> str:
    try:
        value = _SERVER_VERSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except FileNotFoundError:
        logger.debug("Server version file not found at %s; using fallback", _SERVER_VERSION_FILE)
    except OSError:
        logger.warning(
            "Failed to read server version file at %s; using fallback",
            _SERVER_VERSION_FILE,
            exc_info=True,
        )

    return _SERVER_VERSION_FALLBACK
