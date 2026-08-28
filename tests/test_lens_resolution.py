from types import SimpleNamespace

from reolink_downloader import channel_has_telephoto, resolve_lenses_for_channel


def make_host(*, is_nvr, stream_channels=(), dual_lens_channels=()):
    return SimpleNamespace(
        is_nvr=is_nvr,
        stream_channels=list(stream_channels),
        supported=lambda channel, capability: capability == "autotrack_stream" and channel in dual_lens_channels,
    )


class TestChannelHasTelephoto:
    def test_nvr_channel_with_autotrack_capability(self):
        host = make_host(is_nvr=True, dual_lens_channels=[2])
        assert channel_has_telephoto(host, 2) is True

    def test_nvr_channel_without_autotrack_capability(self):
        host = make_host(is_nvr=True, dual_lens_channels=[2])
        assert channel_has_telephoto(host, 0) is False

    def test_standalone_dual_lens_camera_on_channel_0(self):
        # reolink_aio never sets "autotrack_stream" when is_nvr is False;
        # instead it expands stream_channels to include a synthetic 1.
        host = make_host(is_nvr=False, stream_channels=[0, 1])
        assert channel_has_telephoto(host, 0) is True

    def test_standalone_single_lens_camera(self):
        host = make_host(is_nvr=False, stream_channels=[0])
        assert channel_has_telephoto(host, 0) is False

    def test_standalone_only_applies_to_channel_0(self):
        # Guards against misapplying the standalone-camera heuristic to a
        # channel index that isn't the camera's own base channel.
        host = make_host(is_nvr=False, stream_channels=[0, 1])
        assert channel_has_telephoto(host, 1) is False


class TestResolveLensesForChannel:
    def test_keeps_both_when_supported(self):
        host = make_host(is_nvr=True, dual_lens_channels=[1])
        assert resolve_lenses_for_channel(host, 1, ["wide", "telephoto"]) == ["wide", "telephoto"]

    def test_narrows_to_wide_when_unsupported(self):
        host = make_host(is_nvr=True, dual_lens_channels=[1])
        assert resolve_lenses_for_channel(host, 0, ["wide", "telephoto"]) == ["wide"]

    def test_wide_only_request_is_unaffected(self):
        host = make_host(is_nvr=True, dual_lens_channels=[])
        assert resolve_lenses_for_channel(host, 0, ["wide"]) == ["wide"]
