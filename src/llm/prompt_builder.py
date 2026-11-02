"""Prompt builder with sliding window trimming and elaboration detection."""

from pathlib import Path
from typing import Any


class PromptBuilder:
    """Builds chat completion prompts within token budget."""

    def __init__(
        self,
        personality_path: str | Path,
        engine: Any,
        max_context_tokens: int = 13000,
    ) -> None:
        """Initialize PromptBuilder with personality prompt and token budget.

        Args:
            personality_path: Path to personality.md file.
            engine: Engine instance or object with count_tokens(str) -> int method.
            max_context_tokens: Token budget for context (default 13000).
        """
        self.personality_path = Path(personality_path)
        self.engine = engine
        self.max_context_tokens = max_context_tokens

        with open(self.personality_path, "r", encoding="utf-8") as f:
            self.system_prompt = f.read()

    def _turn_tokens(self, turn: dict[str, Any]) -> int:
        """Calculate token count for a turn."""
        content = str(turn.get("content", ""))
        return self.engine.count_tokens(content)

    def build(
        self,
        user_message: str,
        session_turns: list[dict[str, Any]],
    ) -> tuple[list[dict[str, str]], bool]:
        """Build messages list with anchor and recent turns fitting the token budget.

        Trimming policy:
        - Keep first 3 turns as anchor
        - Keep recent turns until total tokens <= max_context_tokens
        - If under budget, include all turns

        Elaboration detection:
        - Checks for "elaborate", "in detail", "explain fully" in user_message

        Args:
            user_message: The new incoming user message.
            session_turns: List of chronological previous messages/turns.

        Returns:
            Tuple of (messages, elaboration_flag).
        """
        elaboration_keywords = ["elaborate", "in detail", "explain fully"]
        lower_msg = user_message.lower()
        elaboration = any(k in lower_msg for k in elaboration_keywords)

        system_tokens = self.engine.count_tokens(self.system_prompt)
        user_tokens = self.engine.count_tokens(user_message)
        base_tokens = system_tokens + user_tokens

        if len(session_turns) <= 3:
            anchor = list(session_turns)
            candidates: list[dict[str, Any]] = []
        else:
            anchor = list(session_turns[:3])
            candidates = list(session_turns[3:])

        anchor_tokens = sum(self._turn_tokens(t) for t in anchor)
        current_tokens = base_tokens + anchor_tokens
        candidates_tokens = sum(self._turn_tokens(t) for t in candidates)

        if current_tokens + candidates_tokens <= self.max_context_tokens:
            trimmed_turns = anchor + candidates
        else:
            # Over budget: trim middle by keeping most recent candidates that fit
            kept_recent = []
            remaining_budget = self.max_context_tokens - current_tokens

            for t in reversed(candidates):
                cost = self._turn_tokens(t)
                if remaining_budget - cost >= 0:
                    kept_recent.append(t)
                    remaining_budget -= cost
                else:
                    break

            kept_recent.reverse()
            trimmed_turns = anchor + kept_recent

        messages: list[dict[str, str]] = (
            [{"role": "system", "content": self.system_prompt}]
            + [
                {"role": t.get("role", "user"), "content": str(t.get("content", ""))}
                for t in trimmed_turns
            ]
            + [{"role": "user", "content": user_message}]
        )

        return messages, elaboration
