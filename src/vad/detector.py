"""
Silero VAD inference wrapper.

Loads a Silero VAD ONNX model with onnxruntime and scores 30 ms PCM frames as
speech probability. Supports both common Silero ONNX interfaces:

  * v4 / v3: inputs ``input``, ``sr``, ``h``, ``c``; outputs ``output``,
    ``hn``, ``cn`` (LSTM state).
  * v5:      inputs ``input``, ``state``, ``sr``; outputs ``output``,
    ``stateN`` (recurrent state).

Frames are 960 bytes (480 samples at 16 kHz). Silero's native analysis window
is 512 samples, so the detector keeps a rolling sample buffer and scores the
most recent window on every call, yielding exactly one probability per 30 ms
frame. Inference runs synchronously in the caller's context: Silero is fast
enough for real-time use on one core, no threads/executors are needed.
"""

import logging
from pathlib import Path

import numpy as np
import onnxruntime as ort

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FRAME_BYTES = 960
DEFAULT_WINDOW_SAMPLES = 512

# Maps a state input name to the output that carries its next value.
_STATE_OUTPUTS = {"state": "stateN", "h": "hn", "c": "cn"}
_STATE_INPUTS = tuple(_STATE_OUTPUTS)


class VADModelError(Exception):
    """The Silero VAD model is missing, unreadable, or returned invalid output."""


def model_available(model_path: str | Path) -> bool:
    """Return True if the model file exists and can be opened."""
    path = Path(model_path)
    return path.is_file() and path.stat().st_size > 0


class SileroVAD:
    """
    Real Silero VAD scorer.

    Call score_frame() with one 960-byte S16_LE mono 16 kHz frame; it returns
    that frame's speech probability in [0.0, 1.0]. Call reset() between
    independent audio streams to clear the rolling buffer and recurrent state.
    """

    def __init__(
        self,
        model_path: str | Path,
        sample_rate: int = SAMPLE_RATE,
        window_samples: int | None = None,
        num_threads: int = 1,
    ) -> None:
        self.model_path = Path(model_path)
        self.sample_rate = sample_rate

        if not self.model_path.is_file():
            raise VADModelError(
                f"Silero VAD model not found: {self.model_path}. "
                "Place silero_vad.onnx there (this system is air-gapped)."
            )

        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = max(1, num_threads)
        session_options.inter_op_num_threads = 1
        session_options.log_severity_level = 3  # errors only

        try:
            self._session = ort.InferenceSession(
                str(self.model_path),
                sess_options=session_options,
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # onnxruntime raises several exception types
            raise VADModelError(
                f"Could not load Silero VAD model {self.model_path}: {exc}"
            ) from exc

        self._inputs = {spec.name: spec for spec in self._session.get_inputs()}
        self._output_names = [spec.name for spec in self._session.get_outputs()]

        if "input" not in self._inputs:
            raise VADModelError(
                f"Unexpected Silero model inputs {sorted(self._inputs)}: no 'input' tensor"
            )
        if "output" not in self._output_names:
            raise VADModelError(
                f"Unexpected Silero model outputs {self._output_names}: no 'output' tensor"
            )

        self.window_samples = window_samples or self._declared_window_samples()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._states: dict[str, np.ndarray] = {}
        self._init_states()

    def _declared_window_samples(self) -> int:
        """Read the model's required 16 kHz window size from its input shape."""
        shape = self._inputs["input"].shape
        if len(shape) >= 2 and isinstance(shape[-1], int) and shape[-1] > 0:
            return shape[-1]
        return DEFAULT_WINDOW_SAMPLES

    def _init_states(self) -> None:
        """Allocate recurrent state tensors from the model's declared shapes."""
        self._states = {}
        for name in _STATE_INPUTS:
            spec = self._inputs.get(name)
            if spec is None:
                continue
            dims = [dim if isinstance(dim, int) and dim > 0 else 1 for dim in spec.shape]
            self._states[name] = np.zeros(dims, dtype=np.float32)
        if self._states:
            logger.debug(
                "Silero recurrent state: %s",
                {name: value.shape for name, value in self._states.items()},
            )

    def reset(self) -> None:
        """Clear the rolling sample buffer and recurrent state."""
        self._buffer = np.zeros(0, dtype=np.float32)
        self._init_states()

    def score_frame(self, frame: bytes) -> float:
        """
        Score one PCM frame.

        Args:
            frame: 960 bytes (480 samples) of S16_LE mono 16 kHz audio.

        Returns:
            Speech probability in [0.0, 1.0].

        Raises:
            ValueError: If the frame is empty or has an odd byte count.
            VADModelError: If inference fails or returns a non-finite value.
        """
        if not frame or len(frame) % 2:
            raise ValueError(
                f"frame must be a non-empty even byte count, got {len(frame)}"
            )

        samples = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
        self._buffer = np.concatenate((self._buffer, samples))
        if self._buffer.size > self.window_samples:
            self._buffer = self._buffer[-self.window_samples :]

        if self._buffer.size < self.window_samples:
            chunk = np.zeros(self.window_samples, dtype=np.float32)
            chunk[-self._buffer.size :] = self._buffer
        else:
            chunk = self._buffer

        feed: dict[str, np.ndarray] = {"input": chunk.reshape(1, -1)}
        if "sr" in self._inputs:
            feed["sr"] = np.array(self.sample_rate, dtype=np.int64)
        feed.update(self._states)

        try:
            outputs = self._session.run(self._output_names, feed)
        except Exception as exc:
            raise VADModelError(f"Silero inference failed: {exc}") from exc

        result = dict(zip(self._output_names, outputs))
        probability = float(np.asarray(result["output"]).reshape(-1)[0])
        if not np.isfinite(probability):
            raise VADModelError(f"Silero returned non-finite probability: {probability}")

        for input_name, state in self._states.items():
            next_name = _STATE_OUTPUTS[input_name]
            if next_name in result:
                self._states[input_name] = np.asarray(
                    result[next_name], dtype=np.float32
                )

        return min(1.0, max(0.0, probability))


__all__ = [
    "DEFAULT_WINDOW_SAMPLES",
    "FRAME_BYTES",
    "SAMPLE_RATE",
    "SileroVAD",
    "VADModelError",
    "model_available",
]
