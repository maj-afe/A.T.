"""
audio-source — microphone gateway for the AT voice loop.

Owns the capture device via arecord (no Python ALSA dependency), frames the
stream into 30 ms / 960-byte PCM chunks, and broadcasts them over
/run/at/audio-source.sock with at-ipc.

The service entry point lives in audio_source.main (run with
`python -m audio_source.main`); this package exports the capture primitives.
"""

from .capture import (
    AudioCapture,
    CaptureError,
    DEFAULT_DEVICE,
    FRAME_BYTES,
    FRAME_MS,
    FrameAssembler,
)

__all__ = [
    "AudioCapture",
    "CaptureError",
    "DEFAULT_DEVICE",
    "FRAME_BYTES",
    "FRAME_MS",
    "FrameAssembler",
]
