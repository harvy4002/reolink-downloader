import asyncio
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

    # <output>/<date>/ch{NN}_<name>/ output directories, no filename
    # collisions across channels.
    dirs = sorted(p.name for p in tmp_path.iterdir())
    assert dirs == ["2024-01-15"]
    channel_dirs = sorted(p.name for p in (tmp_path / "2024-01-15").iterdir())
    assert channel_dirs == ["ch00_Front-Door", "ch01_Backyard"]

    ch0_files = sorted(p.name for p in (tmp_path / "2024-01-15" / "ch00_Front-Door").iterdir())
    ch1_files = sorted(p.name for p in (tmp_path / "2024-01-15" / "ch01_Backyard").iterdir())
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


async def test_downloads_are_ordered_oldest_first_across_channels(tmp_path, monkeypatch):
    # ch0's second file is older than ch1's first file, so a naive
    # per-channel grouping (all of ch0 before any of ch1) would download it
    # out of chronological order. NVR retention deletes the oldest footage
    # first once storage fills up, independently per channel, so the oldest
    # recordings anywhere are the most at risk — they must come first
    # regardless of which channel they're on.
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_old", 1), ("ch0_new", 4)]),
        (1, "main"): _recordings(channel=1, stream="main", names_and_hours=[("ch1_mid", 2), ("ch1_newer", 3)]),
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
    )

    assert fake_bc_cls.calls == [
        (0, "20240115_010000_wide_ch0_old"),
        (1, "20240115_020000_wide_ch1_mid"),
        (1, "20240115_030000_wide_ch1_newer"),
        (0, "20240115_040000_wide_ch0_new"),
    ]


async def test_limit_is_a_global_cap_across_channels(tmp_path, monkeypatch):
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_b", 3)]),
        (1, "main"): _recordings(channel=1, stream="main", names_and_hours=[("ch1_a", 2), ("ch1_b", 4)]),
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

    # Global cap over the oldest-first-across-channels order: the 2 oldest
    # recordings overall, not 2-per-channel or channel-grouped.
    assert fake_bc_cls.calls == [(0, "20240115_010000_wide_ch0_a"), (1, "20240115_020000_wide_ch1_a")]


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
    files = list((tmp_path / "2024-01-15" / "ch00_Cam0").glob("*.mp4"))
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
    assert list((tmp_path / "2024-01-15" / "ch00_Cam0").glob("*.mp4")) == []
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

    files = list((tmp_path / "2024-01-15" / "ch00_Cam0").glob("*.mp4"))
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


async def test_resumes_by_skipping_files_already_on_disk(tmp_path, monkeypatch):
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_b", 2)]),
    }
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls()
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    # Simulate a previous run that already downloaded ch0_a (as a .h264,
    # since real downloads never get a .mp4 extension anymore).
    channel_dir = tmp_path / "2024-01-15" / "ch00_Cam0"
    channel_dir.mkdir(parents=True)
    (channel_dir / "20240115_010000_wide_ch0_a.h264").write_bytes(b"already here")

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

    # Only ch0_b was actually attempted -- ch0_a was skipped as already present.
    assert fake_bc_cls.calls == [(0, "20240115_020000_wide_ch0_b")]
    assert any("already on disk" in m for m in notifier.messages)


async def test_search_summary_reports_already_downloaded_per_channel(tmp_path, monkeypatch, capsys):
    # ch0 has one file already on disk from a previous run; ch1 has none.
    # The per-channel skip count should show up in the search summary
    # itself (both console and Telegram), not just later once downloads
    # start.
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_b", 2)]),
        (1, "main"): _recordings(channel=1, stream="main", names_and_hours=[("ch1_a", 1)]),
    }
    fake_host_cls = make_fake_host_class(
        channels=[0, 1], names={0: "Front Door", 1: "Backyard"}, recordings=recordings
    )
    fake_bc_cls = make_fake_baichuan_cls()
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    channel_dir = tmp_path / "2024-01-15" / "ch00_Front-Door"
    channel_dir.mkdir(parents=True)
    (channel_dir / "20240115_010000_wide_ch0_a.h264").write_bytes(b"already here")

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

    console_out = capsys.readouterr().out
    assert "ch0 (Front Door): 2 found (1 already downloaded, 1 new)" in console_out
    assert "ch1 (Backyard): 1 found" in console_out

    search_message = next(m for m in notifier.messages if "Search complete" in m)
    assert "ch0 (Front Door): 2 found (1 already downloaded, 1 new)" in search_message
    assert "ch1 (Backyard): 1 found" in search_message


