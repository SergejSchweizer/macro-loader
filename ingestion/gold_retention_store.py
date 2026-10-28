"""Physical sweep Adapter for catalog-tombstoned immutable Gold bundles."""

from __future__ import annotations

import logging
from collections.abc import Callable

from application.paths import LakePaths

FaultInjector = Callable[[str], None]
logger = logging.getLogger(__name__)


def _no_fault(stage: str) -> None:
    del stage


class GoldBundleSweeper:
    """Delete the exact three-artifact immutable bundle; missing files are idempotent."""

    def __init__(
        self,
        paths: LakePaths,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self._paths = paths
        self._fault = fault_injector if fault_injector is not None else _no_fault

    def sweep(self, build_id: str) -> None:
        logger.debug("gold bundle sweep started build_id=%s", build_id)
        build_root = self._paths.gold_build_root(build_id)
        artifacts = (
            ("data", self._paths.gold_data(build_id)),
            ("manifest", self._paths.gold_build_manifest(build_id)),
            ("plot", self._paths.gold_build_profile(build_id)),
        )
        try:
            for name, path in artifacts:
                if path.parent != build_root:
                    raise ValueError("Gold retention artifact escapes expected build directory")
                self._fault(f"before_delete:{name}")
                path.unlink(missing_ok=True)
                self._fault(f"after_delete:{name}")
            self._fault("before_remove_dir")
            if build_root.exists():
                build_root.rmdir()
            self._fault("after_remove_dir")
        except Exception as exc:
            logger.debug(
                "gold bundle sweep failed build_id=%s error_type=%s",
                build_id,
                type(exc).__name__,
            )
            raise
        logger.debug(
            "gold bundle sweep completed build_id=%s artifact_count=%d", build_id, len(artifacts)
        )
