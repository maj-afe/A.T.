"""
Whisper-based Speech-to-Text transcriber for the AT voice assistant.

Invokes whisper-cli subprocess on PCM audio converted to temporary WAV format,
extracting transcription text and timing metrics.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import io
import logging
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import tempfile
from typing import Any
import wave

logger = logging.getLogger(__name__)


class STTError(Exception):
    """Raised when transcription fails (subprocess error, timeout, missing files)."""
    pass


@dataclass
class TranscriptionResult:
    """Transcription text and performance metrics."""
    text: str
    duration_ms: float | int
    encode_ms: float | int
    decode_ms: float | int
    total_ms: float | int
    rtf: float

    def to_dict(self) -> dict[str, Any]:
        """Convert result to response payload dictionary format."""
        return {
            "text": self.text,
            "metrics": {
                "duration_ms": self.duration_ms,
                "encode_ms": self.encode_ms,
                "decode_ms": self.decode_ms,
                "total_ms": self.total_ms,
                "rtf": self.rtf,
            },
        }


class WhisperTranscriber:
    """
    Transcribes audio utterances via whisper-cli subprocess.
    """

    def __init__(
        self,
        model_path: str | Path,
        threads: int = 4,
        beam: int = 1,
        language: str = "en",
        timeout: float = 15.0,
        whisper_bin: str = "whisper-cli",
    ) -> None:
        self.model_path = Path(model_path)
        self.threads = threads
        self.beam = beam
        self.language = language
        self.timeout = timeout
        self.whisper_bin = whisper_bin

    @classmethod
    def wrap_wav(cls, pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
        """
        Wrap raw 16-bit mono PCM bytes in a proper WAV header (RIFF, 16kHz mono 16-bit).
        """
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        return buf.getvalue()

    @staticmethod
    def _extract_timing(pattern: str, text: str) -> float | int:
        match = re.search(pattern, text)
        if not match:
            return 0
        val = float(match.group(1))
        return int(val) if val.is_integer() else round(val, 2)

    async def transcribe(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> TranscriptionResult:
        """
        Transcribe PCM or WAV audio bytes.

        Args:
            pcm_bytes: Raw 16-bit PCM bytes or complete WAV audio data.
            sample_rate: Sample rate in Hz (default 16000).

        Returns:
            TranscriptionResult containing text and timing metrics.

        Raises:
            STTError: If model is missing, whisper-cli missing, process fails, or times out.
        """
        if not self.model_path.is_file():
            raise STTError(f"Model file not found: {self.model_path}")

        if not shutil.which(self.whisper_bin) and not Path(self.whisper_bin).is_file():
            raise STTError(f"whisper-cli not found: {self.whisper_bin}")

        if pcm_bytes.startswith(b"RIFF"):
            try:
                with wave.open(io.BytesIO(pcm_bytes), "rb") as wf:
                    if (
                        wf.getnchannels() != 1
                        or wf.getframerate() != sample_rate
                        or wf.getsampwidth() != 2
                    ):
                        raise STTError(
                            f"Invalid WAV format: channels={wf.getnchannels()}, "
                            f"rate={wf.getframerate()}, width={wf.getsampwidth()}. "
                            f"Expected mono {sample_rate}Hz 16-bit."
                        )
                    nframes = wf.getnframes()
                    duration_ms = (nframes / sample_rate) * 1000.0
                wav_data = pcm_bytes
            except (wave.Error, EOFError, struct.error) as exc:
                raise STTError(f"Malformed WAV data: {exc}") from exc
        else:
            if sample_rate != 16000:
                raise STTError(f"Unsupported sample rate: {sample_rate} (expected 16000)")
            if len(pcm_bytes) % 2 != 0:
                raise STTError(
                    f"Invalid PCM length: {len(pcm_bytes)} bytes is not a multiple of 2"
                )
            wav_data = self.wrap_wav(pcm_bytes, sample_rate=sample_rate)
            duration_ms = (len(pcm_bytes) / (sample_rate * 2)) * 1000.0

        temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temp_path = temp_file.name
        try:
            temp_file.write(wav_data)
            temp_file.flush()
            temp_file.close()

            cmd = [
                self.whisper_bin,
                "-m",
                str(self.model_path),
                "-f",
                temp_path,
                "-t",
                str(self.threads),
                "-bs",
                str(self.beam),
                "-nt",
                "-l",
                self.language,
            ]

            try:
                proc = await asyncio.to_thread(
                    subprocess.run,
                    cmd,
                    capture_output=True,
                    timeout=self.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                raise STTError(f"whisper-cli timed out after {self.timeout}s") from exc
            except FileNotFoundError as exc:
                raise STTError(f"whisper-cli not found: {self.whisper_bin}") from exc
            except Exception as exc:
                if isinstance(exc, STTError):
                    raise
                raise STTError(f"Transcription failed: {exc}") from exc

            stderr_text = (
                proc.stderr.decode("utf-8", errors="replace")
                if isinstance(proc.stderr, (bytes, bytearray))
                else str(proc.stderr or "")
            )

            if proc.returncode != 0:
                raise STTError(
                    f"whisper-cli exited with code {proc.returncode}: {stderr_text.strip()}"
                )

            stdout_text = (
                proc.stdout.decode("utf-8", errors="replace")
                if isinstance(proc.stdout, (bytes, bytearray))
                else str(proc.stdout or "")
            )

            text = stdout_text.strip()

            encode_ms = self._extract_timing(r"encode time\s*=\s*([\d.]+)\s*ms", stderr_text)
            decode_ms = self._extract_timing(r"decode time\s*=\s*([\d.]+)\s*ms", stderr_text)
            total_ms = self._extract_timing(r"total time\s*=\s*([\d.]+)\s*ms", stderr_text)

            dur_val = int(duration_ms) if duration_ms.is_integer() else round(duration_ms, 2)
            rtf = round(total_ms / dur_val, 3) if dur_val > 0 else 0.0

            return TranscriptionResult(
                text=text,
                duration_ms=dur_val,
                encode_ms=encode_ms,
                decode_ms=decode_ms,
                total_ms=total_ms,
                rtf=rtf,
            )
        finally:
            if os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
