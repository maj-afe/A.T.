"""Integration tests for TTS module.

Uses real PiperVoice and real paplay. Requires audio hardware.
"""

import io
import wave
import struct
import shutil
import subprocess
import pytest

from tts.synthesizer import Synthesizer
from tts.player import Player
from tts.synthesizer import TTSError


# Skip if no audio device or paplay not available
def has_audio_device():
    """Check if audio device is available for paplay."""
    try:
        result = subprocess.run(
            ["paplay", "--help"],
            capture_output=True,
            timeout=2,
        )
        return result.returncode == 0
    except Exception:
        return False


def has_piper_model():
    """Check if Piper model file exists."""
    import os
    return os.path.exists("/home/aniruddh_sen/Documents/Kate/at/models/tts/en_GB-alan-medium.onnx")


pytestmark = pytest.mark.skipif(
    not (has_audio_device() and has_piper_model()),
    reason="Requires paplay and Piper model",
)


class TestRealSynthesis:
    """Integration tests with real PiperVoice."""

    @pytest.fixture
    def synthesizer(self):
        """Create real synthesizer."""
        return Synthesizer(
            model_path="/home/aniruddh_sen/Documents/Kate/at/models/tts/en_GB-alan-medium.onnx",
            length_scale=0.95,
            noise_scale=0.7,
            noise_w=1.2,
            fade_ms=100,
        )

    def test_real_synthesis_produces_audio(self, synthesizer):
        """Test real synthesis produces valid audio.

        # Catches: piper-tts library integration failure, wrong sample rate, silent output
        """
        wav_bytes = synthesizer.synthesize("Good evening, sir.")

        # Verify WAV structure
        assert wav_bytes[:4] == b"RIFF"
        assert wav_bytes[8:12] == b"WAVE"

        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 22050
            frames = wav.getnframes()
            assert frames > 0

            # Verify duration > 0.5s
            duration = frames / wav.getframerate()
            assert duration > 0.5

            # Verify non-silent (RMS > threshold)
            pcm = wav.readframes(frames)
            samples = struct.unpack(f"<{frames}h", pcm)
            rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
            assert rms > 100, f"Audio too quiet: RMS={rms}"

    def test_at_name_round_trip(self, synthesizer):
        """Test A.T. preprocessing affects duration.

        # Catches: preprocessing not working — "A.T." would produce shorter audio than "ay tee"
        """
        # Synthesize with A.T. (should become "ay tee")
        wav_at = synthesizer.synthesize("A.T. online")

        # Synthesize with explicit "ay tee"
        wav_aytee = synthesizer.synthesize("ay tee online")

        # Synthesize with "at" (word)
        wav_at_word = synthesizer.synthesize("at online")

        with wave.open(io.BytesIO(wav_at), "rb") as wav:
            frames_at = wav.getnframes()
        with wave.open(io.BytesIO(wav_aytee), "rb") as wav:
            frames_aytee = wav.getnframes()
        with wave.open(io.BytesIO(wav_at_word), "rb") as wav:
            frames_at_word = wav.getnframes()

        # "A.T." -> "ay tee" should be longer than "at" (word)
        # because "ay tee" has more phonemes
        assert frames_at > frames_at_word, (
            f"A.T. preprocessed ({frames_at} frames) should be longer than 'at' ({frames_at_word} frames)"
        )

        # "A.T." and "ay tee" should be similar duration
        diff = abs(frames_at - frames_aytee)
        assert diff < frames_at * 0.1, (
            f"A.T. ({frames_at}) and 'ay tee' ({frames_aytee}) should have similar duration"
        )


class TestRealPlayback:
    """Integration tests with real paplay."""

    @pytest.fixture
    def player(self):
        """Create real player."""
        return Player(playback_command="paplay")

    @pytest.fixture
    def valid_wav(self):
        """Generate a real WAV using Piper for testing playback."""
        synth = Synthesizer(
            model_path="/home/aniruddh_sen/Documents/Kate/at/models/tts/en_GB-alan-medium.onnx",
            length_scale=0.95,
            noise_scale=0.7,
            noise_w=1.2,
            fade_ms=100,
        )
        return synth.synthesize("Test playback")

    @pytest.mark.asyncio
    async def test_real_playback_via_paplay(self, player, valid_wav):
        """Test real playback via paplay.

        # Catches: paplay integration failure
        """
        await player.play(valid_wav)
        # If we get here without exception, paplay succeeded

    @pytest.mark.asyncio
    async def test_stop_cancels_playback(self, player, valid_wav):
        """Test stop() cancels in-flight playback."""
        import asyncio

        play_task = asyncio.create_task(player.play(valid_wav))
        await asyncio.sleep(0.1)  # Let it start

        await player.stop()

        # Task should complete (was cancelled)
        await play_task


class TestFullPipeline:
    """End-to-end tests with full pipeline."""

    @pytest.mark.asyncio
    async def test_synthesize_and_play_pipeline(self):
        """Test full synthesize + play pipeline."""
        synth = Synthesizer(
            model_path="/home/aniruddh_sen/Documents/Kate/at/models/tts/en_GB-alan-medium.onnx",
            length_scale=0.95,
            noise_scale=0.7,
            noise_w=1.2,
            fade_ms=100,
        )
        player = Player(playback_command="paplay")

        wav_bytes = synth.synthesize("All systems nominal.")
        await player.play(wav_bytes)

        # Verify WAV was valid
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            assert wav.getframerate() == 22050
            assert wav.getnframes() > 0