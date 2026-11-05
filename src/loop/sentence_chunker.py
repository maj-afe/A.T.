"""
Sentence chunker for text-to-speech pipelining.
"""

from __future__ import annotations

import re

ABBREVIATIONS = {
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "sr.",
    "jr.",
    "etc.",
    "i.e.",
    "e.g.",
    "vs.",
}

MAX_CHUNK_LENGTH = 200


def _force_split(text: str, max_len: int = MAX_CHUNK_LENGTH) -> list[str]:
    """Force-split text exceeding max_len at whitespace boundaries."""
    chunks = []
    remaining = text.strip()
    while len(remaining) > max_len:
        split_idx = remaining[:max_len].rfind(" ")
        if split_idx <= 0:
            split_idx = max_len
        chunk = remaining[:split_idx].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_idx:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def chunk_sentences(text: str) -> list[str]:
    """
    Split text into sentence chunks suitable for TTS.

    Splits on sentence terminal punctuation (. ? ! ;) followed by whitespace or EOS,
    while preserving abbreviations (Mr., Dr., etc.) and decimals.
    Force-splits text at <= 200 characters if no punctuation boundary is found.

    Args:
        text: Input string.

    Returns:
        List of non-empty stripped sentence chunks.
    """
    if not text or not text.strip():
        return []

    raw = text.strip()
    sentences = []
    current_start = 0
    n = len(raw)
    i = 0

    while i < n:
        ch = raw[i]
        if ch in ".?!;":
            # Check if followed by whitespace or EOS
            is_followed_by_space_or_eos = (i + 1 == n) or raw[i + 1].isspace()

            if is_followed_by_space_or_eos:
                # Check for decimal: digit before and digit after
                is_decimal = (
                    ch == "."
                    and i > 0
                    and i + 1 < n
                    and raw[i - 1].isdigit()
                    and raw[i + 1].isdigit()
                )

                # Check for abbreviation
                is_abbrev = False
                if ch == ".":
                    token_start = i - 1
                    while token_start >= current_start and not raw[token_start].isspace():
                        token_start -= 1
                    token = raw[token_start + 1 : i + 1].lower()
                    if token in ABBREVIATIONS:
                        is_abbrev = True

                if not is_decimal and not is_abbrev:
                    # Include consecutive punctuation marks like "?!" or "..."
                    punct_end = i + 1
                    while punct_end < n and raw[punct_end] in ".?!;":
                        punct_end += 1

                    sentence = raw[current_start:punct_end].strip()
                    if sentence:
                        sentences.append(sentence)

                    # Advance past whitespace
                    while punct_end < n and raw[punct_end].isspace():
                        punct_end += 1
                    current_start = punct_end
                    i = punct_end
                    continue
        i += 1

    if current_start < n:
        remainder = raw[current_start:].strip()
        if remainder:
            sentences.append(remainder)

    result = []
    for s in sentences:
        if len(s) > MAX_CHUNK_LENGTH:
            result.extend(_force_split(s, MAX_CHUNK_LENGTH))
        else:
            result.append(s)

    return result
