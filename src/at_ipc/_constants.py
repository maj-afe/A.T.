"""
Shared constants and default configuration values for at-ipc.
"""

from pathlib import Path

DEFAULT_SOCKET_DIR = Path("/run/at")
SOCKET_PERMISSIONS = 0o600

# Timeouts in seconds
DEFAULT_COMMAND_TIMEOUT = 10.0
CONNECTION_TIMEOUT = 2.0
HEADER_READ_TIMEOUT = 30.0
RAW_PAYLOAD_READ_TIMEOUT = 3.0
BROADCAST_WRITE_TIMEOUT = 0.1

# Size thresholds and buffer limits
MAX_JSON_SIZE = 64 * 1024           # 64 KB — spec threshold for control messages
MAX_PAYLOAD_SIZE = 10 * 1024 * 1024 # 10 MB — defensive ceiling for raw payload
STREAM_READER_LIMIT = 1_048_576     # 1 MB — buffer limit for StreamReader lines

# Message typing and routing
MSG_TYPES = frozenset({"event", "command", "response", "error"})
BROADCAST_TARGET = "*"
