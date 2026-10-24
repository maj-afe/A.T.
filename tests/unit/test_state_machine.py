"""
Adversarial unit tests for the VAD two-state machine using a scripted scorer.

No model file and no onnxruntime inference: every probability is deterministic.
"""

import pytest

from vad.state_machine import (
    FRAME_BYTES,
    SPEECH_ENDED,
    SPEECH_REJECTED,
    SPEECH_STARTED,
    VADStateMachine,
)

SPEECH_FRAME = b"\x01" * FRAME_BYTES
SILENCE_FRAME = b"\x00" * FRAME_BYTES


class ContentScorer:
    """Scores frames deterministically: first byte 0x01 = speech, 0x00 = silence."""

    def __init__(self, speech_prob: float = 0.9, silence_prob: float = 0.1) -> None:
        self.speech_prob = speech_prob
        self.silence_prob = silence_prob
        self.calls = 0

    def score_frame(self, frame: bytes) -> float:
        self.calls += 1
        return self.speech_prob if frame[0] == 0x01 else self.silence_prob

    def reset(self) -> None:
        self.calls = 0


def _run(kind: str, count: int) -> list[bytes]:
    """Return `count` identical speech ('v') or silence ('s') frames."""
    return [SPEECH_FRAME if kind == "v" else SILENCE_FRAME] * count


async def _collect(machine: VADStateMachine, frames: list[bytes]):
    return [event async for event in machine.process_frames(frames)]


@pytest.mark.asyncio
async def test_a_silence_speech_silence_emits_start_and_end():
    # Catches: state machine not transitioning on threshold crossing
    scorer = ContentScorer()
    machine = VADStateMachine(scorer)
    frames = _run("s", 3) + _run("v", 10) + _run("s", 25)

    events = await _collect(machine, frames)

    assert [event.type for event in events] == [SPEECH_STARTED, SPEECH_ENDED]
    started, ended = events
    assert started.frame_index == 3
    assert ended.frame_index == 37
    assert ended.duration_ms == 35 * 30  # started at frame 3, ended at frame 37
    assert ended.voiced_ms == 300
    assert ended.audio == b"".join(frames[3:])
    assert machine.state == "SILENT"
    assert machine.buffered_frames == 0


@pytest.mark.asyncio
async def test_b_short_utterance_is_rejected():
    # Catches: short utterances not rejected
    machine = VADStateMachine(ContentScorer())
    frames = _run("s", 3) + _run("v", 5) + _run("s", 25)  # 150 ms of speech

    events = await _collect(machine, frames)

    assert [event.type for event in events] == [SPEECH_STARTED, SPEECH_REJECTED]
    rejected = events[1]
    assert rejected.voiced_ms == 150
    assert rejected.audio is None
    assert machine.state == "SILENT"
    assert machine.buffered_frames == 0
    assert machine.buffered_bytes == 0


@pytest.mark.asyncio
async def test_c_500ms_pause_does_not_split_utterance():
    # Catches: mid-utterance pauses incorrectly ending the utterance
    machine = VADStateMachine(ContentScorer())
    frames = (
        _run("s", 3)
        + _run("v", 10)
        + _run("s", 16)  # 480 ms pause, below the 750 ms threshold
        + _run("v", 10)
        + _run("s", 25)
    )

    events = await _collect(machine, frames)

    assert [event.type for event in events] == [SPEECH_STARTED, SPEECH_ENDED]
    ended = events[1]
    assert ended.frame_index == 63
    assert ended.duration_ms == 61 * 30  # 10 + 16 + 10 + 25 frames
    assert ended.audio == b"".join(frames[3:])


@pytest.mark.asyncio
async def test_d_800ms_pause_splits_into_two_utterances():
    # Catches: 750 ms threshold not respected
    machine = VADStateMachine(ContentScorer())
    frames = (
        _run("s", 3)
        + _run("v", 10)
        + _run("s", 26)  # 780 ms pause, above the threshold
        + _run("v", 10)
        + _run("s", 25)
    )

    events = await _collect(machine, frames)

    assert [event.type for event in events] == [
        SPEECH_STARTED,
        SPEECH_ENDED,
        SPEECH_STARTED,
        SPEECH_ENDED,
    ]
    assert [event.frame_index for event in events] == [3, 37, 39, 73]
    assert len(events[1].audio) == 35 * FRAME_BYTES  # 10 speech + 25 silence
    assert len(events[3].audio) == 35 * FRAME_BYTES


@pytest.mark.asyncio
async def test_e_buffer_cap_forces_end_of_runaway_speech():
    # Catches: buffer growing unbounded on runaway speech
    machine = VADStateMachine(ContentScorer())
    frames = _run("v", 2001)

    events = await _collect(machine, frames)

    assert [event.type for event in events] == [
        SPEECH_STARTED,
        SPEECH_ENDED,
        SPEECH_STARTED,
    ]
    forced_end = events[1]
    assert forced_end.frame_index == 1999  # cap reached on the 2000th frame
    assert forced_end.duration_ms == 2000 * 30  # 60 s
    assert len(forced_end.audio) == 2000 * FRAME_BYTES
    assert events[2].frame_index == 2000  # speech resumes after the forced end
    assert machine.buffered_frames == 1


@pytest.mark.asyncio
async def test_f_wrong_frame_size_raises_value_error():
    # Catches: silent acceptance of malformed input
    scorer = ContentScorer()
    machine = VADStateMachine(scorer)

    with pytest.raises(ValueError, match="960"):
        await machine.process_frame(b"\x00" * 959)
    with pytest.raises(ValueError, match="960"):
        await machine.process_frame(b"\x00" * 961)
    with pytest.raises(ValueError, match="960"):
        await machine.process_frame(b"")

    # Rejected input must not have been scored or buffered.
    assert scorer.calls == 0
    assert machine.state == "SILENT"
    assert machine.buffered_frames == 0
