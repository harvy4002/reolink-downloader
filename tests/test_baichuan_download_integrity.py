"""Integrity checks around download()'s final steps: the received size vs.
what the camera reported, and writing the final file atomically so a crash
mid-write can never leave a truncated file at the final name."""

import struct
from datetime import datetime
from pathlib import Path

import pytest

from reolink_downloader.baichuan import BaichuanDownloader, BaichuanError

_M_IFRAME = 0x63643030
_H264_SPS_NAL = b"\x00\x00\x00\x01\x67\x42\x00\x1e"  # a real (minimal) H.264 SPS NAL


def _build_iframe_frame(nal_payload: bytes) -> bytes:
    """A minimal single BCMEDIA IFRAME frame wrapping nal_payload, matching
    the layout deframe_video() expects: magic, unused, payload_size,
    add_hdr, 8 bytes padding, then the payload itself."""
    header = struct.pack("<IIII", _M_IFRAME, 0, len(nal_payload), 0) + b"\x00" * 8
    frame = header + nal_payload
    pad = (8 - len(frame) % 8) % 8
    return frame + b"\x00" * pad


def _fake_media_responses(poff: int = 4):
    """One valid media message (undecryptable-looking prefix + a real
    IFRAME frame) followed by a clean end-of-stream marker."""
    frame_bytes = _build_iframe_frame(_H264_SPS_NAL)
    body = b"\x00" * poff + frame_bytes
    return iter(
        [
            (143, 54, 200, "1464", poff, body),
            (143, 54, 200, "1464", 0, b""),  # end of stream
        ]
    )


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


async def test_successful_download_writes_final_file_with_no_leftover_tmp(monkeypatch, tmp_path):
    downloader = _make_downloader(monkeypatch, _fake_media_responses())

    out_path = tmp_path / "clip"
    written = await downloader.download(
        out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5)
    )

    assert written == out_path.with_suffix(".h264")
    assert written.exists()
    assert not written.with_name(written.name + ".part").exists()


async def test_crash_mid_write_never_leaves_a_file_at_the_final_path(monkeypatch, tmp_path):
    downloader = _make_downloader(monkeypatch, _fake_media_responses())

    def crashing_write_bytes(self, data):
        raise OSError("simulated kill mid-write")

    monkeypatch.setattr(Path, "write_bytes", crashing_write_bytes)

    out_path = tmp_path / "clip"
    with pytest.raises(OSError, match="simulated kill mid-write"):
        await downloader.download(out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5))

    # No truncated file silently sitting at the name resumability checks for.
    assert not out_path.with_suffix(".h264").exists()


async def test_grossly_short_download_raises_despite_clean_end_of_stream(monkeypatch, tmp_path):
    downloader = _make_downloader(monkeypatch, _fake_media_responses())

    out_path = tmp_path / "clip"
    # The frame we send is a few dozen bytes; claim the camera reports this
    # recording as 10 MB, well past the tolerance.
    with pytest.raises(BaichuanError, match="likely an incomplete download"):
        await downloader.download(
            out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5),
            total_size=10_000_000,
        )
    assert not out_path.with_suffix(".h264").exists()


async def test_size_check_skipped_when_total_size_unknown(monkeypatch, tmp_path):
    downloader = _make_downloader(monkeypatch, _fake_media_responses())

    out_path = tmp_path / "clip"
    # No total_size given -- must not fail just because the file is small.
    written = await downloader.download(
        out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5)
    )
    assert written.exists()


async def test_size_check_tolerates_normal_container_overhead_variance(monkeypatch, tmp_path):
    downloader = _make_downloader(monkeypatch, _fake_media_responses())

    out_path = tmp_path / "clip"
    frame_bytes = _build_iframe_frame(_H264_SPS_NAL)
    received_len = 4 + len(frame_bytes)  # poff prefix + frame, roughly what "media" ends up as
    # A reported size within the 50% tolerance band should not raise.
    written = await downloader.download(
        out_path, start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 0, 5),
        total_size=int(received_len * 1.5),
    )
    assert written.exists()
