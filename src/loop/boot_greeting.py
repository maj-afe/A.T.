"""
Boot greeting selector based on shutdown state and time of day.
"""

from __future__ import annotations

from datetime import datetime


def select_boot_greeting(unclean_shutdown: bool) -> str:
    """
    Select boot greeting line.

    Args:
        unclean_shutdown: True if the previous session exited ungracefully.

    Returns:
        The greeting string to speak on startup.
    """
    if unclean_shutdown:
        return "I'm here, sir."

    hour = datetime.now().hour
    if hour < 12:
        return "Good morning, sir."
    elif hour < 17:
        return "Good afternoon, sir."
    else:
        return "Good evening, sir."
