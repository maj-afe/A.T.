"""TTS (Text-to-Speech) module for AT voice assistant."""
from .synthesizer import Synthesizer, TTSError
from .player import Player

__all__ = ["Synthesizer", "TTSError", "Player"]
