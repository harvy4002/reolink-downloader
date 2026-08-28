"""A stalled Baichuan connection (camera accepts the connection/request but
never responds) must raise BaichuanError promptly rather than hang forever —
this is what lets the auto-recovery retry logic in __init__.py ever engage."""

import asyncio

import pytest

import reolink_downloader.baichuan as bc_module
from reolink_downloader.baichuan import BaichuanDownloader, BaichuanError


@pytest.fixture(autouse=True)
def _short_timeouts(monkeypatch):
    monkeypatch.setattr(bc_module, "CONNECT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(bc_module, "READ_TIMEOUT_SECONDS", 0.1)


async def test_read_times_out_when_camera_goes_silent():
    # A real TCP server that accepts the connection but never writes back,
    # simulating an NVR that stops responding under load. The handler just
    # parks forever (cancelled below) rather than sleeping a fixed duration,
    # since asyncio.Server.wait_closed() waits for in-flight handler tasks —
    # a sleep() here would make the test as slow as that sleep.
    handler_tasks = []

    async def silent_handler(reader, writer):
        handler_tasks.append(asyncio.current_task())
        await asyncio.Future()

    server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        downloader = BaichuanDownloader("127.0.0.1", "user", "pass", port=port)
        await downloader.connect()
        with pytest.raises(BaichuanError, match="no response from camera"):
            await downloader._read_message()
    finally:
        await downloader.close()
        server.close()
        for task in handler_tasks:
            task.cancel()
        await server.wait_closed()


async def test_connect_times_out_when_nothing_accepts():
    async def hang_forever(*args, **kwargs):
        await asyncio.sleep(10)

    import unittest.mock

    with unittest.mock.patch("asyncio.open_connection", side_effect=hang_forever):
        downloader = BaichuanDownloader("10.255.255.1", "user", "pass")
        with pytest.raises(BaichuanError, match="could not connect"):
            await downloader.connect()
