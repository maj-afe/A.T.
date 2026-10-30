"""
Speech-to-Text (STT) module for the AT voice assistant.

Provides whisper.cpp transcription via WhisperTranscriber and the STTService
daemon listening on stt.sock.
"""

from .main import STTService
from .transcriber import STTError, TranscriptionResult, WhisperTranscriber

__all__ = [
    "STTError",
    "STTService",
    "TranscriptionResult",
    "WhisperTranscriber",
]
