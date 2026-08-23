#!/usr/bin/env python3
"""
Reolink Camera Video Downloader
Downloads videos from a Reolink camera within a specified date range.
"""

import argparse
import asyncio
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

from reolink_aio.api import Host

from .baichuan import BaichuanDownloader, BaichuanError


# Map a user-facing lens name to the (label, reolink stream name) used when
# searching for recordings. On dual-lens "single motion" cameras such as the
# TrackMix PoE, both lenses share channel 0 and the telephoto lens is selected
# via a "telephoto_" stream prefix (reolink_aio sets iLogicChannel=1 internally).
LENS_STREAMS: dict[str, tuple[str, str]] = {
    "wide": ("wide", "main"),
    "telephoto": ("telephoto", "telephoto_main"),
}

DEFAULT_FUTURE_DAYS = 2
DEFAULT_STOP_AFTER_EMPTY_DAYS = 7
DEFAULT_MAX_DISCOVERY_DAYS = 365
VIDEO_SUFFIXES = (".mp4", ".h264", ".h265")


def _effective_lenses(host: Host, requested_lenses: list[str]) -> list[str]:
    if "telephoto" not in requested_lenses or 1 in host.stream_channels:
        return requested_lenses

    model = host.model or "this camera"
    if requested_lenses == ["telephoto"]:
        print(
            f"Telephoto lens requested, but {model} reports only stream channel(s) "
            f"{host.stream_channels}; no separate telephoto recordings are available."
        )
        return []

    print(
        f"{model} reports only stream channel(s) {host.stream_channels}; "
        "skipping telephoto downloads to avoid duplicate recordings."
    )
    return [lens for lens in requested_lenses if lens != "telephoto"]


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min),
        datetime.combine(day, time(23, 59, 59)),
    )


def _output_base(output_dir: Path, lens_label: str, vod_file: object, idx: int) -> Path:
    file_name = vod_file.file_name
    start_time_obj = vod_file.start_time

    clean_file_name = Path(file_name).name if file_name else f"recording_{idx}"
    if clean_file_name.endswith(".mp4"):
        clean_file_name = clean_file_name[:-4]
    timestamp_str = (
        start_time_obj.strftime("%Y%m%d_%H%M%S")
        if start_time_obj
        else f"recording_{idx}"
    )
    return output_dir / f"{timestamp_str}_{lens_label}_{clean_file_name}"


def _existing_download(output_base: Path) -> Path | None:
    for suffix in VIDEO_SUFFIXES:
        candidate = output_base.with_suffix(suffix)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


async def _collect_vod_files_for_day(
    host: Host,
    *,
    channel: int,
    day: date,
    start_time: datetime,
    end_time: datetime,
    lenses: list[str],
) -> list[tuple[str, object]]:
    day_start, day_end = _day_bounds(day)

    if day_start > end_time or day_end < start_time:
        return []

    day_files_all: list[tuple[str, object]] = []
    for lens in lenses:
        lens_label, stream = LENS_STREAMS[lens]
        search_start = max(day_start, start_time)
        search_end = min(day_end, end_time)

        print(f"  Checking {lens_label} lens on {day.isoformat()}...")
        _, day_files = await host.request_vod_files(
            channel=channel,
            start=search_start,
            end=search_end,
            status_only=False,
            stream=stream,
        )

        if day_files:
            day_files_all.extend((lens_label, f) for f in day_files)
            print(f"    Found {len(day_files)} file(s)")

    return day_files_all


async def _collect_vod_files_for_range(
    host: Host,
    *,
    channel: int,
    start_time: datetime,
    end_time: datetime,
    lenses: list[str],
) -> list[tuple[str, object]]:
    all_vod_files: list[tuple[str, object]] = []

    for lens in lenses:
        lens_label, stream = LENS_STREAMS[lens]

        print(f"\nSearching {lens_label} lens for recordings from {start_time} to {end_time}...")

        # First get the status to see which days have recordings.
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

        # Request files for each day that has recordings.
        for status in status_list:
            year = status.year
            month = status.month
            for day in status.days:
                day_start, day_end = _day_bounds(date(year, month, day))

                # Only process if this day falls within our requested range.
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

    return all_vod_files


