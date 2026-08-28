"""Every CLI option can also come from a REOLINK_* environment variable
(Docker-friendly), with an explicit CLI flag always overriding it."""

import sys
from datetime import datetime
from unittest.mock import patch

import pytest

import reolink_downloader as rd

REQUIRED_ENV = {
    "REOLINK_IP": "10.0.0.5",
    "REOLINK_USERNAME": "admin",
    "REOLINK_PASSWORD": "secret",
    "REOLINK_START_TIME": "2024-01-01",
    "REOLINK_END_TIME": "2024-01-02",
}


def _run_main_capturing_download_call(monkeypatch, argv, env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["reolink-downloader", *argv])

    captured = {}

    async def fake_download_videos(**kwargs):
        captured.update(kwargs)

    with patch.object(rd, "download_videos", fake_download_videos):
        rd.main()
    return captured


def test_required_options_read_from_env(monkeypatch):
    captured = _run_main_capturing_download_call(monkeypatch, [], REQUIRED_ENV)
    assert captured["ip"] == "10.0.0.5"
    assert captured["username"] == "admin"
    assert captured["password"] == "secret"


def test_date_only_end_time_covers_the_whole_day(monkeypatch):
    # A bare date for --end-time must include all of that day, not stop at
    # its first instant (midnight) -- start-time keeps defaulting to
    # midnight, since a range should *begin* at the start of its first day.
    captured = _run_main_capturing_download_call(monkeypatch, [], REQUIRED_ENV)
    assert captured["start_time"] == datetime(2024, 1, 1, 0, 0, 0)
    assert captured["end_time"] == datetime(2024, 1, 2, 23, 59, 59)


def test_explicit_end_time_of_day_is_not_overridden(monkeypatch):
    env = {**REQUIRED_ENV, "REOLINK_END_TIME": "2024-01-02 08:00:00"}
    captured = _run_main_capturing_download_call(monkeypatch, [], env)
    assert captured["end_time"] == datetime(2024, 1, 2, 8, 0, 0)


def test_optional_settings_read_from_env(monkeypatch):
    env = {
        **REQUIRED_ENV,
        "REOLINK_CHANNEL": "0,2-3",
        "REOLINK_CONCURRENCY": "2",
        "REOLINK_LENS": "wide",
        "REOLINK_LIMIT": "5",
        "REOLINK_DEBUG": "true",
        "REOLINK_OUTPUT": "/data/out",
    }
    captured = _run_main_capturing_download_call(monkeypatch, [], env)
    assert captured["channel_spec"] == {0, 2, 3}
    assert captured["concurrency"] == 2
    assert captured["lenses"] == ["wide"]
    assert captured["limit"] == 5
    assert captured["debug"] is True
    assert str(captured["output_dir"]) == "/data/out"


def test_cli_flag_overrides_env_var(monkeypatch):
    captured = _run_main_capturing_download_call(monkeypatch, ["--ip", "192.168.1.50"], REQUIRED_ENV)
    assert captured["ip"] == "192.168.1.50"


def test_missing_required_option_exits_with_error(monkeypatch, capsys):
    for key in REQUIRED_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["reolink-downloader"])

    with pytest.raises(SystemExit) as exc_info:
        rd.main()

    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "--ip" in err and "--username" in err and "REOLINK_" in err


def test_invalid_lens_from_env_exits_with_error(monkeypatch, capsys):
    env = {**REQUIRED_ENV, "REOLINK_LENS": "bogus"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["reolink-downloader"])

    with pytest.raises(SystemExit) as exc_info:
        rd.main()

    assert exc_info.value.code == 1
    assert "invalid --lens" in capsys.readouterr().err


def test_debug_flag_defaults_false_when_env_unset(monkeypatch):
    monkeypatch.delenv("REOLINK_DEBUG", raising=False)
    captured = _run_main_capturing_download_call(monkeypatch, [], REQUIRED_ENV)
    assert captured["debug"] is False


def test_blank_env_vars_fall_back_to_defaults(monkeypatch):
    # A .env file that lists every key, some left blank as placeholders,
    # must not crash int()-typed options like --limit/--concurrency.
    env = {**REQUIRED_ENV, "REOLINK_LIMIT": "", "REOLINK_CONCURRENCY": "", "REOLINK_CHANNEL": ""}
    captured = _run_main_capturing_download_call(monkeypatch, [], env)
    assert captured["limit"] is None
    assert captured["concurrency"] == 3
    assert captured["channel_spec"] == "all"


def test_max_download_mb_unset_by_default(monkeypatch):
    captured = _run_main_capturing_download_call(monkeypatch, [], REQUIRED_ENV)
    assert captured["max_download_mb"] is None


def test_max_download_mb_read_from_env(monkeypatch):
    env = {**REQUIRED_ENV, "REOLINK_MAX_DOWNLOAD_MB": "300"}
    captured = _run_main_capturing_download_call(monkeypatch, [], env)
    assert captured["max_download_mb"] == 300


def test_max_download_mb_rejects_less_than_one(monkeypatch, capsys):
    env = {**REQUIRED_ENV, "REOLINK_MAX_DOWNLOAD_MB": "0"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["reolink-downloader"])

    with pytest.raises(SystemExit) as exc_info:
        rd.main()

    assert exc_info.value.code == 1
    assert "--max-download-mb" in capsys.readouterr().err
