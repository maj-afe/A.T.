"""
Unit tests for ShuffleBag line selection.
"""

from collections import Counter
import pytest

from loop.shuffle_bag import ShuffleBag


def test_select_returns_valid_line():
    """Verify select() returns one of the configured lines.

    # Catches: empty pool or malformed config
    """
    lines = [("Line 1", 0.5), ("Line 2", 0.3), ("Line 3", 0.2)]
    bag = ShuffleBag(lines)
    selected = bag.select()
    valid_lines = {line for line, _ in lines}
    assert selected in valid_lines


def test_select_never_repeats_consecutive():
    """Call select 100 times, verify no two consecutive selections are identical.

    # Catches: no-repeat logic missing
    """
    lines = [
        ("Sir?", 0.30),
        ("Yes, sir.", 0.10),
        ("Listening, sir.", 0.10),
        ("Go ahead, sir.", 0.10),
        ("Ready, sir.", 0.10),
    ]
    bag = ShuffleBag(lines)
    prev = None
    for _ in range(100):
        curr = bag.select()
        assert curr != prev, f"Got consecutive duplicate selection: {curr!r}"
        prev = curr


def test_select_respects_exclusion():
    """Pool of 2, exhaust one via 10 consecutive calls with exclude set to it; verify always the other.

    # Catches: exclusion ignored
    """
    pool = [("option_a", 0.5), ("option_b", 0.5)]
    bag = ShuffleBag(pool)
    for _ in range(10):
        selected = bag.select(exclude="option_a")
        assert selected == "option_b", f"Expected 'option_b' but got {selected!r}"


def test_weights_approximately_correct():
    """Pool [("a", 0.7), ("b", 0.3)]; call 10000 times; verify a occurs between 65% and 75%.

    # Catches: weight calculation wrong
    """
    pool = [("a", 0.7), ("b", 0.3)]
    bag = ShuffleBag(pool)
    counts = Counter(bag.select() for _ in range(10000))
    a_pct = counts["a"] / 10000.0
    assert 0.65 <= a_pct <= 0.75, f"Expected 'a' frequency in [0.65, 0.75], got {a_pct}"


def test_single_item_pool_returns_it():
    """Pool of 1, exclude=None, returns it; exclude=it, returns it anyway (fallback).

    # Catches: crash on degenerate pool
    """
    pool = [("only_one", 1.0)]
    bag = ShuffleBag(pool)
    assert bag.select() == "only_one"
    assert bag.select(exclude="only_one") == "only_one"
