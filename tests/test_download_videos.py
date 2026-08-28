from datetime import datetime
from pathlib import Path

import reolink_downloader as rd
from conftest import FakeVodFile, RecordingNotifier, make_fake_baichuan_cls, make_fake_host_class


def _recordings(*, channel, stream, names_and_hours):
    return [
        FakeVodFile(f"{name}.mp4", datetime(2024, 1, 15, hour, 0), datetime(2024, 1, 15, hour, 5))
        for name, hour in names_and_hours
    ]


async def test_multi_channel_search_and_download(tmp_path, monkeypatch):
    # Three channels: 0 is plain wide-only, 1 has a telephoto lens, 2 has no
    # recordings at all in range. One file on channel 0 ("b") is set up to
    # fail so we can confirm a per-file failure doesn't stop the run.
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_b", 2)]),
        (1, "main"): _recordings(channel=1, stream="main", names_and_hours=[("ch1_wide_a", 3)]),
        (1, "telephoto_main"): _recordings(channel=1, stream="telephoto_main", names_and_hours=[("ch1_tele_a", 3)]),
    }
    fake_host_cls = make_fake_host_class(
        channels=[0, 1, 2],
        dual_lens_channels=[1],
        names={0: "Front Door", 1: "Backyard", 2: "Garage"},
        recordings=recordings,
    )
    fake_bc_cls = make_fake_baichuan_cls(fail_predicate=lambda name: name.endswith("_b"))
    notifier = RecordingNotifier()

    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide", "telephoto"],
        channel_spec="all",
        concurrency=2,
        notifier=notifier,
    )

    # Per-channel output directories, no filename collisions across channels.
    dirs = sorted(p.name for p in tmp_path.iterdir())
    assert dirs == ["ch00_Front-Door", "ch01_Backyard"]

    ch0_files = sorted(p.name for p in (tmp_path / "ch00_Front-Door").iterdir())
    ch1_files = sorted(p.name for p in (tmp_path / "ch01_Backyard").iterdir())
    assert any("ch0_a" in f for f in ch0_files)
    assert not any("ch0_b" in f for f in ch0_files)  # the failed download wrote nothing
    assert any("ch1_wide_a" in f for f in ch1_files)
    assert any("ch1_tele_a" in f for f in ch1_files)  # telephoto only searched on channel 1

    # Channels 0 and 2 never got a telephoto file at all (no autotrack
    # capability, so resolve_lenses_for_channel narrowed them to wide-only).
    assert {n for c, n in fake_bc_cls.calls if "ch0" in n} == {
        "20240115_010000_wide_ch0_a",
        "20240115_020000_wide_ch0_b",
    }
    # The one telephoto download that did happen (from channel 1) used
    # Baichuan download_channel = channel + 1, per the app's documented
    # (if unverified beyond channel 0) convention.
    [tele_call] = [(c, n) for c, n in fake_bc_cls.calls if "tele" in n]
    assert tele_call == (2, "20240115_030000_telephoto_ch1_tele_a")

    # Telegram: start, one error, an overall-progress update, and a final
    # summary were all recorded.
    joined = "\n".join(notifier.messages)
    assert "starting run" in joined
    assert "failed to download" in joined
    assert "Overall progress" in joined
    assert "finished run" in joined

    # Search results across all 3 channels are grouped into a single
    # message, not one message per channel.
    search_messages = [m for m in notifier.messages if "Search complete" in m]
    assert len(search_messages) == 1
    assert "ch0 (Front Door): 2 found" in search_messages[0]
    assert "ch1 (Backyard): 2 found" in search_messages[0]
    assert "ch2 (Garage): 0 found" in search_messages[0]


async def test_invalid_channel_returns_without_raising(tmp_path, monkeypatch, capsys):
    fake_host_cls = make_fake_host_class(channels=[0, 1])
    fake_bc_cls = make_fake_baichuan_cls()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec={5},  # doesn't exist on this fake device
    )

    assert "not found" in capsys.readouterr().err
    assert fake_bc_cls.calls == []
    assert list(tmp_path.iterdir()) == []


