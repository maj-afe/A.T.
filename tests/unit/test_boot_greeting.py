"""
Unit tests for boot greeting selector.
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

from loop.boot_greeting import select_boot_greeting


def _mock_now_with_hour(hour: int) -> MagicMock:
    mock_dt = MagicMock(spec=datetime)
    mock_dt.hour = hour
    return mock_dt


def test_morning():
    """Morning greeting before 12:00.

    # Catches: morning threshold wrong
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(8)
        assert select_boot_greeting(unclean_shutdown=False) == "Good morning, sir."


def test_afternoon():
    """Afternoon greeting between 12:00 and 17:00.

    # Catches: afternoon threshold wrong
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(14)
        assert select_boot_greeting(unclean_shutdown=False) == "Good afternoon, sir."


def test_evening():
    """Evening greeting after 17:00.

    # Catches: evening threshold wrong
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(20)
        assert select_boot_greeting(unclean_shutdown=False) == "Good evening, sir."


def test_boundary_12():
    """Boundary at 12:00 should be afternoon.

    # Catches: 12:00 falls on wrong side
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(12)
        assert select_boot_greeting(unclean_shutdown=False) == "Good afternoon, sir."


def test_boundary_17():
    """Boundary at 17:00 should be evening.

    # Catches: 17:00 falls on wrong side
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(17)
        assert select_boot_greeting(unclean_shutdown=False) == "Good evening, sir."


def test_unclean_shutdown_overrides():
    """Unclean shutdown takes precedence over time of day.

    # Catches: unclean shutdown path broken
    """
    with patch("loop.boot_greeting.datetime") as mock_datetime:
        mock_datetime.now.return_value = _mock_now_with_hour(8)
        assert select_boot_greeting(unclean_shutdown=True) == "I'm here, sir."
