# Reolink Downloader

Batch download videos from your Reolink camera or NVR.

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

## Multi-channel NVRs

By default (`--channel all`) the tool auto-detects and downloads every channel your NVR
reports. Use `--channel` to restrict to a subset: a single index (`--channel 0`), a
comma-separated list (`--channel 0,2,5`), a range (`--channel 0-3`), or any combination
(`--channel 0,2-4,7`). Recordings are written into a subdirectory per channel
(`<output>/ch00_<camera-name>/...`) to avoid filename collisions, since Reolink's
on-device filenames are often generic per channel.

`--lens` (default `both`) still controls whether the wide and/or telephoto stream is
downloaded on cameras/channels that have a second lens (e.g. TrackMix PoE); it's
automatically narrowed to `wide` on channels that don't have one, so there's no wasted
search on plain single-lens channels.

`--limit` is a **global** cap across all selected channels combined (applied after
sorting by channel then recording time), not a per-channel limit.

### Download concurrency and NVR load

VOD search (finding what recordings exist) runs concurrently across all selected
channels — those are cheap control-plane requests. Actual downloads are bounded by
`--concurrency` (default `3`), since each concurrent download opens its own connection
to the NVR and shares its disk I/O and upload bandwidth. Reolink's own documentation
states that PoE NVRs support at most **4 concurrent playback streams**; going above
that risks connection errors or starving live view/other clients. Drop to `1` for
fully sequential downloads, or raise it only if your NVR clearly handles more.

## Progress logging

Each download logs its own progress at roughly every 20% of the on-camera file size
(or every ~3 seconds if the size isn't known), e.g.:

```
Downloading [5/42] (worker 1): 20240115_140000_wide_...
  [ch0 w1] 20240115_140000_wide_...: 6.2/24.8 MB (25%)
  [ch0 w1] 20240115_140000_wide_...: 12.4/24.8 MB (50%)
  ...
  Saved to: .../ch00_Front-Door/20240115_140000_wide_....mp4
Overall progress: 5/42 (12%) — 5 succeeded, 0 failed
```

The "Overall progress" line updates after every file across all channels/workers
combined, so you always know how far through the whole job you are, not just the file
currently downloading.

## Telegram notifications (optional)

Set these two environment variables to get run start/finish, per-channel progress,
overall job progress, and per-file error notifications pushed to a Telegram bot:

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHAT_ID=your-chat-id
```

Both must be set for notifications to be sent; if either is missing, notifications are
silently skipped and the tool behaves exactly as if Telegram wasn't configured at all —
useful when running unattended (e.g. in a Docker container/cron job) and you still want
visibility without watching logs.

Overall job progress is sent to Telegram at most once per 25% step (plus a final
message when the job completes), rather than on every file, so a large job doesn't
flood the chat.

## Installation

```
uv tool install git+https://github.com/deviantintegral/reolink-downloader
```

## Usage

```
$ uv tool run reolink-downloader --help
usage: reolink-downloader [-h] --ip IP --username USERNAME --password PASSWORD
                          --start-time START_TIME --end-time END_TIME
                          [--output OUTPUT] [--channel CHANNEL]
                          [--concurrency CONCURRENCY]
                          [--lens {wide,telephoto,both}] [--limit LIMIT]
                          [--debug]

Download videos from a Reolink NVR/camera within a specified date range

options:
  -h, --help            show this help message and exit
  --ip IP               Camera IP address or hostname
  --username USERNAME   Camera username
  --password PASSWORD   Camera password
  --start-time START_TIME
                        Start date/time (e.g., '2024-01-01' or '2024-01-01
                        14:30:00')
  --end-time END_TIME   End date/time (e.g., '2024-01-02' or '2024-01-02
                        14:30:00')
  --output OUTPUT       Output directory for downloaded videos (default:
                        ./downloads)
  --channel CHANNEL     NVR channel(s) to download, e.g. '0', '0,2,5', '0-3',
                        or '0,2-4,7'. Default 'all' auto-detects and downloads
                        every channel the device reports.
  --concurrency CONCURRENCY
                        Number of concurrent Baichuan download connections
                        (default: 1, sequential). Reolink PoE NVRs support at
                        most 4 concurrent playback streams; going higher risks
                        contending with live viewing or other clients.
  --lens {wide,telephoto,both}
                        Which lens to download on channels/cameras with a
                        second (telephoto) lens, such as the TrackMix PoE
                        (default: both). Automatically narrowed to 'wide' on
                        channels without a telephoto lens.
  --limit LIMIT         Only download the first N recordings found across all
                        selected channels (useful for testing)
  --debug               Print the raw Baichuan protocol exchange (for
                        diagnosing downloads)

Telegram notifications (optional): set the TELEGRAM_BOT_TOKEN and
TELEGRAM_CHAT_ID environment variables to receive run start/finish, per-
channel progress, and per-file error notifications. Both must be set for
notifications to be sent; otherwise they're silently skipped.
```
