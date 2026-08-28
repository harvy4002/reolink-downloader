"""A real SIGTERM (what Docker sends on stop/restart before SIGKILL) must be
logged and trigger a clean cancellation, rather than the process vanishing
with no trace -- which is what an actual SIGKILL/OOM kill looks like."""

import asyncio
import os
import signal

import pytest

import reolink_downloader as rd


async def test_sigterm_logs_and_cancels_the_running_task(capsys):
    async def long_running():
        await asyncio.sleep(10)

    run_task = asyncio.ensure_future(rd._run_with_graceful_shutdown(long_running()))

    await asyncio.sleep(0.05)  # let the signal handler get installed
    os.kill(os.getpid(), signal.SIGTERM)

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert "Received SIGTERM" in capsys.readouterr().err


async def test_task_completing_normally_needs_no_signal():
    async def quick():
        return "done"

    # Just confirms wrapping in _run_with_graceful_shutdown doesn't change
    # behavior when no signal ever arrives.
    await rd._run_with_graceful_shutdown(quick())
