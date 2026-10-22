"""
Integration test for audio-source: a real arecord subprocess, a real Unix
socket, and the real broadcast path through at-ipc.

Skipped when arecord or a usable default capture device is unavailable.
"""

import asyncio
from pathlib import Path
import shutil
import subprocess

import pytest

from at_ipc.client import IPCClient
from audio_source.capture import DEFAULT_DEVICE, FRAME_BYTES
from audio_source.main import AudioSourceService


def _arecord_can_capture() -> bool:
    """Probe arecord once: binary present and the default device streams data."""
    if shutil.which("arecord") is None:
        return False
    try:
        probe = subprocess.run(
            [
                "arecord",
                "-D",
                DEFAULT_DEVICE,
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-t",
                "raw",
                "-q",
                "-d",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0 and len(probe.stdout) > 0


ARECORD_AVAILABLE = _arecord_can_capture()


@pytest.mark.skipif(
    not ARECORD_AVAILABLE, reason="arecord or a default capture device is unavailable"
)
@pytest.mark.asyncio
async def test_audio_source_broadcasts_960_byte_frames(temp_socket_dir: Path):
    # Catches: audio-source broadcasting wrong frame size, dropping frames
    # under sustained load, or failing to broadcast at all.
    sock_path = temp_socket_dir / "audio-source.sock"

    service = AudioSourceService(
        socket_path=sock_path,
        device=DEFAULT_DEVICE,
        install_signal_handlers=False,
    )
    run_task = asyncio.create_task(service.run())

    frames: list[bytes] = []
    first_frame = asyncio.Event()

    client = IPCClient("test_subscriber", "audio-source", socket_path=sock_path)

    @client.on("audio_frame")
    async def on_audio_frame(envelope, raw: bytes | None):
        assert raw is not None
        frames.append(raw)
        first_frame.set()

    try:
        # Wait for the socket to be bound (run() starts as a task), then connect.
        for _ in range(200):
            if sock_path.exists():
                break
            await asyncio.sleep(0.005)
        assert sock_path.exists(), "audio-source never bound its socket"

        await client.connect()
        await client.wait_ready()
        await asyncio.sleep(0.01)  # let the server register the subscriber

        await asyncio.wait_for(first_frame.wait(), timeout=5.0)

        # arecord writes ~4000-byte chunks, so frames arrive in small bursts.
        # Start the 2-second window at a burst boundary (50 ms of silence),
        # otherwise a cutoff mid-burst undercounts by up to one chunk.
        previous = -1
        while previous != len(frames):
            previous = len(frames)
            await asyncio.sleep(0.05)

        start_count = len(frames)
        await asyncio.sleep(2.0)
        measured = frames[start_count:]

        assert 66 <= len(measured) <= 67, (
            f"expected 66-67 frames in 2 s, got {len(measured)}"
        )
        assert all(len(frame) == FRAME_BYTES for frame in measured)
    finally:
        await client.close()
        service.request_stop()
        await asyncio.wait_for(run_task, timeout=10.0)

    assert not sock_path.exists()
