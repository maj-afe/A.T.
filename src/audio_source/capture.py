"""
arecord subprocess management and fixed-size PCM framing for audio-source.

arecord writes raw S16_LE mono PCM to stdout. Reads arrive in arbitrary sizes
(pipe buffering, ALSA period boundaries), so FrameAssembler re-aligns them into
exact 960-byte (30 ms @ 16 kHz mono 16-bit) frames. Partial frames are never
emitted, including when the stream shuts down mid-frame.
"""

import asyncio
from collections.abc import AsyncIterator
import contextlib
import logging

logger = logging.getLogger(__name__)

# 30 ms of 16 kHz mono 16-bit PCM = 480 samples * 2 bytes = 960 bytes.
SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE * CHANNELS * 2 * FRAME_MS // 1000

DEFAULT_DEVICE = "default"
READ_CHUNK = 4096
TERMINATE_TIMEOUT = 2.0


class CaptureError(Exception):
    """The arecord subprocess could not be started or managed."""


def build_command(
    device: str = DEFAULT_DEVICE,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
) -> list[str]:
    """Return the arecord argv used to capture raw PCM on stdout."""
    return [
        "arecord",
        "-D",
        device,
        "-f",
        "S16_LE",
        "-r",
        str(sample_rate),
        "-c",
        str(channels),
        "-t",
        "raw",
        "-q",
    ]


class FrameAssembler:
    """
    Re-align arbitrary byte chunks into fixed-size frames.

    Bytes are buffered until a whole frame is available; only whole frames are
    ever produced. A trailing partial frame is retained until discard_partial()
    is called (at stream end), so it is never broadcast.
    """

    def __init__(self, frame_bytes: int = FRAME_BYTES) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    @property
    def frame_bytes(self) -> int:
        """Size of a complete frame in bytes."""
        return self._frame_bytes

    @property
    def buffered_bytes(self) -> int:
        """Bytes currently held that do not yet form a complete frame."""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> list[bytes]:
        """Append a chunk and return every complete frame it completed."""
        if chunk:
            self._buffer.extend(chunk)

        frames: list[bytes] = []
        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]
        return frames

    def discard_partial(self) -> int:
        """Drop any buffered incomplete frame; return how many bytes were dropped."""
        discarded = len(self._buffer)
        self._buffer.clear()
        return discarded


class AudioCapture:
    """
    Own an arecord subprocess and expose its output as whole PCM frames.

    The subprocess is only started by start(); frames() yields complete
    960-byte frames until stdout reaches EOF. stop() terminates arecord with
    SIGTERM, escalating to SIGKILL after TERMINATE_TIMEOUT.
    """

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        frame_bytes: int = FRAME_BYTES,
        read_chunk: int = READ_CHUNK,
        terminate_timeout: float = TERMINATE_TIMEOUT,
    ) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_bytes = frame_bytes
        self.read_chunk = read_chunk
        self.terminate_timeout = terminate_timeout

        self._process: asyncio.subprocess.Process | None = None
        self._stdout: asyncio.StreamReader | None = None
        self._stderr_task: asyncio.Task | None = None
        self._last_stderr: str = ""
        self._stop_lock = asyncio.Lock()

    @property
    def pid(self) -> int | None:
        """PID of the running arecord process, or None."""
        return self._process.pid if self._process is not None else None

    @property
    def is_running(self) -> bool:
        """True while an arecord process is still alive."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """
        Spawn arecord with stdout piped.

        Raises:
            CaptureError: If arecord cannot be executed (missing, busy, etc.).
        """
        if self.is_running:
            return

        command = build_command(self.device, self.sample_rate, self.channels)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CaptureError(f"arecord not found: {exc}") from exc
        except OSError as exc:
            raise CaptureError(f"could not start arecord: {exc}") from exc

        self._stdout = self._process.stdout
        self._last_stderr = ""
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        logger.info("arecord started (pid %s, device %s)", self._process.pid, self.device)

    async def frames(self) -> AsyncIterator[bytes]:
        """
        Yield complete PCM frames until arecord's stdout reaches EOF.

        Chunks are read in read_chunk-sized blocks and re-aligned by
        FrameAssembler, so every yielded frame is exactly frame_bytes long.
        """
        stdout = self._stdout
        if stdout is None:
            raise CaptureError("capture is not running")

        assembler = FrameAssembler(self.frame_bytes)
        while True:
            chunk = await stdout.read(self.read_chunk)
            if not chunk:
                discarded = assembler.discard_partial()
                if discarded:
                    logger.debug(
                        "Discarded %d trailing partial frame byte(s) at stream end",
                        discarded,
                    )
                return
            for frame in assembler.feed(chunk):
                yield frame

    async def stop(self) -> None:
        """
        Terminate arecord cleanly: SIGTERM, then SIGKILL after terminate_timeout.

        Safe to call multiple times and concurrently; the first call does the
        work and later calls return once it has finished.
        """
        async with self._stop_lock:
            process = self._process
            self._process = None
            self._stdout = None

            if process is not None and process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass

                try:
                    await asyncio.wait_for(process.wait(), timeout=self.terminate_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "arecord did not exit within %.1f s of SIGTERM; killing it",
                        self.terminate_timeout,
                    )
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

            if self._stderr_task is not None:
                self._stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._stderr_task
                self._stderr_task = None

    def describe_exit(self) -> str:
        """Human-readable reason for the current/last arecord stream ending."""
        process = self._process
        if process is None:
            return "not running"
        if process.returncode is None:
            return "stream ended"
        if process.returncode < 0:
            detail = f"killed by signal {-process.returncode}"
        else:
            detail = f"exit code {process.returncode}"
        if self._last_stderr:
            detail = f"{detail}: {self._last_stderr}"
        return detail

    async def _drain_stderr(self) -> None:
        """Drain arecord's stderr so it can never block on a full pipe."""
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    self._last_stderr = text
                    logger.debug("arecord: %s", text)
        except (asyncio.CancelledError, OSError):
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Error draining arecord stderr: %s", exc)


__all__ = [
    "AudioCapture",
    "CaptureError",
    "DEFAULT_DEVICE",
    "FRAME_BYTES",
    "FRAME_MS",
    "FrameAssembler",
    "build_command",
]
