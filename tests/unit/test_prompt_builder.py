"""Unit tests for PromptBuilder."""

from pathlib import Path
from unittest.mock import MagicMock
import pytest

from llm.prompt_builder import PromptBuilder


@pytest.fixture
def personality_file(tmp_path: Path) -> Path:
    """Create a temporary personality file for unit testing."""
    p = tmp_path / "personality.md"
    p.write_text("You are AT. Address user as sir.", encoding="utf-8")
    return p


@pytest.fixture
def mock_engine() -> MagicMock:
    """Mock engine providing count_tokens method."""
    engine = MagicMock()
    engine.count_tokens.return_value = 10
    return engine


def test_under_budget_includes_all_turns(
    personality_file: Path, mock_engine: MagicMock
) -> None:
    """Under-budget session includes all turns without trimming.

    # Catches: over-trimming
    """
    # 5 turns, 200 tokens each = 1000 tokens total. Budget 13000.
    mock_engine.count_tokens.return_value = 200
    builder = PromptBuilder(
        personality_path=personality_file,
        engine=mock_engine,
        max_context_tokens=13000,
    )

    turns = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"}
        for i in range(5)
    ]
    messages, _ = builder.build("Hello", turns)

    # 1 system + 5 turns + 1 user = 7 messages
    assert len(messages) == 7
    turn_contents = [m["content"] for m in messages[1:-1]]
    assert turn_contents == [f"Turn {i}" for i in range(5)]


def test_over_budget_trims_middle(
    personality_file: Path, mock_engine: MagicMock
) -> None:
    """Over-budget session preserves first 3 anchor turns and recent turns, dropping middle.

    # Catches: anchor not preserved during trim
    """
    # 40 turns, 500 tokens each = 20000 tokens. Budget 13000.
    mock_engine.count_tokens.return_value = 500
    builder = PromptBuilder(
        personality_path=personality_file,
        engine=mock_engine,
        max_context_tokens=13000,
    )

    turns = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"Turn {i}"}
        for i in range(40)
    ]
    messages, _ = builder.build("Hello", turns)

    # First 3 turns must be preserved as anchor
    assert messages[1]["content"] == "Turn 0"
    assert messages[2]["content"] == "Turn 1"
    assert messages[3]["content"] == "Turn 2"

    # Turn 3 (first non-anchor) should have been dropped
    mid_contents = [m["content"] for m in messages]
    assert "Turn 3" not in mid_contents

    # Most recent turn must be preserved
    assert messages[-2]["content"] == "Turn 39"


def test_system_prompt_always_first(
    personality_file: Path, mock_engine: MagicMock
) -> None:
    """System prompt is always the first message in the returned list.

    # Catches: system prompt out of order
    """
    builder = PromptBuilder(
        personality_path=personality_file,
        engine=mock_engine,
        max_context_tokens=13000,
    )
    messages, _ = builder.build("Test prompt", [])
    assert messages[0]["role"] == "system"
    assert "You are AT" in messages[0]["content"]


def test_new_user_message_always_last(
    personality_file: Path, mock_engine: MagicMock
) -> None:
    """New incoming user message is always the last item in the prompt list.

    # Catches: user message placement bug
    """
    builder = PromptBuilder(
        personality_path=personality_file,
        engine=mock_engine,
        max_context_tokens=13000,
    )
    messages, _ = builder.build("Current user question", [{"role": "user", "content": "Previous"}])
    assert messages[-1]["role"] == "user"
    assert messages[-1]["content"] == "Current user question"


def test_elaboration_flag_detected(
    personality_file: Path, mock_engine: MagicMock
) -> None:
    """Elaboration requests trigger the elaboration flag.

    # Catches: elaboration detection missing
    """
    builder = PromptBuilder(
        personality_path=personality_file,
        engine=mock_engine,
        max_context_tokens=13000,
    )

    _, elab1 = builder.build("Can you elaborate on that?", [])
    assert elab1 is True

    _, elab2 = builder.build("Please explain this in detail.", [])
    assert elab2 is True

    _, elab3 = builder.build("Could you explain fully what happened?", [])
    assert elab3 is True

    _, elab_none = builder.build("What is the time?", [])
    assert elab_none is False
