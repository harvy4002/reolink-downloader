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

Recordings are written exactly as the camera sends them: a raw H.264/H.265 elementary
stream (`.h264`/`.h265`), playable directly in VLC/ffmpeg. This tool intentionally does
no conversion — a separate tool for turning these into a more universally playable
format (e.g. `.mp4`) is planned.

## Multi-channel NVRs

By default (`--channel all`) the tool auto-detects and downloads every channel your NVR
reports. Use `--channel` to restrict to a subset: a single index (`--channel 0`), a
comma-separated list (`--channel 0,2,5`), a range (`--channel 0-3`), or any combination
(`--channel 0,2-4,7`). Recordings are organized as
`<output>/<date>/ch00_<camera-name>/...` — grouped by day first, then by channel — to
avoid filename collisions, since Reolink's on-device filenames are often generic per
channel.

`--lens` (default `both`) still controls whether the wide and/or telephoto stream is
downloaded on cameras/channels that have a second lens (e.g. TrackMix PoE); it's
automatically narrowed to `wide` on channels that don't have one, so there's no wasted
search on plain single-lens channels.

Downloads are ordered **oldest-first across all selected channels combined**, not
grouped by channel — NVR storage retention deletes the oldest footage first once it
fills up, independently per channel, so the oldest recordings anywhere are the most at
risk of being deleted before this tool gets to them. `--limit` is a **global** cap
applied after that oldest-first sort, so `--limit N` means "the N oldest recordings
overall," not N per channel.

### Download concurrency and NVR load

VOD search (finding what recordings exist) runs concurrently across all selected
channels — those are cheap control-plane requests. Actual downloads are bounded by
`--concurrency` (default `3`), since each concurrent download opens its own connection
to the NVR and shares its disk I/O and upload bandwidth. Reolink's own documentation
states that PoE NVRs support at most **4 concurrent playback streams**; going above
that risks connection errors or starving live view/other clients. Drop to `1` for
fully sequential downloads, or raise it only if your NVR clearly handles more.

If downloads seem to hang with no progress output at all (even the throttled ~3s
ticks), your NVR likely can't cleanly handle that many simultaneous binary download
sessions even though it accepted the connections/logins fine — try lowering
`--concurrency` (`REOLINK_CONCURRENCY`) to `1` and see if that resolves it.

## Progress logging

Each download logs its own progress at roughly every 20% of the on-camera file size
(or every ~3 seconds if the size isn't known), e.g.:

```
Downloading [5/42] (worker 1): 20240115_140000_wide_...
  [ch0 w1] 20240115_140000_wide_...: 6.2/24.8 MB (25%)
  [ch0 w1] 20240115_140000_wide_...: 12.4/24.8 MB (50%)
  ...
  Saved to: .../2024-01-15/ch00_Front-Door/20240115_140000_wide_....h264
Overall progress: 5/42 (12%) — 5 succeeded, 0 failed
```

The "Overall progress" line updates after every file across all channels/workers
combined, so you always know how far through the whole job you are, not just the file
currently downloading.

## Auto-recovery (retries)

A failed download is retried automatically — up to 3 retries (4 attempts total) with a
short delay between them — before it's logged and reported to Telegram as a permanent
failure. The first attempt reuses the worker's existing connection; retries open a fresh
connection each time, since a stale or dropped connection is often *why* the first
attempt failed. A file that recovers on a retry is counted as succeeded, not failed, and
never triggers a Telegram error notification — only a permanent (all-attempts-exhausted)
failure does.

The same retry budget applies if a worker's *initial* connection to the NVR fails (e.g. a
transient network hiccup): it retries connecting up to 3 more times before giving up on
that worker, so a single flaky connection attempt can't silently skip a whole batch of
downloads. Jobs are pulled from one shared queue, so if a worker does exhaust its
reconnect attempts, any other workers (`--concurrency` > 1) simply pick up the rest —
only with `--concurrency 1` (no other worker to fall back on) would a total connection
failure leave the queue unprocessed.

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
flood the chat. Search results across all channels are likewise sent as a single
combined message once every channel's search finishes, rather than one message per
channel — only genuinely realtime events (progress updates, per-file errors) get their
own message as they happen.

## Installation

```
uv tool install git+https://github.com/deviantintegral/reolink-downloader
```

## Configuration via environment variables

