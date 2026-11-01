"""A tiny shopping-cart module with tests. Two of them fail on purpose, so there is something to triage."""

import pytest


def total(prices, discount=0.0):
    return round(sum(prices) * (1 - discount), 2)


def test_total_without_discount():
    assert total([10, 20]) == 30


def test_total_with_discount():
    assert total([100], 0.1) == 90


def test_total_rounds_to_cents():
    assert total([0.1, 0.2]) == 0.3


def test_discount_above_100_percent_is_rejected():
    # Known bug: total() accepts any discount, so this returns a negative price instead of raising.
    with pytest.raises(ValueError):
        total([10], 1.5)


def test_empty_cart_is_free():
    assert total([]) == 0


@pytest.mark.skip(reason="coupons are not implemented yet")
def test_coupon_codes():
    raise AssertionError("not reached")
