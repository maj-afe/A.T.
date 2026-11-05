"""Unit tests for LLM PostProcessor."""

import pytest
from llm.post_processor import PostProcessor


@pytest.fixture
def processor() -> PostProcessor:
    """Fixture providing a fresh PostProcessor instance."""
    return PostProcessor()


def test_exclamation_replaced(processor: PostProcessor) -> None:
    """Replace ! with . in output.

    # Catches: ! strip regression
    """
    text, _ = processor.process("Hello! How are you!")
    assert text == "Hello. How are you."


def test_banned_phrase_stripped_at_start(processor: PostProcessor) -> None:
    """Strip banned apology phrase from the start of a response.

    # Catches: banned-phrase filter missing
    """
    text, metrics = processor.process("I'm sorry, but I can't do that, sir.")
    assert text == "I can't do that, sir."
    assert metrics["banned_stripped"] is True


def test_as_an_ai_stripped(processor: PostProcessor) -> None:
    """Strip 'As an AI' disclaimer from response start.

    # Catches: AI-disclaimer not stripped
    """
    text, metrics = processor.process("As an AI, I cannot do that, sir.")
    assert text == "I cannot do that, sir."
    assert metrics["banned_stripped"] is True


def test_sir_capitalization_fixed_mid_sentence(processor: PostProcessor) -> None:
    """Ensure mid-sentence 'Sir' is lowercased to 'sir'.

    # Catches: capitalization fix regression
    """
    text, _ = processor.process("Yes, Sir, that is correct, Sir.")
    assert text == "Yes, sir, that is correct, sir."


def test_sentence_truncation_at_3(processor: PostProcessor) -> None:
    """Truncate outputs longer than 3 sentences to exactly 3 sentences.

    # Catches: truncation regression
    """
    input_text = "Sentence one. Sentence two. Sentence three. Sentence four. Sentence five."
    text, metrics = processor.process(input_text, max_sentences=3)
    assert text == "Sentence one. Sentence two. Sentence three."
    assert metrics["truncated"] is True


def test_truncation_preserves_question(processor: PostProcessor) -> None:
    """Ensure sentence truncation preserves question marks in preserved sentences.

    # Catches: truncation loses ?
    """
    input_text = "Yes. No. Maybe? Extra. More."
    text, metrics = processor.process(input_text, max_sentences=3)
    assert text == "Yes. No. Maybe?"
    assert metrics["truncated"] is True


def test_empty_input_returns_empty(processor: PostProcessor) -> None:
    """Empty string input returns empty string without error.

    # Catches: crash on empty
    """
    text, metrics = processor.process("")
    assert text == ""
    assert metrics["truncated"] is False
    assert metrics["shuffled"] is False
    assert metrics["banned_stripped"] is False


def test_idempotent_when_clean(processor: PostProcessor) -> None:
    """Clean compliant text passes through unchanged.

    # Catches: over-processing
    """
    text, metrics = processor.process("Hello, sir.")
    assert text == "Hello, sir."
    assert metrics["truncated"] is False
    assert metrics["shuffled"] is False
    assert metrics["banned_stripped"] is False


def test_refusal_shuffled_on_repeat(processor: PostProcessor) -> None:
    """Repeated refusal in same session is swapped to another phrasing.

    # Catches: refusal shuffler not active, model always produces identical refusal
    """
    session_id = "session_repeat"
    res1, m1 = processor.process("I'd rather not, sir.", session_id=session_id)
    assert res1 == "I'd rather not, sir."
    assert m1["shuffled"] is False

    res2, m2 = processor.process("I'd rather not, sir.", session_id=session_id)
    assert res2 != "I'd rather not, sir."
    assert m2["shuffled"] is True
    assert res2 in processor.REFUSAL_PHRASINGS


def test_refusal_no_shuffle_first_time(processor: PostProcessor) -> None:
    """First refusal in a new session is not swapped.

    # Catches: shuffler firing when it shouldn't
    """
    session_id = "session_first_time"
    res, metrics = processor.process("I'd rather not, sir.", session_id=session_id)
    assert res == "I'd rather not, sir."
    assert metrics["shuffled"] is False


def test_refusal_not_shuffled_for_normal_text(processor: PostProcessor) -> None:
    """Normal text is never treated as refusal or shuffled.

    # Catches: shuffler misidentifying normal text as refusal
    """
    session_id = "session_normal"
    res, metrics = processor.process("What's the time?", session_id=session_id)
    assert res == "What's the time?"
    assert metrics["shuffled"] is False


def test_all_5_phrasings_reachable(processor: PostProcessor) -> None:
    """Verify that all 5 refusal phrasings can appear across repeated refusals.

    # Catches: shuffler weight bias or pool too small
    """
    session_id = "session_variety"
    seen = set()
    current = "I'd rather not, sir."
    for _ in range(40):
        current, _ = processor.process(current, session_id=session_id)
        seen.add(current)

    assert seen == set(processor.REFUSAL_PHRASINGS)
