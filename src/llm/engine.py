"""LLM runtime engine wrapping llama-cpp-python."""

import logging
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from llama_cpp import Llama

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """Base exception for LLM runtime errors."""
    pass


class LLMEngine:
    """LLM Engine wrapping llama-cpp-python for resident Qwen inference."""

    def __init__(
        self,
        model_path: str | Path,
        n_ctx: int = 16384,
        n_threads: int = 4,
        temperature: float = 0.5,
        top_p: float = 0.9,
        top_k: int = 40,
        repeat_penalty: float = 1.1,
        verbose: bool = False,
    ) -> None:
        """Initialize and warm up LLMEngine.

        Args:
            model_path: Path to GGUF model file.
            n_ctx: Context window size (default 16384).
            n_threads: CPU thread count (default 4).
            temperature: Sampling temperature (default 0.5).
            top_p: Nucleus sampling probability (default 0.9).
            top_k: Top-k sampling (default 40).
            repeat_penalty: Repetition penalty (default 1.1).
            verbose: Enable verbose logging from llama.cpp.

        Raises:
            LLMError: If model file is missing or fails to load.
        """
        self.model_path = Path(model_path)
        self.n_ctx = n_ctx
        self.n_threads = n_threads
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k
        self.repeat_penalty = repeat_penalty
        self.verbose = verbose

        if not self.model_path.is_file():
            raise LLMError(f"Model file not found: {self.model_path}")

        try:
            logger.info(
                "Loading LLM model from %s (n_ctx=%d, n_threads=%d)",
                self.model_path,
                self.n_ctx,
                self.n_threads,
            )
            self._llm = Llama(
                model_path=str(self.model_path),
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
                verbose=self.verbose,
            )
            logger.info("Model loaded. Warmup deferred to LLMService with personality prompt.")
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"Failed to initialize LLM engine: {exc}") from exc

    def count_tokens(self, text: str) -> int:
        """Count the number of tokens in a text string.

        Args:
            text: Input string.

        Returns:
            Number of tokens.
        """
        if not text:
            return 0
        return len(self._llm.tokenize(text.encode("utf-8")))

    def generate(
        self,
        messages: list[dict[str, str]],
        max_tokens: int = 150,
        abort_cb: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Run streaming chat completion.

        Args:
            messages: List of chat messages ({"role": ..., "content": ...}).
            max_tokens: Maximum tokens to generate.
            abort_cb: Optional callable returning True if inference should abort.

        Yields:
            Token dicts: {"token": str, "index": int}
            Final dict: {"finish": reason, "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int}}
        """
        prompt_tokens = sum(self.count_tokens(m.get("content", "")) + 4 for m in messages) + 2

        stream = self._llm.create_chat_completion(
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            repeat_penalty=self.repeat_penalty,
        )

        token_idx = 0
        finish_reason = "stop"

        for chunk in stream:
            if abort_cb is not None and abort_cb():
                logger.info("Generation aborted by abort callback at token %d", token_idx)
                yield {
                    "finish": "abort",
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": token_idx,
                        "total_tokens": prompt_tokens + token_idx,
                    },
                }
                return

            choices = chunk.get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]

            delta = choice.get("delta", {})
            content = delta.get("content")
            if content:
                yield {"token": content, "index": token_idx}
                token_idx += 1

        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": token_idx,
            "total_tokens": prompt_tokens + token_idx,
        }
        yield {"finish": finish_reason, "usage": usage}
