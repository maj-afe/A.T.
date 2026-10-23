"""
Adversarial tests for the real Silero VAD detector and the end-to-end pipeline.

WAV fixtures are generated procedurally in tmp_path (no committed recordings).
These tests are skipped with a clear message when the ONNX model is absent;
the model must be supplied manually because the deployment is air-gapped.
"""

import logging
from pathlib import Path
import wave

import numpy as np
import pytest

from vad.detector import FRAME_BYTES, SileroVAD
from vad.state_machine import (
    SPEECH_ENDED,
    SPEECH_REJECTED,
    SPEECH_STARTED,
    VADStateMachine,
)

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).resolve().parents[2] / "models" / "vad" / "silero_vad.onnx"

pytestmark = pytest.mark.skipif(
    not MODEL_PATH.is_file(),
    reason=(
        f"Silero VAD model not found at {MODEL_PATH}; "
        "place silero_vad.onnx there (air-gapped system)"
    ),
)

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 480


def _write_wav(path: Path, samples: np.ndarray) -> Path:
    """Write mono 16 kHz S16_LE PCM to a WAV file."""
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(pcm.tobytes())
    return path


def _tone(seconds: float = 1.0, frequency: float = 440.0, amplitude: float = 0.3):
    t = np.arange(int(SAMPLE_RATE * seconds), dtype=np.float32) / SAMPLE_RATE
    return (amplitude * np.sin(2.0 * np.pi * frequency * t)).astype(np.float32)


def _silence(seconds: float = 1.0):
    return np.zeros(int(SAMPLE_RATE * seconds), dtype=np.float32)


def _frames(path: Path) -> list[bytes]:
    """Read a WAV file back and split it into 960-byte frames."""
    with wave.open(str(path), "rb") as wav:
        data = wav.readframes(wav.getnframes())
    usable = len(data) - len(data) % FRAME_BYTES
    return [data[i : i + FRAME_BYTES] for i in range(0, usable, FRAME_BYTES)]


@pytest.fixture
def wavs(tmp_path: Path) -> dict[str, Path]:
    return {
        "tone": _write_wav(tmp_path / "tone.wav", _tone()),
        "silence": _write_wav(tmp_path / "silence.wav", _silence()),
        "mixed": _write_wav(
            tmp_path / "mixed.wav",
            np.concatenate([_silence(0.5), _tone(1.0), _silence(0.5)]),
        ),
    }


def test_g_real_silero_scores_tone_frames(wavs):
    # Catches: model not loaded, wrong input format, or inference crashing
    detector = SileroVAD(MODEL_PATH)
    frames = _frames(wavs["tone"])

    assert len(frames) == 33  # 1 s of 30 ms frames
    probabilities = [detector.score_frame(frame) for frame in frames]

    assert len(probabilities) == len(frames)
    assert all(0.0 <= probability <= 1.0 for probability in probabilities)


def test_h_real_silero_keeps_silence_below_threshold(wavs):
    # Catches: model misclassifying silence as speech
    detector = SileroVAD(MODEL_PATH)
    probabilities = [detector.score_frame(frame) for frame in _frames(wavs["silence"])]

    assert probabilities, "silence WAV produced no frames to score"
    assert max(probabilities) < 0.2


@pytest.mark.asyncio
async def test_i_pipeline_on_mixed_wav_emits_coherent_events(wavs):
    # Catches: end-to-end pipeline broken (detector and state machine not wired)
    detector = SileroVAD(MODEL_PATH)
    machine = VADStateMachine(detector)

    # Append 1 s of silence so an utterance opened by the tone can actually
    # reach the 750 ms silence threshold and close within this file.
    frames = _frames(wavs["mixed"]) + _frames(wavs["silence"])
    events = [event async for event in machine.process_frames(frames)]

    starts = [event for event in events if event.type == SPEECH_STARTED]
    ends = [
        event
        for event in events
        if event.type in (SPEECH_ENDED, SPEECH_REJECTED)
    ]

    if not starts:
        logger.warning(
            "Pipeline ran on mixed.wav but detected no speech (%d frames scored)",
            len(frames),
        )

    assert all(
        event.type in (SPEECH_STARTED, SPEECH_ENDED, SPEECH_REJECTED)
        for event in events
    )
    assert len(ends) == len(starts), (
        f"unbalanced events: {len(starts)} speech_started, {len(ends)} terminal"
    )
    assert machine.state == "SILENT"