async def test_resume_with_nothing_left_to_download(tmp_path, monkeypatch):
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls()
    notifier = RecordingNotifier()
    monkeypatch.setattr(rd, "Host", fake_host_cls)
    monkeypatch.setattr(rd, "BaichuanDownloader", fake_bc_cls)

    channel_dir = tmp_path / "2024-01-15" / "ch00_Cam0"
    channel_dir.mkdir(parents=True)
    (channel_dir / "20240115_010000_wide_ch0_a.h265").write_bytes(b"already here")

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

    assert fake_bc_cls.calls == []  # never even connected to download anything
    assert any("Found: 1, downloaded: 0, failed: 0, 1 already on disk" in m for m in notifier.messages)


async def test_hourly_heartbeat_reports_progress_on_a_timer(tmp_path, monkeypatch):
    monkeypatch.setattr(rd, "PROGRESS_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("a", 1), ("b", 2)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(download_delay=0.08)
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

    heartbeats = [m for m in notifier.messages if "Still running" in m]
    assert len(heartbeats) >= 1
    assert "elapsed" in heartbeats[0]


async def test_no_heartbeat_after_download_phase_completes(tmp_path, monkeypatch):
    # The heartbeat task must be cancelled once downloads finish, not linger
    # and fire a stray message after the run has already reported completion.
    monkeypatch.setattr(rd, "PROGRESS_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("a", 1)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
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
        concurrency=1,
        notifier=notifier,
    )
    message_count_at_finish = len(notifier.messages)
    await asyncio.sleep(0.2)  # well past several heartbeat intervals
    assert len(notifier.messages) == message_count_at_finish


async def test_debug_logs_sub_stream_comparison_to_console_only(tmp_path, monkeypatch, capsys):
    # Two files on "main", three on "sub" for the same day/channel -- a
    # concrete stand-in for the suspected root cause of the clip-count
    # discrepancy vs. the mobile app (this tool never searches "sub" by
    # default).
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("m_a", 1), ("m_b", 2)]),
        (0, "sub"): _recordings(channel=0, stream="sub", names_and_hours=[("s_a", 1), ("s_b", 2), ("s_c", 3)]),
    }
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
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
        concurrency=1,
        debug=True,
        notifier=notifier,
    )

    out = capsys.readouterr().out
    assert "[diag] 2024-01-15 stream comparison: main=2 sub=3" in out
    # Diagnostic output is console-only -- never sent to Telegram.
    assert not any("diag" in m for m in notifier.messages)
    # The sub-stream files were never actually downloaded or added to the
    # job list -- this is a comparison only, not a behavior change.
    assert not any("s_a" in n or "s_b" in n or "s_c" in n for _, n in fake_bc_cls.calls)


async def test_no_diag_output_without_debug(tmp_path, monkeypatch, capsys):
    recordings = {
        (0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("m_a", 1)]),
        (0, "sub"): _recordings(channel=0, stream="sub", names_and_hours=[("s_a", 1), ("s_b", 2)]),
    }
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
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
    )

    assert "[diag]" not in capsys.readouterr().out


async def test_oversized_recording_is_skipped_without_retries(tmp_path, monkeypatch):
    # ch0_a is a normal file; ch0_huge is "too large" per max_download_mb.
    # The oversized one must fail immediately (no retries -- retrying would
    # just hit the same size limit again) while the other still succeeds.
    recordings = {(0, "main"): _recordings(channel=0, stream="main", names_and_hours=[("ch0_a", 1), ("ch0_huge", 2)])}
    fake_host_cls = make_fake_host_class(channels=[0], recordings=recordings)
    fake_bc_cls = make_fake_baichuan_cls(too_large_names={"20240115_020000_wide_ch0_huge"})
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
        max_download_mb=100,
        notifier=notifier,
    )

    # Exactly one attempt for the oversized file -- no retries wasted on it.
    assert fake_bc_cls.attempt_counts["20240115_020000_wide_ch0_huge"] == 1
    files = list((tmp_path / "2024-01-15" / "ch00_Cam0").glob("*.mp4"))
    assert len(files) == 1
    assert "ch0_a" in files[0].name

    # No per-file Telegram notification for a size-skip (that would be as
    # spammy as the per-file progress messages Telegram already avoids
    # elsewhere) -- it's summarized once in the finish message instead.
    assert not any("too large" in m for m in notifier.messages if "finished run" not in m)
    finish_message = next(m for m in notifier.messages if "finished run" in m)
    assert "1 skipped (too large)" in finish_message
