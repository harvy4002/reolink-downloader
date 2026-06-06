#!/usr/bin/env python3
"""
Reolink Camera Video Downloader
Downloads videos from a Reolink camera within a specified date range.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from reolink_aio.api import Host


# Map a user-facing lens name to the (label, reolink stream name) used when
# searching for recordings. On dual-lens "single motion" cameras such as the
# TrackMix PoE, both lenses share channel 0 and the telephoto lens is selected
# via a "telephoto_" stream prefix (reolink_aio sets iLogicChannel=1 internally).
LENS_STREAMS: dict[str, tuple[str, str]] = {
    "wide": ("wide", "main"),
    "telephoto": ("telephoto", "telephoto_main"),
}


async def download_videos(
    ip: str,
    username: str,
    password: str,
    start_time: datetime,
    end_time: datetime,
    output_dir: Path,
    lenses: list[str],
) -> None:
    """
    Download videos from Reolink camera within the specified date range.

    Args:
        ip: Camera IP address or hostname
        username: Camera username
        password: Camera password
        start_time: Start of date range
        end_time: End of date range
        output_dir: Directory to save downloaded videos
        lenses: Lens names to download (keys of LENS_STREAMS, e.g. ["wide", "telephoto"])
    """
    # Ensure output directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    # Initialize camera connection
    print(f"Connecting to camera at {ip}...")
    host = Host(ip, username, password)

    try:
        # Get camera information and authenticate
        await host.get_host_data()
        print(f"Successfully connected to camera: {host.nvr_name}")

        # Get the first available channel (usually 0 for single cameras)
        if not host.channels:
            print("Error: No channels found on camera")
            return

        # Dual-lens "single motion" cameras (e.g. TrackMix PoE) report only
        # channel 0 in host.channels even though they have two lenses, so we
        # always search on channel 0 and distinguish the lenses by stream name.
        channel = host.channels[0]
        print(f"Using channel: {channel}")
        print(f"Downloading lens(es): {', '.join(lenses)}")

        # Collect all VOD files, tagged with the lens they came from.
        all_vod_files: list[tuple[str, object]] = []
        for lens in lenses:
            lens_label, stream = LENS_STREAMS[lens]

            print(f"\nSearching {lens_label} lens for recordings from {start_time} to {end_time}...")

            # First get the status to see which days have recordings
            status_list, _ = await host.request_vod_files(
                channel=channel,
                start=start_time,
                end=end_time,
                status_only=True,
                stream=stream,
            )

            if not status_list:
                print(f"  No {lens_label} recordings found in the specified date range")
                continue

            # Request files for each day that has recordings
            for status in status_list:
                year = status.year
                month = status.month
                for day in status.days:
                    day_start = datetime(year, month, day, 0, 0, 0)
                    day_end = datetime(year, month, day, 23, 59, 59)

                    # Only process if this day falls within our requested range
                    if day_start > end_time or day_end < start_time:
                        continue

                    print(f"  Checking {year}-{month:02d}-{day:02d}...")
                    _, day_files = await host.request_vod_files(
                        channel=channel,
                        start=max(day_start, start_time),
                        end=min(day_end, end_time),
                        status_only=False,
                        stream=stream,
                    )

                    if day_files:
                        all_vod_files.extend((lens_label, f) for f in day_files)
                        print(f"    Found {len(day_files)} file(s)")

        if not all_vod_files:
            print("No recordings found in the specified date range")
            return

        print(f"\nTotal found: {len(all_vod_files)} recording(s)")

        # Download each file
        for idx, (lens_label, vod_file) in enumerate(all_vod_files, 1):
            # Extract file information
            file_name = vod_file.file_name
            start_time_obj = vod_file.start_time

            # Create a meaningful filename with timestamp
            # Remove any path separators from file_name and use just the filename part
            clean_file_name = Path(file_name).name if file_name else f"recording_{idx}"
            # Remove .mp4 extension if it exists to avoid double extension
            if clean_file_name.endswith('.mp4'):
                clean_file_name = clean_file_name[:-4]

            timestamp_str = start_time_obj.strftime("%Y%m%d_%H%M%S") if start_time_obj else f"recording_{idx}"
            # Tag with the lens so wide/telephoto files don't collide on disk.
            output_filename = f"{timestamp_str}_{lens_label}_{clean_file_name}.mp4"
            output_path = output_dir / output_filename

            print(f"Downloading [{idx}/{len(all_vod_files)}]: {output_filename}...")

            # Download the file
            vod_download = await host.download_vod(
                filename=file_name,
                channel=channel,
            )

            # Save to disk by reading from the stream
            try:
                with open(output_path, "wb") as f:
                    while True:
                        chunk = await vod_download.stream.read(8192)  # Read in 8KB chunks
                        if not chunk:
                            break
                        f.write(chunk)

                print(f"  Saved to: {output_path}")
            finally:
                # Clean up the connection
                await vod_download.close()

        print(f"\nSuccessfully downloaded {len(all_vod_files)} video(s) to {output_dir}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
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


def main():
    """Main entry point for the CLI application."""
    parser = argparse.ArgumentParser(
        description="Download videos from a Reolink camera within a specified date range"
    )

    parser.add_argument(
        "--ip",
        required=True,
        help="Camera IP address or hostname",
    )
    parser.add_argument(
        "--username",
        required=True,
        help="Camera username",
    )
    parser.add_argument(
        "--password",
        required=True,
        help="Camera password",
    )
    parser.add_argument(
        "--start-time",
        required=True,
        help="Start date/time (e.g., '2024-01-01' or '2024-01-01 14:30:00')",
    )
    parser.add_argument(
        "--end-time",
        required=True,
        help="End date/time (e.g., '2024-01-02' or '2024-01-02 14:30:00')",
    )
    parser.add_argument(
        "--output",
        default="./downloads",
        help="Output directory for downloaded videos (default: ./downloads)",
    )
    parser.add_argument(
        "--lens",
        choices=["wide", "telephoto", "both"],
        default="both",
        help=(
            "Which lens to download on dual-lens cameras like the TrackMix PoE "
            "(default: both). Single-lens cameras only have 'wide'."
        ),
    )

    args = parser.parse_args()

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