async def _discover_vod_files(
    host: Host,
    *,
    channel: int,
    end_time: datetime,
    output_dir: Path,
    lenses: list[str],
    future_days: int,
    stop_after_empty_days: int,
    max_discovery_days: int,
    stop_at_existing: bool,
) -> list[tuple[str, object]]:
    search_day = (end_time + timedelta(days=future_days)).date()
    empty_days = 0
    all_vod_files: list[tuple[str, object]] = []

    print(
        "\nAuto-discovering recordings "
        f"from {search_day.isoformat()} backwards "
        f"(stop after {stop_after_empty_days} empty day(s), "
        f"max {max_discovery_days} day(s))..."
    )

    for _ in range(max_discovery_days):
        day_start, day_end = _day_bounds(search_day)
        day_files = await _collect_vod_files_for_day(
            host,
            channel=channel,
            day=search_day,
            start_time=day_start,
            end_time=day_end,
            lenses=lenses,
        )

        if not day_files:
            empty_days += 1
            print(
                f"    No recordings found "
                f"({empty_days}/{stop_after_empty_days} empty day(s))"
            )
            if empty_days >= stop_after_empty_days:
                print(f"Stopping after {empty_days} consecutive empty day(s)")
                break
        else:
            empty_days = 0
            all_vod_files.extend(day_files)
            if stop_at_existing:
                for idx, (lens_label, vod_file) in enumerate(day_files, 1):
                    output_base = _output_base(output_dir, lens_label, vod_file, idx)
                    existing = _existing_download(output_base)
                    if existing:
                        print(f"Stopping discovery at existing download: {existing}")
                        return sorted(
                            all_vod_files,
                            key=lambda item: item[1].start_time or datetime.min,
                        )

        search_day -= timedelta(days=1)

    else:
        print(f"Stopping after checking {max_discovery_days} day(s)")

    # Download oldest recordings first, matching normal chronological archive order.
    return sorted(
        all_vod_files,
        key=lambda item: item[1].start_time or datetime.min,
    )


