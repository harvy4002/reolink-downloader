FROM python:3.14-slim

# ffmpeg is optional at runtime but strongly recommended: without it,
# downloads are written as raw .h264/.h265 elementary streams instead of
# being remuxed into playable .mp4 files.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Pin uv via the officially published binary image rather than pip-installing
# it, per Astral's recommended Docker pattern.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

# --frozen: install exactly what's in uv.lock (reproducible builds).
# --no-dev: skip the pytest/pytest-asyncio dev-only dependency group.
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"
# Without this, Python block-buffers stdout when it isn't a TTY (i.e. always,
# under Docker), so `docker logs -f` / Synology's log viewer show progress in
# large delayed chunks instead of as it happens.
ENV PYTHONUNBUFFERED=1
# Recordings are written here; bind-mount a host directory over it (see
# docker-compose.yml) so they survive after the container exits.
ENV REOLINK_OUTPUT=/downloads
VOLUME ["/downloads"]

ENTRYPOINT ["reolink-downloader"]
