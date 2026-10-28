"""Rsync adapter for a post-publication Gold mirror."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


class RsyncGoldMirror:
    def __init__(self, source_root: Path, destination_root: Path) -> None:
        self._source_root = source_root
        self._destination_root = destination_root

    def sync(self) -> None:
        logger.debug("gold mirror sync started")
        try:
            self._destination_root.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "rsync",
                    "-aH",
                    "--delete-delay",
                    "--partial",
                    f"{self._source_root}/",
                    f"{self._destination_root}/",
                ],
                check=True,
            )
        except Exception as exc:
            logger.debug("gold mirror sync failed error_type=%s", type(exc).__name__)
            raise
        logger.debug("gold mirror sync completed")
