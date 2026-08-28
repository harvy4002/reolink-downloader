from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from reolink_downloader.telegram import TelegramNotifier


class TestEnabled:
    def test_disabled_when_both_unset(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert TelegramNotifier().enabled is False

    def test_disabled_when_only_token_set(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        assert TelegramNotifier().enabled is False

    def test_enabled_when_both_set(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        assert TelegramNotifier().enabled is True


class TestNoOpWhenDisabled:
    async def test_send_never_touches_network(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        notifier = TelegramNotifier()
        with patch("aiohttp.ClientSession") as session_cls:
            await notifier.notify_start(ip="1.2.3.4", channel_spec="all", start_time=None, end_time=None)
            await notifier.notify_finish(ip="1.2.3.4", total_found=0, total_downloaded=0, total_failed=0, output_dir=".")
            await notifier.notify_progress(done=1, total=2, succeeded=1, failed=0)
            session_cls.assert_not_called()


class TestSendsWhenEnabled:
    def _mock_session(self, status=200):
        resp = MagicMock()
        resp.status = status
        resp.text = AsyncMock(return_value="")
        resp_cm = MagicMock()
        resp_cm.__aenter__ = AsyncMock(return_value=resp)
        resp_cm.__aexit__ = AsyncMock(return_value=False)

        session = MagicMock()
        session.post = MagicMock(return_value=resp_cm)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=False)
        return session, session_cm

    async def test_posts_to_telegram_api(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        notifier = TelegramNotifier()
        session, session_cm = self._mock_session()

        with patch("aiohttp.ClientSession", return_value=session_cm) as session_cls:
            await notifier.notify_progress(done=3, total=4, succeeded=3, failed=0)

        session_cls.assert_called_once()
        args, kwargs = session.post.call_args
        assert args[0] == "https://api.telegram.org/botabc123/sendMessage"
        assert kwargs["data"]["chat_id"] == "999"
        assert "3/4" in kwargs["data"]["text"]

    async def test_non_200_response_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        notifier = TelegramNotifier()
        _, session_cm = self._mock_session(status=400)

        with patch("aiohttp.ClientSession", return_value=session_cm):
            await notifier.notify_error(channel=0, file_name="x.mp4", error="boom")

        assert "sendMessage failed" in capsys.readouterr().err

    async def test_network_exception_does_not_raise(self, monkeypatch, capsys):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc123")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        notifier = TelegramNotifier()

        with patch("aiohttp.ClientSession", side_effect=OSError("network down")):
            await notifier.notify_finish(
                ip="1.2.3.4", total_found=1, total_downloaded=1, total_failed=0, output_dir="."
            )

        assert "notification failed" in capsys.readouterr().err
