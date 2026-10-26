from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import polars as pl
import pytest

from application.parallelism import PolarsExecutionPolicy


def test_all_available_cores_matches_polars_thread_pool() -> None:
    assert PolarsExecutionPolicy.all_available_cores().workers == pl.thread_pool_size()


def test_policy_preserves_input_order_and_rejects_invalid_worker_count() -> None:
    assert PolarsExecutionPolicy(3).map(lambda value: value * 2, (3, 1, 2)) == [6, 2, 4]
    with pytest.raises(ValueError, match="workers"):
        PolarsExecutionPolicy(0)


def test_policy_returns_input_order_when_tasks_finish_out_of_order() -> None:
    policy = PolarsExecutionPolicy(3)
    entered = {value: Event() for value in ("first", "second", "third")}
    release = {value: Event() for value in entered}
    completed = {value: Event() for value in entered}
    finished: list[str] = []
    finished_lock = Lock()

    def wait_for_release(value: str) -> str:
        entered[value].set()
        assert release[value].wait(timeout=2)
        with finished_lock:
            finished.append(value)
        completed[value].set()
        return value

    with ThreadPoolExecutor(max_workers=1) as caller:
        pending = caller.submit(policy.map, wait_for_release, ("first", "second", "third"))
        assert all(event.wait(timeout=2) for event in entered.values())
        for value in ("third", "second", "first"):
            release[value].set()
            assert completed[value].wait(timeout=2)
        assert pending.result(timeout=2) == ["first", "second", "third"]

    assert finished == ["third", "second", "first"]


def test_shared_budget_covers_concurrent_single_item_maps() -> None:
    policy = PolarsExecutionPolicy(1)
    first_entered = Event()
    release_first = Event()
    second_entered = Event()

    def first(value: str) -> str:
        first_entered.set()
        assert release_first.wait(timeout=2)
        return value

    def second(value: str) -> str:
        second_entered.set()
        return value

    with ThreadPoolExecutor(max_workers=2) as caller:
        first_result = caller.submit(policy.map, first, ("first",))
        assert first_entered.wait(timeout=2)
        second_result = caller.submit(policy.map, second, ("second",))
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        assert first_result.result(timeout=2) == ["first"]
        assert second_result.result(timeout=2) == ["second"]

    assert second_entered.is_set()
