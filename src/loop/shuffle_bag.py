"""
ShuffleBag implementation for weighted random line selection with no consecutive repeats.
"""

from __future__ import annotations

import random
from typing import Sequence


class ShuffleBag:
    """
    Weighted shuffle bag for dialogue line selection.

    Selects lines based on configured weights while preventing consecutive repeats
    and supporting explicit exclusions.
    """

    def __init__(self, lines_with_weights: Sequence[tuple[str, float]]) -> None:
        """
        Initialize the shuffle bag with weighted lines.

        Args:
            lines_with_weights: List of (line_text, weight) tuples.

        Raises:
            ValueError: If lines_with_weights is empty.
        """
        if not lines_with_weights:
            raise ValueError("ShuffleBag requires at least one line")
        self._lines_with_weights: list[tuple[str, float]] = list(lines_with_weights)
        self._last_selection: str | None = None

    def select(self, exclude: str | None = None) -> str:
        """
        Select a line according to weights, excluding specified line or last selection.

        Args:
            exclude: Optional line to exclude. If None, excludes _last_selection if possible.

        Returns:
            The selected line string.
        """
        unique_lines = {line for line, _ in self._lines_with_weights}

        target_exclude: str | None = None
        if exclude is not None:
            target_exclude = exclude
        elif len(unique_lines) > 2:
            target_exclude = self._last_selection

        candidates = [
            (line, w)
            for line, w in self._lines_with_weights
            if line != target_exclude
        ]
        if not candidates:
            # Fallback if exclusion removed all candidates
            candidates = self._lines_with_weights

        lines = [c[0] for c in candidates]
        weights = [c[1] for c in candidates]

        total_weight = sum(weights)
        if total_weight <= 0:
            weights = [1.0] * len(candidates)

        chosen = random.choices(lines, weights=weights, k=1)[0]
        self._last_selection = chosen
        return chosen
