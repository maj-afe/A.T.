"""
Loop module orchestrating the AT voice interaction pipeline.
"""

from __future__ import annotations

import sys
from types import ModuleType

from .boot_greeting import select_boot_greeting
from .main import LoopService, LoopState
from .sentence_chunker import chunk_sentences
from .shuffle_bag import ShuffleBag

# Provide loop.state_machine alias for LoopState import compatibility
_sm_module = ModuleType("loop.state_machine")
_sm_module.LoopState = LoopState
sys.modules["loop.state_machine"] = _sm_module

__all__ = [
    "LoopService",
    "LoopState",
    "ShuffleBag",
    "chunk_sentences",
    "select_boot_greeting",
]
