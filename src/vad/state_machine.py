"""
Two-state VAD state machine: SILENT and SPEAKING.

Consumes 960-byte (30 ms) PCM frames, scores each with a Silero VAD scorer, and
emits ``speech_started`` / ``speech_ended`` / ``speech_rejected`` events. One
utterance is buffered in memory (never on disk) and capped at 60 s
(1920 frames, per spec/loop force-end rule).
"""

from collections.abc import AsyncIterable, AsyncIterator, Iterable
from dataclasses import dataclass
import logging
import math
from typing import Any, Protocol, Union

logger = logging.getLogger(__name__)

FRAME_BYTES = 960
FRAME_MS = 30
MAX_UTTERANCE_FRAMES = 1920

SILENT = "SILENT"
SPEAKING = "SPEAKING"

SPEECH_STARTED = "speech_started"
SPEECH_ENDED = "speech_ended"
SPEECH_REJECTED = "speech_rejected"

DEFAULT_THRESHOLD = 0.5
DEFAULT_SILENCE_MS = 750
DEFAULT_MIN_SPEECH_MS = 250


class VADScorer(Protocol):
    """Anything that can score a PCM frame and reset its own state."""

    def score_frame(self, frame: bytes) -> float:
        """Return the speech probability in [0.0, 1.0] for one frame."""


@dataclass(frozen=True)
class VADEvent:
    """One observable VAD state-machine event."""

    type: str
    frame_index: int
    probability: float
    duration_ms: int = 0
    voiced_ms: int = 0
    audio: bytes | None = None


