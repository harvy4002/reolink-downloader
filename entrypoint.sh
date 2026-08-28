#!/bin/sh
# Clones the tool's source fresh on every container start, installs its
# dependencies, and runs it — so this image's own build rarely needs to
# change, but every run still gets whatever is currently on REPO_REF.
set -eu

REPO_URL="${REPO_URL:-https://github.com/harvy4002/reolink-downloader.git}"
REPO_REF="${REPO_REF:-main}"
APP_DIR="/app/checkout"

echo "Fetching reolink-downloader @ ${REPO_REF} from ${REPO_URL}..."
rm -rf "$APP_DIR"
git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$APP_DIR"

cd "$APP_DIR"
# --frozen: install exactly what this commit's uv.lock specifies (still
# reproducible per-run, just re-resolved against whatever HEAD currently is).
# --no-dev: skip the pytest/pytest-asyncio dev-only dependency group.
uv sync --frozen --no-dev --no-editable

# --no-sync: `uv run` syncs the project itself by default, which does NOT
# inherit the --no-dev/--no-editable flags above — without this it silently
# reinstalls the dev dependency group (pytest etc.) and switches back to an
# editable install on every single run.
exec uv run --no-sync reolink-downloader "$@"
