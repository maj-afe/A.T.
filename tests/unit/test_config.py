import pytest
import textwrap
import logging
from at_config import Config, ConfigMissingError, ConfigParseError, ConfigValidationError

@pytest.fixture
def valid_at_content():
    return textwrap.dedent("""
        [paths]
        model_paths = "/opt/at/models"
        sqlite_path = "/var/lib/at/memory.db"
        socket_dir = "/run/at"
        personality_file = "/etc/at/personality.md"
        [logging]
        level = "INFO"
        directory = "/var/log/at"
        rotation = "10MB"
        [audio]
        sample_rate = 16000
        frame_size = 480
        [session]
        boundary_duration = 1800
    """)

@pytest.fixture
def valid_modules_content():
    return textwrap.dedent("""
        [wake_word]
        threshold = 0.5
        model_path = "/opt/at/models/wake-word/at.onnx"
        cooldown = 0.4
        [vad]
        sensitivity = 0.5
        silence_ms = 750
        min_speech_ms = 250
        [stt]
        model_path = "/opt/at/models/stt/base.en.bin"
        threads = 3
        beam = 1
        language = "en"
        [llm]
        model_path = "/opt/at/models/llm/qwen2.5-3b-instruct-q4_k_m.gguf"
        threads = 6
        context_window = 16384
        [llm.sampling]
        temperature = 0.7
        top_p = 0.9
        top_k = 40
        repetition_penalty = 1.1
        [llm.budgets]
        max_tokens = 150
        max_tokens_elaboration = 400
        [tts]
        voice_path = "/opt/at/models/tts/en_GB-alan-medium.onnx"
        length_scale = 1.1
        fade_ms = 100
        playback_command = "paplay"
        [loop]
        cooldown = 400
        empty_response_lines = ["No audio, sir."]
        acknowledgment_lines = []
        [loop.boot_greetings]
        morning = "Good morning, sir."
        afternoon = "Good afternoon, sir."
        evening = "Good evening, sir."
        unclean_shutdown = "I'm here, sir."
        cold_boot = "All systems nominal, sir."
        [ipc]
        wake_word = "wakeword.sock"
        vad = "vad.sock"
        stt = "stt.sock"
        llm = "llm.sock"
        tts = "tts.sock"
        loop = "loop.sock"
    """)

def write_configs(tmp_path, at_str, mod_str):
    at_path = tmp_path / "at.toml"
    mod_path = tmp_path / "modules.toml"
    at_path.write_text(at_str)
    mod_path.write_text(mod_str)
    return str(at_path), str(mod_path)

def test_valid_load(tmp_path, valid_at_content, valid_modules_content):
    at, mod = write_configs(tmp_path, valid_at_content, valid_modules_content)
    cfg = Config.load(at, mod)
    
    assert cfg.get_int("audio", "sample_rate") == 16000
    assert cfg.get_float("wake_word", "cooldown") == 0.4
    assert cfg.get_dict("llm", "sampling")["top_k"] == 40

def test_missing_at_file_only(tmp_path, valid_modules_content):
    at = str(tmp_path / "at.toml")
    _, mod = write_configs(tmp_path, "", valid_modules_content)
    (tmp_path / "at.toml").unlink() # Ensure it doesn't exist
    
    with pytest.raises(ConfigMissingError, match="Missing global config file"):
        Config.load(at, mod)

def test_missing_modules_file_only(tmp_path, valid_at_content):
    at, _ = write_configs(tmp_path, valid_at_content, "")
    mod = str(tmp_path / "modules.toml")
    (tmp_path / "modules.toml").unlink() # Ensure it doesn't exist
    
    with pytest.raises(ConfigMissingError, match="Missing modules config file"):
        Config.load(at, mod)

def test_parse_error(tmp_path, valid_modules_content):
    at, mod = write_configs(tmp_path, "[paths]\nbad_toml = ", valid_modules_content)
    with pytest.raises(ConfigParseError, match="Failed to parse"):
        Config.load(at, mod)

def test_missing_key(tmp_path, valid_at_content, valid_modules_content):
    invalid_at = valid_at_content.replace("sample_rate = 16000", "")
    at, mod = write_configs(tmp_path, invalid_at, valid_modules_content)
    with pytest.raises(ConfigValidationError, match="Missing required key '.*sample_rate'"):
        Config.load(at, mod)

def test_bool_rejected_as_float(tmp_path, valid_at_content, valid_modules_content):
    invalid_modules = valid_modules_content.replace("length_scale = 1.1", "length_scale = true")
    at, mod = write_configs(tmp_path, valid_at_content, invalid_modules)
    with pytest.raises(ConfigValidationError, match=".*length_scale.*must be float"):
        Config.load(at, mod)

def test_section_overlap(tmp_path, valid_at_content, valid_modules_content):
    overlap_modules = valid_modules_content + "\n[paths]\noverlap = true\n"
    at, mod = write_configs(tmp_path, valid_at_content, overlap_modules)
    with pytest.raises(ConfigValidationError, match="Section overlap detected"):
        Config.load(at, mod)

def test_unknown_key_warns(tmp_path, valid_at_content, valid_modules_content, caplog):
    warning_at = valid_at_content + "\nunknown_key = 'ignored'\n"
    at, mod = write_configs(tmp_path, warning_at, valid_modules_content)
    
    with caplog.at_level(logging.WARNING):
        Config.load(at, mod)
    
    assert "unknown_key" in caplog.text

def test_accessor_types(tmp_path, valid_at_content, valid_modules_content):
    at, mod = write_configs(tmp_path, valid_at_content, valid_modules_content)
    cfg = Config.load(at, mod)
    
    assert isinstance(cfg.get_int("audio", "frame_size"), int)
    assert isinstance(cfg.get_float("tts", "length_scale"), float)
    assert isinstance(cfg.get_str("logging", "level"), str)
    assert isinstance(cfg.get_list("loop", "empty_response_lines"), list)
    assert isinstance(cfg.get_dict("llm", "sampling"), dict)
    
    with pytest.raises(KeyError):
        cfg.get("nonexistent", "key")