"""
Unit tests for sentence chunker.
"""

from loop.sentence_chunker import chunk_sentences


def test_simple_sentences():
    """Simple sentences split on terminal punctuation.

    # Catches: basic splitting regression
    """
    text = "Hello. World. How are you?"
    expected = ["Hello.", "World.", "How are you?"]
    assert chunk_sentences(text) == expected


def test_abbreviation_not_split():
    """Abbreviations like Mr. are not treated as sentence boundaries.

    # Catches: abbreviation handling missing
    """
    text = "Mr. Smith went to Washington."
    expected = ["Mr. Smith went to Washington."]
    assert chunk_sentences(text) == expected


def test_decimal_not_split():
    """Decimals like 3.14159 are not split.

    # Catches: decimal handling missing
    """
    text = "Pi is 3.14159 approximately."
    expected = ["Pi is 3.14159 approximately."]
    assert chunk_sentences(text) == expected


def test_semicolon_splits():
    """Semicolons act as sentence chunk boundaries.

    # Catches: semicolon handling
    """
    text = "First; second; third."
    expected = ["First;", "second;", "third."]
    assert chunk_sentences(text) == expected


def test_long_sentence_force_split():
    """250-char string with no punctuation splits into 2 pieces at whitespace.

    # Catches: runaway single-sentence response
    """
    # 50 words of 4 chars + 1 space = 250 chars
    words = ["word"] * 50
    text = " ".join(words)
    assert len(text) == 249
    chunks = chunk_sentences(text)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk) <= 200
        assert len(chunk) > 0


def test_empty_returns_empty_list():
    """Empty string returns empty list.

    # Catches: crash on empty input
    """
    assert chunk_sentences("") == []


def test_whitespace_only_returns_empty_list():
    """Whitespace-only string returns empty list.

    # Catches: crash on whitespace
    """
    assert chunk_sentences("   \t \n ") == []


def test_single_sentence_no_split():
    """Single sentence is not split.

    # Catches: over-splitting
    """
    assert chunk_sentences("Yes.") == ["Yes."]
