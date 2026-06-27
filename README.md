# Reolink Downloader

Batch download videos from your Reolink camera.

This is inspired by [oscahie/reolink_downloader](https://github.com/oscahie/reolink_downloader).

Unfortunately, Reolink cameras have undocumented behaviour in the Search API. If you specify a date range longer than
one day, the API will return values as if you passed `onlyStatus: 1`, which returns a bitfield of days with recordings.
At this point, I assumed the reolink_aio library used by Home Assistant would automatically handle this. Of course, it
has the same issue, and this version ended up needing code to search day by day as well. Oh well!

Like the original, this was heavily written by an LLM, though I have reviewed every line of code.

## Fast downloads

Downloads use Reolink's proprietary **Baichuan binary protocol on TCP port 9000** — the
same one the native apps use — instead of the documented HTTPS `cmd=Download` API. On the
cameras tested the HTTPS path is bottlenecked by the camera's TLS to ~800 KB/s (the web UI
export is worse, ~88 KB/s), while the binary protocol reaches several MB/s. The protocol was
reverse-engineered for this tool; see [`PROTOCOL.md`](PROTOCOL.md) for the full write-up.

Recordings arrive as a raw H.264/H.265 elementary stream. If [`ffmpeg`](https://ffmpeg.org/)
is on your `PATH`, files are remuxed to `.mp4`; otherwise the raw `.h264`/`.h265` stream is
written (playable directly in VLC/ffmpeg).

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
