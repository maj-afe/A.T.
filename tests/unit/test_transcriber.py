"""
Unit tests for stt.transcriber (WhisperTranscriber).
"""

from __future__ import annotations

import os
from pathlib import Path
import struct
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from stt.transcriber import STTError, WhisperTranscriber


@pytest.mark.asyncio
async def test_wav_header_correctness():
    # Catches: invalid WAV header causing whisper to reject the file or misread sample rate
    pcm = b"\x01\x02\x03\x04" * 100  # 400 bytes PCM
    wav_bytes = WhisperTranscriber.wrap_wav(pcm, sample_rate=16000)
    assert len(wav_bytes) == 44 + len(pcm)
    (
        riff,
        riff_size,
        wave_tag,
        fmt_tag,
        fmt_size,
        audio_fmt,
        channels,
        rate,
        byte_rate,
        align,
        bits,
        data_tag,
        data_size,
    ) = struct.unpack("<4sI4s4sIHHIIHH4sI", wav_bytes[:44])
    assert riff == b"RIFF"
    assert riff_size == 36 + len(pcm)
    assert wave_tag == b"WAVE"
    assert fmt_tag == b"fmt "
    assert fmt_size == 16
    assert audio_fmt == 1  # PCM
    assert channels == 1  # mono
    assert rate == 16000  # 16kHz
    assert byte_rate == 32000  # 16000 * 1 * 2
    assert align == 2  # 1 channel * 2 bytes/sample
    assert bits == 16  # 16-bit
    assert data_tag == b"data"
    assert data_size == len(pcm)
    assert wav_bytes[44:] == pcm


@pytest.mark.asyncio
async def test_text_parsed_from_stdout(tmp_path: Path):
    # Catches: text extraction including stray whitespace or timestamps
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file)
    mock_proc = MagicMock(returncode=0, stdout=" Hello world \n", stderr="")
    with patch("subprocess.run", return_value=mock_proc):
        result = await transcriber.transcribe(b"\x00\x00" * 1600)
        assert result.text == "Hello world"


@pytest.mark.asyncio
async def test_timings_parsed_from_stderr(tmp_path: Path):
    # Catches: metrics parsing regex not matching whisper's actual output format
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file)
    stderr = (
        "whisper_print_timings:   encode time =  4455.00 ms /     1 runs (  4455.00 ms per run)\n"
        "whisper_print_timings:   decode time =    20.00 ms /     1 runs (    20.00 ms per run)\n"
        "whisper_print_timings:    total time =  4500.00 ms\n"
    )
    mock_proc = MagicMock(returncode=0, stdout="hello", stderr=stderr)
    pcm = b"\x00\x00" * 16000  # 1000 ms audio
    with patch("subprocess.run", return_value=mock_proc):
        result = await transcriber.transcribe(pcm)
        assert result.encode_ms == 4455
        assert result.decode_ms == 20
        assert result.total_ms == 4500
        assert result.duration_ms == 1000
        assert result.rtf == 4.5


@pytest.mark.asyncio
async def test_subprocess_timeout_raises_error(tmp_path: Path):
    # Catches: hung whisper-cli blocking forever
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file, timeout=0.1)
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["whisper-cli"], timeout=0.1),
    ):
        with pytest.raises(STTError, match="timed out"):
            await transcriber.transcribe(b"\x00\x00" * 1600)


@pytest.mark.asyncio
async def test_subprocess_nonzero_exit_raises_error(tmp_path: Path):
    # Catches: silent failure on whisper-cli crash
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file)
    mock_proc = MagicMock(returncode=1, stdout="", stderr="error: failed to load")
    with patch("subprocess.run", return_value=mock_proc):
        with pytest.raises(STTError, match="exited with code 1"):
            await transcriber.transcribe(b"\x00\x00" * 1600)


@pytest.mark.asyncio
async def test_missing_model_raises_error():
    # Catches: late failure when model is absent
    transcriber = WhisperTranscriber(model_path="/nonexistent/model/path.bin")
    with pytest.raises(STTError, match="Model file not found"):
        await transcriber.transcribe(b"\x00\x00" * 1600)


@pytest.mark.asyncio
async def test_tempfile_cleaned_up_on_error(tmp_path: Path):
    # Catches: disk fill from leaked temp files under repeated failures
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file)
    created_file = None

    def fake_run(cmd, *args, **kwargs):
        nonlocal created_file
        created_file = cmd[cmd.index("-f") + 1]
        assert os.path.exists(created_file)
        raise RuntimeError("simulated error during whisper execution")

    with patch("subprocess.run", side_effect=fake_run):
        with pytest.raises(STTError):
            await transcriber.transcribe(b"\x00\x00" * 1600)

    assert created_file is not None
    assert not os.path.exists(created_file)


@pytest.mark.asyncio
async def test_empty_stdout_returns_empty_text(tmp_path: Path):
    # Catches: empty transcription crashing instead of returning cleanly
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"dummy_model")
    transcriber = WhisperTranscriber(model_path=model_file)
    mock_proc = MagicMock(returncode=0, stdout="   \n", stderr="")
    with patch("subprocess.run", return_value=mock_proc):
        result = await transcriber.transcribe(b"\x00\x00" * 1600)
        assert result.text == ""
