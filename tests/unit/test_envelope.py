"""
Unit tests for at_ipc.envelope.
"""

from datetime import datetime, timezone
import pytest

from at_ipc._constants import MAX_JSON_SIZE, MAX_PAYLOAD_SIZE
from at_ipc.envelope import (
    Envelope,
    create_envelope,
    dict_to_envelope,
    envelope_to_dict,
    validate_envelope,
)
from at_ipc.errors import IPCProtocolError


def test_envelope_round_trip():
    """Test full serialize/deserialize round-trip with all fields."""
    env = create_envelope(
        from_module="audio_source",
        to_module="vad",
        type="event",
        name="audio_chunk",
        payload={"frame_id": 42, "samples": 480},
        correlation_id="corr-123",
        bytes_length=960,
    )
    d = envelope_to_dict(env)
    assert d["from"] == "audio_source"
    assert d["to"] == "vad"
    assert d["type"] == "event"
    assert d["name"] == "audio_chunk"
    assert d["correlation_id"] == "corr-123"
    assert d["payload"] == {"frame_id": 42, "samples": 480}
    assert d["bytes"] == 960

    restored = dict_to_envelope(d)
    assert restored.from_module == env.from_module
    assert restored.to_module == env.to_module
    assert restored.type == env.type
    assert restored.name == env.name
    assert restored.msg_id == env.msg_id
    assert restored.correlation_id == env.correlation_id
    assert restored.timestamp == env.timestamp
    assert restored.payload == env.payload
    assert restored.bytes_length == env.bytes_length


def test_envelope_defaults():
    """Test automatic generation of msg_id and UTC timestamp."""
    env = create_envelope(
        from_module="loop",
        to_module="tts",
        type="command",
        name="speak",
    )
    assert env.msg_id
    assert len(env.msg_id) == 36  # UUID format
    assert env.correlation_id == ""
    assert env.payload == {}
    assert env.bytes_length is None

    # Timestamp must parse as valid ISO 8601 UTC
    dt = datetime.fromisoformat(env.timestamp)
    assert dt.tzinfo is not None

    d = envelope_to_dict(env)
    assert "bytes" not in d


def test_invalid_message_type():
    """Invalid message types must raise IPCProtocolError."""
    with pytest.raises(IPCProtocolError, match="type must be one of"):
        create_envelope("m1", "m2", "invalid_type", "test")


@pytest.mark.parametrize(
    "kwargs,err_match",
    [
        ({"from_module": ""}, "from_module"),
        ({"to_module": "  "}, "to_module"),
        ({"name": ""}, "name"),
        ({"msg_id": " "}, "msg_id"),
        ({"timestamp": ""}, "timestamp"),
        ({"payload": "not_a_dict"}, "payload"),
        ({"bytes_length": -1}, "bytes_length"),
        ({"bytes_length": True}, "bytes_length"),
        ({"bytes_length": MAX_PAYLOAD_SIZE + 1}, "exceeds maximum limit"),
    ],
)
def test_validation_constraints(kwargs, err_match):
    """Ensure invalid field values trigger validation errors."""
    valid_args = {
        "from_module": "sender",
        "to_module": "receiver",
        "type": "event",
        "name": "notify",
        "payload": {},
    }
    valid_args.update(kwargs)
    with pytest.raises(IPCProtocolError, match=err_match):
        create_envelope(**valid_args)


def test_dict_to_envelope_missing_keys():
    """Missing required keys in dictionary must raise IPCProtocolError."""
    incomplete = {
        "msg_id": "123",
        "from": "a",
        "to": "b",
        "type": "event",
        # missing name, timestamp, payload
    }
    with pytest.raises(IPCProtocolError, match="Missing required envelope fields"):
        dict_to_envelope(incomplete)


def test_header_exceeds_max_json_size():
    """Header exceeding MAX_JSON_SIZE must be rejected."""
    huge_payload = {"data": "x" * (MAX_JSON_SIZE + 100)}
    env = Envelope(
        from_module="a",
        to_module="b",
        type="event",
        name="test",
        payload=huge_payload,
    )
    with pytest.raises(IPCProtocolError, match="exceeds MAX_JSON_SIZE"):
        envelope_to_dict(env)