async def test_limit_is_a_global_cap_across_channels(tmp_path, monkeypatch):
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_b", 2)]),
        (1, "main"): _recordings(channel=1, stream="main", names_and_hours=[("ch1_a", 1), ("ch1_b", 2)]),
    }
    fake_host_cls = make_fake_host_class(channels=[0, 1], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        concurrency=1,
        limit=2,
    )

    # Deterministic: sorted by (channel, start_time), so the first 2 chosen
    # are channel 0's two files, not one from each channel.
    assert fake_bc_cls.calls == [(0, "20240115_010000_wide_ch0_a"), (0, "20240115_020000_wide_ch0_b")]


async def test_no_recordings_sends_zero_count_finish(tmp_path, monkeypatch):
    fake_host_cls = make_fake_host_class(channels=[0])
    fake_bc_cls = make_fake_baichuan_cls()
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        notifier=notifier,
    )

    assert fake_bc_cls.calls == []
    assert any("Found: 0, downloaded: 0, failed: 0" in m for m in notifier.messages)


async def test_transient_failure_recovers_via_retry(tmp_path, monkeypatch):
    # This file fails its first 2 attempts, then succeeds on the 3rd — well
    # within the 1 + MAX_RETRIES(3) = 4 attempt budget, so the run should
    # report it as downloaded, not failed, and never notify Telegram of an
    # error for it.
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("flaky", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(fail_times={"20240115_010000_wide_flaky": 2})
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        concurrency=1,
        notifier=notifier,
    )

    assert fake_bc_cls.attempt_counts["20240115_010000_wide_flaky"] == 3
    files = list((tmp_path / "ch00_Cam0").glob("*.mp4"))
    assert len(files) == 1
    assert not any("failed to download" in m for m in notifier.messages)
    assert any("Found: 1, downloaded: 1, failed: 0" in m for m in notifier.messages)


async def test_permanent_failure_reported_after_exhausting_retries(tmp_path, monkeypatch):
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("dead", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(fail_predicate=lambda name: True)
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        concurrency=1,
        notifier=notifier,
    )

    # 1 initial attempt + MAX_RETRIES(3) retries = 4 total attempts, then give up.
    assert fake_bc_cls.attempt_counts["20240115_010000_wide_dead"] == rd.MAX_RETRIES + 1
    assert list((tmp_path / "ch00_Cam0").glob("*.mp4")) == []
    error_messages = [m for m in notifier.messages if "failed to download" in m]
    assert len(error_messages) == 1
    assert "4 attempts" in error_messages[0]


async def test_worker_reconnects_after_transient_connection_failure(tmp_path, monkeypatch):
    # The worker's initial connection fails twice before succeeding — well
    # within the retry budget — so the job it was about to process still
    # completes rather than the whole run silently downloading nothing.
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("a", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(connect_fail_times=2)
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        concurrency=1,
    )

    files = list((tmp_path / "ch00_Cam0").glob("*.mp4"))
    assert len(files) == 1


async def test_worker_gives_up_after_exhausting_reconnect_attempts(tmp_path, monkeypatch, capsys):
    # The connection never succeeds; with concurrency=1 there's no sibling
    # worker to pick up the slack, so the job is never attempted at all —
    # but the run must still finish cleanly (no hang, no crash).
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("a", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(connect_fail_times=100)
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    await rd.download_videos(
        ip="10.0.0.5",
        username="u",
        password="p",
        start_time=datetime(2024, 1, 15, 0, 0),
        end_time=datetime(2024, 1, 15, 23, 59),
        output_dir=tmp_path,
        lenses=["wide"],
        channel_spec="all",
        concurrency=1,
    )

    assert fake_bc_cls.calls == []
    assert "giving up on this worker" in capsys.readouterr().err
