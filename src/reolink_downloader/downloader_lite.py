#!/usr/bin/env python3
"""
reolink-downloader-lite
Minimal NVR-focused Baichuan downloader: no lens logic, just channel + stream_type.
"""

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from reolink_aio.api import Host

from .baichuan import BaichuanDownloader, BaichuanError


def parse_datetime(date_string: str) -> datetime:
    formats = [
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(date_string, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unable to parse date '{date_string}'.")


async def download_channel(
    ip: str,
    username: str,
    password: str,
    channel: int,
    stream_type: str,
    start_time: datetime,
    end_time: datetime,
    output_dir: Path,
    debug: bool = False,
    limit: int | None = None,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Connecting to camera at {ip}...")
    host = Host(ip, username, password)

    try:
        await host.get_host_data()
        print(f"Connected: {host.nvr_name}")

        if channel not in host.channels:
            print(f"Error: channel {channel} not found. Available: {host.channels}", file=sys.stderr)
            return

        print(f"Searching channel {channel} ({stream_type}) from {start_time} to {end_time}...")
        _, files = await host.request_vod_files(
            channel=channel, start=start_time, end=end_time, stream="main",
        )

        if not files:
            print("No recordings found in the specified date range")
            return

        print(f"Found {len(files)} recording(s)")
        if limit is not None:
            files = files[:limit]
            print(f"Limiting to first {len(files)}")

        downloaded = 0
        async with BaichuanDownloader(ip, username, password, debug=debug) as bc:
            for idx, vod_file in enumerate(files, 1):
                ts = (
                    vod_file.start_time.strftime("%Y%m%d_%H%M%S")
                    if vod_file.start_time else f"recording_{idx}"
                )
                output_base = output_dir / f"ch{channel}_{stream_type}_{ts}"

                print(f"Downloading [{idx}/{len(files)}]: {output_base.name} "
                      f"({vod_file.start_time} - {vod_file.end_time})...")
                try:
                    written = await bc.download(
                        output_base,
                        start=vod_file.start_time,
                        end=vod_file.end_time,
                        channel=channel,
                        stream_type=stream_type,
                    )
                    print(f"  Saved to: {written}")
                    downloaded += 1
                except (BaichuanError, OSError) as e:
                    print(f"  Failed: {e}", file=sys.stderr)

        print(f"\nDownloaded {downloaded}/{len(files)} video(s) to {output_dir}")

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
    finally:
        await host.logout()


def main():
    parser = argparse.ArgumentParser(description="Minimal NVR channel downloader (Baichuan protocol)")
    parser.add_argument("--ip", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--channel", type=int, required=True, help="NVR channel index to download from")
    parser.add_argument("--stream-type", default="mainStream", choices=["mainStream", "subStream"])
    parser.add_argument("--start-time", required=True)
    parser.add_argument("--end-time", required=True)
    parser.add_argument("--output", default="./downloads")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    try:
        start_time = parse_datetime(args.start_time)
        end_time = parse_datetime(args.end_time)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if start_time >= end_time:
        print("Error: start-time must be before end-time", file=sys.stderr)
        sys.exit(1)

    try:
        asyncio.run(
            download_channel(
                ip=args.ip, username=args.username, password=args.password,
                channel=args.channel, stream_type=args.stream_type,
                start_time=start_time, end_time=end_time,
                output_dir=Path(args.output), debug=args.debug, limit=args.limit,
            )
        )
    except KeyboardInterrupt:
        print("\nCancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()