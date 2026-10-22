"""
Unit tests for audio-source PCM framing (pure logic, no subprocess).
"""

from audio_source.capture import FRAME_BYTES, FrameAssembler


def test_arbitrary_read_sizes_assemble_into_whole_frames():
    # Catches: dropping or coalescing bytes when pipe reads are not 960-byte
    # aligned, which would shift every subsequent frame boundary.
    assembler = FrameAssembler()
    source = bytes(i % 256 for i in range(FRAME_BYTES * 3))

    frames: list[bytes] = []
    offset = 0
    for size in (300, 700, 100, 960, 480):
        frames.extend(assembler.feed(source[offset : offset + size]))
        offset += size

    # 300+700+100+960+480 = 2540 bytes = 2 whole frames + 620 buffered bytes.
    assert offset == 2540
    assert len(frames) == 2
    assert all(len(frame) == FRAME_BYTES for frame in frames)
    assert assembler.buffered_bytes == 620
    assert frames == [
        source[0:FRAME_BYTES],
        source[FRAME_BYTES : 2 * FRAME_BYTES],
    ]


def test_partial_trailing_frame_discarded_at_eof():
    # Catches: broadcasting a short (partial) frame when arecord exits mid-frame.
    assembler = FrameAssembler()

    # 500 bytes alone must never produce a frame.
    assert assembler.feed(b"\x02" * 500) == []
    assert assembler.buffered_bytes == 500
    assert assembler.discard_partial() == 500
    assert assembler.buffered_bytes == 0

    # 960 + 500 bytes -> exactly one frame, 500 bytes left pending.
    frames = assembler.feed(b"\x01" * FRAME_BYTES + b"\x02" * 500)
    assert frames == [b"\x01" * FRAME_BYTES]
    assert assembler.buffered_bytes == 500

    # EOF: the partial frame is discarded, not emitted.
    assert assembler.discard_partial() == 500
    assert assembler.buffered_bytes == 0
    assert assembler.feed(b"") == []


def test_frames_are_byte_exact_regardless_of_chunking():
    # Catches: off-by-one frame boundaries silently corrupting every PCM sample.
    source = bytes((i * 7) % 256 for i in range(FRAME_BYTES * 4))
    assembler = FrameAssembler()

    frames: list[bytes] = []
    chunk_sizes = (1, 17, 959, 2, 3000, 61)
    offset = 0
    index = 0
    while offset < len(source):
        size = chunk_sizes[index % len(chunk_sizes)]
        frames.extend(assembler.feed(source[offset : offset + size]))
        offset += size
        index += 1

    assert len(frames) == 4
    assert assembler.buffered_bytes == 0
    for frame_index, frame in enumerate(frames):
        expected = source[
            frame_index * FRAME_BYTES : (frame_index + 1) * FRAME_BYTES
        ]
        assert frame == expected
        assert len(frame) == FRAME_BYTES
