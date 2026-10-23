"""
vad — Voice Activity Detection for the AT voice loop.

Wraps Silero VAD (ONNX) in a two-state machine that marks speech start/end,
buffers one utterance in memory, and rejects utterances shorter than 250 ms.
Pure inference core: no IPC, no subprocess, no audio persisted.
"""

from .detector import (
    DEFAULT_WINDOW_SAMPLES,
    SAMPLE_RATE,
    SileroVAD,
    VADModelError,
    model_available,
)
from .state_machine import (
    FRAME_BYTES,
    FRAME_MS,
    MAX_UTTERANCE_FRAMES,
    SILENT,
    SPEAKING,
    SPEECH_ENDED,
    SPEECH_REJECTED,
    SPEECH_STARTED,
    VADEvent,
    VADScorer,
    VADStateMachine,
)

__all__ = [
    "DEFAULT_WINDOW_SAMPLES",
    "FRAME_BYTES",
    "FRAME_MS",
    "MAX_UTTERANCE_FRAMES",
    "SAMPLE_RATE",
    "SILENT",
    "SPEAKING",
    "SPEECH_ENDED",
    "SPEECH_REJECTED",
    "SPEECH_STARTED",
    "SileroVAD",
    "VADEvent",
    "VADModelError",
    "VADScorer",
    "VADStateMachine",
    "model_available",
]
