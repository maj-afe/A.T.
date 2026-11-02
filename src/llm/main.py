"""LLM Service main entry point.

Provides IPC server for generate command with token streaming, prompt assembly,
post-processing, session tracking, timeout protection, and graceful shutdown.
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from at_config import Config
from at_ipc import IPCServer, Envelope

from llm.engine import LLMEngine, LLMError
from llm.prompt_builder import PromptBuilder
from llm.post_processor import PostProcessor
from llm.session_reader import SessionReader

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 90.0
SHUTDOWN_GRACE_PERIOD_SECONDS = 30.0


def resolve_config_dir(config_dir: Path | str | None = None) -> Path:
    """Resolve config directory with fallback to local project config/."""
    candidates = []
    if config_dir is not None:
        candidates.append(Path(config_dir))
    candidates.append(Path("/etc/at"))
    candidates.append(Path(__file__).parents[2] / "config")
    candidates.append(Path("config"))

    for c in candidates:
        if (c / "at.toml").is_file() and (c / "modules.toml").is_file():
            return c

    return Path("/etc/at")


def resolve_existing_path(path: Path | str, local_relative: str) -> Path:
    """Resolve a path, falling back to local workspace if candidate does not exist."""
    p = Path(path)
    if p.exists():
        return p
    local_p = Path(__file__).parents[2] / local_relative
    if local_p.exists():
        return local_p
    cwd_p = Path(local_relative)
    if cwd_p.exists():
        return cwd_p
    return p


class LLMService:
    """LLM Service providing chat generation over AT IPC."""

    def __init__(
        self,
        config: Config,
        socket_path: Path | None = None,
        model_path: Path | None = None,
        engine: LLMEngine | None = None,
        session_reader: SessionReader | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        install_signal_handlers: bool = True,
    ) -> None:
        """Initialize LLMService with configuration and dependencies.

        Args:
            config: Loaded AT configuration.
            socket_path: Optional custom socket path override.
            model_path: Optional model path override.
            engine: Optional pre-initialized LLMEngine instance.
            session_reader: Optional pre-initialized SessionReader instance.
            timeout: Generation timeout in seconds (default 90s).
            install_signal_handlers: Whether to install SIGTERM handlers.
        """
        self.config = config
        self.timeout = timeout
        self.install_signal_handlers = install_signal_handlers

        # Resolve paths
        cfg_model = model_path or config.get_str("llm", "model_path")
        self.model_path = resolve_existing_path(
            cfg_model, "models/llm/qwen2.5-3b-instruct-q4_k_m.gguf"
        )

        cfg_personality = config.get_str("paths", "personality_file")
        self.personality_path = resolve_existing_path(
            cfg_personality, "personalities/personality.md"
        )

        cfg_sqlite = config.get_str("paths", "sqlite_path")
        try:
            sqlite_p = Path(cfg_sqlite)
            sqlite_p.parent.mkdir(parents=True, exist_ok=True)
            self.sqlite_path = sqlite_p
        except (PermissionError, OSError):
            self.sqlite_path = Path("/tmp/at_memory.db")

        # LLM parameters
        n_threads = config.get_int("llm", "threads")
        n_ctx = config.get_int("llm", "context_window")
        sampling_cfg = config.get_dict("llm", "sampling")
        temperature = float(sampling_cfg.get("temperature", 0.5))
        top_p = float(sampling_cfg.get("top_p", 0.9))
        top_k = int(sampling_cfg.get("top_k", 40))
        repeat_penalty = float(sampling_cfg.get("repetition_penalty", 1.1))

        budgets_cfg = config.get_dict("llm", "budgets")
        self.max_tokens_default = int(budgets_cfg.get("max_tokens", 150))
        self.max_tokens_elaboration = int(
            budgets_cfg.get("max_tokens_elaboration", 400)
        )

        # Initialize core components
        if engine is not None:
            self.engine = engine
        else:
            logger.info("Initializing LLMEngine...")
            self.engine = LLMEngine(
                model_path=self.model_path,
                n_ctx=n_ctx,
                n_threads=n_threads,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                repeat_penalty=repeat_penalty,
            )

        logger.info("Initializing PromptBuilder with %s...", self.personality_path)
        self.prompt_builder = PromptBuilder(
            personality_path=self.personality_path,
            engine=self.engine,
            max_context_tokens=13000,
        )

        logger.info("Initializing PostProcessor...")
        self.post_processor = PostProcessor()

        if session_reader is not None:
            self.session_reader = session_reader
        else:
            logger.info("Initializing SessionReader at %s...", self.sqlite_path)
            self.session_reader = SessionReader(db_path=self.sqlite_path)

        # Socket and IPCServer setup
        if socket_path is not None:
            self.socket_path = Path(socket_path)
        else:
            try:
                socket_dir = Path(config.get_str("paths", "socket_dir"))
                socket_dir.mkdir(parents=True, exist_ok=True)
                self.socket_path = socket_dir / config.get_str("ipc", "llm")
            except (PermissionError, OSError):
                self.socket_path = Path("/tmp/llm.sock")

        self.ipc_server = IPCServer(
            "llm",
            socket_path=self.socket_path,
            install_signal_handlers=False,
        )
        self.ipc_server.on("generate", self._handle_generate)

        # State tracking for graceful shutdown
        self._running = False
        self._active_generation = False
        self._active_generation_done = asyncio.Event()
        self._active_generation_done.set()
        self._installed_signals: list[signal.Signals] = []

    async def start(self) -> None:
        """Start the IPC server, emit llm_ready event, and register signal handlers."""
        logger.info("Starting LLM IPC server at %s", self.socket_path)
        await self.ipc_server.start()
        self._running = True

        if self.install_signal_handlers:
            self._install_signals()

        # Emit llm_ready event after warmup
        await self.ipc_server.broadcast(
            "llm_ready",
            {"model": str(self.model_path)},
        )
        logger.info("LLM service started and ready")

    async def stop(self) -> None:
        """Stop LLM service cleanly."""
        if not self._running:
            return

        logger.info("Stopping LLM service...")
        self._running = False
        self._remove_signals()

        # Wait for active generation up to grace period
        if self._active_generation:
            logger.info(
                "Waiting up to %.1fs for active generation to complete...",
                SHUTDOWN_GRACE_PERIOD_SECONDS,
            )
            try:
                await asyncio.wait_for(
                    self._active_generation_done.wait(),
                    timeout=SHUTDOWN_GRACE_PERIOD_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("Active generation did not finish before shutdown")

        await self.ipc_server.stop()
        self.session_reader.close()
        logger.info("LLM service stopped cleanly")

    def _install_signals(self) -> None:
        """Install SIGTERM handler for graceful shutdown."""
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(
                signal.SIGTERM,
                lambda: asyncio.create_task(self._handle_sigterm()),
            )
            self._installed_signals.append(signal.SIGTERM)
        except (NotImplementedError, RuntimeError) as exc:
            logger.debug("Could not install SIGTERM handler: %s", exc)

    def _remove_signals(self) -> None:
        """Remove installed signal handlers."""
        loop = asyncio.get_running_loop()
        for sig in self._installed_signals:
            try:
                loop.remove_signal_handler(sig)
            except Exception:
                pass
        self._installed_signals.clear()

    async def _handle_sigterm(self) -> None:
        """Handle SIGTERM signal."""
        logger.info("Received SIGTERM, initiating graceful shutdown")
        await self.stop()

    async def generate_response(
        self,
        user_text: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute chat generation with streaming, timeouts, metrics, and persistence.

        Args:
            user_text: Input message from user.
            session_id: Optional session identifier.

        Returns:
            Response dict with {"text": str, "metrics": dict, "elaboration": bool}.
        """
        self._active_generation = True
        self._active_generation_done.clear()
        start_time = time.perf_counter()

        try:
            effective_session = session_id or self.session_reader.get_or_create_session()
            recent_turns = self.session_reader.get_recent_turns(
                effective_session, max_turns=40
            )

            messages, elaboration = self.prompt_builder.build(user_text, recent_turns)
            max_tokens = (
                self.max_tokens_elaboration if elaboration else self.max_tokens_default
            )

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            abort_event = threading.Event()

            def _worker() -> None:
                try:
                    for chunk in self.engine.generate(
                        messages=messages,
                        max_tokens=max_tokens,
                        abort_cb=abort_event.is_set,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, chunk)
                except Exception as exc:
                    loop.call_soon_threadsafe(queue.put_nowait, {"error": str(exc)})

            worker_thread = threading.Thread(target=_worker, daemon=True)
            worker_thread.start()

            buffer: list[str] = []
            first_token_time: float | None = None
            finish_payload: dict[str, Any] | None = None

            async def _consume() -> dict[str, Any]:
                nonlocal first_token_time, finish_payload
                while True:
                    item = await queue.get()
                    if "error" in item:
                        raise LLMError(item["error"])

                    if "token" in item:
                        if first_token_time is None:
                            first_token_time = time.perf_counter()
                        token_str = item["token"]
                        buffer.append(token_str)
                        # Broadcast token stream
                        await self.ipc_server.broadcast(
                            "llm_token_stream",
                            {"token": token_str, "index": item["index"]},
                        )

                    if "finish" in item:
                        finish_payload = item
                        return item

            try:
                await asyncio.wait_for(_consume(), timeout=self.timeout)
            except asyncio.TimeoutError:
                abort_event.set()
                logger.warning(
                    "LLM generation timed out after %.1f seconds", self.timeout
                )
                await self.ipc_server.broadcast(
                    "llm_timeout",
                    {"session_id": effective_session},
                )
                timeout_text = "That's taking longer than expected, sir."
                metrics = {
                    "prompt_tokens": 0,
                    "completion_tokens": len(buffer),
                    "time_to_first_token_ms": 0.0,
                    "total_time_ms": round((time.perf_counter() - start_time) * 1000.0, 2),
                    "tokens_per_second": 0.0,
                    "truncated": False,
                    "shuffled": False,
                    "banned_stripped": False,
                    "timed_out": True,
                    "elaboration": elaboration,
                }
                return {
                    "text": timeout_text,
                    "metrics": metrics,
                    "elaboration": elaboration,
                }

            raw_text = "".join(buffer)
            end_time = time.perf_counter()
            total_time_ms = (end_time - start_time) * 1000.0
            time_to_first_token_ms = (
                ((first_token_time - start_time) * 1000.0)
                if first_token_time is not None
                else total_time_ms
            )

            # Post-processing
            processed_text, post_metrics = self.post_processor.process(
                raw_text, session_id=effective_session
            )

            usage = finish_payload.get("usage", {}) if finish_payload else {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", len(buffer))
            tokens_per_second = (
                (completion_tokens / (total_time_ms / 1000.0))
                if total_time_ms > 0
                else 0.0
            )

            metrics = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "time_to_first_token_ms": round(time_to_first_token_ms, 2),
                "total_time_ms": round(total_time_ms, 2),
                "tokens_per_second": round(tokens_per_second, 2),
                "truncated": post_metrics.get("truncated", False),
                "shuffled": post_metrics.get("shuffled", False),
                "banned_stripped": post_metrics.get("banned_stripped", False),
                "timed_out": False,
                "elaboration": elaboration,
            }

            logger.info("Generation metrics: %s", metrics)

            # Persist turn
            self.session_reader.write_turn(
                session_id=effective_session,
                user_text=user_text,
                at_text=processed_text,
                metrics=metrics,
            )

            response_payload = {
                "text": processed_text,
                "metrics": metrics,
                "elaboration": elaboration,
            }

            # Broadcast response ready
            await self.ipc_server.broadcast(
                "llm_response_ready",
                response_payload,
            )

            return response_payload

        finally:
            self._active_generation = False
            self._active_generation_done.set()

    async def _handle_generate(
        self, envelope: Envelope, raw_data: bytes | None
    ) -> dict[str, Any]:
        """Handle incoming generate IPC command."""
        payload = envelope.payload
        user_text = payload.get("user_text", "")
        session_id = payload.get("session_id")
        return await self.generate_response(user_text, session_id)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="AT LLM Runtime Service")
    parser.add_argument(
        "--socket-path",
        type=Path,
        default=None,
        help="Custom socket path override",
    )
    parser.add_argument(
        "--model-path",
        type=Path,
        default=None,
        help="Path to GGUF model file override",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("/etc/at"),
        help="Directory containing at.toml and modules.toml",
    )
    parser.add_argument(
        "--smoke",
        type=str,
        default=None,
        help="Run smoke test with given user message and exit",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    """Run LLM service."""
    cfg_dir = resolve_config_dir(args.config_dir)
    at_path = cfg_dir / "at.toml"
    mod_path = cfg_dir / "modules.toml"

    try:
        config = Config.load(str(at_path), str(mod_path))
    except Exception as exc:
        logger.error("Failed to load config from %s: %s", cfg_dir, exc)
        return 1

    service = LLMService(
        config=config,
        socket_path=args.socket_path,
        model_path=args.model_path,
    )

    if args.smoke is not None:
        # Smoke test mode: execute single generation and print response text
        result = await service.generate_response(args.smoke)
        print(result["text"])
        service.session_reader.close()
        return 0

    await service.start()
    try:
        while service._running:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        await service.stop()

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
