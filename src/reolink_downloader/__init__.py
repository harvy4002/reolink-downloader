#!/usr/bin/env python3
"""
Reolink Camera Video Downloader
Downloads videos from one or more channels of a Reolink NVR/camera within a
specified date range.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from reolink_aio.api import Host
from reolink_aio.exceptions import ReolinkError

from .baichuan import BaichuanDownloader, BaichuanError
from .telegram import TelegramNotifier


# Map a user-facing lens name to the (label, reolink stream name) used when
# searching for recordings. On dual-lens "single motion" cameras such as the
# TrackMix PoE, both lenses share one NVR channel and the telephoto lens is
# selected via a "telephoto_" stream prefix (reolink_aio sets iLogicChannel=1
# internally) for search, and via channelId for the actual Baichuan download.
LENS_STREAMS: dict[str, tuple[str, str]] = {
    "wide": ("wide", "main"),
    "telephoto": ("telephoto", "telephoto_main"),
}

MAX_RECOMMENDED_CONCURRENCY = 4  # Reolink PoE NVRs cap concurrent playback streams at 4.

# Auto-recovery: a failed download (or a worker's initial connection) is
# retried this many times before being logged and reported as a permanent
# failure. Total attempts = 1 initial try + this many retries.
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 2.0


@dataclass
class DownloadJob:
    idx: int
    channel: int
    lens_label: str
    vod_file: object
    output_base: Path
    download_channel: int


def channel_has_telephoto(host: Host, channel: int) -> bool:
    """True if this channel exposes a second (telephoto) lens.

    On an NVR, reolink_aio only tags a channel with the "autotrack_stream"
    capability when host.is_nvr is True (see reolink_aio api.py). Standalone
    dual-lens cameras (e.g. TrackMix PoE) are never tagged that way; instead
    reolink_aio expands host.stream_channels to [0, 1] while host.channels
    stays [0], so that's the signal to use for that case.
    """
    if host.is_nvr:
        return host.supported(channel, "autotrack_stream")
    return channel == 0 and 1 in host.stream_channels


def resolve_lenses_for_channel(host: Host, channel: int, lenses: list[str]) -> list[str]:
    """Narrow the requested lens list to what this channel actually supports."""
    if channel_has_telephoto(host, channel):
        return lenses
    return [lens for lens in lenses if lens != "telephoto"]


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")
    return slug or "unnamed"


def _make_progress_logger(job: "DownloadJob", worker_id: int):
    """Build an on_progress callback that logs a single file's download
    progress without flooding the console: at most once per 20% step when
    the on-camera file size is known, or every ~3 seconds otherwise."""
    state = {"bucket": -1, "last_log": 0.0}

    def _on_progress(bytes_so_far: int, total_bytes: int | None) -> None:
        now = time.monotonic()
        if total_bytes:
            pct = min(100, int(bytes_so_far * 100 / total_bytes))
            bucket = pct // 20
            if bucket == state["bucket"] and pct != 100:
                return
            state["bucket"] = bucket
            print(
                f"  [ch{job.channel} w{worker_id}] {job.output_base.name}: "
                f"{bytes_so_far / 1_000_000:.1f}/{total_bytes / 1_000_000:.1f} MB ({pct}%)"
            )
        else:
            if now - state["last_log"] < 3.0:
                return
            print(
                f"  [ch{job.channel} w{worker_id}] {job.output_base.name}: "
                f"{bytes_so_far / 1_000_000:.1f} MB downloaded..."
            )
        state["last_log"] = now

    return _on_progress


async def _attempt_download(bc: BaichuanDownloader, job: "DownloadJob", worker_id: int) -> Path:
    try:
        total_size = job.vod_file.size
    except Exception:
        total_size = None
    return await bc.download(
        job.output_base,
        start=job.vod_file.start_time,
        end=job.vod_file.end_time,
        channel=job.download_channel,
        stream_type="mainStream",
        total_size=total_size,
        on_progress=_make_progress_logger(job, worker_id),
    )


async def _download_with_retries(
    job: "DownloadJob",
    worker_id: int,
    bc: BaichuanDownloader,
    ip: str,
    username: str,
    password: str,
    debug: bool,
) -> Path:
    """Attempt job's download up to 1 + MAX_RETRIES times. The first attempt
    reuses the worker's long-lived connection; retries open a fresh
    short-lived connection each time, since a stale/broken connection may be
    why the first attempt failed. Raises the last error if every attempt
    fails."""
    last_error: BaichuanError | OSError | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            if attempt == 1:
                return await _attempt_download(bc, job, worker_id)
            print(
                f"  Retrying [{job.idx}] {job.output_base.name} "
                f"(attempt {attempt}/{MAX_RETRIES + 1}; previous error: {last_error})"
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)
            async with BaichuanDownloader(ip, username, password, debug=debug) as retry_bc:
                return await _attempt_download(retry_bc, job, worker_id)
        except (BaichuanError, OSError) as e:
            last_error = e
    assert last_error is not None
    raise last_error


async def _search_channel(
    host: Host,
    channel: int,
    lenses: list[str],
    start_time: datetime,
    end_time: datetime,
    notifier: TelegramNotifier,
) -> list[tuple[int, str, object]]:
    """Search one channel for recordings in the given range across the
    requested (and applicable) lenses, day by day."""
    name = host.camera_name(channel)
    channel_lenses = resolve_lenses_for_channel(host, channel, lenses)
    if not channel_lenses:
        print(f"Channel {channel} ({name}): no matching lens for --lens={'/'.join(lenses)}; skipping")
        return []

    found: list[tuple[int, str, object]] = []
    for lens in channel_lenses:
        lens_label, stream = LENS_STREAMS[lens]
        print(f"\nChannel {channel} ({name}): searching {lens_label} lens for recordings from {start_time} to {end_time}...")

        try:
            # First get the status to see which days have recordings. Reolink's
            # search API silently switches to status-only mode for ranges >1 day.
            status_list, _ = await host.request_vod_files(
                channel=channel,
                start=start_time,
                end=end_time,
                status_only=True,
                stream=stream,
            )
        except ReolinkError as e:
            print(f"  Channel {channel} ({name}): {lens_label} search failed: {e}", file=sys.stderr)
            continue

        if not status_list:
            print(f"  No {lens_label} recordings found in the specified date range")
            continue

        for status in status_list:
            year = status.year
            month = status.month
            for day in status.days:
                day_start = datetime(year, month, day, 0, 0, 0)
                day_end = datetime(year, month, day, 23, 59, 59)

                if day_start > end_time or day_end < start_time:
                    continue

                print(f"  Channel {channel}: checking {year}-{month:02d}-{day:02d}...")
                try:
                    _, day_files = await host.request_vod_files(
                        channel=channel,
                        start=max(day_start, start_time),
                        end=min(day_end, end_time),
                        status_only=False,
                        stream=stream,
                    )
                except ReolinkError as e:
                    print(f"    Channel {channel}: search failed for {year}-{month:02d}-{day:02d}: {e}", file=sys.stderr)
                    continue

                if day_files:
                    found.extend((channel, lens_label, f) for f in day_files)
                    print(f"    Found {len(day_files)} file(s)")

    await notifier.notify_channel_progress(channel=channel, name=name, phase="search complete", found=len(found))
    return found


async def _download_worker(
    worker_id: int,
    ip: str,
    username: str,
    password: str,
    debug: bool,
    queue: "asyncio.Queue[DownloadJob | None]",
    total: int,
    channel_totals: dict[int, int],
    channel_done: dict[int, int],
    channel_errors: dict[int, int],
    channel_names: dict[int, str],
    notifier: TelegramNotifier,
    downloaded_count: list[int],
    progress_state: dict,
) -> None:
    """Drain the shared download queue using one persistent Baichuan
    connection. Each file gets up to 1 + MAX_RETRIES attempts before being
    logged and reported to Telegram as a permanent failure; a permanent
    failure never stops the worker. The worker's own initial connection is
    retried the same number of times before this worker gives up entirely,
    leaving the remaining jobs for its siblings to pick up."""
    for connect_attempt in range(1, MAX_RETRIES + 2):
        try:
            async with BaichuanDownloader(ip, username, password, debug=debug) as bc:
                while True:
                    job = await queue.get()
                    if job is None:
                        queue.task_done()
                        return
                    try:
                        print(
                            f"Downloading [{job.idx}/{total}] (worker {worker_id}): {job.output_base.name} "
                            f"({job.vod_file.start_time} - {job.vod_file.end_time})..."
                        )
                        written = await _download_with_retries(
                            job, worker_id, bc, ip, username, password, debug
                        )
                        print(f"  Saved to: {written}")
                        downloaded_count[0] += 1
                    except (BaichuanError, OSError) as e:
                        print(f"  Failed after {MAX_RETRIES + 1} attempts: {e}", file=sys.stderr)
                        channel_errors[job.channel] = channel_errors.get(job.channel, 0) + 1
                        await notifier.notify_error(
                            channel=job.channel,
                            file_name=job.output_base.name,
                            error=f"{e} (after {MAX_RETRIES + 1} attempts)",
                        )
                    finally:
                        queue.task_done()
                        channel_done[job.channel] = channel_done.get(job.channel, 0) + 1

                        failed_total = sum(channel_errors.values())
                        done_total = downloaded_count[0] + failed_total
                        pct = done_total * 100 // total if total else 100
                        print(
                            f"Overall progress: {done_total}/{total} ({pct}%) — "
                            f"{downloaded_count[0]} succeeded, {failed_total} failed"
                        )
                        # Throttle Telegram overall-progress updates to every
                        # 25% step (plus the final message) so a large job
                        # doesn't spam the chat with one message per file.
                        bucket = pct // 25
                        if bucket > progress_state["last_bucket"] or done_total == total:
                            progress_state["last_bucket"] = bucket
                            await notifier.notify_progress(
                                done=done_total, total=total,
                                succeeded=downloaded_count[0], failed=failed_total,
                            )

                        if channel_done[job.channel] == channel_totals[job.channel]:
                            await notifier.notify_channel_progress(
                                channel=job.channel,
                                name=channel_names[job.channel],
                                phase="download complete",
                                succeeded=channel_totals[job.channel] - channel_errors.get(job.channel, 0),
                                failed=channel_errors.get(job.channel, 0),
                            )
        except (BaichuanError, OSError, ConnectionError) as e:
            if connect_attempt >= MAX_RETRIES + 1:
                print(
                    f"Worker {worker_id}: connection failed after {connect_attempt} attempts, "
                    f"giving up on this worker: {e}",
                    file=sys.stderr,
                )
                return
            print(
                f"Worker {worker_id}: connection failed (attempt {connect_attempt}/{MAX_RETRIES + 1}), "
                f"reconnecting: {e}",
                file=sys.stderr,
            )
            await asyncio.sleep(RETRY_DELAY_SECONDS)


async def download_videos(
    ip: str,
    username: str,
    password: str,
    start_time: datetime,
    end_time: datetime,
    output_dir: Path,
    lenses: list[str],
    channel_spec: "str | set[int]" = "all",
    concurrency: int = 3,
    debug: bool = False,
    limit: int | None = None,
    notifier: TelegramNotifier | None = None,
) -> None:
    """
    Download videos from a Reolink NVR/camera within the specified date range.

    Args:
        ip: Camera/NVR IP address or hostname
        username: Camera username
        password: Camera password
        start_time: Start of date range
        end_time: End of date range
        output_dir: Directory to save downloaded videos
        lenses: Lens names to download (keys of LENS_STREAMS, e.g. ["wide", "telephoto"])
        channel_spec: "all" or a set of channel indices to restrict to
        concurrency: Number of concurrent Baichuan download connections
        notifier: Telegram notifier (a no-op instance is created if omitted)
    """
    notifier = notifier or TelegramNotifier()

    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize camera connection
    print(f"Connecting to camera at {ip}...")
    host = Host(ip, username, password)

    channel_spec_str = "all" if channel_spec == "all" else ",".join(str(c) for c in sorted(channel_spec))
    await notifier.notify_start(ip=ip, channel_spec=channel_spec_str, start_time=start_time, end_time=end_time)

    try:
        # Get camera information and authenticate
        await host.get_host_data()
        print(f"Successfully connected to camera: {host.nvr_name}")

        available = set(host.channels)
        if not available:
            print("Error: No channels found on camera", file=sys.stderr)
            return

        if channel_spec == "all":
            selected_channels = sorted(available)
        else:
            invalid = channel_spec - available
            if invalid:
                names = ", ".join(f"{c} ({host.camera_name(c)})" for c in sorted(available))
                print(
                    f"Error: requested channel(s) {sorted(invalid)} not found on this device. "
                    f"Available channels: {names}",
                    file=sys.stderr,
                )
                return
            selected_channels = sorted(channel_spec)

        channel_names = {c: host.camera_name(c) for c in selected_channels}
        print(
            "Selected channels: "
            + ", ".join(f"{c} ({channel_names[c]})" for c in selected_channels)
        )
        print(f"Downloading lens(es): {', '.join(lenses)}")

        # Search all selected channels concurrently — VOD status/search calls
        # are cheap control-plane requests, and reolink_aio's Baichuan
        # transport internally serializes them on the same connection, so
        # this is safe even though it doesn't always yield a full speedup.
        results = await asyncio.gather(
            *(
                _search_channel(host, channel, lenses, start_time, end_time, notifier)
                for channel in selected_channels
            ),
            return_exceptions=True,
        )
        all_vod_files: list[tuple[int, str, object]] = []
        for channel, result in zip(selected_channels, results):
            if isinstance(result, Exception):
                print(f"Channel {channel}: search task failed: {result}", file=sys.stderr)
                continue
            all_vod_files.extend(result)

        if not all_vod_files:
            print("No recordings found in the specified date range")
            await notifier.notify_finish(
                ip=ip, total_found=0, total_downloaded=0, total_failed=0, output_dir=str(output_dir)
            )
            return

        # Sort deterministically before applying --limit: channel searches ran
        # concurrently, so result order would otherwise be non-deterministic
        # across runs, and --limit is a global cap over the combined list.
        all_vod_files.sort(key=lambda t: (t[0], t[2].start_time or datetime.min))

        print(f"\nTotal found: {len(all_vod_files)} recording(s)")

        if limit is not None:
            all_vod_files = all_vod_files[:limit]
            print(f"Limiting to first {len(all_vod_files)} recording(s)")

        # Build the download job list, with a lazily-created per-channel
        # output subdirectory to avoid filename collisions across channels
        # (on-device filenames are often generic per-channel).
        channel_dirs: dict[int, Path] = {}
        jobs: list[DownloadJob] = []
        channel_totals: dict[int, int] = {}
        for idx, (channel, lens_label, vod_file) in enumerate(all_vod_files, 1):
            if channel not in channel_dirs:
                channel_dir = output_dir / f"ch{channel:02d}_{_slug(channel_names[channel])}"
                channel_dir.mkdir(parents=True, exist_ok=True)
                channel_dirs[channel] = channel_dir
            channel_dir = channel_dirs[channel]

            file_name = vod_file.file_name
            start_time_obj = vod_file.start_time
            clean_file_name = Path(file_name).name if file_name else f"recording_{idx}"
            if clean_file_name.endswith(".mp4"):
                clean_file_name = clean_file_name[:-4]
            timestamp_str = (
                start_time_obj.strftime("%Y%m%d_%H%M%S") if start_time_obj else f"recording_{idx}"
            )
            output_base = channel_dir / f"{timestamp_str}_{lens_label}_{clean_file_name}"

            # On dual-lens cameras/channels the lens is selected by channelId
            # (base = wide, base+1 = telephoto); logicChnBitmap stays 255 as
            # the native app sends. This telephoto offset was reverse
            # engineered against a standalone camera always on channel 0;
            # generalizing it to `channel + 1` for a dual-lens camera
            # attached to an NVR channel other than 0 is unverified.
            download_channel = channel + 1 if lens_label == "telephoto" else channel

            jobs.append(DownloadJob(idx, channel, lens_label, vod_file, output_base, download_channel))
            channel_totals[channel] = channel_totals.get(channel, 0) + 1

        # Download over the fast Baichuan (port 9000) binary protocol rather
        # than the slow HTTPS cmd=Download API, using a small bounded worker
        # pool (each worker keeps its own connection alive across its share
        # of the queue). See PROTOCOL.md for the protocol details.
        print(f"\nConnecting to fast download protocol on {ip}:9000 with concurrency={concurrency}...")
        queue: "asyncio.Queue[DownloadJob | None]" = asyncio.Queue()
        for job in jobs:
            queue.put_nowait(job)
        for _ in range(concurrency):
            queue.put_nowait(None)

        channel_done: dict[int, int] = {}
        channel_errors: dict[int, int] = {}
        downloaded_count = [0]
        progress_state = {"last_bucket": -1}

        await asyncio.gather(
            *(
                _download_worker(
                    worker_id,
                    ip,
                    username,
                    password,
                    debug,
                    queue,
                    len(jobs),
                    channel_totals,
                    channel_done,
                    channel_errors,
                    channel_names,
                    notifier,
                    downloaded_count,
                    progress_state,
                )
                for worker_id in range(concurrency)
            ),
            return_exceptions=True,
        )

        downloaded = downloaded_count[0]
        total_failed = len(jobs) - downloaded
        print(f"\nDownloaded {downloaded}/{len(jobs)} video(s) to {output_dir}")
        await notifier.notify_finish(
            ip=ip,
            total_found=len(all_vod_files),
            total_downloaded=downloaded,
            total_failed=total_failed,
            output_dir=str(output_dir),
        )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        await notifier.notify_aborted(ip=ip, error=str(e))
        raise
    finally:
        # Clean up connection
        await host.logout()


def parse_datetime(date_string: str) -> datetime:
    """
    Parse a date string into a datetime object.
    Supports ISO format and common date formats.
    """
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue

    raise ValueError(
        f"Unable to parse date '{date_string}'. "
        f"Supported formats: YYYY-MM-DD [HH:MM[:SS]] or YYYY/MM/DD [HH:MM[:SS]]"
    )


def parse_channel_spec(spec: str) -> "str | set[int]":
    """
    Parse a --channel spec into "all" or a set of channel indices.

    Accepts "all", a single index ("0"), a comma-separated list ("0,2,5"),
    a range ("0-3"), or any combination of these ("0,2-4,7").
    """
    spec = spec.strip()
    if spec.lower() == "all":
        return "all"

    channels: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo_str, _, hi_str = part.partition("-")
            try:
                lo, hi = int(lo_str), int(hi_str)
            except ValueError:
                raise ValueError(f"Invalid channel range '{part}' in --channel '{spec}'")
            if lo > hi:
                raise ValueError(f"Invalid channel range '{part}': start must not be greater than end")
            channels.update(range(lo, hi + 1))
        else:
            try:
                channels.add(int(part))
            except ValueError:
                raise ValueError(f"Invalid channel '{part}' in --channel '{spec}'")

    if not channels:
        raise ValueError(f"--channel '{spec}' did not resolve to any channels")
    return channels


def _env_bool(name: str) -> bool:
    """Parse a boolean-flag environment variable (e.g. REOLINK_DEBUG)."""
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env(name: str, default: str | None = None) -> str | None:
    """Read an environment variable, treating an unset OR blank value as
    "not provided" — Docker .env files commonly list every key with an
    empty placeholder, which should fall through to `default` rather than
    e.g. crashing an int-typed argument on int('')."""
    value = os.environ.get(name)
    return value if value else default


def main():
    """Main entry point for the CLI application."""
    parser = argparse.ArgumentParser(
        description="Download videos from a Reolink NVR/camera within a specified date range",
        epilog=(
            "Every option below can also be set via an environment variable (useful for "
            "Docker/unattended use) — a CLI flag always overrides its environment variable: "
            "REOLINK_IP, REOLINK_USERNAME, REOLINK_PASSWORD, REOLINK_START_TIME, "
            "REOLINK_END_TIME, REOLINK_OUTPUT, REOLINK_CHANNEL, REOLINK_CONCURRENCY, "
            "REOLINK_LENS, REOLINK_LIMIT, REOLINK_DEBUG.\n\n"
            "Telegram notifications (optional): set the TELEGRAM_BOT_TOKEN and "
            "TELEGRAM_CHAT_ID environment variables to receive run start/finish, "
            "per-channel progress, and per-file error notifications. Both must be "
            "set for notifications to be sent; otherwise they're silently skipped."
        ),
    )

    parser.add_argument(
        "--ip",
        default=_env("REOLINK_IP"),
        help="Camera IP address or hostname (env: REOLINK_IP)",
    )
    parser.add_argument(
        "--username",
        default=_env("REOLINK_USERNAME"),
        help="Camera username (env: REOLINK_USERNAME)",
    )
    parser.add_argument(
        "--password",
        default=_env("REOLINK_PASSWORD"),
        help="Camera password (env: REOLINK_PASSWORD)",
    )
    parser.add_argument(
        "--start-time",
        default=_env("REOLINK_START_TIME"),
        help="Start date/time (e.g., '2024-01-01' or '2024-01-01 14:30:00') (env: REOLINK_START_TIME)",
    )
    parser.add_argument(
        "--end-time",
        default=_env("REOLINK_END_TIME"),
        help="End date/time (e.g., '2024-01-02' or '2024-01-02 14:30:00') (env: REOLINK_END_TIME)",
    )
    parser.add_argument(
        "--output",
        default=_env("REOLINK_OUTPUT", "./downloads"),
        help="Output directory for downloaded videos (default: ./downloads) (env: REOLINK_OUTPUT)",
    )
    parser.add_argument(
        "--channel",
        default=_env("REOLINK_CHANNEL", "all"),
        help=(
            "NVR channel(s) to download, e.g. '0', '0,2,5', '0-3', or '0,2-4,7'. "
            "Default 'all' auto-detects and downloads every channel the device reports. "
            "(env: REOLINK_CHANNEL)"
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=_env("REOLINK_CONCURRENCY", "3"),
        help=(
            "Number of concurrent Baichuan download connections (default: 3). "
            "Reolink PoE NVRs support at most 4 concurrent playback streams; going higher "
            "risks contending with live viewing or other clients. (env: REOLINK_CONCURRENCY)"
        ),
    )
    parser.add_argument(
        "--lens",
        choices=["wide", "telephoto", "both"],
        default=_env("REOLINK_LENS", "both"),
        help=(
            "Which lens to download on channels/cameras with a second (telephoto) lens, "
            "such as the TrackMix PoE (default: both). Automatically narrowed to 'wide' "
            "on channels without a telephoto lens. (env: REOLINK_LENS)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=_env("REOLINK_LIMIT"),
        help=(
            "Only download the first N recordings found across all selected channels "
            "(useful for testing) (env: REOLINK_LIMIT)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=_env_bool("REOLINK_DEBUG"),
        help="Print the raw Baichuan protocol exchange (for diagnosing downloads) (env: REOLINK_DEBUG)",
    )

    args = parser.parse_args()

    # Unlike CLI-provided values, argparse defaults sourced from env vars
    # aren't validated against `required`/`choices` — check both here.
    missing = [
        flag
        for flag, value in (
            ("--ip", args.ip),
            ("--username", args.username),
            ("--password", args.password),
            ("--start-time", args.start_time),
            ("--end-time", args.end_time),
        )
        if not value
    ]
    if missing:
        print(
            f"Error: missing required option(s): {', '.join(missing)} "
            "(set via the CLI flag or the matching REOLINK_* environment variable)",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.lens not in ("wide", "telephoto", "both"):
        print(f"Error: invalid --lens '{args.lens}' (choose from wide, telephoto, both)", file=sys.stderr)
        sys.exit(1)

    # Expand the lens choice into the concrete list of lenses to download.
    lenses = ["wide", "telephoto"] if args.lens == "both" else [args.lens]

    # Parse date/time arguments
    try:
        start_time = parse_datetime(args.start_time)
        end_time = parse_datetime(args.end_time)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate date range
    if start_time >= end_time:
        print("Error: start-time must be before end-time", file=sys.stderr)
        sys.exit(1)

    try:
        channel_spec = parse_channel_spec(args.channel)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.concurrency < 1:
        print("Error: --concurrency must be at least 1", file=sys.stderr)
        sys.exit(1)
    if args.concurrency > MAX_RECOMMENDED_CONCURRENCY:
        print(
            f"Warning: --concurrency {args.concurrency} exceeds the {MAX_RECOMMENDED_CONCURRENCY} "
            "concurrent playback streams most Reolink PoE NVRs support; you may see connection "
            "errors or contend with live viewing.",
            file=sys.stderr,
        )

    output_dir = Path(args.output)

    # Run the async download function
    try:
        asyncio.run(
            download_videos(
                ip=args.ip,
                username=args.username,
                password=args.password,
                start_time=start_time,
                end_time=end_time,
                output_dir=output_dir,
                lenses=lenses,
                channel_spec=channel_spec,
                concurrency=args.concurrency,
                debug=args.debug,
                limit=args.limit,
            )
        )
    except KeyboardInterrupt:
        print("\nDownload cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
