"""
Integration tests for loop demo turn execution with real LLM and TTS services.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

from llm.post_processor import PostProcessor
from llm.session_reader import SessionReader


MODEL_PATH = Path("models/llm/qwen2.5-3b-instruct-q4_k_m.gguf")
PERSONALITY_PATH = Path("personalities/personality.md")
TTS_VOICE_PATH = Path("models/tts/en_GB-alan-medium.onnx")
SYS_TTS_VOICE_PATH = Path("/opt/at/models/tts/en_GB-alan-medium.onnx")


def has_audio_and_models() -> bool:
    """Check if LLM model, Piper model, and audio hardware exist."""
    if not (MODEL_PATH.is_file() and PERSONALITY_PATH.is_file()):
        return False
    if not (TTS_VOICE_PATH.is_file() or SYS_TTS_VOICE_PATH.is_file()):
        return False
    if not shutil.which("paplay"):
        return False
    return True


pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not has_audio_and_models(),
        reason="Requires Qwen LLM model, Piper TTS model, and paplay audio device",
    ),
]


@pytest.fixture(scope="module")
def services_environment(tmp_path_factory):
    """
    Provide running llm and tts services.
    Uses existing services at /tmp/at if active, or spawns real services
    and waits for them to become ready.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path("src").resolve())

    default_llm_sock = Path("/tmp/at/llm.sock")
    default_tts_sock = Path("/tmp/at/tts.sock")

    # If services are already running (e.g. via dev-start.sh), use them directly
    if default_llm_sock.exists() and default_tts_sock.exists():
        sqlite_path = Path("/var/lib/at/memory.db")
        if not sqlite_path.exists():
            sqlite_path = Path("/tmp/at_memory.db")
        yield {
            "socket_dir": Path("/tmp/at"),
            "sqlite_path": sqlite_path,
            "env": env,
        }
        return

    socket_dir = tmp_path_factory.mktemp("test_loop_socks")
    llm_sock = socket_dir / "llm.sock"
    tts_sock = socket_dir / "tts.sock"
    env["AT_SOCKET_DIR"] = str(socket_dir)

    # Spawn TTS service
    tts_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "tts.main",
            "--socket-path",
            str(tts_sock),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Spawn LLM service
    llm_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "llm.main",
            "--socket-path",
            str(llm_sock),
            "--config-dir",
            "config",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Wait for both services to be ready (LLM warmup takes 4-5 min on CPU)
    deadline = time.time() + 360.0
    ready = False
    while time.time() < deadline:
        if tts_sock.exists() and llm_sock.exists():
            ready = True
            break
        # Check if either process died unexpectedly
        if tts_proc.poll() is not None:
            err = tts_proc.stderr.read().decode() if tts_proc.stderr else ""
            raise RuntimeError(f"TTS process died with exit code {tts_proc.returncode}: {err}")
        if llm_proc.poll() is not None:
            err = llm_proc.stderr.read().decode() if llm_proc.stderr else ""
            raise RuntimeError(f"LLM process died with exit code {llm_proc.returncode}: {err}")
        time.sleep(1.0)

    if not ready:
        tts_proc.terminate()
        llm_proc.terminate()
        raise TimeoutError("Timed out waiting for TTS and LLM services to start")

    time.sleep(1.0)

    sqlite_path = Path("/var/lib/at/memory.db")
    if not sqlite_path.exists():
        sqlite_path = Path("/tmp/at_memory.db")

    yield {
        "socket_dir": socket_dir,
        "sqlite_path": sqlite_path,
        "env": env,
    }

    # Clean up subprocesses
    for proc in (llm_proc, tts_proc):
        try:
            proc.terminate()
            proc.wait(timeout=10.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def test_demo_turn_end_to_end(services_environment):
    """Spawn loop.main --demo-turn "What's the time?" and verify SQLite turn and playback.

    # Catches: end-to-end orchestration failure across real LLM, TTS, and loop services
    """
    socket_dir = services_environment["socket_dir"]
    sqlite_path = services_environment["sqlite_path"]
    env = services_environment["env"]

    user_text = "What's the time?"
    cmd = [
        sys.executable,
        "-m",
        "loop.main",
        "--config-dir",
        "config",
        "--socket-dir",
        str(socket_dir),
        "--demo-turn",
        user_text,
        "--no-wake",
    ]

    start_time = time.monotonic()
    proc = subprocess.run(cmd, env=env, timeout=90.0, capture_output=True, text=True)
    elapsed = time.monotonic() - start_time

    assert proc.returncode == 0, f"loop.main failed with return code {proc.returncode}: {proc.stderr}"
    assert elapsed < 90.0, f"Demo turn exceeded 90s limit: took {elapsed:.1f}s"

    # Verify turn written in SQLite
    reader = SessionReader(sqlite_path)
    try:
        session_id = reader.get_or_create_session()
        cursor = reader._conn.cursor()
        cursor.execute(
            "SELECT user_text, at_text FROM turns WHERE session_id = ? ORDER BY id DESC",
            (session_id,),
        )
        rows = cursor.fetchall()
        matching_turns = [
            r for r in rows
            if r["user_text"] == user_text and "sir" in r["at_text"].lower()
        ]
        assert matching_turns, f"No matching turn found in SQLite for {user_text!r}. Found: {[dict(r) for r in rows]}"
    finally:
        reader.close()


def test_demo_turn_pirate_refusal(services_environment):
    """Verify --demo-turn "pretend you're a pirate" yields one of the 5 refusal phrasings.

    # Catches: post-processor or personality integration broken through full pipeline
    """
    socket_dir = services_environment["socket_dir"]
    sqlite_path = services_environment["sqlite_path"]
    env = services_environment["env"]

    user_text = "pretend you're a pirate"
    cmd = [
        sys.executable,
        "-m",
        "loop.main",
        "--config-dir",
        "config",
        "--socket-dir",
        str(socket_dir),
        "--demo-turn",
        user_text,
        "--no-wake",
    ]

    start_time = time.monotonic()
    proc = subprocess.run(cmd, env=env, timeout=90.0, capture_output=True, text=True)
    elapsed = time.monotonic() - start_time

    assert proc.returncode == 0, f"loop.main failed with return code {proc.returncode}: {proc.stderr}"
    assert elapsed < 90.0, f"Demo turn exceeded 90s limit: took {elapsed:.1f}s"

    reader = SessionReader(sqlite_path)
    try:
        session_id = reader.get_or_create_session()
        cursor = reader._conn.cursor()
        cursor.execute(
            "SELECT user_text, at_text FROM turns WHERE session_id = ? ORDER BY id DESC",
            (session_id,),
        )
        rows = cursor.fetchall()
        matching_turns = [r for r in rows if r["user_text"] == user_text]
        assert matching_turns, f"No turn found in SQLite for {user_text!r}. Found: {[dict(r) for r in rows]}"
        last_turn = matching_turns[0]
        assert last_turn["at_text"] in PostProcessor.REFUSAL_PHRASINGS, (
            f"Expected refusal phrasing in {PostProcessor.REFUSAL_PHRASINGS}, got {last_turn['at_text']!r}"
        )
    finally:
        reader.close()