class VADStateMachine:
    """
    SILENT/SPEAKING state machine over 30 ms frames.

    SILENT: score frames until one crosses the threshold, then emit
    ``speech_started`` and begin buffering. SPEAKING: buffer frames and track
    continuous silence; 750 ms of silence ends the utterance. Utterances with
    less than 250 ms of voiced frames are rejected. 500 ms pauses do not split
    an utterance; 750 ms pauses do.
    """

    def __init__(
        self,
        scorer: VADScorer,
        *,
        threshold: float = DEFAULT_THRESHOLD,
        silence_ms: int = DEFAULT_SILENCE_MS,
        min_speech_ms: int = DEFAULT_MIN_SPEECH_MS,
        frame_ms: int = FRAME_MS,
        max_utterance_frames: int = MAX_UTTERANCE_FRAMES,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold must be within [0, 1], got {threshold}")
        if silence_ms <= 0:
            raise ValueError(f"silence_ms must be positive, got {silence_ms}")
        if min_speech_ms < 0:
            raise ValueError(f"min_speech_ms cannot be negative, got {min_speech_ms}")
        if frame_ms <= 0:
            raise ValueError(f"frame_ms must be positive, got {frame_ms}")
        if max_utterance_frames <= 0:
            raise ValueError(
                f"max_utterance_frames must be positive, got {max_utterance_frames}"
            )

        self._scorer = scorer
        self.threshold = threshold
        self.silence_ms = silence_ms
        self.min_speech_ms = min_speech_ms
        self.frame_ms = frame_ms
        self.max_utterance_frames = max_utterance_frames

        self._silence_frames = max(1, math.ceil(silence_ms / frame_ms))
        self._min_voiced_frames = math.ceil(min_speech_ms / frame_ms)

        self._state = SILENT
        self._frame_index = 0
        self._buffer: list[bytes] = []
        self._buffer_bytes = 0
        self._voiced_frames = 0
        self._silence_run = 0

    @classmethod
    def from_config(cls, config: Any, scorer: VADScorer) -> "VADStateMachine":
        """
        Build a machine from an at_config.Config instance.

        Reads ``[vad] sensitivity``, ``silence_ms`` and ``min_speech_ms``.
        """
        return cls(
            scorer,
            threshold=float(config.get("vad", "sensitivity")),
            silence_ms=int(config.get("vad", "silence_ms")),
            min_speech_ms=int(config.get("vad", "min_speech_ms")),
        )

    @property
    def state(self) -> str:
        """Current state: SILENT or SPEAKING."""
        return self._state

    @property
    def buffered_frames(self) -> int:
        """Number of frames currently buffered for the open utterance."""
        return len(self._buffer)

    @property
    def buffered_bytes(self) -> int:
        """Number of PCM bytes currently buffered for the open utterance."""
        return self._buffer_bytes

    def reset(self) -> None:
        """Discard the open utterance, state, counters, and scorer state."""
        self._state = SILENT
        self._frame_index = 0
        self._buffer = []
        self._buffer_bytes = 0
        self._voiced_frames = 0
        self._silence_run = 0
        reset = getattr(self._scorer, "reset", None)
        if callable(reset):
            reset()

    async def process_frame(self, frame: bytes) -> list[VADEvent]:
        """
        Score one frame and return the events it produced (0, 1, or more).

        Raises:
            ValueError: If the frame is not exactly 960 bytes.
        """
        if len(frame) != FRAME_BYTES:
            raise ValueError(
                f"VAD expects {FRAME_BYTES}-byte frames, got {len(frame)}; "
                "reframing is the caller's responsibility"
            )
        probability = float(self._scorer.score_frame(frame))
        return self._handle(frame, probability)

    async def process_frames(
        self,
        frames: Union[Iterable[bytes], AsyncIterable[bytes]],
    ) -> AsyncIterator[VADEvent]:
        """Consume a sync or async iterable of frames and yield VAD events."""
        async for frame in _iterate(frames):
            for event in await self.process_frame(frame):
                yield event

    def _handle(self, frame: bytes, probability: float) -> list[VADEvent]:
        """Advance the state machine by one scored frame."""
        index = self._frame_index
        self._frame_index += 1

        if self._state == SILENT:
            if probability < self.threshold:
                return []
            self._state = SPEAKING
            self._buffer = [frame]
            self._buffer_bytes = len(frame)
            self._voiced_frames = 1
            self._silence_run = 0
            return [VADEvent(SPEECH_STARTED, index, probability)]

        # SPEAKING: every frame is buffered, including trailing silence.
        self._buffer.append(frame)
        self._buffer_bytes += len(frame)
        if probability >= self.threshold:
            self._voiced_frames += 1
            self._silence_run = 0
        else:
            self._silence_run += 1

        # Runaway speech: force-end at the cap instead of buffering unbounded.
        if len(self._buffer) >= self.max_utterance_frames:
            logger.info(
                "Utterance reached %d frames; forcing speech_ended",
                self.max_utterance_frames,
            )
            return [self._finish(index, probability, forced=True)]

        if self._silence_run >= self._silence_frames:
            return [self._finish(index, probability, forced=False)]
        return []

    def _finish(self, index: int, probability: float, forced: bool) -> VADEvent:
        """Emit the terminal event for the open utterance and reset."""
        audio = b"".join(self._buffer)
        duration_ms = len(self._buffer) * self.frame_ms
        voiced_ms = self._voiced_frames * self.frame_ms

        if not forced and voiced_ms < self.min_speech_ms:
            event = VADEvent(
                SPEECH_REJECTED,
                index,
                probability,
                duration_ms,
                voiced_ms,
                None,
            )
        else:
            event = VADEvent(
                SPEECH_ENDED,
                index,
                probability,
                duration_ms,
                voiced_ms,
                audio,
            )

        self._state = SILENT
        self._buffer = []
        self._buffer_bytes = 0
        self._voiced_frames = 0
        self._silence_run = 0
        return event


async def _iterate(
    frames: Union[Iterable[bytes], AsyncIterable[bytes]],
) -> AsyncIterator[bytes]:
    """Iterate frames from either a sync or an async iterable."""
    if hasattr(frames, "__aiter__"):
        async for frame in frames:  # type: ignore[union-attr]
            yield frame
    else:
        for frame in frames:
            yield frame


__all__ = [
    "FRAME_BYTES",
    "FRAME_MS",
    "MAX_UTTERANCE_FRAMES",
    "SILENT",
    "SPEAKING",
    "SPEECH_ENDED",
    "SPEECH_REJECTED",
    "SPEECH_STARTED",
    "VADEvent",
    "VADScorer",
    "VADStateMachine",
]