Every CLI option also has a matching environment variable, so it drops straight into
Docker/unattended use without secrets on the command line. A CLI flag always overrides
its environment variable if both are set.

| Flag              | Environment variable   |
| ----------------- | ----------------------- |
| `--ip`             | `REOLINK_IP`             |
| `--username`       | `REOLINK_USERNAME`       |
| `--password`       | `REOLINK_PASSWORD`       |
| `--start-time`     | `REOLINK_START_TIME`     |
| `--end-time`       | `REOLINK_END_TIME`       |
| `--output`         | `REOLINK_OUTPUT`         |
| `--channel`        | `REOLINK_CHANNEL`        |
| `--concurrency`    | `REOLINK_CONCURRENCY`    |
| `--lens`           | `REOLINK_LENS`           |
| `--limit`          | `REOLINK_LIMIT`          |
| `--debug`          | `REOLINK_DEBUG` (`true`/`1`/`yes`/`on`) |

`--ip`, `--username`, `--password`, `--start-time`, and `--end-time` are the only
required options — set each one via its flag or its environment variable.

## Docker

[`docker-compose.yml`](docker-compose.yml) uses a pre-built runtime image
(`ghcr.io/harvy4002/reolink-downloader`) that contains only the Python/git/uv
toolchain — **not** the tool's code. Every time the container starts, its entrypoint
clones the tool fresh from this repo's `main` branch, installs its dependencies, and
runs it. That means:

- **No `build:` step at all** — Compose just pulls the image. This sidesteps Synology
  Container Manager's build engine not having `git` installed (see below).
- **Always the latest code** — push a change to `main` and the very next container run
  picks it up automatically, with nothing to rebuild or re-copy.
- The image itself (published by [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml))
  only needs rebuilding when the *toolchain* changes, which is rare.

Every setting is listed inline in `docker-compose.yml` under `environment:` with the
tool's real defaults and an example value in a comment. This is a one-off job, not a
long-running service — the container starts, downloads everything in the configured
date range, and exits.

```
mkdir reolink-downloader && cd reolink-downloader
curl -O https://raw.githubusercontent.com/harvy4002/reolink-downloader/main/docker-compose.yml
# edit the environment: values for your camera, then:
docker compose up
```

Real credentials can be typed directly into `docker-compose.yml`, but since that file is
tracked in git, prefer creating a sibling `.env` file (see [`.env.example`](.env.example)
for the full list of keys) in the same folder instead — Compose substitutes `${VAR}` from
it automatically, so secrets never need to be committed. `.env` is already covered by
this repo's `.gitignore`.

To pin to a specific release instead of always tracking `main`, set `REPO_REF` to a tag
or commit SHA (and `REPO_URL` if you want to point at a fork).

> **Keep every `environment:` value quoted**, even after editing it — YAML parses an
> unquoted date/number/boolean-shaped value (e.g. `2024-01-01`, `false`) as that native
> type instead of a string, and Compose then rejects it (e.g. an unquoted
> `REOLINK_START_TIME` fails with `invalid jsonType time.Time`). The file already quotes
> everything; just don't remove the quotes when you fill in real values.

### Portainer

Portainer's Stacks feature is generally more reliable than Synology's own Container
Manager for this (see below) — if Container Manager gives you trouble, this is the
easiest path:

1. **Stacks → Add stack**, name it (e.g. `reolink-downloader`).
2. Paste the contents of `docker-compose.yml` into the web editor — or use "Repository"
   mode pointed at `https://github.com/harvy4002/reolink-downloader.git`, file path
   `docker-compose.yml`.
3. Edit the `environment:` values for your camera directly in the editor (keeping them
   quoted, per above), or use Portainer's own "Environment variables" section instead of
   hardcoding them into the stack.
4. Deploy the stack.

If it can't pull the image, Portainer has its own registry credential manager:
**Registries → Add registry → Custom registry**, URL `https://ghcr.io`, your GitHub
username, and a [Personal Access Token (classic)](https://github.com/settings/tokens)
scoped to `read:packages` as the password.

### Synology Container Manager

1. Copy just `docker-compose.yml` into a shared folder (e.g. `docker/reolink-downloader`)
   via File Station — no other project files are needed, since there's nothing to build.
2. Open **Container Manager → Project → Create**, set "Path" to that folder — Container
   Manager picks up `docker-compose.yml` automatically.
3. Edit the `environment:` values in `docker-compose.yml` for your camera, or add a
   sibling `.env` file in the same folder for anything you'd rather not leave in a
   tracked file (Compose substitutes `${VAR}` from it automatically).
4. Start the project (no Build step needed — it only pulls). Since this is a one-off
   job, leave the project's auto-restart setting off (the compose file also sets
   `restart: "no"`).
