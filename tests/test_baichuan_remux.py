"""_remux_to_mp4 command construction — no real ffmpeg invoked."""

from pathlib import Path
from unittest.mock import MagicMock

from reolink_downloader.baichuan import _remux_to_mp4


def _capture_cmd(monkeypatch, returncode=0, mp4_exists=True):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(returncode=returncode)

    monkeypatch.setattr("reolink_downloader.baichuan.subprocess.run", fake_run)
    monkeypatch.setattr(Path, "exists", lambda self: mp4_exists)
    return captured


def test_h264_source_is_remuxed_without_transcode(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    ok = _remux_to_mp4(tmp_path / "clip.h264", tmp_path / "clip.mp4", codec="h264", fps=15)

    assert ok is True
    cmd = captured["cmd"]
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "libx264" not in cmd
    assert cmd[cmd.index("-r") + 1] == "15"


def test_h265_source_is_transcoded_to_h264(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    ok = _remux_to_mp4(tmp_path / "clip.h265", tmp_path / "clip.mp4", codec="h265", fps=None)

    assert ok is True
    cmd = captured["cmd"]
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
    assert "copy" not in cmd  # no lossless path taken for h265
    assert "-r" not in cmd  # fps=None: no forced input frame rate


def test_h265_transcode_still_sets_fps_when_known(monkeypatch, tmp_path):
    captured = _capture_cmd(monkeypatch)
    _remux_to_mp4(tmp_path / "clip.h265", tmp_path / "clip.mp4", codec="h265", fps=25)

    cmd = captured["cmd"]
    assert cmd[cmd.index("-r") + 1] == "25"


def test_ffmpeg_nonzero_exit_returns_false(monkeypatch, tmp_path):
    _capture_cmd(monkeypatch, returncode=1)
    assert _remux_to_mp4(tmp_path / "clip.h264", tmp_path / "clip.mp4", codec="h264") is False


def test_ffmpeg_exception_returns_false(monkeypatch, tmp_path):
    def raising_run(cmd, **kwargs):
        raise OSError("ffmpeg not found")

    monkeypatch.setattr("reolink_downloader.baichuan.subprocess.run", raising_run)
    assert _remux_to_mp4(tmp_path / "clip.h264", tmp_path / "clip.mp4", codec="h264") is False
