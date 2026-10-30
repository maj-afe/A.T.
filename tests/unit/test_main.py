"""
Unit tests for stt.main (STTService IPC interface).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from at_ipc.client import IPCClient
from stt.main import STTService
from stt.transcriber import TranscriptionResult


@pytest.mark.asyncio
async def test_transcribe_pcm_handler_returns_text(temp_socket_dir: Path):
    # Catches: IPC handler not wired to transcriber
    sock_path = temp_socket_dir / "stt_test.sock"
    mock_transcriber = AsyncMock()
    mock_transcriber.transcribe.return_value = TranscriptionResult(
        text="Recognized voice command",
        duration_ms=1000,
        encode_ms=450,
        decode_ms=15,
        total_ms=465,
        rtf=0.465,
    )

    service = STTService(
        transcriber=mock_transcriber,
        socket_path=sock_path,
        install_signal_handlers=False,
    )
    await service.start()

    client = IPCClient("test_client", "stt", socket_path=sock_path)
    try:
        await client.connect()
        await client.wait_ready()
        pcm_data = b"\x00\x00" * 16000
        response = await client.send_command(
            "transcribe_pcm", payload={}, raw_data=pcm_data
        )

        assert response.payload["text"] == "Recognized voice command"
        assert response.payload["metrics"]["duration_ms"] == 1000
        assert response.payload["metrics"]["encode_ms"] == 450
        assert response.payload["metrics"]["rtf"] == 0.465
        mock_transcriber.transcribe.assert_awaited_once_with(pcm_data)
    finally:
        await client.close()
        await service.stop()


@pytest.mark.asyncio
async def test_empty_transcription_returns_empty_flag(temp_socket_dir: Path):
    # Catches: empty transcription indistinguishable from error at IPC layer
    sock_path = temp_socket_dir / "stt_empty_test.sock"
    mock_transcriber = AsyncMock()
    mock_transcriber.transcribe.return_value = TranscriptionResult(
        text="",
        duration_ms=1000,
        encode_ms=400,
        decode_ms=10,
        total_ms=410,
        rtf=0.41,
    )

    service = STTService(
        transcriber=mock_transcriber,
        socket_path=sock_path,
        install_signal_handlers=False,
    )
    await service.start()

    client = IPCClient("test_client", "stt", socket_path=sock_path)
    try:
        await client.connect()
        await client.wait_ready()
        pcm_data = b"\x00\x00" * 16000
        response = await client.send_command(
            "transcribe_pcm", payload={}, raw_data=pcm_data
        )

        assert response.payload["text"] == ""
        assert response.payload["empty"] is True
        assert response.payload["metrics"]["duration_ms"] == 1000
        mock_transcriber.transcribe.assert_awaited_once_with(pcm_data)
    finally:
        await client.close()
        await service.stop()
