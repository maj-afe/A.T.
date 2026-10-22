"""
audio-source service entry point: microphone gateway for the AT voice loop.

Owns the ALSA capture device through arecord, frames the stream into 30 ms
(960-byte) PCM chunks, and broadcasts each frame to all subscribers over
/run/at/audio-source.sock using at-ipc. Frames are dropped for slow
subscribers (real-time audio); arecord is restarted on failure.
"""

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path
import signal

from at_ipc.server import IPCServer

from .capture import AudioCapture, CaptureError, DEFAULT_DEVICE

logger = logging.getLogger(__name__)

MODULE_NAME = "audio-source"
AUDIO_FRAME_EVENT = "audio_frame"
RETRY_DELAY = 2.0


class AudioSourceService:
    """
    Microphone gateway: reads continuously and fans frames out to subscribers.

    The capture loop keeps running even with no subscribers (frames are
    discarded by broadcast), so the device stays hot and the first subscriber
    receives audio immediately.
    """

    def __init__(
        self,
        socket_path: Path | None = None,
        device: str = DEFAULT_DEVICE,
        retry_delay: float = RETRY_DELAY,
        install_signal_handlers: bool = True,
    ) -> None:
        self.retry_delay = retry_delay
        self.install_signal_handlers = install_signal_handlers
        self._server = IPCServer(
            MODULE_NAME, socket_path=socket_path, install_signal_handlers=False
        )
        self._capture = AudioCapture(device=device)
        self._stop_event = asyncio.Event()
        self._capture_stop_task: asyncio.Task | None = None
        self._installed_signals: list[signal.Signals] = []

    @property
    def socket_path(self) -> Path:
        """Path of the Unix domain socket this service listens on."""
        return self._server.socket_path

    def request_stop(self) -> None:
        """
        Request a graceful shutdown.

        Safe to call from another task or from an event-loop signal handler.
        Terminating arecord unblocks the pending stdout read so the run loop
        can reach its cleanup path.
        """
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        logger.info("Shutdown requested")
        if self._capture.is_running and self._capture_stop_task is None:
            self._capture_stop_task = asyncio.create_task(self._capture.stop())

    async def run(self) -> int:
        """
        Run the service until request_stop()/SIGTERM.

        Returns:
            Process exit code (0 on graceful shutdown).
        """
        if self.install_signal_handlers:
            self._install_signal_handlers()

        await self._server.start()
        logger.info("audio-source listening on %s", self.socket_path)

        try:
            while not self._stop_event.is_set():
                try:
                    await self._capture.start()
                except CaptureError as exc:
                    logger.warning(
                        "Capture device unavailable: %s; retrying in %.0f s",
                        exc,
                        self.retry_delay,
                    )
                    if await self._sleep_or_stop(self.retry_delay):
                        break
                    continue

                await self._stream()

                reason = self._capture.describe_exit()
                await self._capture.stop()

                if self._stop_event.is_set():
                    break

                logger.warning(
                    "arecord stopped (%s); restarting in %.0f s",
                    reason,
                    self.retry_delay,
                )
                if await self._sleep_or_stop(self.retry_delay):
                    break
        finally:
            await self._capture.stop()
            if self._capture_stop_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._capture_stop_task
                self._capture_stop_task = None
            if self.install_signal_handlers:
                self._remove_signal_handlers()
            await self._server.stop()

        logger.info("audio-source stopped")
        return 0

    async def _stream(self) -> None:
        """Broadcast complete frames until the capture stream ends or shutdown."""
        try:
            async for frame in self._capture.frames():
                if self._stop_event.is_set():
                    return
                await self._server.broadcast(
                    AUDIO_FRAME_EVENT,
                    raw_data=frame,
                    drop_on_backpressure=True,
                )
        except OSError as exc:
            logger.warning("Capture stream read failed: %s", exc)

    async def _sleep_or_stop(self, delay: float) -> bool:
        """Sleep for delay; return True early if shutdown was requested."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            return True
        except asyncio.TimeoutError:
            return False

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                self._installed_signals.append(sig)
            except (NotImplementedError, RuntimeError) as exc:
                logger.debug("Could not install handler for %s: %s", sig, exc)

    def _remove_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in self._installed_signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        self._installed_signals.clear()


def configure_logging() -> None:
    """Configure service logging for the CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the audio-source service."""
    parser = argparse.ArgumentParser(
        description="AT audio-source: microphone gateway (16 kHz mono PCM frames)"
    )
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Unix socket path (default: /run/at/audio-source.sock)",
    )
    parser.add_argument(
        "--device",
        default=DEFAULT_DEVICE,
        help=f"ALSA capture device (default: {DEFAULT_DEVICE})",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    service = AudioSourceService(socket_path=args.socket_path, device=args.device)
    return await service.run()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    configure_logging()
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
