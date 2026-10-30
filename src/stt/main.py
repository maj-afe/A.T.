"""
Speech-to-Text service entry point for the AT voice loop.

Listens on /run/at/stt.sock using at-ipc, receives PCM utterance audio via
transcribe_pcm commands from Loop Controller, runs whisper-cli transcription,
and returns the text and timing metrics.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from pathlib import Path
import signal
from typing import Any

from at_config import Config
from at_ipc.envelope import Envelope
from at_ipc.server import IPCServer

from .transcriber import STTError, TranscriptionResult, WhisperTranscriber

logger = logging.getLogger(__name__)

MODULE_NAME = "stt"
DEFAULT_SOCKET_PATH = Path("/run/at/stt.sock")
TRANSCRIBE_PCM_COMMAND = "transcribe_pcm"


class STTService:
    """
    STT service managing IPC connection and whisper transcription requests.
    """

    def __init__(
        self,
        config: Config | None = None,
        transcriber: WhisperTranscriber | None = None,
        socket_path: Path | str | None = None,
        model_path: Path | str | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.install_signal_handlers = install_signal_handlers

        if config is None:
            # Attempt to load standard system config or project config
            candidates = [
                ("/etc/at/at.toml", "/etc/at/modules.toml"),
                (
                    str(Path(__file__).parents[2] / "config" / "at.toml"),
                    str(Path(__file__).parents[2] / "config" / "modules.toml"),
                ),
            ]
            for at_p, mod_p in candidates:
                if Path(at_p).is_file() and Path(mod_p).is_file():
                    try:
                        config = Config.load(at_p, mod_p)
                        break
                    except Exception as exc:
                        logger.debug("Could not load config from %s, %s: %s", at_p, mod_p, exc)

        if config is not None:
            cfg_model = config.get("stt", "model_path")
            threads = config.get_int("stt", "threads")
            beam = config.get_int("stt", "beam")
            language = config.get("stt", "language")
        else:
            cfg_model = "/home/aniruddh_sen/Documents/Kate/at/models/stt/ggml-base.en-q5_1.bin"
            threads = 4
            beam = 1
            language = "en"

        effective_model_path = model_path if model_path is not None else cfg_model

        if transcriber is not None:
            self.transcriber = transcriber
        else:
            self.transcriber = WhisperTranscriber(
                model_path=effective_model_path,
                threads=threads,
                beam=beam,
                language=language,
            )

        self.socket_path = (
            Path(socket_path) if socket_path is not None else DEFAULT_SOCKET_PATH
        )
        self._server = IPCServer(
            MODULE_NAME,
            socket_path=self.socket_path,
            install_signal_handlers=False,
        )
        self._server.on(TRANSCRIBE_PCM_COMMAND, self._handle_transcribe_pcm)

        self._stop_event = asyncio.Event()
        self._installed_signals: list[signal.Signals] = []

    def request_stop(self) -> None:
        """Signal the service to gracefully shut down."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        logger.info("Shutdown requested")

    async def stop(self) -> None:
        """Stop the service and its IPC server."""
        self.request_stop()
        if self.install_signal_handlers:
            self._remove_signal_handlers()
        await self._server.stop()

    async def start(self) -> None:
        """Start the STT IPC server."""
        if self.install_signal_handlers:
            self._install_signal_handlers()
        await self._server.start()

    async def run(self) -> int:
        """
        Run the STT service until SIGTERM or request_stop().

        Returns:
            Process exit code (0 on clean shutdown).
        """
        await self.start()
        logger.info("stt service listening on %s", self.socket_path)

        try:
            await self._stop_event.wait()
        finally:
            await self.stop()

        logger.info("stt service stopped cleanly")
        return 0

    async def _handle_transcribe_pcm(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> dict[str, Any]:
        """
        Handle transcribe_pcm command from Loop Controller.

        Receives raw PCM or WAV bytes as raw_data, runs whisper transcription,
        and returns text with timing metrics.
        """
        pcm_bytes = raw_data if raw_data is not None else b""
        result: TranscriptionResult = await self.transcriber.transcribe(pcm_bytes)

        text = result.text.strip() if result.text else ""
        is_empty = not text

        metrics = {
            "duration_ms": result.duration_ms,
            "encode_ms": result.encode_ms,
            "decode_ms": result.decode_ms,
            "total_ms": result.total_ms,
            "rtf": result.rtf,
        }

        logger.info(
            "Transcription completed: duration_ms=%s encode_ms=%s decode_ms=%s total_ms=%s rtf=%s text_len=%d empty=%s",
            result.duration_ms,
            result.encode_ms,
            result.decode_ms,
            result.total_ms,
            result.rtf,
            len(text),
            is_empty,
        )

        if is_empty:
            return {
                "text": "",
                "empty": True,
                "metrics": metrics,
            }

        return {
            "text": text,
            "metrics": metrics,
        }

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
    """Configure service logging for CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI options for the STT service."""
    parser = argparse.ArgumentParser(
        description="AT STT: Speech-to-Text service via whisper.cpp"
    )
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Unix socket path (default: /run/at/stt.sock)",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=None,
        help="Path to whisper ggml model file (overrides config)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    service = STTService(
        socket_path=args.socket_path,
        model_path=args.model_path,
    )
    return await service.run()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    configure_logging()
    return asyncio.run(_run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
