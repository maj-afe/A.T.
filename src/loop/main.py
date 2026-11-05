"""
Loop Service main entry point: orchestrates the AT conversation loop.

Coordinates interactions between wake-word, VAD, STT, LLM, and TTS services
via Unix domain socket IPC.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from enum import Enum
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

from at_config import Config
from at_ipc import IPCClient
from at_ipc.envelope import Envelope

from .boot_greeting import select_boot_greeting
from .sentence_chunker import chunk_sentences
from .shuffle_bag import ShuffleBag

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_DIR = Path("/etc/at")
FALLBACK_CONFIG_DIR = Path("config")
STARTUP_FLAG_FILE = Path("data/loop.startup.flag")


class LoopState(str, Enum):
    """Voice loop orchestrator states."""

    IDLE = "IDLE"
    ACKNOWLEDGING = "ACKNOWLEDGING"
    LISTENING = "LISTENING"
    TRANSCRIBING = "TRANSCRIBING"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    COOLDOWN = "COOLDOWN"


def resolve_config_dir(config_dir: Path | str | None = None) -> Path:
    """Resolve config directory with fallbacks."""
    candidates = []
    if config_dir is not None:
        candidates.append(Path(config_dir))
    candidates.append(DEFAULT_CONFIG_DIR)
    candidates.append(Path(__file__).parents[2] / "config")
    candidates.append(FALLBACK_CONFIG_DIR)

    for c in candidates:
        if (c / "at.toml").is_file() and (c / "modules.toml").is_file():
            return c
    return DEFAULT_CONFIG_DIR


def resolve_socket_path(
    socket_dir: Path, filename: str, custom_socket_dir: Path | None = None
) -> Path:
    """Resolve socket path, checking custom dir, socket_dir, and /tmp/at fallback."""
    if custom_socket_dir is not None:
        return custom_socket_dir / filename

    env_dir = os.environ.get("AT_SOCKET_DIR")
    if env_dir:
        return Path(env_dir) / filename

    p = socket_dir / filename
    if p.exists():
        return p

    tmp_p = Path("/tmp/at") / filename
    if tmp_p.exists():
        return tmp_p

    if not socket_dir.exists() and Path("/tmp/at").is_dir():
        return tmp_p

    return p


class LoopService:
    """AT Loop Controller orchestrating the voice interaction state machine."""

    def __init__(
        self,
        config: Config,
        socket_dir: Path | None = None,
        no_wake: bool = False,
        flag_file: Path | None = None,
    ) -> None:
        self.config = config
        self.no_wake = no_wake
        self.flag_file = flag_file or STARTUP_FLAG_FILE

        cfg_socket_dir = Path(config.get_str("paths", "socket_dir"))
        self.socket_dir = socket_dir or cfg_socket_dir

        self.state = LoopState.IDLE
        self._stop_event = asyncio.Event()
        self._turn_task: asyncio.Task | None = None

        # Playback tracking
        self._playback_finished_event = asyncio.Event()
        self._playback_error: str | None = None
        self._expected_playbacks = 0
        self._playbacks_received = 0

        # VAD speech tracking
        self._speech_started_event = asyncio.Event()
        self._speech_ended_event = asyncio.Event()
        self._last_speech_buffer: bytes | None = None

        # Cooldown duration in seconds
        try:
            self.cooldown_sec = config.get_int("loop", "cooldown") / 1000.0
        except Exception:
            self.cooldown_sec = 0.4

        # Acknowledgment shuffle bag
        self.ack_bag = self._init_ack_bag()

        # Empty response shuffle bag
        self.empty_bag = self._init_empty_bag()

        # Initialize clients
        self.tts_client = IPCClient(
            module_name="loop",
            target_module="tts",
            socket_path=resolve_socket_path(self.socket_dir, "tts.sock", socket_dir),
        )
        self.llm_client = IPCClient(
            module_name="loop",
            target_module="llm",
            socket_path=resolve_socket_path(self.socket_dir, "llm.sock", socket_dir),
        )
        self.stt_client = IPCClient(
            module_name="loop",
            target_module="stt",
            socket_path=resolve_socket_path(self.socket_dir, "stt.sock", socket_dir),
        )
        self.vad_client = IPCClient(
            module_name="loop",
            target_module="vad",
            socket_path=resolve_socket_path(self.socket_dir, "vad.sock", socket_dir),
        )
        self.audio_client = IPCClient(
            module_name="loop",
            target_module="audio-source",
            socket_path=resolve_socket_path(
                self.socket_dir, "audio-source.sock", socket_dir
            ),
        )
        self.wake_client: IPCClient | None = None
        if not self.no_wake:
            self.wake_client = IPCClient(
                module_name="loop",
                target_module="wake_word",
                socket_path=resolve_socket_path(
                    self.socket_dir, "wakeword.sock", socket_dir
                ),
            )

        self._installed_signals: list[signal.Signals] = []
        self._register_event_handlers()

    def _init_ack_bag(self) -> ShuffleBag:
        """Initialize ShuffleBag for wake acknowledgments from config."""
        lines_with_weights: list[tuple[str, float]] = []
        try:
            ack_section = self.config.get("loop", "acknowledgment_lines")
            if isinstance(ack_section, list):
                for item in ack_section:
                    if isinstance(item, dict) and "line" in item and "weight" in item:
                        lines_with_weights.append((item["line"], float(item["weight"])))
        except Exception as exc:
            logger.debug("Could not read acknowledgment_lines from config: %s", exc)

        if not lines_with_weights:
            lines_with_weights = [
                ("Sir?", 0.30),
                ("Yes, sir.", 0.10),
                ("Listening, sir.", 0.10),
                ("Go ahead, sir.", 0.10),
                ("At your service, sir.", 0.10),
                ("Ready, sir.", 0.10),
                ("Sir.", 0.10),
                ("On your command, sir.", 0.05),
            ]
        return ShuffleBag(lines_with_weights)

    def _init_empty_bag(self) -> ShuffleBag:
        """Initialize ShuffleBag for empty transcription lines from config."""
        lines: list[str] = []
        try:
            cfg_lines = self.config.get("loop", "empty_response_lines")
            if isinstance(cfg_lines, list):
                lines = [str(x) for x in cfg_lines if str(x).strip()]
        except Exception as exc:
            logger.debug("Could not read empty_response_lines from config: %s", exc)

        if not lines:
            lines = [
                "I didn't catch that, sir.",
                "Say again, sir.",
                "Nothing came through, sir.",
                "No audio, sir.",
            ]
        return ShuffleBag([(line, 1.0) for line in lines])

    def _register_event_handlers(self) -> None:
        """Subscribe to IPC events from connected services."""
        if self.wake_client is not None:
            self.wake_client.on("wake_detected", self._on_wake_detected)

        self.vad_client.on("speech_started", self._on_speech_started)
        self.vad_client.on("speech_ended", self._on_speech_ended)

        self.tts_client.on("playback_finished", self._on_playback_finished)
        self.tts_client.on("tts_error", self._on_tts_error)

    async def _on_wake_detected(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> None:
        """Handle wake word detection."""
        logger.info("Wake detected: %s", envelope.payload)
        if self.state == LoopState.IDLE and not self._stop_event.is_set():
            if self._turn_task is None or self._turn_task.done():
                self._turn_task = asyncio.create_task(self._run_turn())

    async def _on_speech_started(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> None:
        """Handle speech started notification from VAD."""
        logger.debug("VAD speech_started")
        self._speech_started_event.set()

    async def _on_speech_ended(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> None:
        """Handle speech ended notification from VAD."""
        logger.info("VAD speech_ended: raw_bytes=%d", len(raw_data) if raw_data else 0)
        self._last_speech_buffer = raw_data
        self._speech_ended_event.set()

    async def _on_playback_finished(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> None:
        """Handle playback finished event from TTS."""
        self._playbacks_received += 1
        logger.debug(
            "TTS playback_finished (%d/%d): %s",
            self._playbacks_received,
            self._expected_playbacks,
            envelope.payload,
        )
        if self._playbacks_received >= self._expected_playbacks:
            self._playback_finished_event.set()

    async def _on_tts_error(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> None:
        """Handle TTS error event."""
        error_msg = envelope.payload.get("error", "Unknown TTS error")
        logger.error("TTS error received: %s", error_msg)
        self._playback_error = error_msg
        self._playback_finished_event.set()

    async def _connect_client(
        self, client: IPCClient, timeout: float = 15.0, retry_interval: float = 0.5
    ) -> None:
        """Connect to an IPC client with retry until timeout."""
        deadline = time.monotonic() + timeout
        last_exc = None
        while time.monotonic() < deadline:
            try:
                await client.connect()
                return
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(retry_interval)
        if last_exc is not None:
            raise last_exc

    async def _connect_clients(self) -> None:
        """Connect to required and optional service sockets."""
        # TTS and LLM are required for turns
        await self._connect_client(self.tts_client, timeout=60.0)
        await self._connect_client(self.llm_client, timeout=300.0)

        # STT, VAD, Audio, Wake are connected if available
        for name, client in [
            ("stt", self.stt_client),
            ("vad", self.vad_client),
            ("audio", self.audio_client),
        ]:
            try:
                await self._connect_client(client, timeout=3.0)
                logger.info("Connected to %s", name)
            except Exception as exc:
                logger.warning(
                    "Could not connect to %s (%s): %s", name, client.socket_path, exc
                )

        if self.wake_client is not None:
            try:
                await self._connect_client(self.wake_client, timeout=2.0)
                logger.info("Connected to wake-word service")
            except Exception as exc:
                logger.warning("Could not connect to wake-word service: %s", exc)

    async def _close_clients(self) -> None:
        """Close all connected IPC clients."""
        clients = [
            self.tts_client,
            self.llm_client,
            self.stt_client,
            self.vad_client,
            self.audio_client,
        ]
        if self.wake_client is not None:
            clients.append(self.wake_client)

        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()

    async def _wait_for_playback(self, expected: int, timeout: float) -> bool:
        """Wait for expected number of playback_finished events."""
        self._expected_playbacks = expected
        self._playbacks_received = 0
        self._playback_error = None
        self._playback_finished_event.clear()

        try:
            await asyncio.wait_for(self._playback_finished_event.wait(), timeout=timeout)
            return self._playback_error is None
        except asyncio.TimeoutError:
            logger.warning("Timed out waiting for playback (timeout=%.1fs)", timeout)
            return False

    async def play_boot_greeting(self) -> None:
        """Check startup flag file and speak boot greeting."""
        unclean_shutdown = False
        try:
            if self.flag_file.exists():
                unclean_shutdown = True
                self.flag_file.unlink(missing_ok=True)
            self.flag_file.parent.mkdir(parents=True, exist_ok=True)
            self.flag_file.touch()
        except Exception as exc:
            logger.warning("Could not manage startup flag file %s: %s", self.flag_file, exc)

        greeting = select_boot_greeting(unclean_shutdown=unclean_shutdown)
        logger.info("Playing boot greeting: %r (unclean=%s)", greeting, unclean_shutdown)

        try:
            await self.tts_client.send_command(
                "speak_sentence", {"text": greeting}, timeout=60.0
            )
            await self._wait_for_playback(expected=1, timeout=60.0)
        except Exception as exc:
            logger.error("Failed to play boot greeting: %s", exc)

    async def run_smoke(self) -> int:
        """Run smoke test: connect to TTS, play boot greeting, exit."""
        logger.info("Running loop smoke test...")
        await self._connect_client(self.tts_client, timeout=30.0)
        try:
            await self.play_boot_greeting()
        finally:
            await self.tts_client.close()
            with contextlib.suppress(Exception):
                self.flag_file.unlink(missing_ok=True)
        logger.info("Smoke test completed")
        return 0

    async def run_demo_turn(self, user_text: str) -> int:
        """
        Run a single demo turn bypassing wake/vad/stt:
        LLM generate -> sentence chunking -> TTS playback.
        """
        logger.info("Running demo turn with input: %r", user_text)
        await self._connect_client(self.tts_client, timeout=60.0)
        await self._connect_client(self.llm_client, timeout=300.0)

        try:
            # THINKING
            self.state = LoopState.THINKING
            logger.info("State -> THINKING: generating response from LLM...")
            llm_resp = await self.llm_client.send_command(
                "generate",
                {"user_text": user_text},
                timeout=180.0,
            )
            raw_response_text = llm_resp.payload.get("text", "")
            logger.info("LLM responded: %r", raw_response_text)

            sentences = chunk_sentences(raw_response_text)
            if not sentences:
                logger.warning("No sentences returned from chunker for: %r", raw_response_text)
                return 0

            # SPEAKING
            self.state = LoopState.SPEAKING
            logger.info("State -> SPEAKING: sending %d sentences to TTS...", len(sentences))
            for s in sentences:
                await self.tts_client.send_command(
                    "speak_sentence",
                    {"text": s},
                    timeout=60.0,
                )

            # Wait for all sentences to finish playing
            success = await self._wait_for_playback(
                expected=len(sentences), timeout=300.0
            )
            if not success:
                logger.error("Demo turn TTS playback timed out or failed")
                return 1

            # COOLDOWN
            self.state = LoopState.COOLDOWN
            await asyncio.sleep(self.cooldown_sec)
            self.state = LoopState.IDLE
            logger.info("Demo turn completed successfully")
            return 0
        finally:
            await self._close_clients()
            with contextlib.suppress(Exception):
                self.flag_file.unlink(missing_ok=True)

    async def _run_turn(self) -> None:
        """Execute full voice turn: ACK -> LISTEN -> TRANSCRIBE -> THINK -> SPEAK -> COOLDOWN."""
        try:
            # 1. ACKNOWLEDGING
            self.state = LoopState.ACKNOWLEDGING
            ack_line = self.ack_bag.select()
            logger.info("State -> ACKNOWLEDGING: %r", ack_line)
            try:
                await self.tts_client.send_command(
                    "speak_sentence", {"text": ack_line}, timeout=60.0
                )
                ack_ok = await self._wait_for_playback(expected=1, timeout=90.0)
                if not ack_ok:
                    logger.warning("Ack playback timed out or failed; returning to IDLE")
                    self.state = LoopState.IDLE
                    return
            except Exception as exc:
                logger.error("Failed to speak ack line: %s", exc)
                self.state = LoopState.IDLE
                return

            # 2. LISTENING
            self.state = LoopState.LISTENING
            logger.info("State -> LISTENING")
            self._speech_started_event.clear()
            self._speech_ended_event.clear()
            self._last_speech_buffer = None

            with contextlib.suppress(Exception):
                await self.vad_client.send_command("start_listening", {}, timeout=5.0)

            # Wait for speech to start (timeout 15s)
            try:
                await asyncio.wait_for(self._speech_started_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                logger.info("No speech detected within 15s; returning to IDLE")
                self.state = LoopState.IDLE
                return

            # Speech started, wait for speech to end (timeout 90s)
            try:
                await asyncio.wait_for(self._speech_ended_event.wait(), timeout=90.0)
            except asyncio.TimeoutError:
                logger.warning("Speech did not end within 90s; forcing end")

            pcm_buffer = self._last_speech_buffer or b""

            # 3. TRANSCRIBING
            self.state = LoopState.TRANSCRIBING
            logger.info("State -> TRANSCRIBING: %d bytes", len(pcm_buffer))
            try:
                stt_resp = await self.stt_client.send_command(
                    "transcribe_pcm",
                    {},
                    raw_data=pcm_buffer,
                    timeout=30.0,
                )
            except Exception as exc:
                logger.error("STT transcription failed: %s", exc)
                self.state = LoopState.IDLE
                return

            stt_payload = stt_resp.payload
            user_text = stt_payload.get("text", "").strip()
            is_empty = stt_payload.get("empty", False) or not user_text

            if is_empty:
                logger.info("Transcription empty; selecting empty-response line")
                empty_line = self.empty_bag.select()
                self.state = LoopState.SPEAKING
                await self.tts_client.send_command(
                    "speak_sentence", {"text": empty_line}, timeout=60.0
                )
                await self._wait_for_playback(expected=1, timeout=60.0)
                self.state = LoopState.COOLDOWN
                await asyncio.sleep(self.cooldown_sec)
                self.state = LoopState.IDLE
                return

            # 4. THINKING
            self.state = LoopState.THINKING
            logger.info("State -> THINKING: user_text=%r", user_text)
            try:
                llm_resp = await self.llm_client.send_command(
                    "generate",
                    {"user_text": user_text},
                    timeout=180.0,
                )
            except Exception as exc:
                logger.error("LLM generation failed: %s", exc)
                self.state = LoopState.IDLE
                return

            at_text = llm_resp.payload.get("text", "")
            sentences = chunk_sentences(at_text)
            if not sentences:
                self.state = LoopState.COOLDOWN
                await asyncio.sleep(self.cooldown_sec)
                self.state = LoopState.IDLE
                return

            # 5. SPEAKING
            self.state = LoopState.SPEAKING
            logger.info("State -> SPEAKING: %d sentences", len(sentences))
            for s in sentences:
                await self.tts_client.send_command(
                    "speak_sentence", {"text": s}, timeout=60.0
                )

            await self._wait_for_playback(expected=len(sentences), timeout=300.0)

            # 6. COOLDOWN
            self.state = LoopState.COOLDOWN
            await asyncio.sleep(self.cooldown_sec)
            self.state = LoopState.IDLE
            logger.info("Turn completed -> IDLE")

        except Exception as exc:
            logger.exception("Error during turn: %s", exc)
            self.state = LoopState.IDLE

    def request_stop(self) -> None:
        """Signal graceful shutdown."""
        if not self._stop_event.is_set():
            logger.info("Shutdown requested")
            self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        """Install SIGTERM and SIGINT handlers."""
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
                self._installed_signals.append(sig)
            except (NotImplementedError, RuntimeError) as exc:
                logger.debug("Could not install handler for %s: %s", sig, exc)

    def _remove_signal_handlers(self) -> None:
        """Remove installed signal handlers."""
        loop = asyncio.get_running_loop()
        for sig in self._installed_signals:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)
        self._installed_signals.clear()

    async def run(self) -> int:
        """Run the LoopService."""
        self._install_signal_handlers()
        try:
            await self._connect_clients()
            await self.play_boot_greeting()

            self.state = LoopState.IDLE
            logger.info("LoopService running in IDLE state")

            await self._stop_event.wait()

            # Finish in-flight turn if currently speaking (up to 30s)
            if self._turn_task and not self._turn_task.done():
                logger.info("Waiting for active turn to finish...")
                with contextlib.suppress(asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(self._turn_task, timeout=30.0)

        finally:
            self._remove_signal_handlers()
            await self._close_clients()
            with contextlib.suppress(Exception):
                self.flag_file.unlink(missing_ok=True)
            logger.info("LoopService stopped cleanly")

        return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="AT Loop Controller Service")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="Directory containing at.toml and modules.toml",
    )
    parser.add_argument(
        "--socket-dir",
        type=Path,
        default=None,
        help="Custom socket directory override",
    )
    parser.add_argument(
        "--demo-turn",
        type=str,
        default=None,
        help="Bypass wake/vad/stt and run LLM->TTS for given user text",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Play boot greeting via TTS and exit",
    )
    parser.add_argument(
        "--no-wake",
        action="store_true",
        help="Skip wake-word client connection for dev testing",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Entry point logic."""
    config_dir = resolve_config_dir(args.config_dir)
    at_cfg_path = config_dir / "at.toml"
    mod_cfg_path = config_dir / "modules.toml"

    config = Config.load(str(at_cfg_path), str(mod_cfg_path))
    service = LoopService(
        config=config,
        socket_dir=args.socket_dir,
        no_wake=args.no_wake,
    )

    if args.smoke:
        return await service.run_smoke()

    if args.demo_turn is not None:
        return await service.run_demo_turn(args.demo_turn)

    return await service.run()


def main() -> None:
    """Main CLI entrypoint."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args(sys.argv[1:])
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