5. To fetch a new batch, edit `REOLINK_START_TIME`/`REOLINK_END_TIME` (in the compose
   file or `.env`) and re-run. Any tool code changes on `main` are picked up
   automatically on every run — nothing to re-download.

Downloaded videos land in the `./downloads` folder next to the compose file
(bind-mounted into the container), so they survive after the container exits.

> **`denied` pulling from ghcr.io on Synology?** The image is public — anonymous pulls
> work fine from a normal Docker install — but some Synology Container Manager/DSM
> Docker daemon versions fail to negotiate GHCR's anonymous pull correctly (or have a
> stale/invalid cached login for `ghcr.io` that gets sent instead of an anonymous
> request), producing a misleading `denied` error even though nothing is actually
> private. Fix: add an explicit login for `ghcr.io` in **Container Manager → Registry →
> Settings → Add**, using your GitHub username and a
> [Personal Access Token (classic)](https://github.com/settings/tokens) scoped to just
> `read:packages`. Authenticated pulls sidestep this class of bug regardless of cause.

## Usage

```
$ uv tool run reolink-downloader --help
usage: reolink-downloader [-h] [--ip IP] [--username USERNAME]
                          [--password PASSWORD] [--start-time START_TIME]
                          [--end-time END_TIME] [--output OUTPUT]
                          [--channel CHANNEL] [--concurrency CONCURRENCY]
                          [--lens {wide,telephoto,both}] [--limit LIMIT]
                          [--debug]

Download videos from a Reolink NVR/camera within a specified date range

options:
  -h, --help            show this help message and exit
  --ip IP               Camera IP address or hostname (env: REOLINK_IP)
  --username USERNAME   Camera username (env: REOLINK_USERNAME)
  --password PASSWORD   Camera password (env: REOLINK_PASSWORD)
  --start-time START_TIME
                        Start date/time (e.g., '2024-01-01' or '2024-01-01
                        14:30:00') (env: REOLINK_START_TIME)
  --end-time END_TIME   End date/time (e.g., '2024-01-02' or '2024-01-02
                        14:30:00') (env: REOLINK_END_TIME)
  --output OUTPUT       Output directory for downloaded videos (default:
                        ./downloads) (env: REOLINK_OUTPUT)
  --channel CHANNEL     NVR channel(s) to download, e.g. '0', '0,2,5', '0-3',
                        or '0,2-4,7'. Default 'all' auto-detects and downloads
                        every channel the device reports. (env:
                        REOLINK_CHANNEL)
  --concurrency CONCURRENCY
                        Number of concurrent Baichuan download connections
                        (default: 3). Reolink PoE NVRs support at most 4
                        concurrent playback streams; going higher risks
                        contending with live viewing or other clients. (env:
                        REOLINK_CONCURRENCY)
  --lens {wide,telephoto,both}
                        Which lens to download on channels/cameras with a
                        second (telephoto) lens, such as the TrackMix PoE
                        (default: both). Automatically narrowed to 'wide' on
                        channels without a telephoto lens. (env: REOLINK_LENS)
  --limit LIMIT         Only download the first N recordings found across all
                        selected channels (useful for testing) (env:
                        REOLINK_LIMIT)
  --debug               Print the raw Baichuan protocol exchange (for
                        diagnosing downloads) (env: REOLINK_DEBUG)

Every option above can also be set via an environment variable (useful for
Docker/unattended use) — a CLI flag always overrides its environment
variable: REOLINK_IP, REOLINK_USERNAME, REOLINK_PASSWORD, REOLINK_START_TIME,
REOLINK_END_TIME, REOLINK_OUTPUT, REOLINK_CHANNEL, REOLINK_CONCURRENCY,
REOLINK_LENS, REOLINK_LIMIT, REOLINK_DEBUG. Telegram notifications
(optional): set the TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment
variables to receive run start/finish, per-channel progress, and per-file
error notifications. Both must be set for notifications to be sent;
otherwise they're silently skipped.
```
