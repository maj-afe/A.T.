"""
Message envelope definition, serialization, and validation for at-ipc.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any
import uuid

from ._constants import MAX_JSON_SIZE, MAX_PAYLOAD_SIZE, MSG_TYPES
from .errors import IPCProtocolError


@dataclass
class Envelope:
    """Standardized message envelope for all AT IPC communications."""

    from_module: str
    to_module: str
    type: str
    name: str
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = ""
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    payload: dict[str, Any] = field(default_factory=dict)
    bytes_length: int | None = None


def validate_envelope(env: Envelope) -> None:
    """
    Validate that an Envelope satisfies protocol constraints.

    Raises:
        IPCProtocolError: If any field fails validation.
    """
    if not isinstance(env.msg_id, str) or not env.msg_id.strip():
        raise IPCProtocolError("msg_id must be a non-empty string")

    if not isinstance(env.from_module, str) or not env.from_module.strip():
        raise IPCProtocolError("from_module ('from') must be a non-empty string")

    if not isinstance(env.to_module, str) or not env.to_module.strip():
        raise IPCProtocolError("to_module ('to') must be a non-empty string")

    if env.type not in MSG_TYPES:
        raise IPCProtocolError(
            f"type must be one of {sorted(MSG_TYPES)}, got {env.type!r}"
        )

    if not isinstance(env.name, str) or not env.name.strip():
        raise IPCProtocolError("name must be a non-empty string")

    if not isinstance(env.timestamp, str) or not env.timestamp.strip():
        raise IPCProtocolError("timestamp must be a non-empty string")

    if not isinstance(env.payload, dict):
        raise IPCProtocolError(
            f"payload must be a dictionary/object, got {type(env.payload).__name__}"
        )

    if env.correlation_id is not None and not isinstance(env.correlation_id, str):
        raise IPCProtocolError("correlation_id must be a string")

    if env.bytes_length is not None:
        if not isinstance(env.bytes_length, int) or isinstance(env.bytes_length, bool):
            raise IPCProtocolError("bytes_length ('bytes') must be an integer")
        if env.bytes_length < 0:
            raise IPCProtocolError("bytes_length ('bytes') cannot be negative")
        if env.bytes_length > MAX_PAYLOAD_SIZE:
            raise IPCProtocolError(
                f"bytes_length ({env.bytes_length}) exceeds maximum limit ({MAX_PAYLOAD_SIZE})"
            )


def create_envelope(
    from_module: str,
    to_module: str,
    type: str,
    name: str,
    payload: dict[str, Any] | None = None,
    correlation_id: str = "",
    bytes_length: int | None = None,
    msg_id: str | None = None,
    timestamp: str | None = None,
) -> Envelope:
    """Factory to create and validate an Envelope."""
    env = Envelope(
        from_module=from_module,
        to_module=to_module,
        type=type,
        name=name,
        msg_id=msg_id if msg_id is not None else str(uuid.uuid4()),
        correlation_id=correlation_id,
        timestamp=(
            timestamp
            if timestamp is not None
            else datetime.now(timezone.utc).isoformat()
        ),
        payload=payload if payload is not None else {},
        bytes_length=bytes_length,
    )
    validate_envelope(env)
    return env


def envelope_to_dict(env: Envelope) -> dict[str, Any]:
    """
    Serialize an Envelope into a wire-ready dictionary.

    Validates that the resulting JSON header does not exceed MAX_JSON_SIZE.
    """
    validate_envelope(env)
    d: dict[str, Any] = {
        "msg_id": env.msg_id,
        "correlation_id": env.correlation_id,
        "from": env.from_module,
        "to": env.to_module,
        "type": env.type,
        "name": env.name,
        "timestamp": env.timestamp,
        "payload": env.payload,
    }
    if env.bytes_length is not None:
        d["bytes"] = env.bytes_length

    # Ensure JSON-encoded header line does not exceed MAX_JSON_SIZE
    try:
        encoded = json.dumps(d, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IPCProtocolError(f"Envelope payload is not JSON serializable: {exc}") from exc

    if len(encoded) > MAX_JSON_SIZE:
        raise IPCProtocolError(
            f"Serialized message header ({len(encoded)} bytes) exceeds MAX_JSON_SIZE ({MAX_JSON_SIZE} bytes)"
        )

    return d


def dict_to_envelope(d: dict[str, Any]) -> Envelope:
    """
    Deserialize a wire dictionary into an Envelope.

    Raises:
        IPCProtocolError: If required keys are missing or invalid.
    """
    if not isinstance(d, dict):
        raise IPCProtocolError(f"Expected dict header, got {type(d).__name__}")

    required_keys = {"msg_id", "from", "to", "type", "name", "timestamp", "payload"}
    missing = required_keys - set(d.keys())
    if missing:
        raise IPCProtocolError(f"Missing required envelope fields: {sorted(missing)}")

    correlation_id = d.get("correlation_id", "")
    if correlation_id is None:
        correlation_id = ""

    bytes_length = d.get("bytes")

    env = Envelope(
        msg_id=d["msg_id"],
        correlation_id=correlation_id,
        from_module=d["from"],
        to_module=d["to"],
        type=d["type"],
        name=d["name"],
        timestamp=d["timestamp"],
        payload=d["payload"],
        bytes_length=bytes_length,
    )
    validate_envelope(env)
    return env
