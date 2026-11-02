"""Post processor for LLM output sanitization, truncation, and refusal shuffling."""

import logging
import random
import re
from typing import Any

logger = logging.getLogger(__name__)


def _edit_distance(s1: str, s2: str) -> int:
    """Calculate Levenshtein distance between two strings."""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)

    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


class PostProcessor:
    """Sanitizes raw LLM output, caps sentence counts, and shuffles repeated refusals."""

    REFUSAL_PHRASINGS = [
        "I can't do that, sir.",
        "That's beyond me, sir.",
        "I'd rather not, sir.",
        "Not something I do, sir.",
        "Negative, sir.",
    ]

    BANNED_PHRASES = [
        "I'm sorry, but ",
        "I'm sorry, ",
        "As an AI, ",
        "As an AI ",
        "I can only ",
        "I'm not able to ",
    ]

    def __init__(self) -> None:
        """Initialize PostProcessor with empty session refusal tracking."""
        self._last_refusal: dict[str, str] = {}

    def process(
        self,
        text: str,
        session_id: str | None = None,
        max_sentences: int = 3,
    ) -> tuple[str, dict[str, Any]]:
        """Sanitize text, enforce sentence caps, and apply refusal shuffling.

        Args:
            text: Raw output string from LLM.
            session_id: Optional session identifier for refusal variety tracking.
            max_sentences: Maximum number of sentences to preserve (default 3).

        Returns:
            Tuple of (sanitized_text, metrics_dict) with:
            - truncated: bool
            - shuffled: bool
            - banned_stripped: bool
        """
        if not text or not text.strip():
            return "", {
                "truncated": False,
                "shuffled": False,
                "banned_stripped": False,
            }

        # 1. Replace ! with .
        cleaned = text.replace("!", ".")

        # 2. Fix \bSir\b not at sentence start -> sir (regex (?<!\. )\bSir\b)
        cleaned = re.sub(r"(?<!\. )\bSir\b", "sir", cleaned)

        # 3. Strip banned phrases at sentence start
        banned_stripped = False
        stripped = True
        while stripped:
            stripped = False
            cleaned = cleaned.strip()
            for phrase in self.BANNED_PHRASES:
                if cleaned.startswith(phrase):
                    cleaned = cleaned[len(phrase):].lstrip()
                    banned_stripped = True
                    stripped = True
                    break

        # 4. Split on [.?!] boundaries (preserve punctuation)
        raw_sentences = [
            s.strip() for s in re.split(r"(?<=[.?!])\s+", cleaned) if s.strip()
        ]
        for i in range(len(raw_sentences)):
            for phrase in self.BANNED_PHRASES:
                if raw_sentences[i].startswith(phrase):
                    raw_sentences[i] = raw_sentences[i][len(phrase):].lstrip()
                    banned_stripped = True
        raw_sentences = [s for s in raw_sentences if s]

        # 5. Truncate to first max_sentences
        truncated = len(raw_sentences) > max_sentences
        sentences = raw_sentences[:max_sentences]

        # 6. Rejoin with . between sentences, ensure final ends with . (or ?)
        formatted: list[str] = []
        for s in sentences:
            if not (s.endswith(".") or s.endswith("?")):
                s = s + "."
            formatted.append(s)
        rejoined = " ".join(formatted)

        # 7. Collapse whitespace
        sanitized = re.sub(r"\s+", " ", rejoined).strip()

        # 8. Refusal shuffler (session-scoped)
        shuffled = False
        words = sanitized.split()
        if len(words) <= 6 and "sir" in sanitized.lower():
            matched_phrasing = None
            for phrasing in self.REFUSAL_PHRASINGS:
                if _edit_distance(sanitized.lower(), phrasing.lower()) <= 2:
                    matched_phrasing = phrasing
                    break

            if matched_phrasing is not None:
                sess_key = session_id or "default"
                last = self._last_refusal.get(sess_key)
                if last is not None and matched_phrasing == last:
                    choices = [p for p in self.REFUSAL_PHRASINGS if p != last]
                    new_phrasing = random.choice(choices)
                    logger.info(
                        "refusal shuffled: '%s' -> '%s' (session: %s)",
                        sanitized,
                        new_phrasing,
                        sess_key,
                    )
                    sanitized = new_phrasing
                    self._last_refusal[sess_key] = new_phrasing
                    shuffled = True
                else:
                    self._last_refusal[sess_key] = matched_phrasing

        return sanitized, {
            "truncated": truncated,
            "shuffled": shuffled,
            "banned_stripped": banned_stripped,
        }
