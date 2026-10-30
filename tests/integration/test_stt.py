"""
Integration tests for STT service with real whisper-cli and ggml model.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from at_ipc.client import IPCClient
from at_ipc.errors import IPCResponseError
from stt.main import STTService

MODEL_PATH = "/home/aniruddh_sen/Documents/Kate/at/models/stt/ggml-base.en-q5_1.bin"


@pytest.mark.asyncio
async def test_transcribe_hello_world_real_model(temp_socket_dir: Path):
    # Catches: end-to-end integration failure — WAV wrapping, subprocess invocation, IPC, timing parse
    fixture_path = Path(__file__).parents[1] / "fixtures" / "hello_world.wav"
    if not fixture_path.exists():
        pytest.skip(f"Fixture {fixture_path} not found")

    sock_path = temp_socket_dir / "stt_hello.sock"
    service = STTService(
        model_path=MODEL_PATH,
        socket_path=sock_path,
        install_signal_handlers=False,
    )
    await service.start()
    client = IPCClient("test_client", "stt", socket_path=sock_path)

    try:
        await client.connect()
        await client.wait_ready()
        wav_bytes = fixture_path.read_bytes()
        response = await client.send_command(
            "transcribe_pcm", payload={}, raw_data=wav_bytes, timeout=20.0
        )
        assert "text" in response.payload
        text = response.payload["text"]
        assert "hello" in text.lower()
        metrics = response.payload["metrics"]
        assert metrics["duration_ms"] > 0
        assert metrics["encode_ms"] > 0
        assert metrics["total_ms"] > 0
        assert metrics["rtf"] > 0
    finally:
        await client.close()
        await service.stop()


@pytest.mark.asyncio
async def test_transcribe_silence_returns_empty_or_short(temp_socket_dir: Path):
    # Catches: whisper hallucinating paragraphs of text from silence
    sock_path = temp_socket_dir / "stt_silence.sock"
    service = STTService(
        model_path=MODEL_PATH,
        socket_path=sock_path,
        install_signal_handlers=False,
    )
    await service.start()
    client = IPCClient("test_client", "stt", socket_path=sock_path)

    try:
        await client.connect()
        await client.wait_ready()
        silence_pcm = b"\x00\x00" * 32000  # 2 seconds of 16kHz 16-bit mono silence
        response = await client.send_command(
            "transcribe_pcm", payload={}, raw_data=silence_pcm, timeout=20.0
        )
        is_empty = response.payload.get("empty", False)
        text = response.payload.get("text", "").strip()
        assert is_empty or len(text) <= 10
    finally:
        await client.close()
        await service.stop()


@pytest.mark.asyncio
async def test_subprocess_timeout_enforced(temp_socket_dir: Path):
    # Catches: malformed input hanging the module
    sock_path = temp_socket_dir / "stt_timeout.sock"
    service = STTService(
        model_path=MODEL_PATH,
        socket_path=sock_path,
        install_signal_handlers=False,
    )
    await service.start()
    client = IPCClient("test_client", "stt", socket_path=sock_path)

    try:
        await client.connect()
        await client.wait_ready()
        invalid_wav = b"RIFF\x10\x00\x00\x00WAVEfmt \x10\x00\x00\x00" + os.urandom(100)
        with pytest.raises(IPCResponseError):
            await client.send_command(
                "transcribe_pcm", payload={}, raw_data=invalid_wav, timeout=5.0
            )
    finally:
        await client.close()
        await service.stop()
