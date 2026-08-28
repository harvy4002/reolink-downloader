FROM python:3.14-slim

# git: needed at container *startup* (see entrypoint.sh) to clone the latest
# app code fresh on every run — this is the container's own git, unrelated
# to whatever build host runs `docker build` (this image never needs git to
# build; that's what makes it safe to build on hosts that lack git, like
# Synology's Container Manager).
# ffmpeg: needed for mp4 remuxing.
# ca-certificates: needed for the HTTPS git clone.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Pin uv via the officially published binary image rather than pip-installing
# it, per Astral's recommended Docker pattern.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Without this, Python block-buffers stdout when it isn't a TTY (i.e. always,
# under Docker), so `docker logs -f` / Synology's log viewer show progress in
# large delayed chunks instead of as it happens.
ENV PYTHONUNBUFFERED=1
# Recordings are written here; bind-mount a host directory over it (see
# docker-compose.yml) so they survive after the container exits.
ENV REOLINK_OUTPUT=/downloads
VOLUME ["/downloads"]

ENTRYPOINT ["/entrypoint.sh"]
