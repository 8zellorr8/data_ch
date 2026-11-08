"""A paid token allowance must survive retries, concurrent workers and restarts."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from ai.budget import TokenBudget, TokenReservation
from backend.errors import DomainError


def test_jury_reserve_and_known_usage(tmp_path):
    budget = TokenBudget(tmp_path / "tokens.sqlite3", total=1000, jury_reserve=600)
    first = budget.reserve("test", 300)
    with pytest.raises(DomainError):
        budget.reserve("test", 101)
    budget.settle(first, input_tokens=30, output_tokens=20)
    restarted = TokenBudget(budget.path, total=1000, jury_reserve=600)
    assert restarted.snapshot()["accounted_tokens"] == 50
    second = restarted.reserve("test", 350)
    with pytest.raises(DomainError):
        restarted.reserve("test", 1)
    # An interrupted request remains charged at its conservative reservation.
    assert (
        TokenBudget(budget.path, total=1000, jury_reserve=600).snapshot()["accounted_tokens"] == 400
    )
    restarted.settle(second, input_tokens=None, output_tokens=None)
    assert restarted.snapshot()["uncertain_requests"] == 1
    judge = TokenBudget(budget.path, total=1000, jury_reserve=600, use_jury_reserve=True)
    judge.reserve("test", 600)
    assert judge.snapshot()["remaining_total"] == 0


def test_parallel_workers_cannot_overbook_budget(tmp_path):
    path = tmp_path / "tokens.sqlite3"

    def reserve(_):
        try:
            TokenBudget(path, total=1000, jury_reserve=600).reserve("test", 100)
            return True
        except DomainError:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(reserve, range(12)))
    assert sum(results) == 4
    assert TokenBudget(path, total=1000, jury_reserve=600).snapshot()["accounted_tokens"] == 400


def test_total_usage_and_partial_usage_are_conservative(tmp_path):
    budget = TokenBudget(tmp_path / "tokens.sqlite3", total=1000, jury_reserve=0)
    first = budget.reserve("test", 200)
    budget.settle(first, input_tokens=10, output_tokens=20, total_tokens=60)
    # Repeated completion cannot refund an already accounted request.
    budget.settle(first, input_tokens=0, output_tokens=0)
    second = budget.reserve("test", 200)
    budget.settle(second, input_tokens=10, output_tokens=None)
    assert budget.snapshot()["accounted_tokens"] == 260
    with pytest.raises(ValueError):
        budget.settle(TokenReservation("absent", 1), input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError):
        TokenBudget(budget.path, total=2000, jury_reserve=0).snapshot()


def test_initial_spend_is_not_reset_on_deployment(tmp_path):
    path = tmp_path / "tokens.sqlite3"
    budget = TokenBudget(path, total=1000, jury_reserve=600, initial_spent=350)
    with pytest.raises(DomainError):
        budget.reserve("test", 51)
    assert (
        TokenBudget(path, total=1000, jury_reserve=600, initial_spent=350).snapshot()[
            "accounted_tokens"
        ]
        == 350
    )
