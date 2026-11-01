"""TTS Player using paplay for audio playback.

Writes WAV bytes to a temporary file and plays via paplay subprocess.
Handles cleanup and cancellation.
"""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from .synthesizer import TTSError

logger = logging.getLogger(__name__)


class Player:
    """Plays WAV audio using paplay subprocess.

    Each play() call writes WAV bytes to a NamedTemporaryFile, invokes paplay,
    and ensures the temp file is cleaned up in a finally block.
    """

    def __init__(self, playback_command: str = "paplay") -> None:
        """Initialize player with playback command.

        Args:
            playback_command: Command to play WAV files (default "paplay").
        """
        self.playback_command = playback_command
        self._current_process: asyncio.subprocess.Process | None = None
        self._current_tempfile: Path | None = None

    async def play(self, wav_bytes: bytes) -> None:
        """Play WAV audio bytes via paplay.

        Args:
            wav_bytes: WAV audio data to play.

        Raises:
            TTSError: If paplay is not found, fails, or temp file operations fail.
        """
        # Verify paplay exists
        paplay_path = shutil.which(self.playback_command)
        if not paplay_path:
            raise TTSError(f"Playback command not found: {self.playback_command}")

        # Create temporary file
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav_bytes)
                temp_file = Path(f.name)

            self._current_tempfile = temp_file
            logger.debug("Playing WAV via %s: %s", self.playback_command, temp_file)

            # Launch paplay subprocess
            self._current_process = await asyncio.create_subprocess_exec(
                paplay_path,
                str(temp_file),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

            # Wait for playback to complete
            returncode = await self._current_process.wait()

            if returncode != 0:
                raise TTSError(f"paplay exited with code {returncode}")

        except asyncio.CancelledError:
            # Clean up on cancellation
            await self._cleanup()
            raise
        except TTSError:
            # Re-raise TTSError after cleanup
            await self._cleanup()
            raise
        except Exception as e:
            logger.exception("Playback failed")
            await self._cleanup()
            raise TTSError(f"Playback failed: {e}") from e
        finally:
            # Ensure cleanup happens even on success
            await self._cleanup()

    async def _cleanup(self) -> None:
        """Clean up temp file and process references."""
        # Terminate process if still running
        if self._current_process and self._current_process.returncode is None:
            try:
                self._current_process.terminate()
                await asyncio.wait_for(self._current_process.wait(), timeout=1.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    self._current_process.kill()
                    await self._current_process.wait()
                except Exception:
                    pass

        # Delete temp file
        if self._current_tempfile:
            try:
                self._current_tempfile.unlink(missing_ok=True)
                logger.debug("Cleaned up temp file: %s", self._current_tempfile)
            except Exception as e:
                logger.warning("Failed to delete temp file %s: %s", self._current_tempfile, e)

        self._current_process = None
        self._current_tempfile = None

    async def stop(self) -> None:
        """Stop any in-flight playback and clean up."""
        await self._cleanup()