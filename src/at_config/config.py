import tomllib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

class ConfigError(Exception):
    """Base exception for config errors."""
    pass

class ConfigMissingError(ConfigError):
    """Raised when a required config file is missing."""
    pass

class ConfigParseError(ConfigError):
    """Raised when a config file contains invalid TOML."""
    pass

class ConfigValidationError(ConfigError):
    """Raised when a config file violates schema rules or types."""
    pass

class Config:
    """Read-only configuration loader for the AT voice assistant."""
    
    # Global settings schema
    AT_SCHEMA = {
        "paths": {
            "model_paths": str,
            "sqlite_path": str,
            "socket_dir": str,
            "personality_file": str,
        },
        "logging": {
            "level": str,
            "directory": str,
            "rotation": str,
        },
        "audio": {
            "sample_rate": int,
            "frame_size": int,
        },
        "session": {
            "boundary_duration": int,
        }
    }

    # Per-module settings schema
    MODULES_SCHEMA = {
        "wake_word": {
            "threshold": float,
            "model_path": str,
            "cooldown": float,
        },
        "vad": {
            "sensitivity": float,
            "silence_ms": int,
            "min_speech_ms": int,
        },
        "stt": {
            "model_path": str,
            "threads": int,
            "beam": int,
            "language": str,
        },
        "llm": {
            "model_path": str,
            "threads": int,
            "context_window": int,
            "sampling": {
                "temperature": float,
                "top_p": float,
                "top_k": int,
                "repetition_penalty": float,
            },
            "budgets": {
                "max_tokens": int,
                "max_tokens_elaboration": int,
            },
        },
        "tts": {
            "voice_path": str,
            "length_scale": float,
            "noise_scale": float,
            "noise_w": float,
            "fade_ms": int,
            "playback_command": str,
            },
        "loop": {
            "cooldown": int,
            "acknowledgment_lines": list,
            "empty_response_lines": list,
            "boot_greetings": {
                "morning": str,
                "afternoon": str,
                "evening": str,
                "unclean_shutdown": str,
                "cold_boot": str,
            },
        },
        "ipc": {
            "wake_word": str,
            "vad": str,
            "stt": str,
            "llm": str,
            "tts": str,
            "loop": str,
        }
    }

    def __init__(self, data: dict):
        self._data = data

    @classmethod
    def load(cls, at_path: str = "/etc/at/at.toml", modules_path: str = "/etc/at/modules.toml") -> 'Config':
        """Loads, validates, and merges both config files."""
        at_file = Path(at_path)
        mod_file = Path(modules_path)

        if not at_file.exists():
            raise ConfigMissingError(f"Missing global config file: {at_path}")
        if not mod_file.exists():
            raise ConfigMissingError(f"Missing modules config file: {modules_path}")

        try:
            with open(at_file, "rb") as f:
                at_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigParseError(f"Failed to parse {at_path}: {e}")

        try:
            with open(mod_file, "rb") as f:
                mod_data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigParseError(f"Failed to parse {modules_path}: {e}")

        overlap = set(at_data.keys()) & set(mod_data.keys())
        if overlap:
            raise ConfigValidationError(
                f"Section overlap detected between {at_path} and {modules_path}. "
                f"Conflicting sections: {', '.join(overlap)}"
            )

        cls._validate(at_data, cls.AT_SCHEMA, at_path)
        cls._validate(mod_data, cls.MODULES_SCHEMA, modules_path)

        return cls({**at_data, **mod_data})

    @classmethod
    def _validate(cls, data: dict, schema: dict, file_path: str, prefix: str = ""):
        # Validate schema keys, nested schemas, and exact type matches
        for key_name, expected_type in schema.items():
            if key_name not in data:
                raise ConfigValidationError(f"Missing required key '{prefix}{key_name}' in {file_path}")
            
            val = data[key_name]
            
            if isinstance(expected_type, dict):
                if not isinstance(val, dict):
                    raise ConfigValidationError(f"'{prefix}{key_name}' must be a dictionary/table in {file_path}")
                # Recurse for nested blocks (like llm.sampling)
                cls._validate(val, expected_type, file_path, f"{prefix}{key_name}.")
            else:
                if expected_type is int:
                    if not isinstance(val, int) or isinstance(val, bool):
                        raise ConfigValidationError(f"'{prefix}{key_name}' must be int, got {type(val).__name__}")
                elif expected_type is float:
                    if not isinstance(val, (int, float)) or isinstance(val, bool):
                        raise ConfigValidationError(f"'{prefix}{key_name}' must be float, got {type(val).__name__}")
                elif expected_type is bool:
                    if not isinstance(val, bool):
                        raise ConfigValidationError(f"'{prefix}{key_name}' must be bool, got {type(val).__name__}")
                else:
                    if not isinstance(val, expected_type):
                        raise ConfigValidationError(
                            f"'{prefix}{key_name}' must be {expected_type.__name__}, got {type(val).__name__}"
                        )

        # Warn on unknown keys
        for key_name in data:
            if key_name not in schema:
                logger.warning(f"Unknown key '{prefix}{key_name}' found in {file_path}")

    def get(self, section: str, key: str) -> Any:
        """Retrieves a top-level section key. For nested sections, returns the dict."""
        if section not in self._data:
            raise KeyError(f"Section '{section}' not found in configuration.")
        if key not in self._data[section]:
            raise KeyError(f"Key '{key}' not found in section '{section}'.")
        return self._data[section][key]

    def get_int(self, section: str, key: str) -> int:
        val = self.get(section, key)
        assert isinstance(val, int) and not isinstance(val, bool), f"Type mismatch for [{section}] {key}: expected int, got {type(val).__name__}"
        return val

    def get_float(self, section: str, key: str) -> float:
        val = self.get(section, key)
        assert isinstance(val, (int, float)) and not isinstance(val, bool), f"Type mismatch for [{section}] {key}: expected float, got {type(val).__name__}"
        return val

    def get_str(self, section: str, key: str) -> str:
        val = self.get(section, key)
        assert isinstance(val, str), f"Type mismatch for [{section}] {key}: expected str, got {type(val).__name__}"
        return val

    def get_bool(self, section: str, key: str) -> bool:
        val = self.get(section, key)
        assert isinstance(val, bool), f"Type mismatch for [{section}] {key}: expected bool, got {type(val).__name__}"
        return val

    def get_list(self, section: str, key: str) -> list:
        val = self.get(section, key)
        assert isinstance(val, list), f"Type mismatch for [{section}] {key}: expected list, got {type(val).__name__}"
        return val

    def get_dict(self, section: str, key: str) -> dict:
        val = self.get(section, key)
        assert isinstance(val, dict), f"Type mismatch for [{section}] {key}: expected dict, got {type(val).__name__}"
        return val