async def download_videos(
    ip: str,
    username: str,
    password: str,
    start_time: datetime | None,
    end_time: datetime,
    output_dir: Path,
    lenses: list[str],
    debug: bool = False,
    limit: int | None = None,
    future_days: int = DEFAULT_FUTURE_DAYS,
    stop_after_empty_days: int = DEFAULT_STOP_AFTER_EMPTY_DAYS,
    max_discovery_days: int = DEFAULT_MAX_DISCOVERY_DAYS,
    stop_at_existing: bool = False,
) -> None:
    """
    Download videos from Reolink camera within the specified date range.

    Args:
        ip: Camera IP address or hostname
        username: Camera username
        password: Camera password
        start_time: Start of date range, or None to auto-discover recordings
        end_time: End date/time for explicit ranges or auto-discovery anchor
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
        lenses = _effective_lenses(host, lenses)
        if not lenses:
            return
        print(f"Downloading lens(es): {', '.join(lenses)}")

        if start_time is None:
            all_vod_files = await _discover_vod_files(
                host,
                channel=channel,
                end_time=end_time,
                output_dir=output_dir,
                lenses=lenses,
                future_days=future_days,
                stop_after_empty_days=stop_after_empty_days,
                max_discovery_days=max_discovery_days,
                stop_at_existing=stop_at_existing,
            )
        else:
            all_vod_files = await _collect_vod_files_for_range(
                host,
                channel=channel,
                start_time=start_time,
                end_time=end_time,
                lenses=lenses,
            )

        if not all_vod_files:
            print("No recordings found in the specified date range")
            return

        print(f"\nTotal found: {len(all_vod_files)} recording(s)")

        if limit is not None:
            all_vod_files = all_vod_files[:limit]
            print(f"Limiting to first {len(all_vod_files)} recording(s)")

        pending_vod_files: list[tuple[int, str, object, Path]] = []
        skipped = 0
        for idx, (lens_label, vod_file) in enumerate(all_vod_files, 1):
            output_base = _output_base(output_dir, lens_label, vod_file, idx)
            existing = _existing_download(output_base)
            if existing:
                print(f"Skipping existing download [{idx}/{len(all_vod_files)}]: {existing}")
                skipped += 1
            else:
                pending_vod_files.append((idx, lens_label, vod_file, output_base))

        if not pending_vod_files:
            print(
                f"\nDownloaded 0/{len(all_vod_files)} video(s) to {output_dir}"
                f" ({skipped} skipped)"
            )
            return

        # Download each file over the fast Baichuan (port 9000) binary protocol
        # rather than the slow HTTPS cmd=Download API. A single connection is
        # reused for every clip. See PROTOCOL.md for the protocol details.
        print(f"\nConnecting to fast download protocol on {ip}:9000...")
        downloaded = 0
        async with BaichuanDownloader(ip, username, password, debug=debug) as bc:
            for idx, lens_label, vod_file, output_base in pending_vod_files:
                start_time_obj = vod_file.start_time
                end_time_obj = vod_file.end_time

                print(
                    f"Downloading [{idx}/{len(all_vod_files)}]: {output_base.name} "
                    f"({start_time_obj} - {end_time_obj})..."
                )
                # On dual-lens cameras the lens is selected by channelId
                # (0 = wide, 1 = telephoto); logicChnBitmap stays 255 as the
                # native app sends. Single-lens cameras only use channel 0.
                download_channel = 1 if lens_label == "telephoto" else channel

                try:
                    written = await bc.download(
                        output_base,
                        start=start_time_obj,
                        end=end_time_obj,
                        channel=download_channel,
                        stream_type="mainStream",
                    )
                    print(f"  Saved to: {written}")
                    downloaded += 1
                except (BaichuanError, OSError) as e:
                    print(f"  Failed: {e}", file=sys.stderr)

        print(
            f"\nDownloaded {downloaded}/{len(all_vod_files)} video(s) to {output_dir}"
            f" ({skipped} skipped)"
        )

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
        help=(
            "Start date/time (e.g., '2024-01-01' or '2024-01-01 14:30:00'). "
            "If omitted, recordings are auto-discovered backwards from --end-time or now."
        ),
    )
    parser.add_argument(
        "--end-time",
        help=(
            "End date/time (e.g., '2024-01-02' or '2024-01-02 14:30:00'). "
            "Defaults to now when --start-time is omitted."
        ),
    )
    parser.add_argument(
        "--future-days",
        type=int,
        default=DEFAULT_FUTURE_DAYS,
        help=(
            "When auto-discovering, search this many days after --end-time or now "
            f"before scanning backwards (default: {DEFAULT_FUTURE_DAYS})"
        ),
    )
    parser.add_argument(
        "--stop-after-empty-days",
        type=int,
        default=DEFAULT_STOP_AFTER_EMPTY_DAYS,
        help=(
            "When auto-discovering, stop after this many consecutive days with no recordings "
            f"(default: {DEFAULT_STOP_AFTER_EMPTY_DAYS})"
        ),
    )
    parser.add_argument(
        "--max-discovery-days",
        type=int,
        default=DEFAULT_MAX_DISCOVERY_DAYS,
        help=(
            "When auto-discovering, stop after checking this many days "
            f"(default: {DEFAULT_MAX_DISCOVERY_DAYS})"
        ),
    )
    parser.add_argument(
        "--stop-at-existing",
        action="store_true",
        help=(
            "When auto-discovering, stop scanning backwards once a matching existing "
            "download is found in the output directory"
        ),
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
            "(default: both). Cameras without a separate telephoto stream only download wide."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only download the first N recordings found (useful for testing)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print the raw Baichuan protocol exchange (for diagnosing downloads)",
    )

    args = parser.parse_args()

    # Expand the lens choice into the concrete list of lenses to download.
    lenses = ["wide", "telephoto"] if args.lens == "both" else [args.lens]

    try:
        start_time = parse_datetime(args.start_time) if args.start_time else None
        if args.end_time:
            end_time = parse_datetime(args.end_time)
        elif start_time is None:
            end_time = datetime.now()
        else:
            print("Error: --end-time is required when --start-time is set", file=sys.stderr)
            sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if start_time is not None and start_time >= end_time:
        print("Error: start-time must be before end-time", file=sys.stderr)
        sys.exit(1)

    if args.future_days < 0:
        print("Error: --future-days must be zero or greater", file=sys.stderr)
        sys.exit(1)

    if args.stop_after_empty_days < 1:
        print("Error: --stop-after-empty-days must be at least 1", file=sys.stderr)
        sys.exit(1)

    if args.max_discovery_days < 1:
        print("Error: --max-discovery-days must be at least 1", file=sys.stderr)
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
                debug=args.debug,
                limit=args.limit,
                future_days=args.future_days,
                stop_after_empty_days=args.stop_after_empty_days,
                max_discovery_days=args.max_discovery_days,
                stop_at_existing=args.stop_at_existing,
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
