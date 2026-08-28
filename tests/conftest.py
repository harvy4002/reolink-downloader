"""Shared fakes/fixtures for reolink_downloader tests.

These stand in for reolink_aio.api.Host and BaichuanDownloader so the async
orchestration in download_videos() can be exercised without a real NVR.
"""

from __future__ import annotations

import pytest

import reolink_downloader as rd
from reolink_downloader.baichuan import BaichuanError
from reolink_downloader.telegram import TelegramNotifier


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """download_videos() sleeps RETRY_DELAY_SECONDS between retry attempts;
    zero it out so tests exercising retry/reconnect logic run instantly."""
    monkeypatch.setattr(rd, "RETRY_DELAY_SECONDS", 0)


class FakeStatus:
    def __init__(self, year, month, days):
        self.year = year
        self.month = month
        self.days = days


class FakeVodFile:
    def __init__(self, name, start, end, size=10_000_000):
        self.file_name = name
        self.start_time = start
        self.end_time = end
        self.size = size


def make_fake_host_class(*, channels, is_nvr=True, dual_lens_channels=(), names=None, recordings=None):
    """Build a fake reolink_aio.api.Host replacement.

    recordings: dict[(channel, stream_name), list[FakeVodFile]]
    """
    names = names or {c: f"Cam{c}" for c in channels}
    recordings = recordings or {}

    class FakeHost:
        def __init__(self, ip, username, password):
            self.ip = ip
            self.nvr_name = "TestNVR"
            self.channels = list(channels)
            self.is_nvr = is_nvr
            self.stream_channels = list(channels)

        async def get_host_data(self):
            return None

        def camera_name(self, channel):
            return names[channel]

        def supported(self, channel, capability):
            return capability == "autotrack_stream" and channel in dual_lens_channels

        async def request_vod_files(self, channel, start, end, status_only, stream):
            files = recordings.get((channel, stream), [])
            if not files:
                return [], []
            if status_only:
                year = files[0].start_time.year
                month = files[0].start_time.month
                days = sorted({f.start_time.day for f in files})
                return [FakeStatus(year, month, days)], []
            day_files = [f for f in files if start <= f.start_time <= end]
            return None, day_files

        async def logout(self):
            return None

    return FakeHost


def make_fake_baichuan_cls(*, fail_predicate=lambda name: False, fail_times=None, connect_fail_times=0):
    """Build a fake BaichuanDownloader replacement. Records (channel, name)
    for every download() call in the returned class's `.calls` list, and
    simulates a couple of progress ticks when total_size is known.

    fail_predicate(name): if True, this job's download() always raises
        (permanent failure regardless of retries).
    fail_times: dict[out_path.name, N] — this job's first N download()
        attempts raise BaichuanError, then it succeeds (simulates a
        transient failure that auto-recovery retries past).
    connect_fail_times: the first N BaichuanDownloader connections (across
        the whole fake class, i.e. the worker's initial connection and any
        retry reconnections) raise on __aenter__, then succeed — simulates
        a flaky NVR connection.
    """
    calls: list[tuple[int, str]] = []
    attempt_counts: dict[str, int] = {}
    fail_times = fail_times or {}
    connect_state = {"count": 0}

    class FakeBaichuanDownloader:
        def __init__(self, ip, username, password, debug=False):
            self.ip = ip

        async def __aenter__(self):
            connect_state["count"] += 1
            if connect_state["count"] <= connect_fail_times:
                raise BaichuanError(f"simulated connect failure #{connect_state['count']}")
            return self

        async def __aexit__(self, *exc):
            return None

        async def download(self, out_path, *, start, end, channel, stream_type, total_size=None, on_progress=None):
            calls.append((channel, out_path.name))
            attempt_counts[out_path.name] = attempt_counts.get(out_path.name, 0) + 1
            if on_progress is not None and total_size:
                on_progress(total_size // 2, total_size)
                on_progress(total_size, total_size)
            required_failures = fail_times.get(out_path.name, 0)
            if attempt_counts[out_path.name] <= required_failures or fail_predicate(out_path.name):
                raise BaichuanError(f"simulated failure (attempt {attempt_counts[out_path.name]})")
            out_path.with_suffix(".mp4").write_bytes(b"fake")
            return out_path.with_suffix(".mp4")

    FakeBaichuanDownloader.calls = calls
    FakeBaichuanDownloader.attempt_counts = attempt_counts
    return FakeBaichuanDownloader


class RecordingNotifier(TelegramNotifier):
    """A TelegramNotifier that records formatted messages instead of making
    network calls, so tests can assert on notification content without
    mocking aiohttp."""

    def __init__(self):
        super().__init__(bot_token="test-token", chat_id="test-chat")
        self.messages: list[str] = []

    async def _send(self, text: str) -> None:
        self.messages.append(text)
