"""Bounded all-core execution policy for independent Polars work units."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from threading import BoundedSemaphore
from typing import TypeVar

import polars as pl

logger = logging.getLogger(__name__)

Input = TypeVar("Input")
Output = TypeVar("Output")


@dataclass(frozen=True, slots=True)
class PolarsExecutionPolicy:
    """Share one bounded concurrency budget across independent application work."""

    workers: int
    _slots: BoundedSemaphore = dataclass_field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        object.__setattr__(self, "_slots", BoundedSemaphore(self.workers))

    @classmethod
    def all_available_cores(cls) -> PolarsExecutionPolicy:
        return cls(pl.thread_pool_size())

    def map(self, function: Callable[[Input], Output], values: Sequence[Input]) -> list[Output]:
        item_count = len(values)
        effective_workers = min(self.workers, item_count)
        logger.debug(
            "parallel map started item_count=%d worker_limit=%d effective_workers=%d",
            item_count,
            self.workers,
            effective_workers,
        )

        try:
            if item_count < 2:
                results = [self._run_with_slot(function, value) for value in values]
            else:

                def run(value: Input) -> Output:
                    return self._run_with_slot(function, value)

                with ThreadPoolExecutor(max_workers=effective_workers) as executor:
                    results = list(executor.map(run, values))
        except Exception as exc:
            logger.debug(
                "parallel map failed item_count=%d error_type=%s",
                item_count,
                type(exc).__name__,
            )
            raise

        logger.debug(
            "parallel map completed item_count=%d worker_limit=%d effective_workers=%d",
            item_count,
            self.workers,
            effective_workers,
        )
        return results

    def _run_with_slot(self, function: Callable[[Input], Output], value: Input) -> Output:
        with self._slots:
            return function(value)
