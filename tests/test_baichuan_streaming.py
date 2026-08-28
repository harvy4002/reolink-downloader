"""Streaming-download behavior: frames split across chunk/message
boundaries must still decode correctly, and download() must write video to
disk incrementally (not buffer the whole recording) while still producing
byte-identical output to the old buffer-everything approach."""

import struct
from datetime import datetime

import pytest

from reolink_downloader.baichuan import BaichuanDownloader, _StreamingDeframer

_M_IFRAME = 0x63643030
_H264_SPS_NAL = b"\x00\x00\x00\x01\x67\x42\x00\x1e"  # a real (minimal) H.264 SPS NAL


def _build_iframe_frame(nal_payload: bytes) -> bytes:
    """A minimal single BCMEDIA IFRAME frame wrapping nal_payload, matching
    the layout _StreamingDeframer expects: magic, unused, payload_size,
    add_hdr, 8 bytes padding, then the payload itself."""
    header = struct.pack("<IIII", _M_IFRAME, 0, len(nal_payload), 0) + b"\x00" * 8
    frame = header + nal_payload
    pad = (8 - len(frame) % 8) % 8
    return frame + b"\x00" * pad


def _make_downloader(monkeypatch, responses):
    downloader = BaichuanDownloader("127.0.0.1", "user", "pass")
    downloader._key = b"0123456789abcdef"

    async def fake_send(*args, **kwargs):
        return None

    async def fake_read_message():
        return next(responses)

    monkeypatch.setattr(downloader, "_send", fake_send)
    monkeypatch.setattr(downloader, "_read_message", fake_read_message)
    return downloader


def test_streaming_deframer_reassembles_a_frame_split_across_two_feeds():
    frame = _build_iframe_frame(_H264_SPS_NAL)
    split = len(frame) // 2

    deframer = _StreamingDeframer()
    first = deframer.feed(frame[:split])
    assert bytes(first) == b""  # incomplete frame: nothing decoded yet

    second = deframer.feed(frame[split:])
    assert bytes(second) == _H264_SPS_NAL


def test_streaming_deframer_matches_single_shot_feed():
    frame = _build_iframe_frame(_H264_SPS_NAL)

    whole = _StreamingDeframer().feed(frame)

    piecemeal = _StreamingDeframer()
    out = bytearray()
    for i in range(0, len(frame), 3):  # arbitrary small chunk size
        out += piecemeal.feed(frame[i : i + 3])

    assert bytes(whole) == bytes(out) == _H264_SPS_NAL


async def test_download_reassembles_a_frame_split_across_two_media_messages(monkeypatch, tmp_path):
    frame = _build_iframe_frame(_H264_SPS_NAL)
    split = len(frame) // 2
    poff = 4
    responses = iter(
        [
            (143, 54, 200, "1464", poff, b"\x00" * poff + frame[:split]),
            (143, 54, 200, "1464", poff, b"\x00" * poff + frame[split:]),
            (143, 54, 200, "1464", 0, b""),  # end of stream
        ]
    )
    downloader = _make_downloader(monkeypatch, responses)

    out_path = tmp_path / "clip"
    written = await downloader.download(
        out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5)
    )

    assert written == out_path.with_suffix(".h264")
    assert written.read_bytes() == _H264_SPS_NAL


async def test_download_streams_video_larger_than_the_codec_sniff_prefix(monkeypatch, tmp_path):
    import reolink_downloader.baichuan as bc_module

    monkeypatch.setattr(bc_module, "CODEC_SNIFF_PREFIX_BYTES", 64)

    filler_frames = [_build_iframe_frame(bytes([i % 256]) * 40) for i in range(10)]
    all_frames = [_build_iframe_frame(_H264_SPS_NAL), *filler_frames]

    responses = iter(
        [(143, 54, 200, "1464", 4, b"\x00\x00\x00\x00" + f) for f in all_frames]
        + [(143, 54, 200, "1464", 0, b"")]
    )
    downloader = _make_downloader(monkeypatch, responses)

    out_path = tmp_path / "clip"
    written = await downloader.download(
        out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5)
    )

    expected = _H264_SPS_NAL + b"".join(bytes([i % 256]) * 40 for i in range(10))
    assert written.read_bytes() == expected
