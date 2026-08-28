"""Optional Telegram Bot API notifications.

Configured purely through environment variables (``TELEGRAM_BOT_TOKEN`` and
``TELEGRAM_CHAT_ID``) so it drops cleanly into a Docker deployment. When
either variable is unset, every ``notify_*`` method is a complete no-op (no
network call, no URL construction) so existing setups without Telegram
configured see zero behavior change. Notification failures are caught and
logged to stderr, never raised, since a Telegram outage must never abort a
download run.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime

import aiohttp

TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass
class TelegramNotifier:
    bot_token: str | None = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN"))
    chat_id: str | None = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID"))

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    async def _send(self, text: str) -> None:
        if not self.enabled:
            return
        url = f"{TELEGRAM_API_BASE}/bot{self.bot_token}/sendMessage"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    data={"chat_id": self.chat_id, "text": text},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        print(f"[telegram] sendMessage failed ({resp.status}): {body[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[telegram] notification failed: {e}", file=sys.stderr)

    async def notify_start(
        self, *, ip: str, channel_spec: str, start_time: datetime, end_time: datetime
    ) -> None:
        await self._send(
            f"🎬 reolink-downloader: starting run against {ip}\n"
            f"Channels: {channel_spec}\n"
            f"Range: {start_time} → {end_time}"
        )

    async def notify_search_summary(self, *, results: list[tuple[int, str, int]]) -> None:
        """One combined message covering every searched channel's result,
        rather than a separate message per channel — search across channels
        finishes as a single batch, not a stream of realtime events."""
        lines = []
        for channel, name, found in results:
            if found < 0:
                lines.append(f"  ch{channel} ({name}): search failed")
            else:
                lines.append(f"  ch{channel} ({name}): {found} found")
        await self._send("🔍 Search complete:\n" + "\n".join(lines))

    async def notify_channel_progress(
        self,
        *,
        channel: int,
        name: str,
        phase: str,
        **counts: int,
    ) -> None:
        counts_str = ", ".join(f"{k}={v}" for k, v in counts.items())
        await self._send(f"📡 Channel {channel} ({name}) — {phase}: {counts_str}")

    async def notify_error(self, *, channel: int, file_name: str, error: str) -> None:
        await self._send(f"⚠️ Channel {channel}: failed to download {file_name}: {error}")

    async def notify_progress(
        self, *, done: int, total: int, succeeded: int, failed: int
    ) -> None:
        pct = done * 100 // total if total else 100
        await self._send(
            f"⏳ Overall progress: {done}/{total} ({pct}%) — {succeeded} succeeded, {failed} failed"
        )

    async def notify_heartbeat(
        self, *, done: int, total: int, succeeded: int, failed: int, elapsed: str
    ) -> None:
        """A progress update sent purely on a timer (see
        PROGRESS_HEARTBEAT_INTERVAL_SECONDS), independent of notify_progress's
        milestone-based updates — confirms a long run is still alive."""
        pct = done * 100 // total if total else 100
        await self._send(
            f"⏰ Still running ({elapsed} elapsed): {done}/{total} ({pct}%) — "
            f"{succeeded} succeeded, {failed} failed"
        )

    async def notify_finish(
        self,
        *,
        ip: str,
        total_found: int,
        total_downloaded: int,
        total_failed: int,
        output_dir: str,
        already_present: int = 0,
        skipped_too_large: int = 0,
    ) -> None:
        already_note = f", {already_present} already on disk" if already_present else ""
        skipped_note = f", {skipped_too_large} skipped (too large)" if skipped_too_large else ""
        await self._send(
            f"✅ reolink-downloader: finished run against {ip}\n"
            f"Found: {total_found}, downloaded: {total_downloaded}, failed: {total_failed}"
            f"{already_note}{skipped_note}\n"
            f"Output: {output_dir}"
        )

    async def notify_aborted(self, *, ip: str, error: str) -> None:
        await self._send(f"❌ reolink-downloader: run against {ip} aborted: {error}")
