# Reolink Downloader

Batch download videos from your Reolink camera.

This is inspired by [oscahie/reolink_downloader](https://github.com/oscahie/reolink_downloader).

Unfortunately, Reolink cameras have undocumented behaviour in the Search API. If you specify a date range longer than
one day, the API will return values as if you passed `onlyStatus: 1`, which returns a bitfield of days with recordings.
At this point, I assumed the reolink_aio library used by Home Assistant would automatically handle this. Of course, it
has the same issue, and this version ended up needing code to search day by day as well. Oh well!

Like the original, this was heavily written by an LLM, though I have reviewed every line of code.

## Installation

```
uv tool install git+https://github.com/deviantintegral/reolink-downloader
```

## Usage

```
$ uv tool run reolink-downloader --help
usage: reolink-downloader [-h] --ip IP --username USERNAME --password PASSWORD --start-time START_TIME --end-time END_TIME
[--output OUTPUT]

Download videos from a Reolink camera within a specified date range

options:
-h, --help            show this help message and exit
--ip IP               Camera IP address or hostname
--username USERNAME   Camera username
--password PASSWORD   Camera password
--start-time START_TIME
Start date/time (e.g., '2024-01-01' or '2024-01-01 14:30:00')
--end-time END_TIME   End date/time (e.g., '2024-01-02' or '2024-01-02 14:30:00')
--output OUTPUT       Output directory for downloaded videos (default: ./downloads)
```
