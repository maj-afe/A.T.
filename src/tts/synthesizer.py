"""TTS Synthesizer using Piper TTS library.

Loads PiperVoice once at startup, synthesizes per request with tuned parameters.
Applies A.T. -> "ay tee" preprocessing and 100ms linear fade-out.
"""

import io
import logging
import wave
from pathlib import Path

from piper import PiperVoice, SynthesisConfig

logger = logging.getLogger(__name__)

# Default sample rate for Piper medium voices
DEFAULT_SAMPLE_RATE = 22050


class TTSError(Exception):
    """Base exception for TTS errors."""
    pass


class Synthesizer:
    """Synthesizes text to WAV audio using Piper TTS.

    Loads the model once at initialization. Each synthesize() call produces
    WAV bytes with the configured tuning parameters applied.
    """

    def __init__(
        self,
        model_path: str | Path,
        length_scale: float = 0.95,
        noise_scale: float = 0.7,
        noise_w: float = 1.2,
        fade_ms: int = 100,
    ) -> None:
        """Initialize synthesizer with Piper model and tuning parameters.

        Args:
            model_path: Path to the .onnx model file.
            length_scale: Length scale for synthesis (default 0.95).
            noise_scale: Noise scale for synthesis (default 0.7).
            noise_w: Noise width scale for synthesis (default 1.2).
            fade_ms: Fade-out duration in milliseconds (default 100).
        """
        self.model_path = Path(model_path)
        self.length_scale = length_scale
        self.noise_scale = noise_scale
        self.noise_w = noise_w
        self.fade_ms = fade_ms

        # Load PiperVoice once at startup
        logger.info("Loading Piper model from %s", self.model_path)
        self._voice = PiperVoice.load(str(self.model_path))
        logger.info("Piper model loaded successfully")

        # Create SynthesisConfig with tuned parameters
        self._syn_config = SynthesisConfig(
            length_scale=self.length_scale,
            noise_scale=self.noise_scale,
            noise_w_scale=self.noise_w,
            normalize_audio=True,
            volume=1.0,
        )

    def _preprocess_text(self, text: str) -> str:
        """Apply A.T. -> 'ay tee' preprocessing to text.

        Args:
            text: Input text.

        Returns:
            Text with 'A.T.' replaced by 'ay tee' (case-insensitive).
        """
        # Replace A.T. variations with "ay tee"
        # Handle "A.T.", "A.T", "a.t.", "a.t" etc.
        import re
        return re.sub(r'\bA\.?T\.?\b', 'ay tee', text, flags=re.IGNORECASE)

    def _apply_fade(self, audio_data: bytes, sample_rate: int) -> bytes:
        """Apply linear fade-out to the last fade_ms milliseconds of audio.

        Args:
            audio_data: Raw PCM audio data (16-bit signed, mono).
            sample_rate: Sample rate in Hz.

        Returns:
            Audio data with fade-out applied.
        """
        if self.fade_ms <= 0:
            return audio_data

        # Calculate number of samples to fade
        fade_samples = int(sample_rate * self.fade_ms / 1000)
        num_samples = len(audio_data) // 2  # 16-bit = 2 bytes per sample

        if fade_samples >= num_samples:
            # Fade entire audio if it's shorter than fade duration
            fade_samples = num_samples

        if fade_samples == 0:
            return audio_data

        # Convert to mutable list of samples
        samples = list(int.from_bytes(audio_data[i:i+2], 'little', signed=True)
                       for i in range(0, len(audio_data), 2))

        # Apply linear fade to last fade_samples
        start_idx = num_samples - fade_samples
        for i in range(fade_samples):
            idx = start_idx + i
            # Linear fade from 1.0 to 0.0
            factor = 1.0 - (i / fade_samples)
            samples[idx] = int(samples[idx] * factor)

        # Convert back to bytes
        return b''.join(s.to_bytes(2, 'little', signed=True) for s in samples)

    def synthesize(self, text: str) -> bytes:
        """Synthesize text to WAV bytes.

        Args:
            text: Text to synthesize.

        Returns:
            WAV audio data as bytes.

        Raises:
            TTSError: If synthesis fails.
        """
        if not text or not text.strip():
            # Return minimal valid WAV for empty text
            return self._create_empty_wav()

        # Apply A.T. preprocessing
        processed_text = self._preprocess_text(text)
        logger.debug("Synthesizing: %r -> %r", text, processed_text)

        try:
            # Use BytesIO to capture WAV output
            wav_buffer = io.BytesIO()

            with wave.open(wav_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)  # mono
                wav_file.setsampwidth(2)  # 16-bit
                wav_file.setframerate(DEFAULT_SAMPLE_RATE)
                # Synthesize directly to the wave file
                self._voice.synthesize_wav(
                    processed_text,
                    wav_file,
                    syn_config=self._syn_config,
                )

            wav_bytes = wav_buffer.getvalue()

            # Apply fade-out to the audio data (not the WAV header)
            # We need to extract PCM data, apply fade, and rebuild WAV
            # For simplicity and correctness, re-read the WAV, apply fade, rewrite
            wav_buffer = io.BytesIO(wav_bytes)
            with wave.open(wav_buffer, "rb") as wav_file:
                # Verify format
                if wav_file.getnchannels() != 1 or wav_file.getsampwidth() != 2:
                    raise TTSError(f"Unexpected WAV format: {wav_file.getnchannels()}ch, {wav_file.getsampwidth()*8}-bit")
                sample_rate = wav_file.getframerate()
                pcm_data = wav_file.readframes(wav_file.getnframes())

            # Apply fade to PCM data
            faded_pcm = self._apply_fade(pcm_data, sample_rate)

            # Rebuild WAV with faded audio
            output_buffer = io.BytesIO()
            with wave.open(output_buffer, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(faded_pcm)

            return output_buffer.getvalue()

        except Exception as e:
            logger.exception("Synthesis failed for text: %r", text)
            raise TTSError(f"Synthesis failed: {e}") from e

    def _create_empty_wav(self) -> bytes:
        """Create a minimal valid WAV file with no audio data."""
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(DEFAULT_SAMPLE_RATE)
            # No frames written - minimal valid WAV
        return buffer.getvalue()